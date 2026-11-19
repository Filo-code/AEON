"""HTTP client for the PAPER bot (read-only public APIs; nothing here can place an order).

Guarantees
- ONE global deadline per logical request: connect + streamed body + retries + back-off waits + host spacing
  all have to fit in `deadline` seconds. A server that trickles bytes cannot hang the caller.
- Retries with exponential back-off and full jitter, only for errors where a retry can help
  (timeouts, network errors, 5xx). 4xx are never retried. 429 honours Retry-After and puts the host in
  a cool-down so the bot does not hammer it.
- Every outcome is classified: ok | rate_limited | timeout | network | http_5xx | http_4xx | malformed |
  cooldown | circuit_open | cancelled. Per-host health (consecutive failures, down since) is exposed so the
  bot can alert "API non disponibile" and keep running when a single source is down.
- Circuit breaker per host: after N consecutive failures the host is skipped for a growing cool-down
  (capped), then probed again. The bot never stops because one API is down.
- Cancellable: when the bot receives SIGTERM, `cancel()` makes every wait return immediately.
"""
import json, logging, random, threading, time
from collections import deque

import requests

log = logging.getLogger("paperbot.net")

OK, RATE, TIMEOUT, NETWORK, E5XX, E4XX, MALFORMED, COOLDOWN, CIRCUIT, CANCELLED, DEFERRED = (
    "ok", "rate_limited", "timeout", "network", "http_5xx", "http_4xx", "malformed", "cooldown",
    "circuit_open", "cancelled", "deferred")
RETRYABLE = {TIMEOUT, NETWORK, E5XX}


class Resp:
    __slots__ = ("status", "data", "kind", "elapsed", "attempts", "error")

    def __init__(self, status=None, data=None, kind=OK, elapsed=0.0, attempts=0, error=""):
        self.status, self.data, self.kind, self.elapsed, self.attempts, self.error = status, data, kind, elapsed, attempts, error

    @property
    def ok(self):
        return self.kind == OK

    def __repr__(self):
        return f"Resp({self.status}, {self.kind}, {self.elapsed:.2f}s, attempts={self.attempts})"


class HostHealth:
    def __init__(self):
        self.consec = 0            # consecutive failed logical requests
        self.down_since = None     # ts of first failure in the current failure streak
        self.last_ok = None
        self.last_kind = None
        self.open_until = 0.0      # circuit breaker
        self.kinds = {}            # kind -> count (lifetime)

    def as_dict(self):
        return {"consec": self.consec, "down_since": self.down_since, "last_ok": self.last_ok,
                "last_kind": self.last_kind, "open_until": self.open_until, "kinds": dict(self.kinds)}


