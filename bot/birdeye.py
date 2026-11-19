"""Birdeye public API (read-only market data). OPTIONAL and OFF by default (env BIRDEYE_ENABLED=1 + BIRDEYE_API_KEY).

Used for two opt-in features (proposals, see PROPOSTE_STRATEGIA.md):
  1. extra top-trader source for the COPY agent (global best traders from /trader/gainers-losers + the top
     traders of each candidate token from /defi/v2/tokens/top_traders) -> fixes 'not_enough_top_traders' when
     Vybe is missing or its list does not overlap with the wallets we can see;
  2. data source for the AntiRug Security Engine (bot/antirug.py, env ANTIRUG_MODE=shadow|active): holder list
     (/defi/v3/token/holder), liquidity (/defi/price?include_liquidity=true, /defi/v3/liquidity/history/token) and
     the tags of the token's top traders.
Endpoints verified working by the user on 13/11/2026: /defi/price, /defi/v2/tokens/top_traders,
/trader/gainers-losers, /defi/v3/token/holder. Headers: X-API-KEY + x-chain: solana.

Safety: the API key is only ever placed in a request header; it is never logged, saved or shown (status() exposes
only whether a key is present). Client-side rate limit (token bucket, default 60 requests/minute) - when the
budget is exhausted a call is SKIPPED (returns None) instead of waiting, so the trading loop is never slowed.
Every failure returns None and the caller falls back to the original behaviour. Paper trading only."""
import logging, threading, time

log = logging.getLogger("paperbot.birdeye")

BASE = "https://public-api.birdeye.so"


def _f(x):
    try:
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


class RateLimiter:
    """Token bucket: `rpm` requests per minute, burst = min(rpm, 10) unless given. acquire(wait) waits at most `wait` s.
    (13/11 live: Birdeye answered 429 to a burst of calls although the minute total was < 60 -> config burst: 1.)"""

    def __init__(self, rpm=60, clock=time.monotonic, sleep=time.sleep, burst=None):
        self.rate = max(1.0, float(rpm)) / 60.0
        self.cap = float(min(max(1, int(rpm)), 10) if burst is None else max(1, int(burst)))
        self.tokens, self.t = self.cap, clock()
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()

    def _refill(self):
        n = self.clock()
        self.tokens = min(self.cap, self.tokens + (n - self.t) * self.rate)
        self.t = n

    def acquire(self, wait=0.0):
        end = self.clock() + wait
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
                need = (1 - self.tokens) / self.rate
            if self.clock() + need > end:
                return False
            self.sleep(min(need, 0.5))


class Birdeye:
    def __init__(self, http, key, cfg=None, clock=time.time):
        self.h, self.c = http, dict(cfg or {})
        self._key = key                                      # private: never logged / serialised
        self.base = self.c.get("base_url", BASE)
        self.rl = RateLimiter(self.c.get("rpm", 60), burst=self.c.get("burst"))
        self.max_wait = float(self.c.get("max_wait_s", 1.0))
        self.clock = clock
        self.cache = {}                                      # (kind, arg) -> (ts, value)
        self.stats = {"ok": 0, "fail": 0, "skipped_rate_limit": 0, "calls": {}}
        self.disabled_until = 0.0                            # GLOBAL pause (429, or the key itself stopped working)
        self.last_error = ""
        self.last_http_status = None
        self.last_http_endpoint = ""
        self.last_http_detail = ""
        # 401/403 on ONE endpoint usually means "your plan does not include this endpoint" (13/11: the live key gets
        # 401 on /defi/v3/liquidity/history/token but 200 on price/holder/top_traders/gainers-losers). So an auth
        # failure disables only THAT endpoint (default 6 h); the whole client pauses only when endpoints that HAD
        # worked start failing with 401/403 too (key revoked / plan expired).
        self.endpoint_disable_s = float(self.c.get("endpoint_disable_s", 6 * 3600))
        self.global_auth_pause_s = float(self.c.get("global_auth_pause_s", 3600))
        self.global_auth_after = int(self.c.get("global_auth_after", 2))   # distinct previously-working endpoints
        self.global_auth_window_s = float(self.c.get("global_auth_window_s", 900))
        self.endpoint_off = {}                               # path -> disabled until (clock)
        self.endpoint_ok = set()                             # paths that answered 200 + valid data at least once
        self._auth_fail = {}                                 # previously-working path -> clock of its 401/403

    def __repr__(self):                                     # never print the key, even by accident
        return f"<Birdeye base={self.base} key={'set' if self._key else 'missing'}>"

    # ------------------------------------------------------------------ plumbing
    def _hdr(self):
        return {"X-API-KEY": self._key, "x-chain": "solana", "accept": "application/json"}

    def _get(self, path, params, ttl=0.0, cache_key=None):
        ck = cache_key or (path, tuple(sorted((params or {}).items())))
        hit = self.cache.get(ck)
        if hit and self.clock() - hit[0] < ttl:
            return hit[1]
        if not self._key or self.clock() < self.disabled_until:
            return None
        if self.endpoint_disabled(path):
            self.stats["skipped_disabled"] = self.stats.get("skipped_disabled", 0) + 1
            return None
        cycle_remaining = self.h.cycle_remaining(default=self.max_wait) if hasattr(self.h, "cycle_remaining") else self.max_wait
        wait_budget = min(self.max_wait, max(0.0, cycle_remaining))
        if not self.rl.acquire(wait_budget):
            self.stats["skipped_rate_limit"] += 1
            return None
        self.stats["calls"][path] = self.stats["calls"].get(path, 0) + 1
        r = self.h.request_ex(self.base + path, headers=self._hdr(), params=params, tries=1,
                              deadline=self.c.get("timeout_s", 8))
        d = r.data if r.status == 200 else None
        if r.status in (401, 403):
            self._on_auth_failure(path, r.status)
        elif r.status == 429:
            self.disabled_until = self.clock() + 30
            self.last_error = "429 rate limit"
        if not (isinstance(d, dict) and d.get("success") is not False and isinstance(d.get("data"), (dict, list))):
            self.stats["fail"] += 1
            if r.status is not None:
                self.last_http_status = r.status
                self.last_http_endpoint = path
                self.last_http_detail = ""
                if isinstance(r.data, dict):
                    for key in ("message", "msg", "code", "error"):
                        value = r.data.get(key)
                        if value not in (None, "", False):
                            self.last_http_detail = f"{key}={str(value)[:180]}"
                            break
            if r.status not in (401, 403, 429):
                self.last_error = (
                    f"HTTP {r.status}: {self.last_http_detail}"
                    if self.last_http_detail else (r.kind or "malformed")
                )
            return None
        self.stats["ok"] += 1
        self.endpoint_ok.add(path)
        self._auth_fail.pop(path, None)
        self.cache[ck] = (self.clock(), d["data"])
        if len(self.cache) > 2000:
            self.cache = dict(list(self.cache.items())[-1000:])
        return d["data"]

    def endpoint_disabled(self, path):
        until = self.endpoint_off.get(path)
        if until is None:
            return False
        if self.clock() >= until:
            del self.endpoint_off[path]                     # retry once the window has passed
            return False
        return True

    def _on_auth_failure(self, path, status):
        now = self.clock()
        newly = not self.endpoint_disabled(path)
        self.endpoint_off[path] = now + self.endpoint_disable_s
        self.last_error = f"HTTP {status} su {path} (endpoint non incluso nel piano o chiave)"
        if newly:                                           # logged once per disable window
            log.warning("Birdeye %s -> HTTP %s: endpoint disabled for %.0f h (not in this key's plan?); other "
                        "Birdeye endpoints keep working", path, status, self.endpoint_disable_s / 3600)
        if path in self.endpoint_ok:                        # it HAD worked: maybe the key itself is gone
            self._auth_fail[path] = now
            recent = [p for p, t in self._auth_fail.items() if now - t <= self.global_auth_window_s]
            if len(recent) >= self.global_auth_after and now >= self.disabled_until:
                self.disabled_until = now + self.global_auth_pause_s
                self.last_error = f"HTTP {status} su {len(recent)} endpoint che funzionavano (chiave o piano Birdeye)"
                log.warning("Birdeye: %d previously working endpoints now refuse the key -> Birdeye paused for "
                            "%.0f min (check BIRDEYE_API_KEY / plan)", len(recent), self.global_auth_pause_s / 60)

    # ------------------------------------------------------------------ endpoints
    def price(self, mint, with_liquidity=True):
        """(price_usd, liquidity_usd) or None."""
        d = self._get("/defi/price", {"address": mint, "include_liquidity": "true" if with_liquidity else "false"},
                      ttl=self.c.get("price_cache_s", 10))
        if not isinstance(d, dict):
            return None
        p = _f(d.get("value"))
        if p is None or p <= 0:
            return None
        return p, _f(d.get("liquidity"))

    def top10_pct(self, mint):
        """Share of supply held by the top 10 holders, in percent (e.g. 18.42), or None."""
        d = self._get("/defi/v3/token/holder", {"address": mint, "offset": 0, "limit": 10},
                      ttl=self.c.get("holder_cache_s", 60))
        if not isinstance(d, dict):
            return None
        for k in ("top10HoldPercent", "top10_hold_percent", "top10HolderPercent"):
            v = _f(d.get(k))
            if v is not None:
                return v
        return None

    def token_top_traders(self, mint, time_frame="24h", limit=10):
        """Wallets that traded this token the most (bots excluded) -> set (may be empty) or None on failure."""
        d = self._get("/defi/v2/tokens/top_traders", {"address": mint, "time_frame": time_frame, "sort_by": "volume",
                                                      "sort_type": "desc", "offset": 0, "limit": min(10, int(limit))},
                      ttl=self.c.get("token_traders_cache_s", 120))
        items = d.get("items") if isinstance(d, dict) else d if isinstance(d, list) else None
        if items is None:
            return None
        out = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            tags = " ".join(str(t) for t in (it.get("tags") or [])).lower()
            w = it.get("owner") or it.get("address")
            if isinstance(w, str) and len(w) >= 32 and "bot" not in tags:
                out.add(w)
        return out

    def token_trader_tags(self, mint, time_frame="24h", limit=10):
        """Lower-case wallet tags of the token's top traders (same cached call as token_top_traders) or None."""
        d = self._get("/defi/v2/tokens/top_traders", {"address": mint, "time_frame": time_frame, "sort_by": "volume",
                                                      "sort_type": "desc", "offset": 0, "limit": min(10, int(limit))},
                      ttl=self.c.get("token_traders_cache_s", 120))
        items = d.get("items") if isinstance(d, dict) else d if isinstance(d, list) else None
        if items is None:
            return None
        return [str(t).lower() for it in items if isinstance(it, dict) for t in (it.get("tags") or [])]

    def best_traders(self, types=("1W", "today"), pages=5):
        """Global best traders by PnL (/trader/gainers-losers), 10 per page -> set or None if every call failed."""
        out, any_ok = set(), False
        for ty in types:
            for pg in range(int(pages)):
                d = self._get("/trader/gainers-losers", {"type": ty, "sort_by": "PnL", "sort_type": "desc",
                                                         "offset": pg * 10, "limit": 10}, ttl=600)
                items = d.get("items") if isinstance(d, dict) else d if isinstance(d, list) else None
                if items is None:
                    break
                any_ok = True
                got = [it.get("address") for it in items if isinstance(it, dict) and isinstance(it.get("address"), str)]
                out |= {w for w in got if len(w) >= 32}
                if len(got) < 10:
                    break
        return out if any_ok else None

    def status(self):
        return {"key": bool(self._key), "ok": self.stats["ok"], "fail": self.stats["fail"],
                "skipped_rate_limit": self.stats["skipped_rate_limit"], "calls": dict(self.stats["calls"]),
                "paused_s": max(0, round(self.disabled_until - self.clock())), "last_error": self.last_error,
                "last_http_status": self.last_http_status,
                "last_http_endpoint": self.last_http_endpoint,
                "last_http_detail": self.last_http_detail,
                "disabled_endpoints": {p: max(0, round(u - self.clock())) for p, u in self.endpoint_off.items()
                                       if u > self.clock()}}