class Http:
    """Polite HTTP client with per-host spacing, global deadline, retries, classification and circuit breaker."""
    SPACING = {"api.geckoterminal.com": 6.0, "api.rugcheck.xyz": 1.2,
               "api.dexscreener.com": 0.4, "api.vybenetwork.xyz": 0.5,
               "api.jup.ag": 2.2,                      # Jupiter keyless quotes: 0.5 req/s
               "api.mainnet-beta.solana.com": 0.35}    # public Solana RPC
    COOLDOWN_429 = {"api.jup.ag": 15}
    MAX_BODY = 20_000_000

    def __init__(self, total_timeout=20.0, connect_timeout=5.0, read_timeout=10.0, on_progress=None,
                 retries=2, backoff_base=0.5, backoff_cap=8.0, breaker_after=5, breaker_base=30.0, breaker_cap=300.0):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "solana-paper-bot/4.0 (simulation; read-only)"
        self.total, self.ct, self.rt = float(total_timeout), float(connect_timeout), float(read_timeout)
        self.retries, self.bb, self.bcap = int(retries), float(backoff_base), float(backoff_cap)
        self.breaker_after, self.breaker_base, self.breaker_cap = breaker_after, breaker_base, breaker_cap
        self.last, self.calls, self.cooldown = {}, {}, {}
        # Atomic per-host request reservations, using monotonic time.
        self.next_allowed = {}
        self.ok, self.err = {}, {}
        self.health = {}
        self.call_times = deque(maxlen=2000)       # ops/min on the dashboard
        self.on_progress = on_progress or (lambda: None)
        self._cancel = threading.Event()
        self._lock = threading.RLock()             # protects per-host state shared by bot/Telegram threads
        self._budget_local = threading.local()     # per-thread cycle budget; Telegram must not consume bot-loop budget

    def begin_cycle_budget(self, seconds):
        """Cap all HTTP requests in the calling thread to one shared cycle deadline."""
        seconds = max(0.0, float(seconds))
        self._budget_local.deadline = time.monotonic() + seconds

    def end_cycle_budget(self):
        """Clear the calling thread's cycle budget."""
        if hasattr(self._budget_local, "deadline"):
            del self._budget_local.deadline

    def cycle_remaining(self, default=None):
        """Seconds left in this thread's API-cycle budget, or `default` if unset."""
        deadline = getattr(self._budget_local, "deadline", None)
        return default if deadline is None else max(0.0, deadline - time.monotonic())

    # ---------------------------------------------------------------- control
    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def _sleep(self, sec):
        """Interruptible sleep; returns False if cancelled."""
        if sec > 0:
            self._cancel.wait(sec)
        return not self._cancel.is_set()

    def _h(self, host):
        with self._lock:
            return self.health.setdefault(host, HostHealth())

    def _count(self, host, ok):
        with self._lock:
            d = self.ok if ok else self.err
            d[host] = d.get(host, 0) + 1

    def _finish(self, host, resp, record=True):
        h = self._h(host)
        h.kinds[resp.kind] = h.kinds.get(resp.kind, 0) + 1
        h.last_kind = resp.kind
        if not record:
            return resp
        if resp.kind == OK or resp.kind == E4XX:       # a 4xx is a valid answer from a healthy server
            h.consec, h.down_since, h.last_ok = 0, None, time.time()
        elif resp.kind not in (CANCELLED, COOLDOWN, CIRCUIT, RATE, DEFERRED):
            h.consec += 1
            h.down_since = h.down_since or time.time()
            if h.consec >= self.breaker_after:
                cd = min(self.breaker_cap, self.breaker_base * 2 ** (h.consec - self.breaker_after))
                h.open_until = time.time() + cd * random.uniform(0.8, 1.2)
                log.warning("circuit open for %s: %d consecutive failures (last %s), pausing %.0fs",
                            host, h.consec, resp.kind, h.open_until - time.time())
        return resp

    def backoff(self, attempt):
        """Exponential back-off with full jitter."""
        return random.uniform(0, min(self.bcap, self.bb * 2 ** attempt))

    # ---------------------------------------------------------------- core
    def request_ex(self, url, headers=None, params=None, json_body=None, tries=None, deadline=None):
        """Returns a Resp. Never raises. `tries` = max attempts; `deadline` = global seconds for everything."""
        host = url.split("/")[2]
        t0 = time.time()
        end = t0 + (deadline if deadline is not None else self.total)
        # A cycle budget is shared by sequential API calls on the main trading thread.
        # It prevents N individually valid timeouts from accumulating into a long manage/discover pass.
        budget_end = getattr(self._budget_local, "deadline", None)
        if budget_end is not None:
            end = min(end, time.time() + max(0.0, budget_end - time.monotonic()))
        tries = max(1, tries if tries is not None else self.retries + 1)
        if self.cancelled:
            return Resp(kind=CANCELLED)
        with self._lock:
            cooling_until = self.cooldown.get(host, 0)
        if time.time() < cooling_until:
            return self._finish(host, Resp(kind=COOLDOWN, error="429 cool-down"), record=False)
        h = self._h(host)
        if time.time() < h.open_until:
            return self._finish(host, Resp(kind=CIRCUIT, error="circuit open"), record=False)
        resp = Resp(kind=TIMEOUT, error="deadline before first attempt")
        for attempt in range(tries):
            # Reserve a unique slot atomically. Concurrent threads cannot
            # calculate the same host-spacing window.
            spacing = self.SPACING.get(host, 0.3)
            with self._lock:
                now_mono = time.monotonic()
                slot = max(now_mono, self.next_allowed.get(host, 0.0))
                wait = slot - now_mono

                if time.time() + wait >= end:
                    resp = Resp(
                        kind=DEFERRED,
                        error="deadline (host spacing)",
                        attempts=attempt
                    )
                    break

                self.next_allowed[host] = slot + spacing

            if wait > 0 and not self._sleep(wait):
                resp = Resp(kind=CANCELLED)
                break

            with self._lock:
                actual_start = time.time()
                self.last[host] = actual_start
                self.calls[host] = self.calls.get(host, 0) + 1
                self.call_times.append(actual_start)
            resp = self._attempt(url, host, headers, params, json_body, end)
            resp.attempts = attempt + 1
            self.on_progress()
            if resp.kind == RATE:
                ra = resp.data if isinstance(resp.data, (int, float)) else None
                cd = max(self.COOLDOWN_429.get(host, 30), ra or 0)
                with self._lock:
                    self.cooldown[host] = time.time() + cd
                    self.calls["429_" + host] = self.calls.get("429_" + host, 0) + 1
                log.warning("429 rate-limited by %s, pausing that source for %.0fs", host, cd)
                resp.data = None
                break
            if resp.kind not in RETRYABLE or attempt == tries - 1:
                break
            pause = self.backoff(attempt)
            if time.time() + pause >= end:
                break
            if not self._sleep(pause):
                resp = Resp(kind=CANCELLED); break
        resp.elapsed = time.time() - t0
        # A deferred request never reached the provider.
        if resp.kind != DEFERRED:
            self._count(host, resp.ok)

        if not resp.ok and resp.kind not in (E4XX, CANCELLED, DEFERRED):
            log.warning("HTTP %s %s: %s (%s, %.1fs, %d tentativi)", resp.kind, host, resp.status or "-",
                        resp.error[:120], resp.elapsed, resp.attempts)
        return self._finish(host, resp)

    def _attempt(self, url, host, headers, params, json_body, end):
        r = None
        remaining = end - time.time()
        if remaining <= 0.05:
            return Resp(kind=TIMEOUT, error="global deadline exceeded")
        to = (min(self.ct, remaining), min(self.rt, remaining))
        try:
            if json_body is not None:
                r = self.s.post(url, headers=headers, json=json_body, timeout=to, stream=True)
            else:
                r = self.s.get(url, headers=headers, params=params, timeout=to, stream=True)
            if r.status_code == 429:
                ra = None
                try:
                    ra = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    pass
                return Resp(429, ra, RATE, error="rate limited")
            buf = bytearray()
            read1 = getattr(r.raw, "read1", None)
            while True:
                if time.time() > end:
                    return Resp(r.status_code, None, TIMEOUT, error="global deadline exceeded while reading body")
                if self.cancelled:
                    return Resp(kind=CANCELLED)
                chunk = read1(65536, decode_content=True) if read1 else r.raw.read(4096, decode_content=True)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > self.MAX_BODY:
                    return Resp(r.status_code, None, MALFORMED, error="response too large")
            if r.status_code >= 500:
                return Resp(r.status_code, None, E5XX, error=f"HTTP {r.status_code}")
            if r.status_code != 200:
                data = None
                try:
                    data = json.loads(bytes(buf)) if buf else None
                except ValueError:
                    pass
                return Resp(r.status_code, data, E4XX, error=f"HTTP {r.status_code}")
            try:
                data = json.loads(bytes(buf))
            except ValueError as e:
                return Resp(200, None, MALFORMED, error=f"invalid JSON: {e}")
            return Resp(200, data, OK)
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, TimeoutError) as e:
            return Resp(kind=TIMEOUT, error=type(e).__name__)
        except requests.exceptions.ConnectionError as e:
            # urllib3 wraps read timeouts on streamed bodies into ConnectionError
            k = TIMEOUT if "timed out" in str(e).lower() else NETWORK
            return Resp(kind=k, error=str(e)[:160])
        except (requests.RequestException, OSError, ValueError) as e:
            return Resp(kind=NETWORK, error=f"{type(e).__name__}: {str(e)[:120]}")
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass

    # ---------------------------------------------------------------- legacy API (kept for all callers/tests)
    def request(self, url, headers=None, params=None, tries=2, raw_status=False, json_body=None, deadline=None):
        """Returns parsed JSON (or None). With raw_status=True returns (status_code, json_or_None)."""
        r = self.request_ex(url, headers=headers, params=params, json_body=json_body, tries=tries, deadline=deadline)
        if raw_status:
            return (r.status, r.data if r.kind in (OK, E4XX) else None)
        return r.data if r.ok else None

    get = request

    def ops_per_min(self):
        cut = time.time() - 60
        return sum(1 for t in list(self.call_times) if t >= cut)

    def health_report(self):
        with self._lock:
            return {h: v.as_dict() for h, v in self.health.items()}

    def down_hosts(self, min_seconds):
        now = time.time()
        with self._lock:
            return {h: v for h, v in self.health.items() if v.down_since and now - v.down_since >= min_seconds}
