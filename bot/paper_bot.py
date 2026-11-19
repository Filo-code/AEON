#!/usr/bin/env python3
"""
Solana memecoin PAPER-TRADING bot (simulation only) - VPS edition.

- NO wallet, NO private keys, NO transaction signing, NO real orders. Ever.
- Strategy (unchanged from the original bot):
  * new pairs / volume spikes -> GeckoTerminal new_pools + DexScreener (GMGN substitute)
  * holder filters (dev %, top-10 %, RugCheck risk, whale buy) -> RugCheck + GeckoTerminal trades
  * copy top traders (>= N Vybe top-trader wallets in the token) -> Vybe (VYBE_API_KEY)
  * exits: stop loss, take profit, trailing stop, max hold; realistic slippage / fees / failed exits
- Upgrades (v3):
  * copy_top_traders entries have their OWN exits: follow the top-trader wallets that were in the token
    (on-chain balances via public Solana RPC, RugCheck holders as fallback) and exit when the majority
    of them sold/reduced; longer max hold, wider stop loss, wider trailing stop (config: copy_exits)
  * fill prices from Jupiter's free QUOTE API (read-only: no transaction is ever built or signed);
    fallback to the liquidity-based slippage model when the quote fails; source logged per trade
  * per-token re-entry cooldown after a sell
- VPS robustness:
  * hard total deadline on every HTTP call (connect + per-chunk read + overall deadline)
  * watchdog thread: if the main loop stops making progress, state is saved and the process exits
    so Docker / systemd restarts it
  * state (cash, open positions, trades, equity curve) persisted atomically to DATA_DIR/state.json
    and resumed on restart; positions past max hold are closed first thing after a stall/restart
  * Telegram notifications on every simulated buy / sell / failed sell, start / stop, daily summary
"""
import argparse, csv, json, logging, math, os, random, signal, sys, threading, time
from typing import Any, List, Optional
import requests
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import net  # noqa: E402
from birdeye import Birdeye
import antirug as antirug_mod  # noqa: E402
from net import Http  # noqa: E402,F401 (re-exported: tests and tools import paper_bot.Http)
import storage  # noqa: E402
from storage import atomic_json, load_json_safe, InstanceLock, Backups, disk_status, sd_notify  # noqa: E402
import telegram_notify as tg  # noqa: E402
import screenshot  # noqa: E402
from telegram_commands import Commands  # noqa: E402
from cervello import Brain, BOUNDS, validate_seed  # noqa: E402
import profiles  # noqa: E402
import disk_guard  # noqa: E402
from risk import RiskManager  # noqa: E402
from pumpfun import PumpFunScanner  # noqa: E402
from gmgn import GmGnScanner  # noqa: E402
from gmgn_adapter import GMGNIntelligenceAdapter  # noqa: E402
import reset_manager  # noqa: E402
from rpc_manager import RPCManager, create_rpc_manager_from_env  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_MINTS = {"SOL": SOL, "WSOL": SOL, "USDC": USDC}
STATE_VERSION = 1
USDC_DEC = 6


def cfg_set(c, key, val):
    """Set a config value; 'a.b' sets c['a']['b'] (nested sections such as copy_exits)."""
    if "." in key:
        sec, k = key.split(".", 1)
        c.setdefault(sec, {})[k] = val
    else:
        c[key] = val


def cfg_get(c, key, default=None):
    if "." in key:
        sec, k = key.split(".", 1)
        return (c.get(sec) or {}).get(k, default)
    return c.get(key, default)


log = logging.getLogger("paperbot")


def now():
    return time.time()


def iso(ts=None):
    return datetime.fromtimestamp(ts or now(), tz=timezone.utc).isoformat(timespec="seconds")


def f(x, d=0.0):
    try:
        return float(x) if x is not None else d
    except (TypeError, ValueError):
        return d


def finite(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


# ---------------------------------------------------------------- data sources
class GeckoTerminal:
    B = "https://api.geckoterminal.com/api/v2/networks/solana"

    def __init__(self, http):
        self.h = http

    @staticmethod
    def _parse(p):
        a = p["attributes"]; rel = p["relationships"]
        base = rel["base_token"]["data"]["id"].split("_", 1)[1]
        quote = rel["quote_token"]["data"]["id"].split("_", 1)[1]
        tx = a.get("transactions", {}).get("m5", {})
        vol = a.get("volume_usd", {})
        created = a.get("pool_created_at")
        cts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None
        return {"pool": a["address"], "mint": base, "quote_mint": quote, "name": a.get("name"),
                "dex": rel.get("dex", {}).get("data", {}).get("id"),
                "price": f(a.get("base_token_price_usd")), "created_ts": cts,
                "quote_price": f(a.get("quote_token_price_usd")),
                "vol_m5": f(vol.get("m5")), "vol_h1": f(vol.get("h1")),
                "buys_m5": int(tx.get("buys") or 0), "sells_m5": int(tx.get("sells") or 0),
                "liquidity": f(a.get("reserve_in_usd")), "fdv": f(a.get("fdv_usd")), "src": "geckoterminal"}

    def new_pools(self, pages):
        out = []
        for pg in range(1, pages + 1):
            d = self.h.get(f"{self.B}/new_pools", params={"page": pg, "include": ""})
            rows = d.get("data") if isinstance(d, dict) else None
            for p in rows if isinstance(rows, list) else []:
                try:
                    out.append(self._parse(p))
                except Exception:
                    pass
        return out

    def trades(self, pool, min_usd=0):
        params = {"trade_volume_in_usd_greater_than": min_usd} if min_usd else None
        d = self.h.get(f"{self.B}/pools/{pool}/trades", params=params)
        if not isinstance(d, dict) or not isinstance(d.get("data", []), list):
            return None
        return [t["attributes"] for t in d.get("data", []) if isinstance(t, dict) and isinstance(t.get("attributes"), dict)]


class DexScreener:
    B = "https://api.dexscreener.com"

    def __init__(self, http):
        self.h = http
        self.last_ok = True  # False when the last tokens() call could not get an answer (network/API down)

    def tokens(self, mints):
        """mint -> best (highest-volume) Solana pair snapshot. self.last_ok tells whether DexScreener ANSWERED:
        a token missing from a valid answer (possible rug) is different from no answer at all (outage)."""
        res = {}
        mints = list(dict.fromkeys(mints))
        ok = True
        for i in range(0, len(mints), 30):
            d = self.h.get(f"{self.B}/tokens/v1/solana/{','.join(mints[i:i+30])}")
            if not isinstance(d, list):
                ok = False
                d = []
            for p in d:
                if not isinstance(p, dict):
                    continue
                try:
                    m = (p.get("baseToken") or {}).get("address")
                except AttributeError:
                    continue
                if not m:
                    continue
                try:
                    snap = {"pool": p.get("pairAddress"), "mint": m, "quote_mint": p.get("quoteToken", {}).get("address"),
                            "name": p.get("baseToken", {}).get("symbol"), "dex": p.get("dexId"),
                            "price": f(p.get("priceUsd")),
                            "created_ts": (p.get("pairCreatedAt") or 0) / 1000 or None,
                            "vol_m5": f(p.get("volume", {}).get("m5")), "vol_h1": f(p.get("volume", {}).get("h1")),
                            "buys_m5": int(p.get("txns", {}).get("m5", {}).get("buys") or 0),
                            "sells_m5": int(p.get("txns", {}).get("m5", {}).get("sells") or 0),
                            "liquidity": f((p.get("liquidity") or {}).get("usd")), "fdv": f(p.get("fdv")),
                            "chg_h1": f((p.get("priceChange") or {}).get("h1")),
                            "src": "dexscreener"}
                except (AttributeError, TypeError, ValueError):
                    continue  # malformed pair: skip it, keep the others
                if not finite(snap["price"]) or snap["price"] < 0:
                    continue
                if m not in res or snap["vol_h1"] > res[m]["vol_h1"]:
                    res[m] = snap
        self.last_ok = ok
        return res


class RugCheck:
    B = "https://api.rugcheck.xyz/v1"

    def __init__(self, http):
        self.h = http
        self.keep_raw = False  # True only when the AntiRug engine is on (it re-reads the same report)
        self.raw = {}

    def _keep(self, mint, d):
        if self.keep_raw and isinstance(d, dict):
            self.raw[mint] = (now(), d)
            if len(self.raw) > 200:
                self.raw = dict(list(self.raw.items())[-100:])

    def report(self, mint, ttl=300):
        """Raw RugCheck report (AntiRug). Reuses the report fetched by the holder filter when fresh."""
        hit = self.raw.get(mint)
        if hit and now() - hit[0] < ttl:
            return hit[1]
        d = self.h.get(f"{self.B}/tokens/{mint}/report")
        self._keep(mint, d)
        return d if isinstance(d, dict) else None

    def holders(self, mint):
        d = self.h.get(f"{self.B}/tokens/{mint}/report")
        self._keep(mint, d)
        if not d or not isinstance(d, dict):
            return None
        try:
            return self._parse(d)
        except (AttributeError, TypeError, ValueError) as e:  # malformed report: treated as "no holder data"
            log.warning("RugCheck report malformed for %s: %s", mint[:8], e)
            return None

    @staticmethod
    def _parse(d):
        known = d.get("knownAccounts") or {}
        skip_types = {"AMM", "LOCKER"}
        skip = {k for k, v in known.items() if (v or {}).get("type") in skip_types}
        for m in d.get("markets") or []:
            skip.update(filter(None, [m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB")]))
        creator = d.get("creator")
        holders = [h for h in (d.get("topHolders") or []) if isinstance(h, dict)]
        real = [h for h in holders if h.get("owner") not in skip and h.get("address") not in skip]
        top10 = sum(f(h.get("pct")) for h in real[:10])
        dev = sum(f(h.get("pct")) for h in holders if creator and h.get("owner") == creator)
        dev_source = "verified"
        if dev == 0 and creator and d.get("creatorBalance"):
            supply = f((d.get("token") or {}).get("supply"))
            if supply:
                dev = 100 * f(d.get("creatorBalance")) / supply
                dev_source = "fallback_calculation"
        elif dev == 0:
            dev_source = "unknown"
        top10_quality = "VERIFIED" if real else "UNKNOWN"
        return {"source": "rugcheck", "dev_pct": dev, "dev_pct_source": dev_source,
                "top10_pct": top10, "top10_pct_quality": top10_quality,
                "risk_score": d.get("score_normalised"), "rugged": bool(d.get("rugged")),
                "risks": [r.get("name") for r in d.get("risks") or []],
                "n_holders": d.get("totalHolders"),
                "decimals": (d.get("token") or {}).get("decimals"),
                "holder_wallets": [h.get("owner") for h in real],
                "holder_amounts": {h.get("owner"): f(h.get("uiAmount")) for h in holders if h.get("owner")}}


class Vybe:
    """Vybe Network API (same endpoints as vybenetwork/solana-top-{traders,holders}-api). Needs VYBE_API_KEY."""
    B = "https://api.vybenetwork.xyz"

    def __init__(self, http, key, cfg, state_path):
        self.h, self.c, self.path = http, cfg, state_path
        self.hdr = {"X-API-Key": key, "Accept": "application/json"}
        self.disabled = set()
        self.state = {"month": datetime.now().strftime("%Y-%m"), "day": "", "month_credits": 0, "day_credits": 0, "calls": {}}
        try:
            st = json.load(open(state_path))
            if st.get("month") == self.state["month"]:
                self.state = st
        except Exception:
            pass

    def _save(self):
        try:
            atomic_json(self.path, self.state)
        except Exception as e:
            log.warning("cannot save Vybe usage: %s", e)

    def _can(self, endpoint):
        if endpoint in self.disabled:
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state["day"] != today:
            self.state["day"], self.state["day_credits"] = today, 0
        cost = self.c["vybe_credit_costs"].get(endpoint, 80)
        if self.state["month_credits"] + cost > self.c["vybe_monthly_credits"] * self.c["vybe_monthly_use_pct"]:
            log.warning("Vybe monthly credit budget reached; skipping %s", endpoint); return False
        if self.state["day_credits"] + cost > self.c["vybe_daily_credit_cap"]:
            log.info("Vybe daily credit cap reached; skipping %s (RugCheck stays in use)", endpoint); return False
        return True

    def _get(self, endpoint, path, params):
        if not self._can(endpoint):
            return None
        cost = self.c["vybe_credit_costs"].get(endpoint, 80)
        self.state["month_credits"] += cost; self.state["day_credits"] += cost
        self.state["calls"][endpoint] = self.state["calls"].get(endpoint, 0) + 1
        self._save()
        code, data = self.h.request(self.B + path, headers=self.hdr, params=params, tries=1, raw_status=True)
        if code in (401, 403):
            log.warning("Vybe %s -> HTTP %s; disabling this endpoint (plan/key)", endpoint, code)
            self.disabled.add(endpoint); return None
        if code != 200:
            log.warning("Vybe %s -> HTTP %s", endpoint, code); return None
        return data

    def top_holders(self, mint, limit=20):
        d = self._get("top_holders", f"/v4/tokens/{mint}/top-holders", {"limit": limit, "page": 0})
        return (d or {}).get("data") if d else None

    def top_traders(self, resolution, limit):
        d = self._get("top_traders", "/v4/wallets/top-traders",
                      {"resolution": resolution, "limit": limit, "sortByDesc": "realizedPnlUsd"})
        return {t.get("accountAddress") for t in (d or {}).get("data", []) if t.get("accountAddress")}

    def usage(self):
        return {"estimated_credits_used_this_month": self.state["month_credits"],
                "estimated_credits_left_this_month": self.c["vybe_monthly_credits"] - self.state["month_credits"],
                "credits_used_today": self.state["day_credits"], "calls_this_month": self.state["calls"],
                "disabled_endpoints": sorted(self.disabled),
                "note": "local estimate from configured per-call costs; verify in the Vybe dashboard"}


class Jupiter:
    """Jupiter swap QUOTE API (read-only price discovery)."""

    def __init__(self, http, cfg, key=""):
        self.h, self.c = http, cfg
        self.hdr = {"x-api-key": key} if key else None
        self.cache = {}
        self.stats = {"ok": 0, "fail": 0, "no_route": 0}
        self.unavailable = {}
        self.last_reason = ""

    def _miss(self, reason):
        self.last_reason = reason
        self.unavailable[reason] = self.unavailable.get(reason, 0) + 1

    @staticmethod
    def _impact_pct(d):
        if d.get("priceImpact") is not None:
            return abs(f(d.get("priceImpact")))
        return 100 * f(d.get("priceImpactPct"))

    def quote(self, in_mint, out_mint, amount_raw, deadline=None):
        amount_raw = int(amount_raw)
        self.last_reason = ""
        if amount_raw <= 0:
            self._miss("bad_amount")
            return None
        key = (in_mint, out_mint, amount_raw)
        hit = self.cache.get(key)
        if hit and now() - hit[0] < self.c.get("cache_seconds", 10):
            return {**hit[1], "age_s": round(now() - hit[0], 2), "cached": True}
        last_kind = "unknown"
        # One bounded deadline covers all configured Jupiter endpoints. A 429 is
        # never retried here: Http owns Retry-After/cooldown and the caller falls
        # back to the liquidity model instead of blocking the trading loop.
        quote_budget = float(self.c.get("request_deadline_seconds", 2.5) if deadline is None else deadline)
        deadline_at = now() + max(0.05, quote_budget)
        for ep in self.c.get("endpoints") or ["https://api.jup.ag/swap/v1/quote"]:
            params = {"inputMint": in_mint, "outputMint": out_mint, "amount": amount_raw}
            if ep.endswith("/quote"):
                params.update({"slippageBps": self.c.get("slippage_bps", 5000), "swapMode": "ExactIn"})
            t_req = now()
            remaining = deadline_at - t_req
            cycle_remaining = getattr(self.h, "cycle_remaining", None)
            cycle_budget = cycle_remaining() if callable(cycle_remaining) else None
            if cycle_budget is not None:
                remaining = min(remaining, max(0.0, float(cycle_budget)))

            if remaining <= 0.05:
                last_kind = (
                    net.DEFERRED
                    if cycle_budget is not None and float(cycle_budget) <= 0.05
                    else "deadline"
                )
                break

            r = self.h.request_ex(ep, headers=self.hdr, params=params, tries=1, deadline=remaining)
            if r.kind in (net.RATE, net.COOLDOWN, net.CIRCUIT):
                last_kind = r.kind
                # Endpoints may share the same host/cooldown. Never clear it or
                # immediately retry around the server's rate-limit response.
                break
            d = r.data if r.status == 200 else None
            if r.status == 200 and isinstance(d, dict) and d.get("outAmount"):
                try:
                    out = int(d["outAmount"])
                except (TypeError, ValueError):
                    last_kind = "malformed"
                    continue
                labels = [((x.get("swapInfo") or {}).get("label") or "?") for x in d.get("routePlan") or [] if isinstance(x, dict)]
                route = d.get("router") or "+".join(dict.fromkeys(labels))
                q = {"out": out, "in": amount_raw, "impact_pct": self._impact_pct(d),
                     "route": str(route or "")[:60], "endpoint": "/".join(ep.split("/")[-2:]),
                     "quote_ts": t_req, "latency_s": round(now() - t_req, 3)}
                self.cache[key] = (t_req, q)
                if len(self.cache) > 500:
                    self.cache = dict(list(self.cache.items())[-200:])
                self.stats["ok"] += 1
                return {**q, "age_s": round(now() - t_req, 2), "cached": False}
            if r.status in (400, 404):
                # A single endpoint returning 400/404 does not prove that
                # every configured Jupiter endpoint has no route.
                last_kind = "no_route"
                continue
            last_kind = "malformed" if r.status == 200 else (r.kind or "unknown")

        # Classify no-route only after all configured endpoints were tried.
        if last_kind == "no_route":
            self.stats["no_route"] += 1
            self._miss("no_route")
            return None

        self.stats["fail"] += 1
        self._miss(last_kind)
        return None


class _RPCManagerWrapper:
    """Wrapper to make RPCManager compatible with existing SolanaRPC interface."""

    def __init__(self, rpc_manager: 'RPCManager'):
        self._manager = rpc_manager
        self.stats = {"ok": 0, "fail": 0, "last_success_ts": None, "last_failure_ts": None}

    def begin_cycle_budget(self, seconds: float):
        self._manager.begin_cycle_budget(seconds)

    def end_cycle_budget(self):
        self._manager.end_cycle_budget()

    def call(self, method: str, params: List[Any]) -> Optional[Any]:
        result, provider = self._manager.call(method, params)
        now_ts = now()
        if result is not None:
            self.stats["ok"] += 1
            self.stats["last_success_ts"] = now_ts
        else:
            self.stats["fail"] += 1
            self.stats["last_failure_ts"] = now_ts
        return result

    def decimals(self, mint: str) -> Optional[int]:
        r = self.call("getTokenSupply", [mint])
        try:
            return int(r["value"]["decimals"])
        except (TypeError, KeyError, ValueError):
            return None

    def balance(self, owner: str, mint: str) -> Optional[float]:
        r = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        if r is None:
            return None
        tot = 0.0
        for a in r.get("value") or []:
            try:
                ta = a["account"]["data"]["parsed"]["info"]["tokenAmount"]
                tot += f(ta.get("uiAmountString") or ta.get("uiAmount"))
            except (KeyError, TypeError):
                pass
        return tot

    def balances_batch(self, wallets: list, mint: str) -> dict:
        result = {}
        for i in range(0, len(wallets), 50):
            chunk = wallets[i:i + 50]
            for w in chunk:
                b = self.balance(w, mint)
                if b is not None:
                    result[w] = b
        return result


class SolanaRPC:
    """Read-only JSON-RPC (token decimals, a wallet's balance of a token). Public endpoint by default."""

    def __init__(self, http, url):
        self.h, self.url = http, url
        self.stats = {"ok": 0, "fail": 0}

    def call(self, method, params):
        code, d = self.h.request(self.url, json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                 tries=1, raw_status=True)
        if code == 200 and isinstance(d, dict) and "result" in d:
            self.stats["ok"] += 1
            return d["result"]
        self.stats["fail"] += 1
        return None

    def decimals(self, mint):
        r = self.call("getTokenSupply", [mint])
        try:
            return int(r["value"]["decimals"])
        except (TypeError, KeyError, ValueError):
            return None

    def balance(self, owner, mint):
        r = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        if r is None:
            return None
        tot = 0.0
        for a in r.get("value") or []:
            try:
                ta = a["account"]["data"]["parsed"]["info"]["tokenAmount"]
                tot += f(ta.get("uiAmountString") or ta.get("uiAmount"))
            except (KeyError, TypeError):
                pass
        return tot


AGENTS = ["stream", "trend", "volume", "smart", "copy", "holders", "whales", "dev"]


# ---------------------------------------------------------------- the bot
class PaperBot:
    def __init__(self, cfg, data_dir, reset=False):
        self.c = cfg
        self.data = data_dir
        self.reset_pending = False
        # --- Pump.fun Scanner (free public channels only)
        pf_cfg = self.c.get("pumpfun") or {}
        self.pumpfun = PumpFunScanner(
            data_dir,
            api_key=os.getenv("PUMPPORTAL_API_KEY", "").strip(),
            enabled=pf_cfg.get("enabled", True) and os.getenv("PUMPFUN_ENABLED", "1").lower() not in ("0", "false", "no", "off"),
            max_events=pf_cfg.get("max_events", 200),
        )
        # --- GMGN Scanner / Intelligence Adapter (free public channels + optional Agent API)
        gmgn_cfg = self.c.get("gmgn") or {}
        gmgn_enabled = gmgn_cfg.get("enabled", True) and os.getenv("GMGN_ENABLED", "1").lower() not in ("0", "false", "no", "off")
        gmgn_key = os.getenv("GMGN_API_KEY", "").strip()
        if gmgn_key:
            self.gmgn = GMGNIntelligenceAdapter(
                data_dir=data_dir,
                gmgn_api_key=gmgn_key,
                enabled=gmgn_enabled,
                max_events=gmgn_cfg.get("max_events", 200),
            )
        else:
            self.gmgn = GmGnScanner(
                data_dir,
                api_key="",
                enabled=gmgn_enabled,
                max_events=gmgn_cfg.get("max_events", 200),
                public_channels=gmgn_cfg.get("public_channels", ["new_pool_info", "new_pair_update", "new_launched_info", "chain_stat"]),
            )
        self.heartbeat = now()
        self.session_start = now()
        self.shot = screenshot.settings(cfg)
        self._shot_req = threading.Event()
        self._shot_thread = None
        self.shot_status = {"sent": 0, "fallback": 0, "last_ok": None, "last_error": None}
        self.http = Http(cfg.get("http_total_timeout_s", 20), cfg.get("http_connect_timeout_s", 5),
                         cfg.get("http_read_timeout_s", 10), on_progress=self.beat,
                         retries=cfg.get("http_retries", 2), breaker_after=cfg.get("http_breaker_after", 5))
        self.gt, self.ds, self.rc = GeckoTerminal(self.http), DexScreener(self.http), RugCheck(self.http)
        key = os.getenv("VYBE_API_KEY", "").strip()
        self.vybe = Vybe(self.http, key, cfg, os.path.join(data_dir, "vybe_usage.json")) if key else None
        bc = cfg.get("birdeye") or {}
        be_on = os.getenv("BIRDEYE_ENABLED", str(bc.get("enabled", 0))).strip().lower() in ("1", "true", "yes", "on")
        bkey = os.getenv("BIRDEYE_API_KEY", "").strip()
        self.birdeye = Birdeye(self.http, bkey, bc) if (be_on and bkey) else None
        if be_on and not bkey:
            log.warning("BIRDEYE_ENABLED=1 but BIRDEYE_API_KEY is empty: Birdeye stays OFF")
        self.birdeye_copy = bool(self.birdeye and bc.get("copy_source", True))
        self.antirug_mode, ar_warn = antirug_mod.resolve_mode(os.environ, cfg.get("antirug") or {})
        if ar_warn:
            log.warning(ar_warn)
        self.antirug_enforce, ae_warn = antirug_mod.resolve_enforce(os.environ, cfg.get("antirug") or {},
                                                                  self.antirug_mode)
        if ae_warn:
            log.warning(ae_warn)
        self.profile, self.profile_src, self.profile_visible = profiles.resolve(data_dir)
        self.profile_started = self.profile
        _pa = profiles.PROFILES[self.profile]
        if _pa["antirug"] == "active":
            self.antirug_mode, self.antirug_enforce = "active", ()
        elif _pa["antirug"] == "shadow+top10" and self.antirug_mode != "active":
            self.antirug_mode = "shadow"
            self.antirug_enforce = tuple(dict.fromkeys(tuple(self.antirug_enforce) + ("holders_top10_concentrated",)))
        if _pa["risk_mode"]:
            self.c.setdefault("risk", {})["mode"] = _pa["risk_mode"]
        if self.profile_visible:
            log.warning("Risk profile: %s (%s)", self.profile.upper(), self.profile_src)
        self.antirug = None
        jc = cfg.get("jupiter") or {}
        self.jup = Jupiter(self.http, jc, os.getenv("JUPITER_API_KEY", "").strip()) if jc.get("enabled", True) else None

        self.rpc_manager = create_rpc_manager_from_env(lambda: requests.Session())
        self.rpc_manager.start_health_checks()
        self.rpc = _RPCManagerWrapper(self.rpc_manager)

        self.decimals_cache = {}
        self.tt_cache = os.path.join(data_dir, "top_traders_cache.json")
        self.state_path = os.path.join(data_dir, "state.json")
        self.csv_path = os.path.join(data_dir, "trades.csv")
        self.tg = tg.Telegram(outbox_path=os.path.join(data_dir, "telegram_outbox.log"),
                              enabled=cfg.get("telegram_enabled", True),
                              pending_path=os.path.join(data_dir, "telegram_pending.json"))
        if self.antirug_mode != "off":
            self.rc.keep_raw = True
            self.antirug = antirug_mod.AntiRugEngine(
                cfg.get("antirug") or {}, self.antirug_mode, http=self.http, birdeye=self.birdeye,
                rc_report=self.rc.report, jup_quote=self._ar_quote if self.jup else None, decimals=self.token_decimals,
                slippage_bps=jc.get("slippage_bps", 5000), clock=now, data_dir=data_dir,
                sol_price=lambda: self.meta.get("sol_price") or self.c["costs"]["sol_price_usd_fallback"],
                notify=lambda text, key: self.tg.send(text, key=key), enforce=self.antirug_enforce)
            log.warning("AntiRug Security Engine ON in %s mode (%s)", self.antirug_mode.upper(),
                        ("observe only, never blocks" if not self.antirug_enforce else
                         "observe only EXCEPT enforced: " + ",".join(self.antirug_enforce) + " (verified data only)")
                        if self.antirug_mode == "shadow" else "FAIL-CLOSED: blocks entries")
        self.tz = ZoneInfo(os.getenv("TZ") or cfg.get("timezone", "Europe/Rome")) if ZoneInfo else None
        self.watch, self.rejected, self.holder_cache = {}, {}, {}
        self._holder_cache_last_prune = 0.0
        self.top_traders, self.top_traders_ts = set(), 0
        self.tt_sources = {}
        self.allowed_quotes = {QUOTE_MINTS[s] for s in cfg["allowed_quote_symbols"] if s in QUOTE_MINTS}
        self.stop, self.stop_reason, self._stop_ts, self.final_saved = False, "stop", None, False
        self.last_manage = 0
        self.last_save = 0
        self.lock = threading.RLock()
        random.seed(cfg["costs"].get("random_seed"))
        self.resumed = False
        self.recovered = None
        self.prev_risk = None
        self.ops = {"loop_errors": [], "last_err_alert": 0, "api_alerted": {}, "disk": {}, "last_disk": 0,
                    "last_backup": now(), "last_health": 0, "dash_fail_since": None, "dash_alerted": False,
                    "last_dash": 0, "last_hb_write": 0, "backup_day_notified": ""}
        self.heartbeat_path = os.path.join(data_dir, "heartbeat.json")
        self.backups = Backups(data_dir, keep=(cfg.get("backup") or {}).get("keep", 48),
                               min_free_mb=(cfg.get("backup") or {}).get("min_free_mb", 200))
        self.fx_now = self.get_fx()
        self.c["starting_balance_usd"] = round(self.c["starting_balance_eur"] * self.fx_now, 4)

        ap_cfg = self.c.get("adaptive_poll") or {}
        self.adaptive_poll = {
            "enabled": ap_cfg.get("enabled", True),
            "base_interval": ap_cfg.get("base_interval_seconds", 8),
            "accelerated_interval": ap_cfg.get("accelerated_interval_seconds", 1),
            "emergency_interval": ap_cfg.get("emergency_interval_seconds", 0.5),
            "triggers": ap_cfg.get("triggers", {}),
            "cooldown_seconds": ap_cfg.get("cooldown_seconds", 300),
            "max_accelerated": ap_cfg.get("max_accelerated_positions", 10),
            "accelerated": {},
            "last_check": 0,
        }

        rm_cfg = self.c.get("recovery_mode") or {}
        self.recovery_mode = {
            "enabled": rm_cfg.get("enabled", True),
            "crash_threshold": rm_cfg.get("crash_threshold_pct", 0.70),
            "min_liquidity_usd": rm_cfg.get("min_liquidity_usd", 1000),
            "verification_timeout": rm_cfg.get("verification_timeout_s", 30),
            "max_retries": rm_cfg.get("max_verification_retries", 3),
            "require_jupiter_route": rm_cfg.get("require_jupiter_route", True),
            "verifying": {},
        }

        self._new_state()
        if not reset:
            self._load_state()
        self.meta.setdefault("simulation_only", True)
        tg.set_currency(self.meta["fx_start"], "€")
        seed_brain_rules(data_dir)
        self.brain = Brain(self.c, data_dir, self.tz, locked=(self.meta.get("overrides") or {}).keys())
        try:
            ch = self.brain.startup_slippage_check() if hasattr(self.brain, "startup_slippage_check") else None
            if ch:
                self._brain_startup_change = ch
        except Exception as e:
            log.warning("brain start-up slippage check failed: %s", e)
        self.risk = RiskManager(self.c, self.prev_risk, self.tz)
        self.cmds = Commands(self, self.tg)
        sname = os.getenv("SESSION_NAME", "").strip()
        if sname and not self.meta.get("session"):
            self.meta["session"] = {"name": sname, "started_ts": now(), "start_eur": self.c["starting_balance_eur"],
                                    "fresh": not self.closed and not self.positions}
        self.provenance_on = os.getenv("PRICE_PROVENANCE", "").strip().lower() in ("1", "true", "yes", "on")
        dg_on = os.getenv("DISK_GUARD", "").strip().lower() in ("1", "true", "yes", "on")
        self.disk_guard = disk_guard.DiskGuard(cfg, data_dir, lambda text, key: self.tg.send(text, key=key),
                                               meta=self.meta) if dg_on else None

        from wallet_intelligence import create_wallet_intelligence, WalletIntelligenceConfig
        wi_config = WalletIntelligenceConfig()
        self.wallet_intelligence = create_wallet_intelligence(data_dir, wi_config)
        if self.gmgn and hasattr(self.gmgn, 'agent_client') and self.gmgn.agent_client:
            self._ingest_wallet_intelligence()

    # ---- state
    def _new_state(self):
        self.cash = self.c["starting_balance_usd"]
        self.positions, self.closed, self.events, self.equity = {}, [], [], []
        self.stats = {"pools_seen": 0, "spike_candidates": 0, "copy_candidates": 0, "filtered_out": {},
                      "failed_exits": 0, "signals": 0}
        self.agents = {a: {"n": 0, "ok": 0, "last": "", "ts": 0} for a in AGENTS}
        self.meta = {"created_ts": now(), "restarts": 0, "watchdog_restarts": 0, "stalls": [],
                     "last_daily_summary": "", "sol_price": None, "fx_start": self.fx_now,
                     "dead": False, "dead_ts": None, "last_exit": {}, "simulation_only": True,
                     "slip_src": {"jupiter": 0, "stimato": 0},
                     "quality": {"quote_unavailable": {}, "price_anomalies": 0, "rug_assumption_skipped_outage": 0,
                                 "signal_latency_s": []}}

    @staticmethod
    def _valid_state(st):
        return (isinstance(st, dict) and finite(st.get("cash")) and isinstance(st.get("positions", {}), dict)
                and isinstance(st.get("closed", []), list) and finite(st.get("starting_balance_usd")))

    def _load_state(self):
        st, src = load_json_safe(self.state_path, validate=self._valid_state,
                                 backup_dir=os.path.join(self.data, "backups"), backup_name="state.json")
        if src == "missing":
            return
        if st is None:
            log.critical("state.json and all backups unreadable - starting fresh; old file kept as state.json.bad")
            try:
                os.replace(self.state_path, self.state_path + f".{int(now())}.bad")
            except Exception:
                pass
            self.recovered = {"kind": "corrupt_reset", "src": src}
            return
        if src != "main":
            self.recovered = {"kind": "restored_backup", "src": src}
        if st.get("version") != STATE_VERSION or st.get("starting_balance_eur") != self.c["starting_balance_eur"]:
            log.warning("state.json from a different version/start balance: archived, starting fresh")
            os.replace(self.state_path, self.state_path + f".{int(now())}.old")
            return
        self.cash = st["cash"]
        self.positions = st.get("positions", {})
        self.closed = st.get("closed", [])
        self.events = st.get("events", [])
        self.equity = st.get("equity", [])
        self.stats.update(st.get("stats", {}))
        for a, v in (st.get("agents") or {}).items():
            if a in self.agents:
                self.agents[a].update(v)
        self.meta.update(st.get("meta", {}))
        self.meta["simulation_only"] = True
        self.c["starting_balance_usd"] = st["starting_balance_usd"]
        for k, v in (self.meta.get("overrides") or {}).items():
            cfg_set(self.c, k, v)
        self.meta["restarts"] = self.meta.get("restarts", 0) + 1
        self.prev_risk = st.get("risk_state")
        last_hb = st.get("heartbeat_ts") or 0
        try:
            last_hb = max(last_hb, json.load(open(self.heartbeat_path)).get("ts") or 0)
        except Exception:
            pass
        if st.get("status") in ("running", "restarting", "starting") and not self.recovered:
            self.recovered = {"kind": "watchdog" if st.get("status") == "restarting" else "crash",
                              "down_sec": round(now() - last_hb) if last_hb else None}
        if last_hb and now() - last_hb > 120:
            self.meta["stalls"].append({"from": last_hb, "to": now(), "sec": round(now() - last_hb),
                                        "kind": "offline/restart"})
        self.resumed = True

        if "adaptive_poll" in st:
            saved_ap = st["adaptive_poll"]
            self.adaptive_poll.update(saved_ap)
            self.adaptive_poll["accelerated"] = {}

        if "recovery_mode" in st:
            saved_rm = st["recovery_mode"]
            self.recovery_mode.update(saved_rm)
            self.recovery_mode["verifying"] = {}

        t_boot = now()
        for _m, _p in self.positions.items():
            _p.pop("exit_in_progress", None)
            _p.pop("exit_started_ts", None)
            _p["_exit_retry_count"] = 0
            _p.pop("_exit_next_retry_ts", None)
            _p.setdefault("partial_exit_seq", int(_p.get("partial_exits", 0)))
            if _p.get("entry_type") == "volume_spike" and self._adaptive_holding_enabled():
                _p["adaptive_holding_enabled"] = True
                _p.setdefault("adaptive_last_eval_price", _p.get("last") or _p["entry_mid"])
                _p.setdefault("adaptive_last_eval_ts", _p.get("last_price_ts") or _p["entry_ts"])
                initial_sec = float((self.c.get("adaptive_holding") or {}).get("initial_minutes", 15)) * 60.0
                if (t_boot - _p["entry_ts"]) >= initial_sec:
                    # Punto 5: rivalutazione IMMEDIATA al primo ciclo dopo il riavvio, poi milestone successive
                    _p["adaptive_holding_next_check"] = t_boot
                    _p["adaptive_needs_immediate_eval"] = True
                else:
                    _p["adaptive_holding_next_check"] = _p["entry_ts"] + initial_sec

        log.info("Resumed state: cash $%.2f, %d open positions, %d closed trades (restart #%d)",
                 self.cash, len(self.positions), len(self._completed_trade_rows()), self.meta["restarts"])

    def save_state(self, status="running", lock_timeout=None):
        if not self.lock.acquire(timeout=-1 if lock_timeout is None else lock_timeout):
            log.error("save_state: lock busy for %ss, snapshot skipped (last good state stays on disk)", lock_timeout)
            self._mark_persistence_degraded("state snapshot skipped: lock busy")
            return False
        try:
            return self._save_state_locked(status)
        finally:
            self.lock.release()

    def _save_state_with_retry(self, status="stopped", attempts=2, lock_timeout=3.0, retry_delay=0.25):
        """Bounded critical save: never force-unlocks the RLock or assumes a failed save succeeded."""
        attempts = max(1, int(attempts))
        for attempt in range(attempts):
            if self.save_state(status, lock_timeout=lock_timeout):
                return True
            if attempt + 1 < attempts:
                log.warning("critical state save attempt %d/%d failed; retrying", attempt + 1, attempts)
                time.sleep(max(0.0, float(retry_delay)))
        log.critical("CRITICAL STATE NOT SAVED (%s); last known-good state file retained", status)
        return False

    def _mark_persistence_degraded(self, reason):
        was_degraded = bool(self.meta.get("persistence_degraded"))
        self.meta["persistence_degraded"] = True
        self.meta["persistence_error"] = str(reason)[:240]
        self.ops["persistence_degraded"] = True
        if not was_degraded:
            self.ops["persistence_errors"] = self.ops.get("persistence_errors", 0) + 1
            log.critical("PERSISTENCE DEGRADED: %s; new entries disabled until state is saved successfully", reason)
            try:
                self.tg.send("🚨 <b>AEON PERSISTENCE DEGRADED</b>\nSalvataggio dello stato non riuscito. Nuovi ingressi bloccati fino al ripristino.")
            except Exception:
                log.exception("cannot notify persistence degradation")

    def _save_state_locked(self, status):
        # Clear a previous degraded flag only for the snapshot we are now attempting.
        # If the write fails, the exception handler sets it again in memory.
        was_degraded = bool(self.meta.get("persistence_degraded"))
        self.meta["persistence_degraded"] = False
        self.meta.pop("persistence_error", None)
        self.ops["persistence_degraded"] = False
        st = {"version": STATE_VERSION, "simulation_only": True, "status": status,
              "heartbeat_ts": self.heartbeat, "saved_ts": now(),
              "starting_balance_usd": self.c["starting_balance_usd"], "cash": self.cash,
              "starting_balance_eur": self.c["starting_balance_eur"], "currency": "EUR",
              "fx_usd_per_eur": self.meta.get("fx_start"), "fx_now": self.fx_now,
              "survival": self.survival_status(), "dead": bool(self.meta.get("dead")),
              "positions": self.positions, "closed": self.closed[-2000:], "events": self.events[-200:],
              "equity": self.equity[-6000:], "stats": self.stats, "agents": self.agents, "meta": self.meta,
              "watch_count": len(self.watch), "ops_per_min": self.http.ops_per_min(),
              "api_ok": self.http.ok, "api_err": self.http.err,
              "vybe": self.vybe.usage() if self.vybe else None,
              "adaptive_poll": {k: v for k, v in self.adaptive_poll.items() if k != "accelerated"},
              "recovery_mode": {k: v for k, v in self.recovery_mode.items() if k != "verifying"},
              **({"birdeye": {**self.birdeye.status(), "copy": self.birdeye_copy, "antirug": self.antirug_mode}}
                 if getattr(self, "birdeye", None) else {}),
              **({"antirug": self.antirug.summary()} if getattr(self, "antirug", None) else {}),
              **({"risk_profile": self.profile_info()} if getattr(self, "profile_visible", False) else {}),
              **({"price_provenance": self.provenance_summary()} if getattr(self, "provenance_on", False) else {}),
              **({"session": self.meta.get("session")} if self.meta.get("session") else {}),
              "copy_traders": len(self.top_traders),
              "telegram": {"dry": self.tg.dry, "sent": self.tg.sent, "failed": self.tg.failed,
                           "persist_errors": getattr(self.tg, "persist_errors", 0),
                           "persist_last_error": getattr(self.tg, "persist_last_error", None)},
              "config": {**{k: self.c[k] for k in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
                                                   "trailing_activate_pct", "max_hold_minutes", "position_size_pct")},
                         "reentry_cooldown_minutes": self.c.get("reentry_cooldown_minutes", 30)},
              "exit_rules": {"volume_spike": self.exit_rules("volume_spike"),
                             "copy_top_traders": self.exit_rules("copy_top_traders")},
              "jupiter": {"enabled": bool(self.jup), **(self.jup.stats if self.jup else {})},
              "rpc": {**self.rpc.stats, **(self.rpc_manager.get_all_stats() if hasattr(self, "rpc_manager") else {})},
              "paused": bool(self.meta.get("paused")), "ticker": self._ticker(),
              "brain": self.brain.summary() if hasattr(self, "brain") else None,
              "risk": self.risk.summary() if hasattr(self, "risk") else None,
              "risk_state": self.risk.state() if hasattr(self, "risk") else None,
              "jupiter_unavailable": getattr(self.jup, "unavailable", {}) if self.jup else {},
              "health": {"hosts": self.http.health_report(), "disk": self.ops.get("disk"),
                         "backups": len(self.backups.list()), "backup_errors": self.ops.get("backup_errors", 0),
                         "telemetry_write_errors": self.ops.get("telemetry_write_errors", 0),
                         "persistence_degraded": bool(self.meta.get("persistence_degraded")),
                         "pid": os.getpid()},
              "realized_balance": self.realized_value(),
              "non_liquidatable_value": self.non_liquidatable_value(),
              "executable_liquidation_value": self.executable_liquidation_value(),
              "smart_money": self._smart_money_snapshot()}
        try:
            if getattr(self, "antirug", None):
                self.antirug.save()
            atomic_json(self.state_path, storage.sanitize(st), keep_bak=True)
            self.last_save = now()
            if was_degraded:
                log.info("PERSISTENCE RECOVERED: state saved successfully; new entries re-enabled")
                try:
                    self.tg.send("✅ <b>AEON PERSISTENCE RECOVERED</b>\nSalvataggio dello stato ripristinato. Nuovi ingressi nuovamente abilitati.")
                except Exception:
                    log.exception("cannot notify persistence recovery")
            return True
        except Exception as e:
            log.exception("cannot write critical bot state: %s", e)
            self.ops.setdefault("save_errors", 0)
            self.ops["save_errors"] += 1
            self._mark_persistence_degraded(f"state write failed: {type(e).__name__}: {e}")
            return False

    def write_heartbeat(self):
        try:
            atomic_json(self.heartbeat_path, {"ts": self.heartbeat, "pid": os.getpid(), "status":
                                              "dead" if self.meta.get("dead") else "running"}, indent=None)
            self.ops["last_hb_write"] = now()
        except Exception as e:
            self.ops["heartbeat_write_errors"] = self.ops.get("heartbeat_write_errors", 0) + 1
            log.error("cannot write heartbeat (critical state snapshot is tracked separately): %s", e)

    def _ticker(self):
        out = []
        for m, p in self.positions.items():
            out.append({"token": p["name"], "chg": round(100 * (p["last"] / p["entry_mid"] - 1), 1), "open": True})
        for t in reversed(self.closed[-12:]):
            out.append({"token": t["token"], "chg": round(t["pnl_pct"], 1), "open": False})
        return out[:14]

    def get_fx(self):
        path = os.path.join(self.data, "fx.json")
        try:
            c = json.load(open(path))
            if now() - c["ts"] < self.c.get("fx_cache_hours", 12) * 3600 and 0.5 < c["rate"] < 2:
                return c["rate"]
        except Exception:
            pass
        rate = None
        d = self.http.get("https://api.frankfurter.app/latest", params={"from": "EUR", "to": "USD"}, tries=1)
        if d and (d.get("rates") or {}).get("USD"):
            rate = f(d["rates"]["USD"])
        if not rate:
            d = self.http.get("https://open.er-api.com/v6/latest/EUR", tries=1)
            rate = f(((d or {}).get("rates") or {}).get("USD")) or None
        if rate and 0.5 < rate < 2:
            try:
                atomic_json(path, {"ts": now(), "rate": rate})
            except Exception:
                pass
            log.info("EUR/USD rate %.4f", rate)
            return rate
        log.warning("FX APIs unavailable, using fallback EUR/USD %.4f", self.c.get("fx_fallback_usd_per_eur", 1.08))
        return float(self.c.get("fx_fallback_usd_per_eur", 1.08))

    # ---- survival mode
    def _sv(self):
        return self.c.get("survival") or {}

    def min_trade_usd(self):
        return self._sv().get("min_trade_eur", 3) * self.meta.get("fx_start", self.fx_now)

    def next_size(self):
        size = max(self.equity_value() * self.eff("position_size_pct"), self.min_trade_usd())
        return round(min(size, self.cash), 4)

    def can_enter(self):
        if self.meta.get("dead") or self.meta.get("paused"):
            return False
        if self.meta.get("persistence_degraded"):
            return False
        return True

    def provenance_summary(self):
        out = {"VERIFIED": {"n": 0, "pnl_usd": 0.0, "wins": 0}, "ESTIMATED": {"n": 0, "pnl_usd": 0.0, "wins": 0}}
        for t in self.closed:
            k = t.get("price_provenance")
            if k in out:
                out[k]["n"] += 1; out[k]["pnl_usd"] = round(out[k]["pnl_usd"] + t["pnl_usd"], 4)
                out[k]["wins"] += t["pnl_usd"] > 0
        out["REJECTED"] = {"n": self.meta.get("price_rejected_n", 0),
                           "unconfirmed_jumps_ignored": (self.meta.get("quality") or {}).get("price_anomalies", 0)}
        return out

    def eff(self, key):
        return profiles.effective(getattr(self, "profile", profiles.DEFAULT), key, self.c.get(key))

    def set_profile(self, name):
        p = profiles.normalize(name)
        if not p:
            return None
        self.profile, self.profile_src, self.profile_visible = p, "telegram /aggr", True
        profiles.save(self.data, p)
        log.warning("Risk profile set to %s via Telegram", p.upper())
        return p

    def profile_info(self):
        pa = profiles.PROFILES[self.profile]
        st = profiles.PROFILES[self.profile_started]
        restart = (pa["antirug"] != st["antirug"]) or (pa["risk_mode"] != st["risk_mode"])
        return {"name": self.profile, "label": profiles.LABEL[self.profile], "source": self.profile_src,
                "desc": profiles.describe(self.profile), "restart_needed": restart,
                "effective": {k: self.eff(k) for k in ("position_size_pct", "max_open_positions", "min_liquidity_usd",
                                                       "max_top10_pct", "min_volume_5m_usd", "spike_multiplier",
                                                       "max_entry_slippage_pct")},
                "antirug_mode": self.antirug_mode, "risk_mode": self.risk.mode if hasattr(self, "risk") else None}

    def survival_status(self):
        sv = self._sv()
        eq = self.equity_value()
        mode = "dead" if self.meta.get("dead") else "paused" if self.meta.get("paused") else "ok"
        nxt = self.next_size() if not self.meta.get("dead") else 0
        return {"enabled": bool(sv.get("enabled", True)), "mode": mode, "equity_usd": eq,
                "min_trade_usd": self.min_trade_usd(),
                "next_size_usd": nxt, "size_pct_eff": 100 * nxt / eq if eq else 0,
                "max_open": self.c.get("max_open_positions"), "dead_ts": self.meta.get("dead_ts"),
                "death_line_usd": self.min_trade_usd() + 2 * self._net_fee_usd()}

    def check_death(self):
        if self.meta.get("dead") or not self._sv().get("enabled", True) or self.positions:
            return
        if self.cash < self.min_trade_usd() + 2 * self._net_fee_usd():
            self.meta["dead"], self.meta["dead_ts"] = True, now()
            log.warning("DEAD: cash $%.2f below minimum tradable amount. No more trades, ever.", self.cash)
            self.event("DEAD", "BOT", "capitale esaurito")
            sv = self.survival_status()
            sv["lifetime"] = f"{(now() - self.meta.get('created_ts', now())) / 3600:.1f} h".replace(".", ",")
            self.tg.send(tg.msg_dead(self.report(), self.c["starting_balance_usd"], sv))
            self.save_state("dead")

    def beat(self):
        self.heartbeat = now()

    def agent(self, name, ok=None, last=None, load=None):
        a = self.agents[name]
        if load is not None:
            a["load"] = max(0, min(100, round(load)))
        a["n"] += 1
        if ok:
            a["ok"] += 1
        if last:
            a["last"] = last.replace("$$", "$")
        a["ts"] = now()

    def event(self, kind, token, text, pnl=None):
        self.events.append({"ts": now(), "kind": kind, "token": token, "text": text, "pnl": pnl})
        self.events = self.events[-200:]

    def equity_value(self, force_fresh=False):
        total = self.cash
        for m, p in self.positions.items():
            price = p["last"]
            if force_fresh and not getattr(self.ds, "last_ok", True):
                jq = self.jup_sell(m, price, p["qty"])
                if jq:
                    implied = jq[1]
                    price = min(price, implied)
            total += p["qty"] * price
        return total

    def realized_value(self):
        return self.cash + sum(p["cost_usd"] for p in self.positions.values())

    def non_liquidatable_value(self):
        total = 0.0
        for m, p in self.positions.items():
            if p.get("non_liquidatable"):
                total += p["qty"] * p["last"]
        return total

    def executable_liquidation_value(self):
        total = 0.0
        for m, p in self.positions.items():
            if p.get("non_liquidatable"):
                liq_val = p.get("liquidation_value", 0)
                total += liq_val
        return total

    def mark_equity(self, force=False):
        if force or not self.equity or now() - self.equity[-1][0] >= 60:
            self.equity.append([round(now()), round(self.equity_value(), 4), round(self.realized_value(), 4)])
            if len(self.equity) > 20000:
                self.equity = self.equity[-6000:]

    # ---- simulated execution
    def _slip(self, size_usd, liq):
        k = self.c["costs"]
        liq = liq if liq and liq > 0 else k["fallback_liquidity_usd"]
        extra = k["low_liq_extra_pct"] * max(0.0, 1 - liq / k["low_liquidity_usd"])
        slip = k["min_slippage_pct"] + extra + k["impact_factor"] * size_usd / liq
        return min(max(slip, k["min_slippage_pct"]), k["max_slippage_pct"])

    def _dex_fee(self, dex):
        t = self.c["costs"]["dex_fee_pct"]
        return t.get((dex or "").lower(), t["default"])

    def _net_fee_usd(self):
        k = self.c["costs"]
        return k["network_fee_sol_per_tx"] * (self.meta.get("sol_price") or k["sol_price_usd_fallback"])

    def _estimated_breakeven_price(self, position):
        """Return a modelled net break-even trigger for the remaining position.

        This is an estimate, not a guaranteed executable price. It includes
        network fees, DEX fees and the liquidity-based exit-slippage model.
        """
        qty = max(float(position.get("qty", 0.0)), 0.0)
        if qty <= 0:
            return float(position.get("entry_fill", 0.0)), 0.0, 0.0
        remaining_cost = max(float(position.get("cost_usd", 0.0)), 0.0)
        mark = max(float(position.get("last", 0.0) or 0.0), 0.0)
        est_exit_fee = self._net_fee_usd()
        est_slippage = self._slip(qty * mark, position.get("liquidity"))
        est_dex_rate = max(0.0, min(float(self._dex_fee(position.get("dex"))), 0.99))
        net_price_factor = max((1.0 - est_slippage) * (1.0 - est_dex_rate), 1e-9)
        price = (remaining_cost + est_exit_fee) / (qty * net_price_factor)
        return price, est_slippage, est_dex_rate

    # ---- exit rules per entry type
    def exit_rules(self, p_or_type):
        et = p_or_type if isinstance(p_or_type, str) else p_or_type.get("entry_type")
        r = {k: self.c[k] for k in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
                                    "trailing_activate_pct", "max_hold_minutes")}
        r["follow_wallets"] = False

        # Progressive TP is intentionally limited to volume-spike positions.
        # Copy Top Traders keeps its independent wallet-follow exit policy.
        ptp = self.c.get("partial_tp") or {}
        r["partial_tp_enabled"] = bool(ptp.get("enabled", False) and et != "copy_top_traders")
        r["tp1_pct"] = ptp.get("tp1_pct", 0.25)
        r["tp1_sell_fraction"] = ptp.get("tp1_sell_fraction", 0.50)
        r["move_sl_to_breakeven"] = ptp.get("move_sl_to_breakeven", True)
        r["tp2_pct"] = r["take_profit_pct"] if ptp.get("enabled", False) else ptp.get("tp2_pct", 0.60)

        if et == "volume_spike":
            ah_cfg = self.c.get("adaptive_holding") or {}
            if self._adaptive_holding_enabled():
                r["max_hold_minutes"] = ah_cfg.get("initial_minutes", 15)
                r["adaptive_holding_enabled"] = True
                r["adaptive_max_minutes"] = ah_cfg.get("max_minutes", 30)

        if et == "copy_top_traders":
            ce = self.c.get("copy_exits") or {}
            for k in r:
                if k in ce:
                    r[k] = ce[k]
            r["follow_wallets"] = bool(ce.get("follow_wallets", True))
            r["sold_majority_pct"] = ce.get("sold_majority_pct", 0.5)
            r["reduced_pct"] = ce.get("reduced_pct", 0.5)
            r["wallet_check_seconds"] = ce.get("wallet_check_seconds", 60)
            r["min_tracked_wallets"] = ce.get("min_tracked_wallets", 2)
            # Copy trade: uscita ordinaria guidata dalla maggioranza dei wallet.
            # Restano attivi solo lo stop-loss e le protezioni di sicurezza.
            r["take_profit_pct"] = 0.0
            r["trailing_stop_pct"] = 0.0
            r["trailing_activate_pct"] = 0.0
        return r

    def reentry_block(self, mint, entry_type):
        if entry_type != "volume_spike":
            return 0
        cd = (self.c.get("reentry_cooldown_minutes") or 0) * 60
        last = (self.meta.get("last_exit") or {}).get(mint)
        return max(0, cd - (now() - last)) if (cd and last) else 0

    def token_decimals(self, mint):
        if mint in self.decimals_cache:
            return self.decimals_cache[mint]
        dec = ((self.holder_cache.get(mint) or {}).get("info") or {}).get("decimals")
        if dec is None:
            dec = self.rpc.decimals(mint)
        if dec is not None:
            self.decimals_cache[mint] = int(dec)
            if len(self.decimals_cache) > 3000:
                self.decimals_cache = dict(list(self.decimals_cache.items())[-1500:])
        return dec

    def _jc(self):
        return self.c.get("jupiter") or {}

    def jup_buy(self, mint, mid, usd_in):
        self._px_reason = "jupiter_disabled" if not self.jup else "bad_mid" if mid <= 0 else ""
        if not self.jup or mid <= 0:
            return None
        dec = self.token_decimals(mint)
        if dec is None:
            self._px_reason = "no_decimals"
            return None
        q = self.jup.quote(USDC, mint, round(usd_in * 10 ** USDC_DEC))
        if not q or q["out"] <= 0:
            self._px_reason = getattr(self.jup, "last_reason", "") or "empty_quote"
            return None
        mq = self._jc().get("max_quote_age_s", 15)
        if mq and (q.get("age_s") or 0) > mq:
            self._px_reason = "stale_quote"
            return None
        qty = q["out"] / 10 ** dec * (1 - self._jc().get("exec_extra_pct", 0.0))
        fill = usd_in / qty
        slip = fill / mid - 1
        if not -self._jc().get("max_negative_slip_pct", 0.10) <= slip <= 0.95:
            log.warning("Jupiter buy quote for %s implausible vs mid (slip %.1f%%): using the model", mint[:8], 100 * slip)
            self._px_reason = "implausible_vs_mid"
            self._implausible = (qty, fill, slip, q)
            return None
        return qty, fill, slip, q

    def jup_sell(self, mint, mid, qty, deadline=None):
        self._px_reason = "jupiter_disabled" if not self.jup else "bad_mid" if (mid <= 0 or qty <= 0) else ""
        if not self.jup or mid <= 0 or qty <= 0:
            return None
        dec = self.token_decimals(mint)
        if dec is None:
            self._px_reason = "no_decimals"
            return None
        # Convert the UI float to the nearest SPL base unit; int() truncation can
        # silently drop one unit due to binary floating-point representation.
        amount_raw = int(round(float(qty) * (10 ** dec)))
        q = self.jup.quote(mint, USDC, amount_raw, deadline=deadline)
        if not q or q["out"] <= 0:
            self._px_reason = getattr(self.jup, "last_reason", "") or "empty_quote"
            return None
        mq = self._jc().get("max_quote_age_s", 15)
        if mq and (q.get("age_s") or 0) > mq:
            self._px_reason = "stale_quote"
            return None
        usd_out = q["out"] / 10 ** USDC_DEC * (1 - self._jc().get("exec_extra_pct", 0.0))
        fill = usd_out / qty
        slip = 1 - fill / mid
        if not -self._jc().get("max_negative_slip_pct", 0.10) <= slip <= 0.99:
            log.warning("Jupiter sell quote for %s implausible vs mid (slip %.1f%%): using the model", mint[:8], 100 * slip)
            self._px_reason = "implausible_vs_mid"
            self._implausible = (usd_out, fill, slip, q)
            return None
        return usd_out, fill, slip, q

    def _pi(self):
        c = self.c.get("price_integrity") or {}
        return c if c.get("enabled", True) else {"reject_entry_on_quote_mismatch": False,
                                                "use_jupiter_exit_when_mid_implausible": False,
                                                "max_tick_jump_x": 0}

    def _count_quality(self, key):
        q = self.meta.setdefault("quality", {})
        q[key] = q.get(key, 0) + 1

    def _confirm_tick(self, m, p, price):
        """Return (confirmed, implied_price, quote_available); cap the quote latency."""
        self._implausible = None
        dec = self.decimals_cache.get(m)
        if dec is None:
            dec = ((self.holder_cache.get(m) or {}).get("info") or {}).get("decimals")
            if dec is None:
                return False, None, False
            try:
                dec = int(dec)
            except (TypeError, ValueError, OverflowError):
                return False, None, False
            self.decimals_cache[m] = dec
        jq = self.jup_sell(m, price, p["qty"], deadline=1.5)
        got = jq or self._implausible
        if not got:
            return False, None, False
        implied = got[1]
        tol = float(self._pi().get("confirm_tolerance_x", 3))
        return implied * tol >= price and implied <= price * tol, implied, True

    def _count_src(self, src, reason=None):
        d = self.meta.setdefault("slip_src", {"jupiter": 0, "stimato": 0})
        d[src] = d.get(src, 0) + 1
        if src != "jupiter":
            q = self.meta.setdefault("quality", {}).setdefault("quote_unavailable", {})
            r = reason or "unknown"
            q[r] = q.get(r, 0) + 1

    def _guards_entry(self, snap, size, jq):
        g = profiles.guards(self.profile, self.c.get("trade_guards") or {})
        if g.get("require_jupiter_route") and not jq:
            return "guard_no_jupiter_route"
        mx = g.get("max_size_to_liquidity")
        if mx and snap.get("liquidity") and size / snap["liquidity"] > mx:
            return "guard_liquidity_too_low"
        mi = g.get("max_price_impact_pct")
        if mi and jq and jq[3].get("impact_pct", 0) > mi:
            return "guard_price_impact"
        return ""

    # ---- copy exits
    def init_tracking(self, p, wallets, hinfo):
        ce = self.c.get("copy_exits") or {}
        amounts = (hinfo or {}).get("holder_amounts") or {}
        tr = {"wallets": {}, "last_check": now(), "sold": 0, "n": 0, "src": "rpc"}
        for w in list(wallets)[: ce.get("max_tracked_wallets", 15)]:
            bal, src = self.rpc.balance(w, p["mint"]), "rpc"
            if bal is None and amounts.get(w):
                bal, src = amounts[w], "rugcheck"
            self.beat()
            if bal and bal > 0:
                tr["wallets"][w] = {"start": bal, "now": bal, "sold": False, "src": src}
        tr["n"] = len(tr["wallets"])
        p["track"] = tr
        log.info("Copy %s: following %d/%d top-trader wallets (on-chain balances)", p["name"], tr["n"], len(wallets))

    def check_wallets(self, p, rules):
        tr = p.get("track") or {}
        if not tr.get("n") or tr["n"] < rules["min_tracked_wallets"]:
            return False
        if now() - tr.get("last_check", 0) >= rules["wallet_check_seconds"]:
            tr["last_check"] = now()
            rc = None
            unsold = [w for w, ws in tr["wallets"].items() if not ws["sold"]]
            balances = self.rpc.balances_batch(unsold, p["mint"])
            for w, ws in tr["wallets"].items():
                if ws["sold"]:
                    continue
                bal = balances.get(w)
                if bal is None:
                    if rc is None:
                        rc = self.rc.holders(p["mint"]) or {}
                    am = rc.get("holder_amounts") or {}
                    if w in am:
                        bal = am[w]
                    elif am and ws["src"] == "rugcheck":
                        bal = 0.0
                if bal is None:
                    continue
                ws["now"] = bal
                if bal <= ws["start"] * (1 - rules["reduced_pct"]):
                    ws["sold"], ws["sold_ts"] = True, now()
            tr["sold"] = sum(1 for ws in tr["wallets"].values() if ws["sold"])
            self.agent("copy", True, f"{tr['sold']}/{tr['n']} top trader usciti · ${p['name']}")
        return tr["sold"] > tr["n"] * rules["sold_majority_pct"]

    def buy(self, snap, entry_type, info, track=None, hinfo=None, requested_size_usd=None):
        if not self.can_enter():
            return
        lim = self.eff("max_open_positions")
        if (lim and len(self.positions) >= lim) or snap["mint"] in self.positions:
            return
        if self.reentry_block(snap["mint"], entry_type):
            self._reject(snap["mint"], "reentry_cooldown"); return
        t_sig = snap.get("_signal_ts") or now()
        open_cost = sum(q["cost_usd"] for q in self.positions.values())
        rok, rwhy = self.risk.check_entry(snap["mint"], self.positions, self.equity_value(), self.cash, open_cost)
        if not rok:
            self._reject(snap["mint"], rwhy); return
        if self.risk.mode == "survival":
            risk_approved_size = self.next_size()
        else:
            risk_approved_size, _note = self.risk.size(
                self.equity_value(), self.cash, open_cost, self.eff("position_size_pct"),
                self.min_trade_usd()
            )
        size = risk_approved_size
        if requested_size_usd is not None:
            # Wallet Intelligence sizing is denominated in USD. It may reduce
            # the normal risk-approved size, never increase or bypass it.
            try:
                requested_size_usd = float(requested_size_usd)
            except (TypeError, ValueError):
                requested_size_usd = 0.0
            if not math.isfinite(requested_size_usd) or requested_size_usd <= 0:
                self._reject(snap["mint"], "invalid_wallet_recommended_size")
                return
            size = min(risk_approved_size, requested_size_usd)
        if size < self.min_trade_usd() or snap["price"] <= 0:
            return
        if snap.get("src") != "dexscreener":
            ds = self.ds.tokens([snap["mint"]]).get(snap["mint"])
            if not ds or ds["price"] <= 0:
                self._reject(snap["mint"], "no_dexscreener_price_yet"); return
            snap = {**snap, "price": ds["price"], "liquidity": ds["liquidity"] or snap["liquidity"], "src": "dexscreener"}
        net_fee = self._net_fee_usd()
        self.risk.begin_buy(snap["mint"])
        try:
            jq = self.jup_buy(snap["mint"], snap["price"], size - net_fee)
        finally:
            self.risk.end_buy(snap["mint"], False)
        px_reason = "" if jq else (self._px_reason or "unknown")
        if not jq and px_reason == "implausible_vs_mid" and self._pi().get("reject_entry_on_quote_mismatch", True):
            log.warning("ENTRY REJECTED %s %s: Jupiter quote vs mid mismatch (price not verifiable)",
                        snap.get("name"), snap["mint"][:8])
            self._count_quality("entry_rejected_price_mismatch")
            self._reject(snap["mint"], "price_unverified_quote_mismatch"); return
        gr = self._guards_entry(snap, size, jq)
        if gr:
            self._reject(snap["mint"], gr); return
        if jq:
            qty, fill, slip, quote = jq
            dex_fee, src = 0.0, "jupiter"
        else:
            slip = self._slip(size, snap["liquidity"])
            fill = snap["price"] * (1 + slip)
            dex_fee = size * self._dex_fee(snap["dex"])
            qty = (size - dex_fee - net_fee) / fill
            quote, src = None, "stimato"
        hard_cap = self.c.get("preventive_max_entry_slippage_pct")
        if not finite(hard_cap) or float(hard_cap) < 0:
            log.error("ENTRY REJECTED: invalid preventive_max_entry_slippage_pct=%r", hard_cap)
            self._count_quality("entry_rejected_invalid_slippage_config")
            self._reject(snap["mint"], "invalid_preventive_slippage_config")
            return

        brain_lim = self.eff("max_entry_slippage_pct")
        effective_limit = min(
            float(hard_cap),
            float(brain_lim) if finite(brain_lim) and float(brain_lim) >= 0 else float(hard_cap)
        )

        if 100 * slip > effective_limit:
            log.info(
                "ENTRY REJECTED %s %s: buy slippage %.2f%% (%s) above effective limit %.2f%% "
                "(hard cap %.2f%%, brain limit %s)",
                snap.get("name"), snap["mint"][:8], 100 * slip, src,
                effective_limit, float(hard_cap),
                f"{float(brain_lim):.2f}%" if finite(brain_lim) else "unset"
            )
            self._count_quality("entry_rejected_slippage")
            self._reject(snap["mint"], "max_entry_slippage")
            return
        self._count_src(src, px_reason)
        self.risk.end_buy(snap["mint"], True)
        with self.lock:
            size = round(size, 4)
            self.cash = round(self.cash - size, 4)
            p = {"mint": snap["mint"], "pool": snap["pool"], "name": snap["name"], "dex": snap["dex"],
                 "entry_type": entry_type, "entry_ts": now(), "entry_mid": snap["price"], "entry_fill": fill,
                 "qty": qty, "token_decimals": self.decimals_cache.get(snap["mint"]),
                 "cost_usd": size, "liquidity": snap["liquidity"], "peak": snap["price"],
                 "last": snap["price"], "last_price_ts": now(), "info": info,
                 "costs": {"buy_slip_pct": round(100 * slip, 2), "buy_dex_fee": round(dex_fee, 4),
                           "buy_net_fee": round(net_fee, 4), "buy_slip_src": src,
                           "buy_route": (quote or {}).get("route")}, "failed_exits": 0}
            p["costs"].update({"buy_slip_kind": "stima_da_quote_jupiter" if quote else "stima_modello_liquidita",
                               "buy_fallback_reason": px_reason or None,
                               "buy_quote_age_s": (quote or {}).get("age_s"),
                               "buy_quote_endpoint": (quote or {}).get("endpoint"),
                               "buy_price_impact_pct": (quote or {}).get("impact_pct")})
            lat = round(now() - t_sig, 3)
            p["exec"] = {"signal_ts": t_sig, "signal_latency_s": lat,
                         "size_to_liquidity": round(size / snap["liquidity"], 5) if snap.get("liquidity") else None,
                         "market_mid": snap["price"], "sim_exec_price": fill}
            ql = self.meta.setdefault("quality", {}).setdefault("signal_latency_s", [])
            ql.append(lat)
            del ql[:-200]
            p["trade_id"] = f"{int(p['entry_ts'] * 1000)}_{snap['mint'][:8]}"
            self.positions[snap["mint"]] = p
            p["mfe_pct"] = 0.0
            p["mae_pct"] = 0.0
            p["mfe_ts"] = None
            p["mae_ts"] = None
            p["peak_price"] = snap["price"]
            p["entry_liquidity"] = snap["liquidity"]
            p["entry_vol_5m"] = snap.get("vol_m5")
            p["entry_vol_1h"] = snap.get("vol_h1")
            p["entry_baseline_vol"] = snap.get("vol_h1")
            p["entry_age_sec"] = (now() - snap["created_ts"]) if snap.get("created_ts") else snap.get("age_min", 0) * 60
            p["entry_fdv"] = snap.get("fdv")
            p["entry_price_chg_1m"] = snap.get("chg_m1", snap.get("chg_h1"))
            p["entry_price_chg_5m"] = snap.get("chg_m5", snap.get("chg_h1"))
            p["entry_price_chg_15m"] = snap.get("chg_m15", snap.get("chg_h1"))
            p["entry_price_chg_1h"] = snap.get("chg_h1")
            p["entry_buys_m5"] = snap.get("buys_m5")
            p["entry_sells_m5"] = snap.get("sells_m5")
            p["entry_txns_m5"] = snap.get("buys_m5", 0) + snap.get("sells_m5", 0)
            p["equity_before"] = round(self.equity_value() + size, 4)
            p["cash_before"] = round(self.cash + size, 4)

            ah_cfg = self.c.get("adaptive_holding") or {}
            if entry_type == "volume_spike" and self._adaptive_holding_enabled() and self._verify_paper_mode():
                initial_min = ah_cfg.get("initial_minutes", 15)
                max_min = ah_cfg.get("max_minutes", 30)
                entry_ts = now()
                p["adaptive_holding_enabled"] = True
                p["adaptive_holding_stage"] = 0
                p["adaptive_holding_extensions"] = 0
                p["adaptive_holding_started_at"] = entry_ts
                p["adaptive_holding_next_check"] = entry_ts + initial_min * 60
                p["adaptive_holding_last_decision"] = "INIT"
                p["adaptive_holding_last_reason"] = "position_opened"
                p["adaptive_holding_max_deadline"] = entry_ts + max_min * 60
                p["adaptive_holding_baseline_quote"] = None
                p["adaptive_holding_initial_threshold_min"] = initial_min
            else:
                p["adaptive_holding_enabled"] = False

            # Progressive Take Profit state fields
            p["tp1_hit"] = False
            p["tp1_qty_sold"] = 0.0
            p["tp1_price"] = 0.0
            p["tp1_realized_pnl"] = 0.0
            p["tp1_fees"] = 0.0
            p["tp1_sell_qty_requested"] = 0.0
            p["breakeven_activated"] = False
            p["breakeven_price"] = 0.0
            p["original_qty"] = qty
            p["original_cost_usd"] = size
            p["remaining_qty"] = qty
            p["tp2_done"] = False

            why = (f"vol ×{info.get('vol_ratio', 0):.0f}" if entry_type == "volume_spike"
                   else f"{info.get('top_traders_in')} top trader")
            self.event("BUY", snap["name"], f"{why} · €{size / self.meta.get('fx_start', 1):.2f}")
            self.mark_equity(force=True)
            log.info("PAPER BUY [%s] %s %s size $%.2f mid %.10g fill %.10g (slip %.1f%% %s%s, dex fee $%.3f, net fee $%.3f) | %s",
                     entry_type, snap["name"], snap["mint"][:8], size, snap["price"], fill, slip * 100, src,
                     f" via {quote['route']}" if quote else "", dex_fee, net_fee, info)
            if entry_type == "copy_top_traders" and track and self.exit_rules(p)["follow_wallets"]:
                try:
                    self.init_tracking(p, track, hinfo)
                except Exception as e:
                    log.warning("wallet tracking init failed for %s: %s", p["name"], e)
            self.tg.send(tg.msg_buy(p, self.cash, len(self.positions), self.exit_rules(p)))
            self.save_state()

    def _get_exit_lock(self):
        lk = getattr(self, "_exit_lock", None)
        if lk is None:
            lk = threading.Lock()
            self._exit_lock = lk
        return lk

    @staticmethod
    def _is_valid_jupiter_route(route):
        if isinstance(route, str):
            return len(route.strip()) > 0
        if isinstance(route, (list, tuple, dict)):
            return len(route) > 0
        return False

    def evaluate_exit_executability(self, mint, price, qty, position, force_emergency=False):
        """Mandatory pre-sell check (FASE 3: Exit Slippage Intelligence)."""
        es_cfg = self.c.get("exit_slippage") or {}
        max_slip_pct = float(es_cfg.get("max_exit_slippage_pct", 8.0))
        crit_slip_pct = float(es_cfg.get("critical_exit_slippage_pct", 25.0))
        max_impact_pct = float(es_cfg.get("max_price_impact_pct", 10.0))
        max_age_s = float(es_cfg.get("max_quote_age_s", self._jc().get("max_quote_age_s", 15)))
        max_liq_ratio = float(es_cfg.get("max_size_to_liquidity_ratio", 0.15))

        liq_usd = float(position.get("liquidity") or 0.0)
        theoretical_usd = float(qty) * float(price)

        res = {
            "executable": False,
            "action": "BLOCKED",
            "sell_qty": 0.0,
            "quote": None,
            "slippage_pct": None,
            "price_impact_pct": None,
            "quote_age_s": None,
            "route_valid": False,
            "theoretical_usd": round(theoretical_usd, 4),
            "estimated_exit_usd": 0.0,
            "value_gap_pct": None,
            "liquidity_usd": liq_usd,
            "size_to_liq_ratio": round(theoretical_usd / liq_usd, 4) if liq_usd > 0 else None,
            "reason": "",
            "is_api_error": False,
            "is_liquidity_collapse": False,
        }

        if qty <= 0 or price <= 0:
            res["reason"] = "invalid_qty_or_price"
            return res

        min_liq = float((self.c.get("adaptive_holding") or {}).get("min_liquidity_usd", 1500))
        if liq_usd > 0 and liq_usd < min_liq * 0.35:
            res["is_liquidity_collapse"] = True

        if self.jup and position.get("_exit_retry_count", 0) > 0:
            dec = self.token_decimals(mint)
            if dec is not None:
                raw_amt = int(round(float(qty) * (10 ** dec)))
                self.jup.cache.pop((mint, USDC, raw_amt), None)

        self._implausible = None
        jq = self.jup_sell(mint, price, qty)
        px_reason = "" if jq else (getattr(self, "_px_reason", "") or "unknown")

        if not jq and px_reason == "implausible_vs_mid" and self._implausible:
            usd_out_imp, fill_imp, slip_imp, q_imp = self._implausible
            if slip_imp > 0 and usd_out_imp > 0:
                jq = (usd_out_imp, fill_imp, slip_imp, q_imp)
                px_reason = ""

        if not jq:
            if px_reason in ("timeout", "deadline", "rate_limit", "cooldown", "circuit", "conn_error", "http_5xx", "deferred"):
                res["is_api_error"] = True
                res["action"] = "WAIT_REQUOTE"
                res["reason"] = f"api_temporary_error:{px_reason}"
                return res
            if px_reason == "stale_quote":
                res["action"] = "WAIT_REQUOTE"
                res["reason"] = "stale_quote"
                return res
            if px_reason in ("no_route", "empty_quote"):
                res["reason"] = f"invalid_or_missing_route:{px_reason}"
                if liq_usd < min_liq:
                    res["is_liquidity_collapse"] = True
                    res["action"] = "NON_LIQUIDATABLE"
                else:
                    res["action"] = "WAIT_REQUOTE"
                return res
            res["reason"] = f"quote_unavailable:{px_reason}"
            res["action"] = "WAIT_REQUOTE"
            return res

        usd_out, fill, slip, quote = jq
        slip_pct = round(slip * 100.0, 2)
        impact_pct = round(float(quote.get("impact_pct") or 0.0), 2)
        age_s = float(quote.get("age_s") or 0.0)
        route = quote.get("route")
        # Punto 1: verifica rigorosa della presenza di una route valida, MAI aggirabile da usd_out > 0
        route_ok = self._is_valid_jupiter_route(route)
        gap_pct = round((1.0 - (usd_out / theoretical_usd)) * 100.0, 2) if theoretical_usd > 0 else 0.0

        res.update({
            "quote": jq,
            "slippage_pct": slip_pct,
            "price_impact_pct": impact_pct,
            "quote_age_s": age_s,
            "route_valid": route_ok,
            "estimated_exit_usd": round(usd_out, 4),
            "value_gap_pct": gap_pct,
        })

        if age_s > max_age_s:
            res["action"] = "WAIT_REQUOTE"
            res["reason"] = f"quote_too_old:{age_s:.1f}s>{max_age_s}s"
            return res
        if not route_ok:
            res["action"] = "WAIT_REQUOTE"
            res["reason"] = "empty_or_invalid_swap_route"
            return res

        size_ratio_ok = (liq_usd <= 0) or ((theoretical_usd / liq_usd) <= max_liq_ratio)
        impact_ok = impact_pct <= max_impact_pct
        effective_slip_cap = crit_slip_pct if force_emergency else max_slip_pct

        if slip_pct <= effective_slip_cap and (impact_ok or force_emergency) and (size_ratio_ok or force_emergency):
            res["executable"] = True
            res["action"] = "EXECUTE_FULL"
            res["sell_qty"] = float(qty)
            res["reason"] = "ok"
            return res

        if es_cfg.get("partial_exit_enabled", True) and not position.get("_partial_exit_in_progress"):
            min_partial_usd = float(es_cfg.get("min_partial_exit_usd", 1.0))
            for frac in (es_cfg.get("partial_exit_fractions") or [0.50, 0.25]):
                part_qty = self._normalize_sell_quantity(mint, float(qty) * float(frac), float(qty))
                if part_qty <= 0 or part_qty >= float(qty):
                    continue
                if part_qty * price < min_partial_usd:
                    continue
                p_jq = self.jup_sell(mint, price, part_qty)
                if p_jq:
                    p_out, p_fill, p_slip, p_quote = p_jq
                    if not self._is_valid_jupiter_route(p_quote.get("route")):
                        continue
                    if float(p_quote.get("age_s") or 0.0) > max_age_s:
                        continue
                    p_slip_pct = round(p_slip * 100.0, 2)
                    p_impact_pct = round(float(p_quote.get("impact_pct") or 0.0), 2)
                    if p_slip_pct <= effective_slip_cap and p_impact_pct <= max_impact_pct:
                        res["executable"] = True
                        res["action"] = "EXECUTE_PARTIAL"
                        res["sell_qty"] = part_qty
                        res["quote"] = p_jq
                        res["slippage_pct"] = p_slip_pct
                        res["price_impact_pct"] = p_impact_pct
                        res["estimated_exit_usd"] = round(p_out, 4)
                        res["value_gap_pct"] = round((1.0 - (p_out / (part_qty * price))) * 100.0, 2)
                        res["reason"] = f"partial_reduces_slippage:{slip_pct:.1f}%->{p_slip_pct:.1f}%"
                        return res

        if slip_pct >= 85.0 or (liq_usd > 0 and liq_usd < 500):
            res["is_liquidity_collapse"] = True
            res["action"] = "NON_LIQUIDATABLE"
            res["reason"] = f"critical_illiquidity_slip_{slip_pct:.1f}%"
        else:
            res["action"] = "WAIT_REQUOTE"
            res["reason"] = f"high_exit_slippage:{slip_pct:.1f}%>{effective_slip_cap:.1f}%"
        return res

    def try_sell(self, mint, price, reason, tick_drop, sell_qty=None, force_emergency=False):
        """Sell with atomic anti-double-sell lock, timed retry backoff, and Exit Slippage Intelligence."""
        t_call = now()
        # Punto 2: blocco atomico attivato PRIMA della verifica Jupiter e garantito da try/finally
        with self._get_exit_lock():
            if mint not in self.positions:
                return
            p = self.positions[mint]
            if p.get("exit_in_progress") and t_call - float(p.get("exit_started_ts") or 0) < 15.0:
                log.debug("try_sell(%s) skipped: atomic exit lock active", mint[:8])
                return

            # Punto 6: rispetta retry_backoff_seconds senza bloccare il ciclo principale
            next_retry_ts = float(p.get("_exit_next_retry_ts") or 0.0)
            is_emergency = force_emergency or reason in ("stop_loss", "breakeven_stop", "trailing_stop") or "rug" in reason or "critical" in reason
            if next_retry_ts > t_call and not is_emergency:
                log.debug("try_sell(%s) waiting retry backoff (%.1fs left)", mint[:8], next_retry_ts - t_call)
                return

            p["exit_in_progress"] = True
            p["exit_started_ts"] = t_call

        try:
            k = self.c["costs"]
            if k["simulate_failed_exits"] and tick_drop >= k["sharp_dump_pct"] and random.random() < k["failed_exit_prob"]:
                fee = self._net_fee_usd()
                p["failed_exits"] += 1
                self.stats["failed_exits"] = self.stats.get("failed_exits", 0) + 1
                p["fees_wasted"] = p.get("fees_wasted", 0) + fee
                log.info("PAPER SELL FAILED (sim) %s on sharp dump %.0f%% - retry next tick", p["name"], 100 * tick_drop)
                self.event("FAIL", p["name"], f"vendita fallita · crollo -{100 * tick_drop:.0f}%")
                self.tg.send(tg.msg_failed_sell(p, tick_drop, fee))
                return

            target_qty = sell_qty if sell_qty is not None else p["qty"]
            check = self.evaluate_exit_executability(mint, price, target_qty, p, force_emergency=is_emergency)

            es_cfg = self.c.get("exit_slippage") or {}
            max_retries = int(es_cfg.get("max_exit_retries", 4))
            backoff_s = float(es_cfg.get("retry_backoff_seconds", 3.0))
            retries = int(p.get("_exit_retry_count", 0))

            p.setdefault("exit_attempts_log", []).append({
                "ts": round(now(), 2),
                "reason": reason,
                "action": check["action"],
                "slippage_pct": check["slippage_pct"],
                "impact_pct": check["price_impact_pct"],
                "theoretical_usd": check["theoretical_usd"],
                "estimated_exit_usd": check["estimated_exit_usd"],
                "detail": check["reason"],
            })
            del p["exit_attempts_log"][:-20]

            if check["action"] in ("EXECUTE_FULL", "EXECUTE_PARTIAL"):
                p["_exit_retry_count"] = 0
                p.pop("_exit_api_error_log_ts", None)
                p.pop("_exit_next_retry_ts", None)
                p.pop("_exit_blocked_since", None)
                if check["action"] == "EXECUTE_PARTIAL":
                    p["partial_exit_seq"] = int(p.get("partial_exit_seq", 0)) + 1
                actual_reason = reason if check["action"] == "EXECUTE_FULL" else f"{reason}_partial_slip_guard"
                qty_before = float(p.get("qty") or 0.0)
                self.sell(mint, price, actual_reason, sell_qty=check["sell_qty"],
                          prevalidated_quote=check["quote"], _from_try_sell=True)

                # Recovery is confirmed only if the position was actually reduced.
                if force_emergency and reason == "recovered_liquidity_exit":
                    remaining = self.positions.get(mint)
                    if remaining is not None:
                        qty_after = float(remaining.get("qty") or 0.0)
                        if qty_after < qty_before - max(1e-12, abs(qty_before) * 1e-9):
                            remaining.pop("non_liquidatable", None)
                            remaining.pop("liquidation_value", None)
                            remaining["recovery_classification"] = "RECOVERED_LIQUIDITY"
                return

            if check["action"] == "NON_LIQUIDATABLE":
                p["non_liquidatable"] = True
                p["liquidation_value"] = check["estimated_exit_usd"]
                p["recovery_classification"] = "NON_LIQUIDATABLE"
                p["_exit_next_retry_ts"] = now() + float(es_cfg.get("non_liquidatable_recheck_seconds", 30.0))
                log.warning("EXIT BLOCKED [%s] %s: liquidita insufficiente (%s). Nessun fill inventato.",
                            reason, p["name"], check["reason"])
                self.save_state()
                return

            if retries < max_retries:
                p["_exit_retry_count"] = retries + 1
                p["_exit_next_retry_ts"] = now() + backoff_s
                p.setdefault("_exit_blocked_since", now())
                if self.adaptive_poll.get("enabled", True):
                    self.adaptive_poll["accelerated"][mint] = {
                        "until": now() + max(15.0, backoff_s * 3.0),
                        "reason": f"exit_requote_{reason}",
                        "emergency": True,
                    }
                # Retry mantenuto; evita una riga di log per ogni tentativo.
                self.save_state()
                return

            p["_exit_retry_count"] = 0
            p["_exit_next_retry_ts"] = now() + backoff_s
            if check["is_api_error"]:
                last_log = float(p.get("_exit_api_error_log_ts") or 0)
                if now() - last_log >= 300:
                    log.warning("EXIT API TIMEOUT %s [%s]: %d tentativi falliti; posizione mantenuta aperta.",
                                p["name"], reason, max_retries)
                    p["_exit_api_error_log_ts"] = now()
                return

            if check["is_liquidity_collapse"]:
                p["non_liquidatable"] = True
                p["liquidation_value"] = check["estimated_exit_usd"]
                p["recovery_classification"] = "NON_LIQUIDATABLE"
            log.warning("EXIT ABORTED %s [%s]: nessuna quotazione eseguibile entro i limiti dopo %d tentativi (%s).",
                        p["name"], reason, max_retries, check["reason"])
            self.save_state()
        finally:
            if mint in self.positions:
                self.positions[mint].pop("exit_in_progress", None)
                self.positions[mint].pop("exit_started_ts", None)

    def _token_qty_epsilon(self, mint, reference_qty=0.0):
        """Half a token base unit when decimals are known; conservative float tolerance otherwise."""
        dec = self.decimals_cache.get(mint)
        if dec is None:
            dec = ((self.holder_cache.get(mint) or {}).get("info") or {}).get("decimals")
        try:
            dec = int(dec) if dec is not None else None
        except (TypeError, ValueError, OverflowError):
            dec = None
        if dec is not None and 0 <= dec <= 18:
            return 0.5 * (10.0 ** -dec)
        return max(1e-12, abs(float(reference_qty or 0.0)) * 1e-12)

    def _normalize_sell_quantity(self, mint, requested_qty, available_qty):
        """Clamp and quantize token quantity without rounding small memecoin balances to 6 decimals."""
        try:
            available = float(available_qty)
            requested = float(requested_qty)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if not math.isfinite(available) or not math.isfinite(requested) or available <= 0 or requested <= 0:
            return 0.0
        requested = min(requested, available)
        eps = self._token_qty_epsilon(mint, available)
        # A request leaving less than half the smallest token unit is a full close, not a dust position.
        if available - requested <= eps:
            return available
        dec = self.decimals_cache.get(mint)
        if dec is None:
            dec = ((self.holder_cache.get(mint) or {}).get("info") or {}).get("decimals")
        try:
            dec = int(dec) if dec is not None else None
        except (TypeError, ValueError, OverflowError):
            dec = None
        if dec is not None and 0 <= dec <= 18:
            scale = 10 ** dec
            requested = math.floor(requested * scale + 1e-9) / scale
        if requested <= 0:
            return 0.0
        if available - requested <= eps:
            return available
        return min(requested, available)

    def sell(self, mint, price, reason, sell_qty=None, prevalidated_quote=None, _from_try_sell=False):
        """
        Sell position (full or partial).
        sell_qty: quantity to sell. If None or >= current qty, sells entire position.
        """
        if not _from_try_sell:
            with self._get_exit_lock():
                if mint not in self.positions:
                    log.debug("sell(%s) ignored: no open position", mint[:8])
                    return
                p0 = self.positions[mint]
                if reason != "end_of_run" and p0.get("exit_in_progress") and now() - float(p0.get("exit_started_ts") or 0) < 15.0:
                    log.debug("sell(%s) ignored: exit already in progress", mint[:8])
                    return
                p0["exit_in_progress"] = True
                p0["exit_started_ts"] = now()
        else:
            if mint not in self.positions:
                return
            p0 = self.positions[mint]

        try:
            self._sell_inner(mint, p0, price, reason, sell_qty=sell_qty, prevalidated_quote=prevalidated_quote)
        finally:
            if not _from_try_sell and mint in self.positions:
                self.positions[mint].pop("exit_in_progress", None)
                self.positions[mint].pop("exit_started_ts", None)

    def _sell_inner(self, mint, p0, price, reason, sell_qty=None, prevalidated_quote=None):
        self._px_reason = "rug_assumed"
        self._implausible = None

        requested_qty = sell_qty if sell_qty is not None else p0["qty"]
        qty_to_sell = self._normalize_sell_quantity(mint, requested_qty, p0["qty"])
        if qty_to_sell <= 0:
            log.warning("sell(%s) ignored: requested token quantity is invalid or below one base unit", mint[:8])
            return

        jq = prevalidated_quote or (self.jup_sell(mint, price, qty_to_sell) if reason != "no_price_data_assume_rug" else None)
        px_reason = "" if jq else (self._px_reason or "unknown")
        if (not jq and px_reason == "implausible_vs_mid" and self._implausible and self._implausible[2] > 0
                and self._pi().get("use_jupiter_exit_when_mid_implausible", True)):
            log.warning("%s: mid not confirmed by Jupiter -> exit booked at the Jupiter quote", p0["name"])
            self._count_quality("exit_at_jupiter_mid_implausible")
            jq, px_reason = self._implausible, ""
        allow_fallback = bool((self.c.get("exit_slippage") or {}).get("allow_model_fallback_on_api_error", False))
        if not jq and reason != "no_price_data_assume_rug" and not allow_fallback:
            log.warning("sell(%s) aborted: no valid Jupiter quote (%s), no simulated fill invented", mint[:8], px_reason)
            return
        with self.lock:
            if mint not in self.positions:
                return
            p = self.positions[mint]

            # Treat sub-base-unit residuals as dust and close the logical position fully.
            qty_to_sell = min(qty_to_sell, float(p.get("qty", 0.0)))
            qty_epsilon = self._token_qty_epsilon(mint, p.get("qty", 0.0))
            is_partial = (float(p.get("qty", 0.0)) - qty_to_sell) > qty_epsilon
            if not is_partial:
                qty_to_sell = float(p["qty"])

            net_fee = self._net_fee_usd() + p.get("fees_wasted", 0)
            if jq:
                usd_out, fill, slip, quote = jq
                dex_fee, src = 0.0, "jupiter"
                proceeds = max(usd_out - net_fee, 0.0)
            else:
                gross = qty_to_sell * price
                slip = self._slip(gross, p["liquidity"])
                fill = price * (1 - slip)
                dex_fee = qty_to_sell * fill * self._dex_fee(p["dex"])
                proceeds = max(qty_to_sell * fill - dex_fee - net_fee, 0.0)
                quote, src = None, "stimato"
            self._count_src(src, px_reason)
            rules = self.exit_rules(p)
            tr = p.get("track") or {}

            # For partial sell: allocate cost basis proportionally
            proceeds = round(proceeds, 4)
            if is_partial:
                qty_ratio = qty_to_sell / p["qty"]
                cost_usd_partial = round(p["cost_usd"] * qty_ratio, 4)
                pnl = round(proceeds - cost_usd_partial, 4)
                # Update position with remaining quantity and cost
                p["qty"] = max(0.0, float(p["qty"]) - qty_to_sell)
                p["cost_usd"] = round(max(0.0, float(p["cost_usd"]) - cost_usd_partial), 4)
                p["remaining_qty"] = p["qty"]
                self.cash = round(self.cash + proceeds, 4)
                p["partial_realized_pnl"] = round(float(p.get("partial_realized_pnl", 0.0)) + pnl, 4)
                # Track and allocate fees proportionally without double-charging
                buy_dex_fee_alloc = round(p["costs"].get("buy_dex_fee", 0.0) * qty_ratio, 4)
                buy_net_fee_alloc = round(p["costs"].get("buy_net_fee", 0.0) * qty_ratio, 4)
                p["costs"]["buy_dex_fee"] = max(0.0, round(p["costs"].get("buy_dex_fee", 0.0) - buy_dex_fee_alloc, 4))
                p["costs"]["buy_net_fee"] = max(0.0, round(p["costs"].get("buy_net_fee", 0.0) - buy_net_fee_alloc, 4))
                # fees_wasted has now been included in this partial sell's
                # net_fee and cash accounting. Consume it so the next exit
                # cannot charge the same failed-exit fees a second time.
                p["fees_wasted"] = 0.0

                # Record partial sell details
                is_tp1 = reason == "take_profit_1" or reason.startswith("take_profit_1_")
                if is_tp1:
                    p["tp1_hit"] = True
                    p["tp1_qty_sold"] = qty_to_sell
                    p["tp1_price"] = fill
                    p["tp1_realized_pnl"] = pnl
                    p["tp1_fees"] = net_fee + dex_fee
                    p["tp1_sell_qty_requested"] = qty_to_sell

                    # Activate breakeven if configured
                    if rules.get("move_sl_to_breakeven"):
                        # Estimated net break-even for the remaining leg. This is a
                        # modelled trigger, not a guaranteed executable fill price.
                        breakeven_price, est_slippage, est_dex_rate = self._estimated_breakeven_price(p)
                        p["breakeven_estimated_slippage_pct"] = round(est_slippage * 100, 4)
                        p["breakeven_estimated_dex_fee_pct"] = round(est_dex_rate * 100, 4)
                        p["breakeven_activated"] = True
                        p["breakeven_price"] = breakeven_price

                elif reason == "take_profit_2":
                    p["tp2_done"] = True

                # Don't remove position, just update it
                held = now() - p["entry_ts"]
                late = max(0, held - rules["max_hold_minutes"] * 60) if reason.startswith("time_exit") else 0
                ar_info = (p.get("info") or {}).get("antirug") or {}

                # Create trade record for partial exit
                t = {"entry_time_utc": iso(p["entry_ts"]), "exit_time_utc": iso(), "entry_ts": p["entry_ts"],
                     "exit_ts": now(), "hold_sec": round(held), "late_sec": round(late),
                     "token": p["name"], "mint": mint, "pool": p["pool"], "dex": p["dex"], "entry_type": p["entry_type"],
                     "entry_mid": p["entry_mid"], "entry_fill": p["entry_fill"], "exit_mid": price, "exit_fill": fill,
                     "cost_usd": round(cost_usd_partial, 4), "proceeds_usd": round(proceeds, 4), "pnl_usd": round(pnl, 4),
                     "pnl_pct": round(100 * pnl / cost_usd_partial, 2) if cost_usd_partial > 0 else 0, "exit_reason": reason,
                     "price_move_pct": round(100 * (price / p["entry_mid"] - 1), 2),
                     "buy_slip_pct": p["costs"]["buy_slip_pct"], "sell_slip_pct": round(100 * slip, 2),
                     "buy_slip_src": p["costs"].get("buy_slip_src", "stimato"), "sell_slip_src": src,
                     "sell_route": (quote or {}).get("route"),
                     "wallets_tracked": tr.get("n", 0), "wallets_sold": tr.get("sold", 0),
                     "dex_fees_usd": round(buy_dex_fee_alloc + dex_fee, 4),
                     "network_fees_usd": round(buy_net_fee_alloc + net_fee, 4), "failed_exits": p["failed_exits"],
                     "cash_after": round(self.cash, 4), "filters": json.dumps(p["info"]),
                     "slippage_kind": "stima (quote Jupiter o modello), non realizzato",
                     "buy_fallback_reason": p["costs"].get("buy_fallback_reason"), "sell_fallback_reason": px_reason or None,
                     "sell_quote_age_s": (quote or {}).get("age_s"), "sell_price_impact_pct": (quote or {}).get("impact_pct"),
                     "signal_latency_s": (p.get("exec") or {}).get("signal_latency_s"),
                     "equity_before": p.get("equity_before"), "cash_before": p.get("cash_before"),
                     "equity_after": round(self.equity_value(), 4),
                     "entry_liquidity": p.get("entry_liquidity", p.get("liquidity")),
                     "entry_age_sec": p.get("entry_age_sec"), "entry_fdv": p.get("entry_fdv"),
                     "mfe_pct": p.get("mfe_pct"), "mae_pct": p.get("mae_pct"),
                     "mfe_ts": p.get("mfe_ts"), "mae_ts": p.get("mae_ts"),
                     "recovery_classification": p.get("recovery_classification"),
                     "antirug_mode": ar_info.get("mode"), "antirug_decision": ar_info.get("decision"),
                     "adaptive_holding_enabled": p.get("adaptive_holding_enabled", False),
                     "adaptive_holding_final_stage": p.get("adaptive_holding_stage", 0),
                     "adaptive_holding_extensions": p.get("adaptive_holding_extensions", 0),
                     "adaptive_holding_final_decision": p.get("adaptive_holding_last_decision"),
                     "adaptive_holding_max_deadline": p.get("adaptive_holding_max_deadline"),
                     "adaptive_holding_initial_threshold_min": p.get("adaptive_holding_initial_threshold_min"),
                     "adaptive_baseline_quote": p.get("adaptive_holding_baseline_quote"),
                     "exit_liquidity": p.get("liquidity"),
                      "trade_id": p.get("trade_id", f"{int(p['entry_ts'] * 1000)}_{mint[:8]}"),
                      "counts_as_trade": False,
                     "partial": True,
                     "partial_qty_sold": qty_to_sell,
                     "partial_qty_remaining": p["qty"],
                     "tp1_hit": p.get("tp1_hit", False),
                     "tp2_done": p.get("tp2_done", False)}

                if getattr(self, "provenance_on", False):
                    t["price_provenance"] = ("VERIFIED" if t["buy_slip_src"] == "jupiter" and src == "jupiter"
                                             and reason != "unconfirmed_price_exit" else "ESTIMATED")

                self.closed.append(t)
                self.meta.setdefault("last_exit", {})[mint] = now()
                if len(self.meta["last_exit"]) > 2000:
                    self.meta["last_exit"] = dict(sorted(self.meta["last_exit"].items(), key=lambda kv: kv[1])[-1000:])

                self.event("SELL", p["name"], reason, pnl=round(pnl, 4))
                self.mark_equity(force=True)
                self._write_trade(t)

                log.info("PAPER PARTIAL SELL [%s] %s pnl $%.2f (%.1f%%) reason=%s slip %.1f%% %s qty_sold=%.12g qty_rem=%.12g cash=$%.2f",
                         p["entry_type"], p["name"], pnl, t["pnl_pct"], reason, 100 * slip, src, qty_to_sell, p["qty"], self.cash)

                # Send partial sell notification
                if is_tp1:
                    self.tg.send(tg.msg_tp1(p, self.cash, rules, qty_to_sell, fill, pnl, cost_usd_partial))
                    if rules.get("move_sl_to_breakeven") and p.get("breakeven_activated"):
                        self.tg.send(tg.msg_breakeven(p, self.cash, rules, p["breakeven_price"]))
                elif reason == "take_profit_2":
                    self.tg.send(tg.msg_tp2(t, self.cash, self.c["starting_balance_usd"], self.closed, rules,
                                             equity=self.equity_value(), n_open=len(self.positions)))
                else:
                    self.tg.send(tg.msg_sell(t, self.cash, self.c["starting_balance_usd"], self.closed, rules,
                                             equity=self.equity_value(), n_open=len(self.positions)))

                # Partial fills update mark-to-market drawdown and daily-loss guards,
                # but must not increment/reset consecutive completed-trade losses.
                try:
                    self.risk.tick(self.equity_value())
                    for ev in self.risk.pop_events():
                        self.notify_risk(ev)
                except Exception as e:
                    log.exception("risk mark-to-market error after partial sell: %s", e)

                self.save_state()
                self.check_death()
                return

            # Full position sell (original logic)
            p = self.positions.pop(mint)
            proceeds = round(proceeds, 4)
            self.cash = round(self.cash + proceeds, 4)
            cost_usd_final = round(p["cost_usd"], 4)
            pnl = round(proceeds - cost_usd_final, 4)
            held = now() - p["entry_ts"]
            late = max(0, held - rules["max_hold_minutes"] * 60) if reason.startswith("time_exit") else 0
            if reason == "take_profit" and src == "jupiter" and pnl < 0:
                log.warning("%s: exit triggered as %s on an unconfirmed mid, booked P&L %.2f -> labelled "
                            "unconfirmed_price_exit", p["name"], reason, pnl)
                reason = "unconfirmed_price_exit"
            ar_info = (p.get("info") or {}).get("antirug") or {}
            partial_pnl = float(p.get("partial_realized_pnl", p.get("tp1_realized_pnl", 0.0)))
            total_cum_pnl = round(pnl + partial_pnl, 4)
            orig_cost = float(p.get("original_cost_usd", p["cost_usd"]))
            cum_pnl_pct = round(100 * total_cum_pnl / orig_cost, 2) if orig_cost > 0 else 0
            t = {"entry_time_utc": iso(p["entry_ts"]), "exit_time_utc": iso(), "entry_ts": p["entry_ts"],
                 "exit_ts": now(), "hold_sec": round(held), "late_sec": round(late),
                 "token": p["name"], "mint": mint, "pool": p["pool"], "dex": p["dex"], "entry_type": p["entry_type"],
                 "entry_mid": p["entry_mid"], "entry_fill": p["entry_fill"], "exit_mid": price, "exit_fill": fill,
                 "trade_id": p.get("trade_id", f"{int(p['entry_ts'] * 1000)}_{mint[:8]}"),
                 "counts_as_trade": True,
                 "cost_usd": cost_usd_final, "proceeds_usd": proceeds, "pnl_usd": pnl,
                 "pnl_pct": round(100 * pnl / cost_usd_final, 2) if cost_usd_final > 0 else 0,
                 "tp1_realized_pnl_usd": round(float(p.get("tp1_realized_pnl", 0.0)), 4),
                 "cumulative_pnl_usd": total_cum_pnl,
                 "cumulative_pnl_pct": cum_pnl_pct,
                 "exit_reason": reason,
                 "price_move_pct": round(100 * (price / p["entry_mid"] - 1), 2),
                 "buy_slip_pct": p["costs"]["buy_slip_pct"], "sell_slip_pct": round(100 * slip, 2),
                 "buy_slip_src": p["costs"].get("buy_slip_src", "stimato"), "sell_slip_src": src,
                 "sell_route": (quote or {}).get("route"),
                 "wallets_tracked": tr.get("n", 0), "wallets_sold": tr.get("sold", 0),
                 "dex_fees_usd": round(p["costs"].get("buy_dex_fee", 0.0) + dex_fee, 4),
                 "network_fees_usd": round(p["costs"].get("buy_net_fee", 0.0) + net_fee, 4), "failed_exits": p["failed_exits"],
                 "cash_after": round(self.cash, 4), "filters": json.dumps(p["info"]),
                 "slippage_kind": "stima (quote Jupiter o modello), non realizzato",
                 "buy_fallback_reason": p["costs"].get("buy_fallback_reason"), "sell_fallback_reason": px_reason or None,
                 "sell_quote_age_s": (quote or {}).get("age_s"), "sell_price_impact_pct": (quote or {}).get("impact_pct"),
                 "signal_latency_s": (p.get("exec") or {}).get("signal_latency_s"),
                 "equity_before": p.get("equity_before"), "cash_before": p.get("cash_before"),
                 "equity_after": round(self.equity_value(), 4),
                 "entry_liquidity": p.get("entry_liquidity", p.get("liquidity")),
                 "entry_age_sec": p.get("entry_age_sec"), "entry_fdv": p.get("entry_fdv"),
                 "mfe_pct": p.get("mfe_pct"), "mae_pct": p.get("mae_pct"),
                 "mfe_ts": p.get("mfe_ts"), "mae_ts": p.get("mae_ts"),
                 "recovery_classification": p.get("recovery_classification"),
                 "antirug_mode": ar_info.get("mode"), "antirug_decision": ar_info.get("decision"),
                 "adaptive_holding_enabled": p.get("adaptive_holding_enabled", False),
                 "adaptive_holding_final_stage": p.get("adaptive_holding_stage", 0),
                 "adaptive_holding_extensions": p.get("adaptive_holding_extensions", 0),
                 "adaptive_holding_final_decision": p.get("adaptive_holding_last_decision"),
                 "adaptive_holding_max_deadline": p.get("adaptive_holding_max_deadline"),
                 "adaptive_holding_initial_threshold_min": p.get("adaptive_holding_initial_threshold_min"),
                 "adaptive_baseline_quote": p.get("adaptive_holding_baseline_quote"),
                 "exit_liquidity": p.get("liquidity")}
            if getattr(self, "provenance_on", False):
                t["price_provenance"] = ("VERIFIED" if t["buy_slip_src"] == "jupiter" and src == "jupiter"
                                         and reason != "unconfirmed_price_exit" else "ESTIMATED")
            self.closed.append(t)
            self.meta.setdefault("last_exit", {})[mint] = now()
            if len(self.meta["last_exit"]) > 2000:
                self.meta["last_exit"] = dict(sorted(self.meta["last_exit"].items(), key=lambda kv: kv[1])[-1000:])
            self.event("SELL", p["name"], reason, pnl=round(pnl, 4))
            self.mark_equity(force=True)
            self._write_trade(t)
            log.info("PAPER SELL [%s] %s pnl $%.2f (%.1f%%) reason=%s slip %.1f%% %s failed_exits=%d cash=$%.2f",
                     p["entry_type"], p["name"], pnl, t["pnl_pct"], reason, 100 * slip, src, p["failed_exits"], self.cash)
            if reason == "take_profit_2":
                self.tg.send(tg.msg_tp2(t, self.cash, self.c["starting_balance_usd"], self.closed, rules,
                                        equity=self.equity_value(), n_open=len(self.positions)))
            else:
                self.tg.send(tg.msg_sell(t, self.cash, self.c["starting_balance_usd"], self.closed, rules,
                                         equity=self.equity_value(), n_open=len(self.positions)))
            try:
                cumulative_pnl = float(t.get("cumulative_pnl_usd", pnl))
                self.risk.on_trade_closed(cumulative_pnl, self.equity_value())
                for ev in self.risk.pop_events():
                    self.notify_risk(ev)
            except Exception as e:
                log.exception("risk manager error (trading continues): %s", e)
            try:
                brain_trade = dict(t)
                cumulative_pnl = float(t.get("cumulative_pnl_usd", t["pnl_usd"]))
                original_cost = float(p.get("original_cost_usd", p.get("cost_usd", 0.0)))
                brain_trade["pnl_usd"] = cumulative_pnl
                brain_trade["pnl_pct"] = (100.0 * cumulative_pnl / original_cost) if original_cost > 0 else 0.0
                brain_trade["partial"] = False
                brain_trade["counts_as_trade"] = True
                brain_trade["execution_pnl_usd"] = t["pnl_usd"]
                self.brain.record(p, brain_trade)
                if self.brain.due():
                    a = self.brain.analyze()
                    self.event("BRAIN", "CERVELLO", f"{len(a['changes'])} modifiche")
                    self.tg.send(tg.msg_brain(a))
            except Exception as e:
                log.exception("brain error (trading continues): %s", e)
            self.save_state()
            self.check_death()
    def _write_trade(self, t):
        try:
            trade_id = t.get("trade_id", f"{int(t['entry_ts'])}_{t['mint'][:8]}")
            self._write_trades_csv(t, trade_id)
            self._write_snapshots_csv(t, trade_id)
            self._write_execution_events_csv(t, trade_id)
            self._write_strategy_signals_csv(t, trade_id)
            self._write_recovery_events_csv(t, trade_id)
        except Exception as e:
            self.ops["telemetry_write_errors"] = self.ops.get("telemetry_write_errors", 0) + 1
            log.error("cannot append secondary telemetry CSVs (state snapshot remains authoritative): %s", e)

    def _write_trades_csv(self, t, trade_id):
        t_with_id = {"trade_id": trade_id, **t}
        info = json.loads(t.get("filters", "{}")) if isinstance(t.get("filters"), str) else t.get("filters", {})
        spike_meta = info.get("spike_meta", {})
        hinfo = info.get("holder_info", {})

        def safe_get(d, key, *defaults):
            if d is None:
                return defaults[0] if defaults else None
            v = d.get(key)
            if v is not None:
                return v
            for default in defaults:
                val = default() if callable(default) else default
                if val is not None:
                    return val
            return None

        def safe_mul(v, mult, default=0):
            return (v * mult) if v is not None else default

        telemetry_fields = {
            "trade_id": trade_id,
            "token_name": t.get("token"),
            "token_symbol": t.get("token"),
            "mint_address": t.get("mint"),
            "pool_address": t.get("pool"),
            "strategy": t.get("entry_type"),
            "entry_signal": "spike_detected" if t.get("entry_type") == "volume_spike" else "copy_signal",
            "entry_timestamp_utc": t.get("entry_time_utc"),
            "exit_timestamp_utc": t.get("exit_time_utc"),
            "entry_reason": t.get("entry_type"),
            "exit_reason": t.get("exit_reason"),
            "software_version": "AEON-2.0",
            "configuration_version": self.meta.get("config_version", "1.0"),
            "token_age_seconds": safe_get(t, "entry_age_sec", (safe_get(t, "age_min", 0) or 0) * 60),
            "pair_age_seconds": safe_get(t, "entry_age_sec", (safe_get(t, "age_min", 0) or 0) * 60),
            "first_seen_timestamp": t.get("entry_time_utc"),
            "price_usd": t.get("entry_mid"),
            "market_cap_usd": safe_get(t, "entry_fdv", safe_get(info, "fdv")),
            "fdv_usd": safe_get(t, "entry_fdv", safe_get(info, "fdv")),
            "liquidity_usd": safe_get(t, "entry_liquidity", safe_get(spike_meta, "liquidity_usd", safe_get(info, "entry_liquidity"))),
            "volume_1m_usd": safe_get(info, "entry_vol_1m"),
            "volume_5m_usd": safe_get(info, "entry_vol_5m", safe_get(info, "vol_m5", safe_get(spike_meta, "vol_m5"))),
            "volume_15m_usd": safe_get(info, "entry_vol_15m"),
            "volume_1h_usd": safe_get(info, "entry_vol_1h", safe_get(info, "vol_h1", safe_get(spike_meta, "vol_h1"))),
            "volume_spike_multiplier": safe_get(info, "vol_ratio", safe_get(spike_meta, "vol_ratio")),
            "reference_volume": safe_get(info, "entry_baseline_vol", safe_get(spike_meta, "vol_h1")),
            "price_change_1m": safe_get(info, "entry_price_chg_1m"),
            "price_change_5m": safe_get(info, "entry_price_chg_5m"),
            "price_change_15m": safe_get(info, "entry_price_chg_15m"),
            "price_change_1h": safe_get(info, "entry_price_chg_1h", safe_get(info, "chg_h1", safe_get(spike_meta, "chg_h1"))),
            "buy_volume": safe_get(info, "entry_buys_m5", safe_get(info, "buys_m5", safe_get(spike_meta, "buys_m5"))),
            "sell_volume": safe_get(info, "entry_sells_m5", safe_get(info, "sells_m5", safe_get(spike_meta, "sells_m5"))),
            "buy_sell_ratio": round(safe_get(info, "buys_m5", safe_get(spike_meta, "buys_m5"), 0) / max(safe_get(info, "sells_m5", safe_get(spike_meta, "sells_m5"), 1), 1), 2),
            "transaction_count": safe_get(info, "entry_txns_m5"),
            "buy_count": safe_get(info, "entry_buys_m5", safe_get(info, "buys_m5", safe_get(spike_meta, "buys_m5"))),
            "sell_count": safe_get(info, "entry_sells_m5", safe_get(info, "sells_m5", safe_get(spike_meta, "sells_m5"))),
            "dev_wallet_identified": safe_get(hinfo, "dev_pct", safe_get(info, "dev_pct", 0)) > 0,
            "dev_holdings_pct": safe_get(hinfo, "dev_pct", safe_get(info, "dev_pct")),
            "top_10_holder_pct": safe_get(hinfo, "top10_pct", safe_get(info, "top10_pct")),
            "top_20_holder_pct": safe_get(hinfo, "top20_pct"),
            "insider_wallet_count": safe_get(hinfo, "insider_count"),
            "whale_wallet_count": safe_get(hinfo, "whale_count"),
            "whale_holdings_usd": safe_get(info, "whale_buy_usd"),
            "smart_money_score": safe_get(info, "smart_money_score"),
            "copy_signal_score": safe_get(info, "copy_signal_score"),
            "wallets_tracked": t.get("wallets_tracked"),
            "wallets_buying": safe_get(info, "wallets_buying"),
            "wallets_selling": safe_get(info, "wallets_selling"),
            "wallets_sold": t.get("wallets_sold"),
            "intended_order_size": t.get("cost_usd"),
            "actual_order_size": t.get("cost_usd"),
            "entry_reference_price": t.get("entry_mid"),
            "entry_quote_price": t.get("entry_fill"),
            "entry_execution_price": t.get("entry_fill"),
            "entry_price_impact_pct": safe_get(t, "buy_price_impact_pct"),
            "entry_expected_slippage_pct": t.get("buy_slip_pct"),
            "entry_realized_slippage_pct": t.get("buy_slip_pct"),
            "entry_route": t.get("buy_route"),
            "entry_quote_timestamp": safe_get(t, "buy_quote_age_s"),
            "entry_quote_age_ms": safe_mul(safe_get(t, "buy_quote_age_s"), 1000),
            "entry_execution_status": "filled",
            "entry_failure_reason": t.get("buy_fallback_reason"),
            "exit_reference_price": t.get("exit_mid"),
            "exit_quote_price": t.get("exit_fill"),
            "exit_execution_price": t.get("exit_fill"),
            "exit_price_impact_pct": safe_get(t, "sell_price_impact_pct"),
            "exit_expected_slippage_pct": t.get("sell_slip_pct"),
            "exit_realized_slippage_pct": t.get("sell_slip_pct"),
            "exit_route": t.get("sell_route"),
            "exit_quote_timestamp": safe_get(t, "sell_quote_age_s"),
            "exit_quote_age_ms": safe_mul(safe_get(t, "sell_quote_age_s"), 1000),
            "exit_execution_status": "filled",
            "exit_failure_reason": t.get("sell_fallback_reason"),
            "mfe_pct": safe_get(t, "mfe_pct"),
            "mae_pct": safe_get(t, "mae_pct"),
            "mfe_timestamp": safe_get(t, "mfe_ts"),
            "mae_timestamp": safe_get(t, "mae_ts"),
            "recovery_mode": safe_get(t, "recovery_mode"),
            "recovery_triggered": safe_get(t, "recovery_triggered", False),
            "recovery_trigger_reason": safe_get(t, "recovery_trigger_reason"),
            "recovery_threshold_pct": safe_get(t, "recovery_threshold_pct", 0.70),
            "drawdown_at_trigger": safe_get(t, "drawdown_at_trigger"),
            "recovery_action": safe_get(t, "recovery_action"),
            "recovery_result": safe_get(t, "recovery_result"),
            "antirug_mode": safe_get(t, "antirug_mode"),
            "antirug_decision": safe_get(t, "antirug_decision"),
            "antirug_evidence": safe_get(t, "antirug_evidence"),
            "liquidity_status": safe_get(t, "liquidity_status"),
            "data_quality_status": t.get("price_provenance", "ESTIMATED"),
            "adaptive_holding_enabled": safe_get(t, "adaptive_holding_enabled", False),
            "adaptive_holding_final_stage": safe_get(t, "adaptive_holding_final_stage", 0),
            "adaptive_holding_extensions": safe_get(t, "adaptive_holding_extensions", 0),
            "adaptive_holding_final_decision": safe_get(t, "adaptive_holding_final_decision"),
            "adaptive_holding_max_deadline_ts": safe_get(t, "adaptive_holding_max_deadline"),
            "adaptive_holding_initial_threshold_min": safe_get(t, "adaptive_holding_initial_threshold_min"),
            "adaptive_baseline_quote_proceeds_usd": safe_get(t, "adaptive_baseline_quote", lambda: (t.get("adaptive_baseline_quote") or {}).get("usd_out")),
            "adaptive_baseline_quote_slippage_pct": safe_get(t, "adaptive_baseline_quote", lambda: round((t.get("adaptive_baseline_quote") or {}).get("slip", 0) * 100, 2)),
            "adaptive_baseline_quote_fill": safe_get(t, "adaptive_baseline_quote", lambda: (t.get("adaptive_baseline_quote") or {}).get("fill")),
            "adaptive_baseline_quote_valid": safe_get(t, "adaptive_baseline_quote", lambda: bool(t.get("adaptive_baseline_quote"))),
            "adaptive_baseline_dex_fee_usd": safe_get(t, "adaptive_baseline_quote", lambda: (
                (bq := t.get("adaptive_baseline_quote")) and
                (usd_out := bq.get("usd_out", 0)) and
                (dex_name := t.get("dex", "default")) and
                usd_out * self._dex_fee(dex_name)
            ) if t.get("adaptive_baseline_quote") else None),
            "adaptive_baseline_net_fee_usd": safe_get(t, "adaptive_baseline_quote", lambda: self._net_fee_usd() if t.get("adaptive_baseline_quote") else None),
            "adaptive_baseline_net_proceeds_usd": safe_get(t, "adaptive_baseline_quote", lambda: (
                (bq := t.get("adaptive_baseline_quote")) and
                (usd_out := bq.get("usd_out", 0)) and
                (dex_name := t.get("dex", "default")) and
                (dex_fee := usd_out * self._dex_fee(dex_name)) and
                (net_fee := self._net_fee_usd()) and
                max(usd_out - dex_fee - net_fee, 0.0)
            ) if t.get("adaptive_baseline_quote") else None),
            "adaptive_baseline_pnl_usd": safe_get(t, "adaptive_baseline_quote", lambda: (
                (bq := t.get("adaptive_baseline_quote")) and
                (usd_out := bq.get("usd_out", 0)) and
                (dex_name := t.get("dex", "default")) and
                (dex_fee := usd_out * self._dex_fee(dex_name)) and
                (net_fee := self._net_fee_usd()) and
                (net_proceeds := max(usd_out - dex_fee - net_fee, 0.0)) and
                round(net_proceeds - t.get("cost_usd", 0), 4)
            ) if t.get("adaptive_baseline_quote") else None),
            "adaptive_baseline_pnl_pct": safe_get(t, "adaptive_baseline_quote", lambda: (
                (bq := t.get("adaptive_baseline_quote")) and
                (usd_out := bq.get("usd_out", 0)) and
                (dex_name := t.get("dex", "default")) and
                (dex_fee := usd_out * self._dex_fee(dex_name)) and
                (net_fee := self._net_fee_usd()) and
                (net_proceeds := max(usd_out - dex_fee - net_fee, 0.0)) and
                (cost := t.get("cost_usd", 0)) and
                round(100 * (net_proceeds - cost) / max(cost, 1), 2) if cost > 0 else None
            ) if t.get("adaptive_baseline_quote") else None),
            "adaptive_baseline_fee_known": safe_get(t, "adaptive_baseline_quote", lambda: True if t.get("adaptive_baseline_quote") else False),
            "entry_cost": t.get("cost_usd"),
            "exit_proceeds": t.get("proceeds_usd"),
            "trading_fees": t.get("dex_fees_usd"),
            "network_fees": t.get("network_fees_usd"),
            "gross_pnl": t.get("pnl_usd") + t.get("dex_fees_usd", 0) + t.get("network_fees_usd", 0),
            "net_pnl": t.get("pnl_usd"),
            "realized_pnl": t.get("pnl_usd"),
            "unrealized_pnl": 0,
            "equity_before": t.get("equity_before"),
            "equity_after": t.get("equity_after"),
            "cash_before": t.get("cash_before"),
            "cash_after": t.get("cash_after"),
            "position_value_before": t.get("cost_usd"),
            "position_value_after": 0,
        }

        full_row = {**telemetry_fields, **t_with_id}

        new = not os.path.exists(self.csv_path)
        if not new:
            with open(self.csv_path) as fh:
                head = fh.readline().strip()
            if head != ",".join(full_row.keys()):
                os.replace(self.csv_path, self.csv_path[:-4] + f".{int(now())}.csv")
                new = True
        with open(self.csv_path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(full_row.keys()))
            if new:
                w.writeheader()
            w.writerow(full_row)

    def _write_snapshots_csv(self, t, trade_id):
        path = os.path.join(self.data, "trade_snapshots.csv")
        info = json.loads(t.get("filters", "{}")) if isinstance(t.get("filters"), str) else t.get("filters", {})
        spike_meta = info.get("spike_meta", {})

        snapshots = [
            {
                "trade_id": trade_id,
                "timestamp_utc": t.get("entry_time_utc"),
                "price_usd": t.get("entry_mid"),
                "liquidity_usd": spike_meta.get("liquidity_usd") or info.get("entry_liquidity"),
                "volume_5m_usd": info.get("vol_m5") or spike_meta.get("vol_m5"),
                "price_change_since_entry_pct": 0,
                "unrealized_pnl_pct": 0,
                "unrealized_pnl_usd": 0,
                "drawdown_from_peak_pct": 0,
                "polling_interval_ms": 8000,
                "price_source": "dexscreener",
                "quote_age_ms": 0,
                "data_quality": "VERIFIED",
                "snapshot_type": "entry"
            },
            {
                "trade_id": trade_id,
                "timestamp_utc": t.get("exit_time_utc"),
                "price_usd": t.get("exit_mid"),
                "liquidity_usd": t.get("exit_liquidity") or info.get("exit_liquidity"),
                "volume_5m_usd": t.get("exit_vol_5m") or info.get("exit_vol_5m"),
                "price_change_since_entry_pct": t.get("price_move_pct"),
                "unrealized_pnl_pct": t.get("pnl_pct"),
                "unrealized_pnl_usd": t.get("pnl_usd"),
                "drawdown_from_peak_pct": t.get("mae_pct", 0),
                "polling_interval_ms": 8000,
                "price_source": t.get("sell_slip_src", "dexscreener"),
                "quote_age_ms": int((t.get("sell_quote_age_s") or 0) * 1000),
                "data_quality": t.get("price_provenance", "ESTIMATED"),
                "snapshot_type": "exit"
            }
        ]

        for snap in snapshots:
            new = not os.path.exists(path)
            if not new:
                with open(path) as fh:
                    head = fh.readline().strip()
                if head != ",".join(snap.keys()):
                    os.replace(path, path[:-4] + f".{int(now())}.csv")
                    new = True
            with open(path, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(snap.keys()))
                if new:
                    w.writeheader()
                w.writerow(snap)

    def _write_execution_events_csv(self, t, trade_id):
        path = os.path.join(self.data, "execution_events.csv")

        events = [
            {
                "trade_id": trade_id,
                "event_type": "entry_quote",
                "timestamp_utc": t.get("entry_time_utc"),
                "side": "buy",
                "reference_price": t.get("entry_mid"),
                "quote_price": t.get("entry_fill"),
                "price_impact_pct": t.get("buy_price_impact_pct"),
                "expected_slippage_pct": t.get("buy_slip_pct"),
                "route": t.get("buy_route"),
                "quote_age_s": t.get("buy_quote_age_s"),
                "endpoint": t.get("buy_quote_endpoint"),
                "status": "success" if t.get("buy_slip_src") == "jupiter" else "fallback_model",
                "failure_reason": t.get("buy_fallback_reason"),
            },
            {
                "trade_id": trade_id,
                "event_type": "exit_quote",
                "timestamp_utc": t.get("exit_time_utc"),
                "side": "sell",
                "reference_price": t.get("exit_mid"),
                "quote_price": t.get("exit_fill"),
                "price_impact_pct": t.get("sell_price_impact_pct"),
                "expected_slippage_pct": t.get("sell_slip_pct"),
                "route": t.get("sell_route"),
                "quote_age_s": t.get("sell_quote_age_s"),
                "endpoint": t.get("sell_quote_endpoint"),
                "status": "success" if t.get("sell_slip_src") == "jupiter" else "fallback_model",
                "failure_reason": t.get("sell_fallback_reason"),
            }
        ]

        for i in range(t.get("failed_exits", 0)):
            events.append({
                "trade_id": trade_id,
                "event_type": "failed_exit",
                "timestamp_utc": t.get("exit_time_utc"),
                "side": "sell",
                "reference_price": t.get("exit_mid"),
                "quote_price": None,
                "price_impact_pct": None,
                "expected_slippage_pct": None,
                "route": None,
                "quote_age_s": None,
                "endpoint": None,
                "status": "failed",
                "failure_reason": "sharp_dump_simulated",
            })

        for evt in events:
            new = not os.path.exists(path)
            if not new:
                with open(path) as fh:
                    head = fh.readline().strip()
                if head != ",".join(evt.keys()):
                    os.replace(path, path[:-4] + f".{int(now())}.csv")
                    new = True
            with open(path, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(evt.keys()))
                if new:
                    w.writeheader()
                w.writerow(evt)

    def _write_strategy_signals_csv(self, t, trade_id):
        path = os.path.join(self.data, "strategy_signals.csv")

        signal_row = {
            "trade_id": trade_id,
            "signal_type": "entry",
            "strategy": t.get("entry_type"),
            "timestamp_utc": t.get("entry_time_utc"),
            "decision": "ALLOW",
            "reason": f"{t.get('entry_type')}_signal",
            "filters_passed": True,
            "filters_detail": json.dumps(t.get("filters", {})),
            "spike_ratio": t.get("vol_ratio"),
            "token_age_min": t.get("age_min"),
            "liquidity_usd": t.get("entry_liquidity"),
            "dev_pct": t.get("dev_pct"),
            "top10_pct": t.get("top10_pct"),
            "whale_buy_usd": t.get("whale_buy_usd"),
            "holder_data_quality": t.get("holder_src"),
        }

        exit_signal = {
            "trade_id": trade_id,
            "signal_type": "exit",
            "strategy": t.get("entry_type"),
            "timestamp_utc": t.get("exit_time_utc"),
            "decision": t.get("exit_reason"),
            "reason": t.get("exit_reason"),
            "filters_passed": True,
            "filters_detail": "{}",
            "spike_ratio": None,
            "token_age_min": t.get("hold_sec", 0) / 60,
            "liquidity_usd": t.get("exit_liquidity"),
            "dev_pct": None,
            "top10_pct": None,
            "whale_buy_usd": None,
            "holder_data_quality": None,
        }

        adaptive_signals = []
        if t.get("adaptive_holding_enabled", False):
            adaptive_signals.append({
                "trade_id": trade_id,
                "signal_type": "adaptive_summary",
                "strategy": t.get("entry_type"),
                "timestamp_utc": t.get("exit_time_utc"),
                "decision": t.get("adaptive_holding_final_decision", t.get("exit_reason")),
                "reason": f"adaptive_extensions={t.get('adaptive_holding_extensions', 0)} stages={t.get('adaptive_holding_final_stage', 0)}",
                "filters_passed": True,
                "filters_detail": json.dumps({
                    "baseline_15min_quote": t.get("adaptive_baseline_quote"),
                    "final_stage": t.get("adaptive_holding_final_stage", 0),
                    "total_extensions": t.get("adaptive_holding_extensions", 0),
                    "max_deadline_ts": t.get("adaptive_holding_max_deadline"),
                }),
                "spike_ratio": None,
                "token_age_min": t.get("hold_sec", 0) / 60,
                "liquidity_usd": t.get("exit_liquidity"),
                "dev_pct": None,
                "top10_pct": None,
                "whale_buy_usd": None,
                "holder_data_quality": None,
            })

        for sig in [signal_row, exit_signal] + adaptive_signals:
            new = not os.path.exists(path)
            if not new:
                with open(path) as fh:
                    head = fh.readline().strip()
                if head != ",".join(sig.keys()):
                    os.replace(path, path[:-4] + f".{int(now())}.csv")
                    new = True
            with open(path, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(sig.keys()))
                if new:
                    w.writeheader()
                w.writerow(sig)

    def _write_recovery_events_csv(self, t, trade_id):
        path = os.path.join(self.data, "recovery_events.csv")

        if t.get("recovery_triggered") or t.get("antirug_decision"):
            event = {
                "trade_id": trade_id,
                "event_type": "recovery" if t.get("recovery_triggered") else "antirug",
                "timestamp_utc": t.get("exit_time_utc"),
                "recovery_mode": t.get("recovery_mode"),
                "recovery_triggered": t.get("recovery_triggered", False),
                "recovery_trigger_reason": t.get("recovery_trigger_reason"),
                "recovery_threshold_pct": t.get("recovery_threshold_pct", 0.70),
                "drawdown_at_trigger": t.get("drawdown_at_trigger"),
                "recovery_action": t.get("recovery_action"),
                "recovery_result": t.get("recovery_result"),
                "antirug_mode": t.get("antirug_mode"),
                "antirug_decision": t.get("antirug_decision"),
                "antirug_evidence": t.get("antirug_evidence"),
                "classification": t.get("recovery_classification"),
                "liquidity_status": t.get("liquidity_status"),
            }

            new = not os.path.exists(path)
            if not new:
                with open(path) as fh:
                    head = fh.readline().strip()
                if head != ",".join(event.keys()):
                    os.replace(path, path[:-4] + f".{int(now())}.csv")
                    new = True
            with open(path, "a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(event.keys()))
                if new:
                    w.writeheader()
                w.writerow(event)

    # ---- filters
    PRICE_REJECTS = ("price_unverified_quote_mismatch",)

    def _recently_rejected(self, mint, at=None):
        """Return True for a mint rejected in the last five minutes; prune expired entries."""
        ts = now() if at is None else at
        rejected = getattr(self, "rejected", {})
        entry = rejected.get(mint)
        if entry:
            try:
                if ts - float(entry[0]) < 300:
                    return True
            except (TypeError, ValueError, IndexError):
                pass
            rejected.pop(mint, None)
        return False

    def _reject(self, mint, reason):
        if reason in self.PRICE_REJECTS and getattr(self, "provenance_on", False):
            rj = self.meta.setdefault("price_rejected", [])
            rj.append({"ts": now(), "mint": mint, "reason": reason, "price_provenance": "REJECTED"})
            del rj[:-500]
            self.meta["price_rejected_n"] = self.meta.get("price_rejected_n", 0) + 1
        self.rejected[mint] = (now(), reason)
        self.stats["filtered_out"][reason] = self.stats["filtered_out"].get(reason, 0) + 1
        return False

    def _prune_holder_cache(self, at=None):
        """Expire holder snapshots by TTL, then trim the oldest fresh entries if over capacity."""
        current = now() if at is None else float(at)
        ttl = max(60.0, float(self.c.get("holder_cache_ttl_s", 300)))
        max_entries = max(100, int(self.c.get("holder_cache_max_entries", 3000)))
        stale = []
        for mint, item in self.holder_cache.items():
            try:
                ts = float(item.get("ts", 0))
            except (AttributeError, TypeError, ValueError, OverflowError):
                ts = 0.0
            if ts <= 0 or current - ts > ttl or ts > current + 60:
                stale.append(mint)
        for mint in stale:
            self.holder_cache.pop(mint, None)
        overflow = len(self.holder_cache) - max_entries
        if overflow > 0:
            def _cache_ts(item):
                try:
                    return float((item or {}).get("ts", 0))
                except (AttributeError, TypeError, ValueError, OverflowError):
                    return 0.0
            oldest = sorted(self.holder_cache.items(), key=lambda kv: _cache_ts(kv[1]))
            for mint, _ in oldest[:overflow]:
                self.holder_cache.pop(mint, None)
        self._holder_cache_last_prune = current
        return len(stale)

    def holder_filter(self, snap):
        mint, name = snap["mint"], snap["name"]
        current = now()
        ttl = max(60.0, float(self.c.get("holder_cache_ttl_s", 300)))
        if (current - getattr(self, "_holder_cache_last_prune", 0) >= 60
                or len(self.holder_cache) > int(self.c.get("holder_cache_max_entries", 3000))):
            self._prune_holder_cache(current)
        c = self.holder_cache.get(mint)
        if not c or current - float(c.get("ts", 0)) > ttl:
            info = self.rc.holders(mint)
            c = {"ts": current, "info": info}
            self.holder_cache[mint] = c
            if len(self.holder_cache) > int(self.c.get("holder_cache_max_entries", 3000)):
                self._prune_holder_cache(current)
        info = c["info"]
        if not info:
            self.agent("holders", False, f"${name} · dati holder non disponibili")
            return self._reject(mint, "holder_data_unavailable"), None
        if info.get("rugged"):
            self.agent("holders", False, f"${name} · RUGGED")
            return self._reject(mint, "rugged"), info
        dev = info.get("dev_pct")
        dev_source = info.get("dev_pct_source", "unknown")
        dev_ok = dev is None or dev <= self.c["max_dev_holding_pct"]
        self.agent("dev", dev_ok, f"dev {f(dev):.1f}% ({dev_source}) · ${name} · {'ok' if dev_ok else 'scartato'}")
        if not dev_ok:
            return self._reject(mint, "dev_holding_too_high"), info
        top10_quality = info.get("top10_pct_quality", "UNKNOWN")
        top10_pct = info.get("top10_pct", 0)
        if top10_quality == "UNKNOWN":
            if self.c.get("reject_unknown_top10", True):
                self.agent("holders", False, f"${name} · top10 UNKNOWN · rejected")
                return self._reject(mint, "top10_unknown"), info
            else:
                self.agent("holders", True, f"${name} · top10 UNKNOWN · allowed (config)")
        else:
            top_ok = top10_pct <= self.eff("max_top10_pct")
            nh = info.get("n_holders")
            self.agent("holders", top_ok, f"${name} top10 {top10_pct:.1f}% ({top10_quality})" + (f" · {nh} holder" if nh else ""))
            if not top_ok:
                return self._reject(mint, "top10_too_concentrated"), info
        lim = self.c.get("max_rugcheck_score_normalised")
        if lim is not None and info.get("risk_score") is not None and info["risk_score"] > lim:
            return self._reject(mint, "rugcheck_risk_too_high"), info
        return True, info

    def vybe_confirm(self, snap):
        if not (self.vybe and self.c["vybe_confirm_holders"]):
            return True, None
        hs = self.vybe.top_holders(snap["mint"])
        if not hs:
            return True, None
        real = [h for h in hs if not h.get("ownerName")]
        top10 = sum(f(h.get("percentageOfSupplyHeld")) for h in real[:10])
        if top10 > self.eff("max_top10_pct"):
            return self._reject(snap["mint"], "vybe_top10_too_concentrated"), round(top10, 2)
        return True, round(top10, 2)

    def whale_check(self, snap):
        if not self.c["require_whale_buy"]:
            return True, None
        trades = self.gt.trades(snap["pool"], self.c["whale_buy_usd"])
        if trades is None:
            self.agent("whales", False, f"${snap['name']} · dati non disponibili")
            return self._reject(snap["mint"], "whale_data_unavailable"), None
        cutoff = now() - self.c["whale_lookback_minutes"] * 60
        for t in trades:
            try:
                ts = datetime.fromisoformat(t["block_timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, AttributeError, TypeError, ValueError):
                continue
            if t.get("kind") == "buy" and ts >= cutoff and f(t.get("volume_in_usd")) >= self.c["whale_buy_usd"]:
                v = f(t.get("volume_in_usd"))
                self.agent("whales", True, f"whale +${v:,.0f} · ${snap['name']}")
                return True, round(v, 2)
        self.agent("whales", False, f"nessuna balena · ${snap['name']}")
        return self._reject(snap["mint"], "no_whale_buy"), None

    def spike(self, s):
        created_ts = s.get("created_ts")
        if created_ts is None:
            age_min = 1e9
            age_source = "unknown"
        else:
            age_min = (now() - created_ts) / 60
            age_source = "created_ts"

        vol_h1 = s.get("vol_h1") or 0
        vol_m5 = s.get("vol_m5") or 0
        if vol_h1 > vol_m5:
            historical_base = (vol_h1 - vol_m5) / 11.0
            base = max(historical_base, self.c["min_baseline_5m_usd"])
            baseline_quality = "historical"
        else:
            base = self.c["min_baseline_5m_usd"]
            baseline_quality = "minimum_floor"

        ratio = vol_m5 / base if base else 0

        liquidity = s.get("liquidity") or 0
        min_liquidity = self.eff("min_liquidity_usd")
        liquidity_ok = liquidity >= min_liquidity
        liquidity_quality = "verified" if liquidity > 0 else "unknown"

        chg_5m = s.get("chg_h1", 0)
        late_entry = chg_5m > 50

        ok = (age_min <= self.c["max_pair_age_minutes"]
              and ratio >= self.eff("spike_multiplier")
              and vol_m5 >= self.eff("min_volume_5m_usd")
              and s["buys_m5"] >= self.c["min_buys_5m"]
              and s["buys_m5"] >= self.c["min_buy_sell_ratio"] * max(s["sells_m5"], 1)
              and liquidity_ok
              and s["quote_mint"] in self.allowed_quotes)

        return ok, round(ratio, 2), round(age_min, 1), liquidity_ok, late_entry, {
            "age_source": age_source,
            "baseline_quality": baseline_quality,
            "liquidity_quality": liquidity_quality,
            "liquidity_usd": liquidity,
            "min_liquidity_usd": min_liquidity
        }

    # ---- loops
    def refresh_top_traders(self):
        if not ((self.vybe or self.birdeye_copy) and self.c["copy_traders_enabled"]):
            return
        if now() - self.top_traders_ts < self.c["top_traders_refresh_minutes"] * 60:
            return
        try:
            cached = json.load(open(self.tt_cache))
            if now() - cached["ts"] < self.c["top_traders_refresh_minutes"] * 60 and cached["wallets"]:
                self.top_traders, self.top_traders_ts = set(cached["wallets"]), cached["ts"]
                self.tt_sources = cached.get("sources") or {}
                log.info("Loaded %d cached top-trader wallets", len(self.top_traders))
                self.agent("smart", True, f"{len(self.top_traders)} top trader in lista (cache)")
                return
        except Exception:
            pass
        self.top_traders_ts = now()
        tt = self.vybe.top_traders(self.c["top_traders_resolution"], self.c["top_traders_limit"]) if self.vybe else set()
        if self.vybe and not tt:
            log.warning("Vybe top-traders request failed (check VYBE_API_KEY / plan)")
        if self.birdeye_copy:
            bc = self.c.get("birdeye") or {}
            bt = self.birdeye.best_traders(tuple(bc.get("best_trader_types", ["1W", "today"])),
                                           bc.get("best_trader_pages", 5))
            if bt:
                log.info("Loaded %d top-trader wallets from Birdeye", len(bt))
                srcs = {w: ["vybe"] for w in (tt or ())}
                for w in bt:
                    srcs.setdefault(w, []).append("birdeye")
                self.tt_sources = srcs
                tt = set(tt or set()) | bt
            else:
                log.warning("Birdeye best-traders request failed: copy agent uses %s",
                            "the Vybe list only" if tt else "no list (retry at next refresh)")
        if not tt:
            self.top_traders_ts = now() - self.c["top_traders_refresh_minutes"] * 60 + 600
        if tt:
            self.top_traders = tt
            atomic_json(self.tt_cache, {"ts": now(), "wallets": sorted(tt),
                                        **({"sources": self.tt_sources} if self.birdeye_copy else {})})
            if not self.birdeye_copy:
                log.info("Loaded %d top-trader wallets from Vybe", len(tt))
            self.agent("smart", True, f"{len(tt)} top trader caricati da " +
                       ("Vybe" if not self.birdeye_copy else "Vybe + Birdeye" if self.vybe else "Birdeye"))
            if self.gmgn and hasattr(self.gmgn, "agent_client") and self.gmgn.agent_client:
                self._ingest_wallet_intelligence()

    # ---- AntiRug Security Engine
    def _ar_quote(self, in_mint, out_mint, amount_raw):
        q = self.jup.quote(in_mint, out_mint, amount_raw)
        return q, ("" if q else getattr(self.jup, "last_reason", "") or "unknown")

    def _planned_quote_usd(self):
        if self.risk.mode == "survival":
            size = self.next_size()
        else:
            size = self.risk.size(self.equity_value(), self.cash, sum(q["cost_usd"] for q in self.positions.values()),
                                  self.eff("position_size_pct"), self.min_trade_usd())[0]
        return max(size - self._net_fee_usd(), 0.0)

    def antirug_gate(self, s, entry_type, hinfo, ctx=None):
        seen = getattr(self, "_ar_liq_seen", {}).get(s["mint"])
        s = {**s, "liquidity_ts": seen[0] if seen and seen[1] == s.get("liquidity") and seen[1] else None}
        try:
            v = self.antirug.evaluate(s, self._planned_quote_usd(), hinfo, {**(ctx or {}), "entry_type": entry_type})
            ok, why = self.antirug.gate(v)
        except Exception as e:
            log.exception("AntiRug error for %s: %s", s.get("name"), e)
            if self.antirug.mode == "active":
                return self._reject(s["mint"], "antirug_error"), {}
            return True, {}
        log.info("ANTIRUG [%s] %s %s -> %s risk %s (%s, %s) failed=%s unverified=%s", self.antirug.mode.upper(),
                 s.get("name"), s["mint"][:8], v["decision"], v["risk"], v["level"], v["reason"],
                 v["checks_failed"], v["checks_unverified"])
        self.antirug.maybe_alert(v)
        self.antirug.track(v, entered=False)
        info = {"antirug": {"mode": self.antirug.mode, "decision": v["decision"], "risk": v["risk"],
                            "reason": v["reason"], "confidence": v.get("confidence")}}
        if v.get("enforced"):
            info["antirug"]["enforced"] = list(v["enforced"])
            log.warning("ANTIRUG ENFORCED block (SHADOW + %s): %s %s top10 %s%% -> entry rejected", ",".join(v["enforced"]),
                        s.get("name"), s["mint"][:8], (v["mandatory"]["holders.top10"] or {}).get("value_pct"))
        if not ok:
            self.agent("holders", False, f"${s.get('name')} · AntiRug {v['decision'].lower()}")
            return self._reject(s["mint"], why), info
        return True, info

    def _antirug_mode_notice(self):
        try:
            if self.antirug:
                prev = self.antirug.prev_mode
                if prev != self.antirug.mode:
                    self.tg.send(antirug_mod.mode_change_text(prev, self.antirug.mode), key="antirug:mode")
                    self.antirug.save()
            else:
                prev = antirug_mod.previous_mode(self.data)
                if prev and prev != "off":
                    self.tg.send(antirug_mod.mode_change_text(prev, "off"), key="antirug:mode")
                    antirug_mod.record_off(self.data)
        except Exception as e:
            log.warning("AntiRug mode notice failed: %s", e)

    def _antirug_after_buy(self, mint):
        p = self.positions.get(mint)
        if p and self.antirug:
            self.antirug.note_entry(mint, p["costs"].get("buy_slip_pct"), p["costs"].get("buy_slip_src"))

    def _ingest_wallet_intelligence(self):
        try:
            if not self.gmgn or not self.gmgn.agent_client:
                return
            if not self.top_traders:
                return

            wallet_list = list(self.top_traders)[:100]
            stats_data = self.gmgn.agent_client.get_wallet_stats(
                chain="sol",
                wallet_addresses=wallet_list,
                period="30d"
            )

            if stats_data and isinstance(stats_data, dict):
                wallets_data = stats_data.get("wallets") or stats_data.get("list") or []
                for wallet_data in wallets_data:
                    if not isinstance(wallet_data, dict):
                        continue

                    wallet_addr = wallet_data.get("wallet_address") or wallet_data.get("address")
                    if not wallet_addr:
                        continue

                    try:
                        profits_data = self.gmgn.agent_client.get_wallet_profits(
                            chain="sol",
                            wallet_addresses=[wallet_addr],
                            period="30d"
                        )
                        if profits_data and isinstance(profits_data, dict):
                            trades_data = profits_data.get("trades") or profits_data.get("list") or []
                            if trades_data:
                                self.wallet_intelligence.ingest_wallet_trades(
                                    wallet_addr,
                                    trades_data,
                                    source="gmgn",
                                    save_immediately=False
                                )
                    except Exception as e:
                        log.debug("Failed to ingest wallet profits for %s: %s", wallet_addr[:8], e)
                with self.wallet_intelligence.lock:
                    self.wallet_intelligence._update_rankings()
                    self.wallet_intelligence._save()

        except Exception as e:
            log.warning("Failed to ingest wallet intelligence: %s", e)

    def _smart_money_snapshot(self):
        wi = getattr(self, "wallet_intelligence", None)
        if not wi:
            return {"enabled": False, "rankings": {}, "total_tracked": 0, "total_verified": 0}
        try:
            r = wi.get_rankings()
            return {
                "enabled": True,
                "rankings": r.rankings if r else {},
                "total_tracked": r.total_tracked if r else 0,
                "total_verified": r.total_verified if r else 0,
                "updated_ts": r.updated_timestamp if r else 0,
            }
        except Exception as e:
            log.warning("smart money snapshot failed: %s", e)
            return {"enabled": False, "rankings": {}, "total_tracked": 0, "total_verified": 0}

    def antirug_followups(self):
        if not self.antirug:
            return
        try:
            due = self.antirug.due_followups()
            if due:
                snaps = self.ds.tokens(due[:90])
                self.antirug.record_prices({m: {"price": x["price"], "liquidity": x.get("liquidity")}
                                            for m, x in snaps.items()}, answered=self.ds.last_ok)
        except Exception as e:
            log.warning("AntiRug follow-up error: %s", e)

    def _poll_interval(self):
        if not self.adaptive_poll.get("enabled", True):
            return self.c["price_poll_seconds"]
        ap = self.adaptive_poll
        t = now()
        for mint, info in ap["accelerated"].items():
            if info.get("emergency") and t < info.get("until", 0):
                return ap["emergency_interval"]
        if ap["accelerated"]:
            active = [m for m, info in ap["accelerated"].items() if t < info.get("until", 0)]
            if active:
                return ap["accelerated_interval"]
        return ap["base_interval"]

    def _update_adaptive_poll(self, mint, price, position):
        if not self.adaptive_poll.get("enabled", True):
            return
        ap = self.adaptive_poll
        t = now()
        triggers = ap.get("triggers", {})
        rules = self.exit_rules(position)

        accelerate = False
        reason = ""

        near_exit_pct = triggers.get("near_exit_pct", 0.02)
        chg = price / position["entry_mid"] - 1

        if rules.get("stop_loss_pct") and chg <= -rules["stop_loss_pct"] * (1 + near_exit_pct):
            accelerate = True
            reason = "near_stop_loss"
        elif rules.get("take_profit_pct") and chg >= rules["take_profit_pct"] * (1 - near_exit_pct):
            accelerate = True
            reason = "near_take_profit"
        elif (rules.get("trailing_stop_pct") and position.get("peak")
              and price <= position["peak"] * (1 + rules["trailing_stop_pct"] * near_exit_pct)):
            accelerate = True
            reason = "near_trailing_stop"
        elif position.get("last") and abs(price / position["last"] - 1) >= triggers.get("tick_move_pct", 0.05):
            accelerate = True
            reason = "rapid_price_move"

        if accelerate:
            if len(ap["accelerated"]) < ap["max_accelerated"] or mint in ap["accelerated"]:
                ap["accelerated"][mint] = {
                    "until": t + ap["cooldown_seconds"],
                    "reason": reason,
                    "emergency": False
                }
                ap["last_check"] = t
        else:
            if mint in ap["accelerated"] and t >= ap["accelerated"][mint].get("until", 0):
                ap["accelerated"].pop(mint, None)

        expired = [m for m, info in ap["accelerated"].items() if t >= info.get("until", 0)]
        for m in expired:
            ap["accelerated"].pop(m, None)

    # =========================================================================
    # ADAPTIVE HOLDING ENGINE v1 (PAPER TRADING ONLY)
    # =========================================================================
    _AH_TRUE_VALUES = ("1", "true", "yes", "on")
    _AH_FALSE_VALUES = ("0", "false", "no", "off")

    def _adaptive_holding_enabled(self):
        env_val = os.getenv("ADAPTIVE_HOLDING_ENABLED", "").strip().lower()
        if env_val:
            if env_val in self._AH_TRUE_VALUES:
                return True
            if env_val in self._AH_FALSE_VALUES:
                return False
            log.warning("ADAPTIVE_HOLDING_ENABLED=%r is not a valid boolean (expected 1/0/true/false/yes/no/on/off): "
                        "Adaptive Holding DISABLED (fail-closed)", env_val)
            return False
        return bool((self.c.get("adaptive_holding") or {}).get("enabled", False))

    def _verify_paper_mode(self):
        ah_cfg = self.c.get("adaptive_holding") or {}
        if not ah_cfg.get("paper_only_check", True):
            return True

        if not self.meta.get("simulation_only", False):
            log.warning("Adaptive Holding: state.json simulation_only is not True -> DISABLED")
            return False

        wallet_env_vars = [
            "SOLANA_PRIVATE_KEY", "PRIVATE_KEY", "WALLET_PRIVATE_KEY", "FEE_PAYER_PRIVATE_KEY",
            "SIGNING_KEY", "KEYPAIR_PATH", "KEYPAIR_FILE", "WALLET_KEYPAIR"
        ]
        for var in wallet_env_vars:
            if os.getenv(var):
                log.warning("Adaptive Holding: wallet env var %s present -> DISABLED (fail-closed)", var)
                return False

        if not self._adaptive_holding_enabled():
            return False

        return True

    def _next_adaptive_check_ts(self, entry_ts, current_ts=None):
        """Milestone schedule: 15m -> 20m -> 30m -> 60m -> 120m -> 180m -> +60m recurring."""
        t = now() if current_ts is None else current_ts
        ah_cfg = self.c.get("adaptive_holding") or {}
        schedule = sorted(ah_cfg.get("evaluation_schedule_minutes") or [15, 20, 30, 60, 120, 180])
        recurring = max(5, int(ah_cfg.get("recurring_interval_after_max_minutes", 60)))
        elapsed_min = max(0.0, (t - entry_ts) / 60.0)
        for step_min in schedule:
            if elapsed_min < step_min - 1e-6:
                return entry_ts + step_min * 60.0
        last_step = schedule[-1] if schedule else 180
        extra_steps = int((elapsed_min - last_step) // recurring) + 1
        return entry_ts + (last_step + extra_steps * recurring) * 60.0

    def _evaluate_adaptive_holding(self, mint, position, price, snap):
        """Time elapsed is a re-evaluation trigger, NEVER an autonomous exit reason while trend holds."""
        if not self._adaptive_holding_enabled() or not self._verify_paper_mode():
            return "CLOSE", "adaptive_holding_disabled_or_non_paper", 0, {}, None

        ah_cfg = self.c.get("adaptive_holding") or {}
        t = now()
        entry_ts = position["entry_ts"]
        held_min = (t - entry_ts) / 60.0
        next_check = position.get("adaptive_holding_next_check", 0)

        if t < next_check:
            return "HOLD", "before_next_evaluation", 0, {}, None

        indicators = {"held_minutes": round(held_min, 1)}
        score_components = {}

        entry_mid = position["entry_mid"]
        price_move_pct = (price / entry_mid - 1.0) * 100.0 if entry_mid > 0 else 0.0
        indicators["price_move_pct"] = round(price_move_pct, 2)
        if price_move_pct >= 20.0:
            score_components["price_move_pct"] = 100.0
        elif price_move_pct >= 5.0:
            score_components["price_move_pct"] = 75.0 + (price_move_pct - 5.0) * 25.0 / 15.0
        elif price_move_pct >= 0.0:
            score_components["price_move_pct"] = 60.0 + price_move_pct * 3.0
        elif price_move_pct <= -10.0:
            score_components["price_move_pct"] = 0.0
        else:
            score_components["price_move_pct"] = max(0.0, 60.0 + price_move_pct * 6.0)

        # Punto 3: usa il prezzo di riferimento della precedente valutazione adattiva (MAI position['last'] appena sovrascritto)
        ref_price = float(position.get("adaptive_last_eval_price") or entry_mid or 0.0)
        momentum_pct = ((price / ref_price - 1.0) * 100.0) if ref_price > 0 else 0.0
        buys_m5 = int(snap.get("buys_m5") or 0)
        sells_m5 = int(snap.get("sells_m5") or 0)
        bs_ratio = buys_m5 / max(sells_m5, 1)
        indicators["momentum_ref_price"] = round(ref_price, 8)
        indicators["momentum_pct"] = round(momentum_pct, 2)
        indicators["buy_sell_ratio_m5"] = round(bs_ratio, 2)

        mom_score = 50.0 + max(-50.0, min(50.0, momentum_pct * 8.0))
        flow_score = 100.0 if bs_ratio >= 1.5 else (65.0 if bs_ratio >= 1.0 else (30.0 if bs_ratio >= 0.7 else 0.0))
        score_components["momentum"] = 0.5 * mom_score + 0.5 * flow_score

        entry_vol_5m = position.get("entry_vol_5m") or position.get("entry_baseline_vol") or 0.0
        current_vol_5m = float(snap.get("vol_m5") or 0.0)
        indicators["current_vol_5m_usd"] = round(current_vol_5m, 2)
        if entry_vol_5m > 0 and current_vol_5m > 0:
            vol_ratio = current_vol_5m / entry_vol_5m
            indicators["volume_ratio"] = round(vol_ratio, 2)
            if vol_ratio >= 1.5:
                score_components["volume_accel"] = 100.0
            elif vol_ratio >= 0.7:
                score_components["volume_accel"] = 60.0 + (vol_ratio - 0.7) * 50.0
            elif vol_ratio <= 0.25:
                score_components["volume_accel"] = 0.0
            else:
                score_components["volume_accel"] = (vol_ratio - 0.25) * 60.0 / 0.45
        else:
            indicators["volume_ratio"] = 0.0
            score_components["volume_accel"] = 45.0 if current_vol_5m > 1000 else 15.0

        entry_liq = float(position.get("entry_liquidity") or 0.0)
        current_liq = float(snap.get("liquidity") or 0.0)
        min_liq = float(ah_cfg.get("min_liquidity_usd", 1500))
        indicators["current_liquidity_usd"] = round(current_liq, 2)
        if current_liq < min_liq:
            score_components["liquidity"] = 0.0
        elif entry_liq > 0:
            liq_ratio = current_liq / entry_liq
            indicators["liquidity_ratio"] = round(liq_ratio, 2)
            if liq_ratio >= 0.90:
                score_components["liquidity"] = 100.0
            elif liq_ratio >= 0.65:
                score_components["liquidity"] = 60.0 + (liq_ratio - 0.65) * 160.0
            elif liq_ratio <= 0.40:
                score_components["liquidity"] = 0.0
            else:
                score_components["liquidity"] = (liq_ratio - 0.40) * 60.0 / 0.25
        else:
            score_components["liquidity"] = 60.0

        peak_price = max(float(position.get("peak_price") or entry_mid), price)
        drawdown_pct = ((peak_price - price) / peak_price * 100.0) if peak_price > 0 else 0.0
        indicators["drawdown_from_peak_pct"] = round(drawdown_pct, 2)
        dd_score = 100.0 if drawdown_pct <= 4.0 else (0.0 if drawdown_pct >= 18.0 else 100.0 - (drawdown_pct - 4.0) * 100.0 / 14.0)

        antirug_score = 100.0
        if self.antirug:
            try:
                eval_snap = {**snap, "mint": mint, "name": position.get("name"), "liquidity_ts": t}
                ar_result = self.antirug.evaluate(eval_snap, position.get("cost_usd", self._planned_quote_usd()))
                if ar_result and ar_result.get("risk") is not None:
                    antirug_score = max(0.0, 100.0 - float(ar_result["risk"]))
                    indicators["antirug_risk_score"] = float(ar_result["risk"])
            except Exception:
                antirug_score = 50.0
        if self.rc:
            try:
                rc = self.rc.holders(mint)
                if rc and rc.get("rugged", False):
                    antirug_score = 0.0
                    indicators["rugged"] = True
            except Exception:
                pass
        score_components["risk"] = 0.5 * dd_score + 0.5 * antirug_score

        w = ah_cfg.get("indicators") or {}
        weights = {
            "price_move_pct": float(w.get("price_move_pct_weight", 25)),
            "momentum": float(w.get("momentum_weight", 20)),
            "volume_accel": float(w.get("volume_accel_weight", 20)),
            "liquidity": float(w.get("liquidity_weight", 20)),
            "risk": float(w.get("risk_weight", 15)),
        }
        tot_w = max(sum(weights.values()), 1.0)
        score = sum(score_components[k] * weights[k] for k in weights) / tot_w
        indicators["composite_score"] = round(score, 1)

        # Aggiorna lo snapshot dedicato alla valutazione adattiva
        position["adaptive_last_eval_price"] = price
        position["adaptive_last_eval_ts"] = t

        exec_check = self.evaluate_exit_executability(mint, price, position["qty"], position)
        baseline_quote = None
        if exec_check.get("quote"):
            usd_out, fill, slip, quote = exec_check["quote"]
            net_p = max(usd_out - self._net_fee_usd(), 0.0)
            cost_u = float(position.get("cost_usd") or 0.0)
            pnl_p = round(100.0 * (net_p - cost_u) / cost_u, 2) if cost_u > 0 else 0.0
            baseline_quote = {"usd_out": usd_out, "fill": fill, "slip": slip, "quote": quote, "ts": t, "pnl_pct": pnl_p}
            indicators["jupiter_sell_slippage_pct"] = exec_check["slippage_pct"]
            indicators["jupiter_sell_quote_usd"] = exec_check["estimated_exit_usd"]

        if indicators.get("rugged") or antirug_score < 20.0:
            return "CLOSE", "critical_rug_or_risk_signal", score, indicators, baseline_quote
        if current_liq < min_liq or (entry_liq > 0 and current_liq / entry_liq < 0.45):
            return "CLOSE", "liquidity_deterioration", score, indicators, baseline_quote
        if buys_m5 + sells_m5 >= 8 and bs_ratio < 0.45 and momentum_pct < -3.0:
            return "CLOSE", "heavy_sell_pressure_and_negative_momentum", score, indicators, baseline_quote

        min_score = float(ah_cfg.get("min_score", 58))
        if score < min_score:
            return "CLOSE", f"trend_deteriorated_score_{round(score)}_below_{round(min_score)}", score, indicators, baseline_quote

        stage = int(position.get("adaptive_holding_stage", 0)) + 1
        decision = f"HOLD_STAGE_{stage}"
        return decision, f"positive_trend_score_{round(score)}_at_{round(held_min)}m", score, indicators, baseline_quote

    def _smart_recovery_classify(self, mint, position, price, tick_drop):
        rm = self.recovery_mode
        if not rm.get("enabled", True):
            return "GENUINE_CRASH"

        t = now()
        if tick_drop < rm.get("crash_threshold", 0.70):
            return "GENUINE_CRASH"

        if mint in rm["verifying"]:
            vinfo = rm["verifying"][mint]
            if t - vinfo["start_ts"] > rm.get("verification_timeout", 30):
                classification = vinfo.get("classification")
                if classification in ("NON_LIQUIDATABLE", "GENUINE_CRASH", "DATA_ERROR"):
                    return classification
                return "UNRESOLVED"
            return "VERIFYING"

        rm["verifying"][mint] = {
            "start_ts": t,
            "retries": 0,
            "classification": None,
            "provider_errors": []
        }

        try:
            jq = self.jup_sell(mint, price, position["qty"])
            has_jupiter_route = jq is not None and jq[0] > 0

            liq_usd = 0
            if self.birdeye:
                try:
                    be_price = self.birdeye.price(mint, with_liquidity=True)
                    if be_price:
                        liq_usd = be_price[1] or 0
                except Exception:
                    pass
            if not liq_usd:
                liq_usd = position.get("liquidity", 0)

            rc = self.rc.holders(mint) if self.rc else None
            is_rugged = rc and rc.get("rugged", False)

            min_liq = rm.get("min_liquidity_usd", 1000)
            require_route = rm.get("require_jupiter_route", True)

            if is_rugged:
                classification = "NON_LIQUIDATABLE"
            elif has_jupiter_route and liq_usd >= min_liq:
                classification = "GENUINE_CRASH"
            elif not has_jupiter_route and require_route:
                if liq_usd < min_liq:
                    classification = "NON_LIQUIDATABLE"
                else:
                    classification = "UNRESOLVED"
            elif liq_usd < min_liq:
                classification = "NON_LIQUIDATABLE"
            else:
                classification = "DATA_ERROR"

            rm["verifying"][mint]["classification"] = classification
            if classification in ("GENUINE_CRASH", "NON_LIQUIDATABLE", "DATA_ERROR", "UNRESOLVED"):
                rm["verifying"].pop(mint, None)
            return classification

        except Exception as e:
            log.warning("SMART Recovery verification failed for %s: %s", mint[:8], e)
            vinfo = rm["verifying"][mint]
            vinfo["retries"] += 1
            vinfo["provider_errors"].append(str(e))
            if vinfo["retries"] >= rm.get("max_retries", 3):
                classification = vinfo.get("classification")
                if classification in ("NON_LIQUIDATABLE", "GENUINE_CRASH"):
                    return classification
                return "UNRESOLVED"
            return "VERIFYING"

    def _handle_recovery_state(self, mint, position, price, tick_drop):
        rm = self.recovery_mode
        if not rm.get("enabled", True):
            return False

        classification = self._smart_recovery_classify(mint, position, price, tick_drop)

        if classification == "VERIFYING":
            if not position.get("_recovery_notified"):
                self.tg.send(tg.msg_smart_recovery_started(position["name"], mint, tick_drop),
                             key=f"recovery_start:{mint}")
                position["_recovery_notified"] = True
            return True

        elif classification == "NON_LIQUIDATABLE":
            position["non_liquidatable"] = True
            position["liquidation_value"] = 0
            position["recovery_classification"] = "NON_LIQUIDATABLE"
            position["_recovery_notified"] = False
            self.tg.send(tg.msg_smart_recovery_result(position["name"], mint, "NON_LIQUIDATABLE",
                                                      "Pool drenato o nessuna quotazione Jupiter eseguibile."),
                         key=f"recovery:{mint}")
            self.tg.send(tg.msg_non_liquidatable_position(position["name"], mint,
                                                          position["qty"] * position["last"], 0),
                         key=f"nonliq:{mint}")
            return True

        elif classification == "DATA_ERROR":
            self.tg.send(tg.msg_smart_recovery_result(position["name"], mint, "DATA_ERROR",
                                                      "Tick anomalo rifiutato, prezzo precedente mantenuto."),
                         key=f"recovery:{mint}")
            rm["verifying"].pop(mint, None)
            return True

        elif classification == "UNRESOLVED":
            position["recovery_classification"] = "UNRESOLVED"
            position["_recovery_notified"] = False
            self.tg.send(tg.msg_smart_recovery_result(position["name"], mint, "UNRESOLVED",
                                                      "Dati insufficienti per confermare crash o rug. Verifica in corso."),
                         key=f"recovery:{mint}")
            return False

        elif classification == "GENUINE_CRASH":
            position["recovery_classification"] = "GENUINE_CRASH"
            position["_recovery_notified"] = False
            self.tg.send(tg.msg_smart_recovery_result(position["name"], mint, "GENUINE_CRASH",
                                                      "Quotazione Jupiter e liquidità confermate."),
                         key=f"recovery:{mint}")
            return False

        return False

    def manage_if_due(self):
        self.cmds.run_actions()
        if self.stop:
            return
        interval = self._poll_interval()
        if now() - self.last_manage >= interval:
            self.manage()

    def _pumpfun_to_pools(self):
        pools = []
        for ev in self.pumpfun.get_recent_events(limit=100):
            etype = ev.get("event_type")
            if etype != "new_token":
                continue
            mint = ev.get("token_mint")
            if not mint:
                continue
            quote_mint = ev.get("quote_mint") or SOL
            ev["quote_mint"] = quote_mint
            if quote_mint not in self.allowed_quotes:
                log.debug("Pump.fun event %s skipped: invalid quote_mint (%s)", mint[:8], quote_mint)
                continue
            created_ts = ev.get("block_time")
            if created_ts is None:
                created_ts = ev.get("received_at")
            if created_ts is None:
                log.debug("Pump.fun event %s skipped: no valid timestamp", mint[:8])
                continue
            pool = {
                "pool": ev.get("pool_address", ""),
                "mint": mint,
                "quote_mint": quote_mint,
                "name": ev.get("token_name") or ev.get("token_symbol", ""),
                "dex": "pumpfun",
                "price": f(ev.get("price_usd")),
                "created_ts": created_ts,
                "vol_m5": f(ev.get("volume_usd")),
                "vol_h1": f(ev.get("volume_usd")),
                "buys_m5": ev.get("buy_count") or 0,
                "sells_m5": ev.get("sell_count") or 0,
                "liquidity": f(ev.get("liquidity_usd")),
                "fdv": f(ev.get("market_cap_usd")),
                "src": "pumpfun",
            }
            pools.append(pool)
        return pools

    def _gmgn_to_pools(self):
        pools = []
        for ev in self.gmgn.get_recent_events(limit=100):
            etype = ev.get("event_type")
            if etype not in ("new_pool", "token_launch"):
                continue
            mint = ev.get("token_mint")
            if not mint:
                continue
            quote_mint = ev.get("quote_mint")
            if not quote_mint or quote_mint not in self.allowed_quotes:
                log.debug("GMGN event %s skipped: missing or invalid quote_mint (%s)", mint[:8] if mint else "unknown", quote_mint)
                continue
            created_ts = ev.get("block_time")
            if created_ts is None:
                created_ts = ev.get("received_at")
            if created_ts is None:
                log.debug("GMGN event %s skipped: no valid timestamp", mint[:8])
                continue
            pool = {
                "pool": ev.get("pool_address", ""),
                "mint": mint,
                "quote_mint": quote_mint,
                "name": ev.get("token_name") or ev.get("token_symbol", ""),
                "dex": "gmgn",
                "price": f(ev.get("price_usd")),
                "created_ts": created_ts,
                "vol_m5": f(ev.get("volume_usd")),
                "vol_h1": f(ev.get("volume_usd")),
                "buys_m5": ev.get("buy_count") or 0,
                "sells_m5": ev.get("sell_count") or 0,
                "liquidity": f(ev.get("liquidity_usd")),
                "fdv": f(ev.get("market_cap_usd")),
                "src": "gmgn",
            }
            pools.append(pool)
        return pools

    def discover(self):
        # Expire temporary rejections before processing a new discovery pass.
        # This keeps the cache bounded and prevents repeated expensive checks for rejected mints.
        t_discover = now()
        for rejected_mint, rejected_entry in list(self.rejected.items()):
            try:
                rejected_ts = float(rejected_entry[0])
            except (TypeError, ValueError, IndexError):
                self.rejected.pop(rejected_mint, None)
                continue
            if t_discover - rejected_ts >= 300:
                self.rejected.pop(rejected_mint, None)
        self.stats["gt_trades_made_this_pass"] = 0
        self.refresh_top_traders()
        if self.antirug:
            self.antirug_followups()

        pf_pools = self._pumpfun_to_pools()
        gmgn_pools = self._gmgn_to_pools()
        gt_pools = self.gt.new_pools(self.c["new_pool_pages"])

        all_pools = {}
        for p in pf_pools + gmgn_pools + gt_pools:
            mint = p.get("mint")
            if mint and mint not in all_pools:
                all_pools[mint] = p
        pools = list(all_pools.values())

        for p in pools:
            if p["quote_mint"] == SOL and p.get("quote_price"):
                self.meta["sol_price"] = p["quote_price"]; break
        newest = None
        for p in pools:
            if p["mint"] not in self.watch:
                self.stats["pools_seen"] += 1
                if p["created_ts"] and (newest is None or p["created_ts"] > newest["created_ts"]):
                    newest = p
                if p["quote_mint"] in self.allowed_quotes:
                    self.watch[p["mint"]] = p
        if pools:
            if newest:
                age = max(0, now() - newest["created_ts"])
                self.agent("stream", True, f"nuova coppia ${(newest['name'] or '?').split(' /')[0]} · {age / 60:.0f} min")
            else:
                self.agent("stream", True, None)
        else:
            self.agent("stream", False, "GeckoTerminal non risponde")
        max_age = self.c["max_pair_age_minutes"] * 60
        self.watch = {m: s for m, s in self.watch.items() if s["created_ts"] and now() - s["created_ts"] <= max_age}
        fresh = self.ds.tokens(list(self.watch)[-150:])
        if self.antirug:
            seen = self.__dict__.setdefault("_ar_liq_seen", {})
            t_fetch = now()
            for m, x in fresh.items():
                self.antirug.observe_liquidity(m, x.get("liquidity"))
                seen[m] = (t_fetch, x.get("liquidity"))
            if len(seen) > 3000:
                for k in list(seen)[:1500]:
                    seen.pop(k, None)
        for m, s in fresh.items():
            if m in self.watch and s["quote_mint"] in self.allowed_quotes:
                orig = self.watch[m]
                if s.get("price") and s["price"] > 0:
                    orig["price"] = s["price"]
                if s.get("liquidity") and s["liquidity"] > 0:
                    orig["liquidity"] = s["liquidity"]
                elif not orig.get("liquidity") or orig["liquidity"] <= 0:
                    orig["liquidity"] = 0
                if s.get("vol_m5") is not None:
                    orig["vol_m5"] = s["vol_m5"]
                if s.get("vol_h1") is not None:
                    orig["vol_h1"] = s["vol_h1"]
                if s.get("buys_m5") is not None:
                    orig["buys_m5"] = s["buys_m5"]
                if s.get("sells_m5") is not None:
                    orig["sells_m5"] = s["sells_m5"]
                if not orig.get("created_ts") and s.get("created_ts"):
                    orig["created_ts"] = s.get("created_ts")
                if s.get("fdv") is not None:
                    orig["fdv"] = s["fdv"]
                self.watch[m] = orig
        if fresh:
            top = max(fresh.values(), key=lambda s: s["vol_m5"])
            self.agent("trend", True, f"${top['name']} · vol 5m ${top['vol_m5']:,.0f} · {len(self.watch)} in lista",
                       load=100 * len(self.watch) / 150)
        if self.watch:
            best = max(self.watch.values(), key=lambda s: self.spike(s)[1])
            br = self.spike(best)[1]
            self.agent("volume", br >= self.c["spike_multiplier"], f"${best['name']} vol ×{br:.1f} / 5m",
                       load=100 * br / self.c["spike_multiplier"])
        self.manage_if_due()
        for m, s in list(self.watch.items()):
            if self.stop:
                break
            self.manage_if_due()
            self.beat()
            if m in self.positions:
                continue
            # A rejected mint is skipped for the remainder of its 5-minute cooldown.
            if self._recently_rejected(m):
                continue
            ok, ratio, age, liquidity_ok, late_entry, spike_meta = self.spike(s)
            if ok and self.reentry_block(m, "volume_spike"):
                ok = False
                self.stats["filtered_out"]["reentry_cooldown"] = self.stats["filtered_out"].get("reentry_cooldown", 0) + 1
            if ok:
                s["_signal_ts"] = now()
                self.stats["spike_candidates"] += 1
                self.stats["signals"] = self.stats.get("signals", 0) + 1
                hok, hinfo = self.holder_filter(s)
                if not hok:
                    # holder_filter records the temporary rejection; do not run whale/Vybe/AntiRug.
                    continue
                wok, wusd = self.whale_check(s)
                if not wok:
                    continue
                vok, vtop10 = self.vybe_confirm(s)
                if not vok:
                    continue
                vinfo = {"vybe_top10_pct": vtop10, "vol_ratio": ratio, "age_min": age, "vol_m5": s["vol_m5"],
                         "dev_pct": round(f(hinfo.get("dev_pct")), 2),
                         "top10_pct": round(hinfo["top10_pct"], 2), "whale_buy_usd": wusd,
                         "holder_src": hinfo["source"],
                         "spike_meta": spike_meta}
                if self.antirug:
                    aok, ainfo = self.antirug_gate(s, "volume_spike", hinfo, {"signal": dict(vinfo)})
                    if not aok:
                        # antirug_gate records the rejection; stop this token's pipeline immediately.
                        continue
                    vinfo.update(ainfo)
                self.buy(s, "volume_spike", vinfo)
                if self.antirug:
                    self._antirug_after_buy(m)
            elif self.top_traders and s["vol_m5"] >= self.c["min_volume_5m_usd"] / 2:
                self.stats["signals"] = self.stats.get("signals", 0) + 1
                hok, hinfo = self.holder_filter(s)
                if not hok:
                    # holder_filter records the temporary rejection; do not run whale/Vybe/AntiRug.
                    continue
                w_hold = set(hinfo.get("holder_wallets") or [])
                gt_budget = self.c.get("gt_trades_per_discovery_pass", 10)
                gt_calls_made = self.stats.get("gt_trades_made_this_pass", 0)
                if gt_calls_made < gt_budget:
                    w_gt = {t.get("tx_from_address") for t in (self.gt.trades(s["pool"]) or []) if t.get("kind") == "buy"}
                    self.stats["gt_trades_made_this_pass"] = gt_calls_made + 1
                else:
                    w_gt = set()
                wallets = w_hold | w_gt
                w_be = set()
                if self.birdeye_copy and (self.c.get("birdeye") or {}).get("token_traders", True):
                    w_be = self.birdeye.token_top_traders(m) or set()
                wallets |= w_be
                matched = wallets & self.top_traders
                n = len(matched)
                ok_copy = n >= self.c["min_top_traders_in"]
                self.agent("smart", ok_copy, f"{n} top trader in ${s['name']}")
                ainfo = None
                if ok_copy and self.antirug:
                    aok, ainfo = self.antirug_gate(s, "copy_top_traders", hinfo,
                                                   {"top_traders_in": n, "signal": {"top_traders_in": n, "age_min": age}})
                    if not aok:
                        continue
                if ok_copy:
                    s["_signal_ts"] = now()
                    self.stats["copy_candidates"] += 1
                    size = self.next_size() / self.meta.get("fx_start", 1)
                    self.agent("copy", True, f"copia €{size:.2f} · ${s['name']}")
                    cinfo = {"top_traders_in": n, "age_min": age, "top10_pct": round(hinfo["top10_pct"], 2)}
                    if self.birdeye_copy:
                        src = self.tt_sources or {}
                        cinfo["sources"] = {
                            "list": {"vybe": sum(1 for w in matched if "vybe" in src.get(w, ())),
                                     "birdeye": sum(1 for w in matched if "birdeye" in src.get(w, ()))},
                            "seen_in_token_via": {"holders": len(matched & w_hold), "gecko_trades": len(matched & w_gt),
                                                  "birdeye_token_traders": len(matched & w_be)},
                            "list_only_birdeye": sum(1 for w in matched if src.get(w) == ["birdeye"])}
                    if ainfo:
                        cinfo.update(ainfo)

                    for wallet in matched:
                        metrics = self.wallet_intelligence.get_wallet_metrics(wallet)
                        if metrics:
                            cinfo.setdefault("wallet_intelligence", {})[wallet] = {
                                "category": metrics.classification,
                                "score": metrics.score,
                                "verified_trades": metrics.verified_trades,
                                "profit_factor": metrics.profit_factor,
                                "expectancy_pct": metrics.expectancy_per_trade_pct,
                            }

                    wi_signal = None          # no wallet metrics / UNVERIFIED -> normal risk-approved size
                    primary_wallet = sorted(matched)[0] if matched else None
                    if primary_wallet:
                        token_price = s.get("price", 0)
                        token_liquidity = s.get("liquidity", 0)
                        metrics = self.wallet_intelligence.get_wallet_metrics(primary_wallet)
                        if metrics and metrics.classification != "UNVERIFIED":
                            wi_signal = self.wallet_intelligence.evaluate_copy_signal(
                                source_wallet=primary_wallet,
                                token_mint=s["mint"],
                                token_symbol=s["name"],
                                source_buy_timestamp=s.get("_signal_ts", now()),
                                source_buy_price_usd=token_price,
                                source_amount_usd=self.next_size(),
                                source_signature="",
                                token_age_minutes=age,
                                token_liquidity_usd=token_liquidity,
                                current_price_usd=token_price,
                                buy_price_impact_pct=None,
                                sell_route_available=True,
                                sell_price_impact_pct=None,
                                independent_wallet_count=n,
                                is_coordinated=False,
                                portfolio_capital_usd=self.equity_value(),
                                existing_exposure_usd=0.0,
                            )
                            cinfo["smart_money_signal"] = {
                                "wallet": primary_wallet,
                                "category": wi_signal.wallet_category,
                                "score": wi_signal.wallet_score,
                                "reliability": wi_signal.wallet_reliability,
                                "recommended_size_usd": wi_signal.recommended_size_usd,
                                "recommended_size_pct": wi_signal.recommended_size_pct_of_capital,
                                "is_late_entry": wi_signal.is_late_entry,
                                "rejection_reasons": wi_signal.rejection_reasons,
                            }
                            if wi_signal.rejection_reasons:
                                self._reject(m, "smart_money_rejected: " + "; ".join(wi_signal.rejection_reasons))
                                continue
                            # The recommended size remains USD for execution;
                            # display conversion, if needed, belongs only in logs/UI.
                        else:
                            cinfo["smart_money_signal"] = {
                                "wallet": primary_wallet,
                                "category": "UNVERIFIED",
                                "score": 0.0,
                                "reliability": 0.0,
                                "recommended_size_usd": 0.0,
                                "recommended_size_pct": 0.0,
                                "is_late_entry": False,
                                "rejection_reasons": ["No wallet intelligence data available"],
                            }
                    if ainfo:
                        cinfo.update(ainfo)
                    wi_size_usd = None
                    if wi_signal is not None and wi_signal.recommended_size_usd > 0:
                        wi_size_usd = wi_signal.recommended_size_usd
                    self.buy(
                        s, "copy_top_traders", cinfo, track=sorted(matched), hinfo=hinfo,
                        requested_size_usd=wi_size_usd
                    )
                    if self.antirug:
                        self._antirug_after_buy(m)
                else:
                    self._reject(m, "not_enough_top_traders")

    def manage(self):
        self.last_manage = now()
        if not self.positions:
            return
        snaps = self.ds.tokens(list(self.positions))
        feed_ok = getattr(self.ds, "last_ok", True)
        for m, p in list(self.positions.items()):
            if m not in self.positions:
                continue
            rules = self.exit_rules(p)
            max_hold = rules["max_hold_minutes"] * 60
            held = now() - p["entry_ts"]
            s = snaps.get(m)
            if not s or s["price"] <= 0:
                q = self.meta.setdefault("quality", {})
                key = "missing_price_feed_ok" if feed_ok else "missing_price_feed_down"
                q[key] = q.get(key, 0) + 1
                if not p.get("_missing_price_logged"):
                    p["_missing_price_logged"] = True
                    log.warning("%s: market price unavailable (feed_ok=%s); checking time/wallet exits via last mark",
                                p["name"], feed_ok)
                follow = rules["follow_wallets"] and p.get("track")
                adaptive_active = p.get("adaptive_holding_enabled", False) and self._adaptive_holding_enabled()
                if follow and self.check_wallets(p, rules):
                    self.try_sell(m, p["last"], "top_traders_sold", 0.0)
                elif held >= max_hold and not adaptive_active and p.get("entry_type") != "copy_top_traders":
                    self.try_sell(m, p["last"], "time_exit", 0.0)
                continue
            price = s["price"]
            p.pop("_outage_logged", None)
            p.pop("_missing_price_logged", None)

            self._update_adaptive_poll(m, price, p)

            jump = float(self._pi().get("max_tick_jump_x", 20) or 0)
            if jump and p["last"] and price / p["last"] >= jump:
                ok, implied, quote_available = self._confirm_tick(m, p, price)
                if ok:
                    p.pop("_pending_jump_px", None)
                    p.pop("_pending_jump_n", None)
                    log.info("%s: x%.0f price move confirmed by Jupiter (implied %.3e)", p["name"], price / p["last"],
                             implied)
                elif not quote_available:
                    # Network/API failure is not a negative price confirmation. Require
                    # two consecutive Dex observations within 10% before accepting the jump.
                    previous = p.get("_pending_jump_px")
                    consistent = previous and previous > 0 and 0.90 <= price / previous <= 1.10
                    p["_pending_jump_n"] = p.get("_pending_jump_n", 0) + 1 if consistent else 1
                    p["_pending_jump_px"] = price
                    if p["_pending_jump_n"] >= 2:
                        log.warning("%s: accepting x%.0f price jump after two consistent market observations; "
                                    "Jupiter unavailable", p["name"], price / p["last"])
                        p.pop("_pending_jump_px", None)
                        p.pop("_pending_jump_n", None)
                    else:
                        self._count_quality("price_ticks_pending_jupiter_unavailable")
                        price = p["last"]
                else:
                    self._count_quality("price_ticks_rejected")
                    p.pop("_pending_jump_px", None)
                    p.pop("_pending_jump_n", None)
                    if p.get("_rejected_px") != price:
                        p["_rejected_px"] = price
                        log.warning("%s %s: price tick %.3e is x%.0f the last mark %.3e and disagrees with "
                                    "Jupiter (implied %.3e): tick ignored", p["name"], m[:8], price,
                                    price / p["last"], p["last"], implied)
                    price = p["last"]
            jx = (self.c.get("trade_guards") or {}).get("confirm_price_jump_x")
            if p["last"] and (price / p["last"] >= 5 or price / p["last"] <= 0.2):
                q = self.meta.setdefault("quality", {})
                q["price_anomalies"] = q.get("price_anomalies", 0) + 1
                if jx and price / p["last"] >= jx and not p.get("_jump_seen"):
                    p["_jump_seen"] = price
                    continue
            p.pop("_jump_seen", None)
            tick_drop = max(0.0, 1 - price / p["last"]) if p["last"] else 0.0

            chg_pct = price / p["entry_mid"] - 1
            if chg_pct > p.get("mfe_pct", 0):
                p["mfe_pct"] = chg_pct
                p["mfe_ts"] = now()
            if chg_pct < p.get("mae_pct", 0):
                p["mae_pct"] = chg_pct
                p["mae_ts"] = now()
            if price > p.get("peak_price", 0):
                p["peak_price"] = price

            if self._handle_recovery_state(m, p, price, tick_drop):
                p["last"], p["peak"], p["last_price_ts"] = price, max(p["peak"], price), now()
                continue

            p["last"], p["peak"], p["last_price_ts"] = price, max(p["peak"], price), now()

            # Priority 0: Non-liquidatable monitoring (Punto 4: MAI chiusura fittizia; rivaluta periodicamente Jupiter)
            if p.get("non_liquidatable"):
                recheck_s = float((self.c.get("exit_slippage") or {}).get("non_liquidatable_recheck_seconds", 30.0))
                if now() >= float(p.get("_exit_next_retry_ts") or 0.0):
                    p["_exit_next_retry_ts"] = now() + recheck_s
                    # Single emergency evaluation inside try_sell's atomic lock.
                    # Its validated quote and authorized quantity are reused by sell().
                    self.try_sell(
                        m, price, "recovered_liquidity_exit", tick_drop,
                        force_emergency=True
                    )
                continue

            chg = price / p["entry_mid"] - 1
            follow = rules["follow_wallets"] and p.get("track")
            adaptive_active = p.get("adaptive_holding_enabled", False) and self._adaptive_holding_enabled()

            # Check if partial TP is enabled for this position
            ptp_enabled = rules.get("partial_tp_enabled", False)
            tp1_pct = rules.get("tp1_pct", 0.25)
            tp1_sell_fraction = rules.get("tp1_sell_fraction", 0.50)
            move_sl_to_breakeven = rules.get("move_sl_to_breakeven", True)
            tp2_pct = rules.get("tp2_pct", 0.60)

            # Priority 1: Standard stop loss
            if chg <= -rules["stop_loss_pct"]:
                self.try_sell(m, price, "stop_loss", tick_drop)
                continue
            # Priority 2: TP1 - partial take profit
            if ptp_enabled and not p.get("tp1_hit", False) and chg >= tp1_pct:
                # Calculate quantity to sell (fraction of original quantity)
                orig_qty = p.get("original_qty", p["qty"])
                sell_qty = orig_qty * tp1_sell_fraction
                # Don't sell more than we have
                sell_qty = min(sell_qty, p["qty"])
                if sell_qty > 0:
                    self.try_sell(m, price, "take_profit_1", tick_drop, sell_qty)
                continue
            # Priority 3: Breakeven stop (if TP1 hit and breakeven activated)
            if ptp_enabled and p.get("breakeven_activated", False) and price <= p.get("breakeven_price", 0):
                self.try_sell(m, price, "breakeven_stop", tick_drop)
                continue
            # Priority 4: Trailing stop (use tighter of breakeven or trailing)
            if (rules["trailing_stop_pct"] and p["peak"] / p["entry_mid"] - 1 >= rules["trailing_activate_pct"]):
                trailing_stop_price = p["peak"] * (1 - rules["trailing_stop_pct"])
                # Use the tighter (higher) stop price
                effective_stop = trailing_stop_price
                if ptp_enabled and p.get("breakeven_activated", False):
                    effective_stop = max(trailing_stop_price, p["breakeven_price"])
                if price <= effective_stop:
                    self.try_sell(m, price, "trailing_stop", tick_drop)
                    continue
            # Priority 5: TP2 - final take profit
            if ptp_enabled and p.get("tp1_hit", False) and not p.get("tp2_done", False) and chg >= tp2_pct:
                self.try_sell(m, price, "take_profit_2", tick_drop)
                continue
            # Priority 6: Wallet follow exit (copy_top_traders)
            if follow and self.check_wallets(p, rules):
                self.try_sell(m, price, "top_traders_sold", tick_drop)
                continue
            # Priority 7: Adaptive holding (time is a milestone trigger, never an autonomous sell)
            if adaptive_active and now() >= p.get("adaptive_holding_next_check", 0):
                s = snaps.get(m)
                if s and s.get("price", 0) > 0:
                    decision, reason, score, indicators, baseline_quote = self._evaluate_adaptive_holding(
                        m, p, price, s
                    )
                    p.pop("adaptive_needs_immediate_eval", None)
                    if decision == "CLOSE":
                        self.try_sell(m, price, f"adaptive_holding_{reason}", tick_drop)
                    elif decision.startswith("HOLD_STAGE_"):
                        p["adaptive_holding_stage"] = int(p.get("adaptive_holding_stage", 0)) + 1
                        p["adaptive_holding_extensions"] = int(p.get("adaptive_holding_extensions", 0)) + 1
                        p["adaptive_holding_next_check"] = self._next_adaptive_check_ts(p["entry_ts"])
                        p["adaptive_holding_last_decision"] = decision
                        p["adaptive_holding_last_reason"] = reason
                        if baseline_quote and p.get("adaptive_holding_baseline_quote") is None:
                            p["adaptive_holding_baseline_quote"] = baseline_quote
                        log.info("ADAPTIVE HOLDING [%s] %s: %s (score=%.1f) reason=%s next_check=%ds",
                                 p["entry_type"], p["name"], decision, score, reason,
                                 max(0, int(p["adaptive_holding_next_check"] - now())))
                        self.save_state()
            elif held >= max_hold and not adaptive_active and p.get("entry_type") != "copy_top_traders":
                self.try_sell(m, price, "time_exit", tick_drop)

    # ---- reporting
    def _completed_trade_rows(self, rows=None):
        rows = self.closed if rows is None else rows
        # Backward-compatible: old TP1 rows predate counts_as_trade.
        return [t for t in rows if t.get("counts_as_trade", not (t.get("partial") or t.get("exit_reason") == "take_profit_1")) is not False]

    @staticmethod
    def _logical_trade_pnl(trade):
        return float(trade.get("cumulative_pnl_usd", trade.get("pnl_usd", 0.0)) or 0.0)

    def report(self, period_hours=None):
        executions = self.closed
        completed = self._completed_trade_rows(executions)
        wins = [t for t in completed if self._logical_trade_pnl(t) > 0]
        by_reason = {}
        for t in executions:
            by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
        s = {"equity": self.equity_value(), "start": self.c["starting_balance_usd"], "n": len(completed), "wins": len(wins),
             "win_rate": 100 * len(wins) / len(completed) if completed else None,
             "best": max(completed, key=self._logical_trade_pnl) if completed else None,
             "worst": min(completed, key=self._logical_trade_pnl) if completed else None,
             "fees": sum(t["dex_fees_usd"] + t["network_fees_usd"] for t in executions),
             "avg_slip": (sum(t["buy_slip_pct"] + t["sell_slip_pct"] for t in executions) / (2 * len(executions))) if executions else 0,
             "by_reason": by_reason, "open": len(self.positions)}
        if period_hours:
            cut = now() - period_hours * 3600
            rec = [t for t in executions if t.get("exit_ts", 0) >= cut]
            period_completed = self._completed_trade_rows(rec)
            s["period_n"], s["period_pnl"] = len(period_completed), sum(t["pnl_usd"] for t in rec)
        return s

    def maybe_daily_summary(self):
        hour = self.c.get("daily_summary_hour")
        if hour is None:
            return
        local = datetime.now(self.tz) if self.tz else datetime.now()
        today = local.strftime("%Y-%m-%d")
        if local.hour >= int(hour) and self.meta.get("last_daily_summary") != today:
            self.meta["last_daily_summary"] = today
            self.tg.send(tg.msg_daily(self.report(24), "giornaliero " + local.strftime("%d/%m %H:%M")))

    def notify_risk(self, ev):
        try:
            self.tg.send(tg.msg_risk(ev, self.risk.mode), key=f"risk:{ev.get('kind')}:{ev.get('reason', ev.get('step'))}:{int(ev.get('ts', 0)) // 60}")
        except Exception as e:
            log.warning("risk notification failed: %s", e)

    # ------------------------------------------------------------------ hourly dashboard screenshot
    def screenshot_active(self):
        tgo = getattr(self, "tg", None)
        return bool(self.shot.get("hourly_screenshot")) and tgo is not None and tgo.enabled and not tgo.dry

    def request_screenshot(self):
        self._shot_req.set()

    def _shot_texts(self):
        eq, cash, start = self.equity_value(), self.cash, self.c["starting_balance_usd"]
        pnl = eq - start
        line = (f"💼 Valore {tg.money(eq)} · cassa {tg.money(cash)} · P&L {tg.money(pnl, sign=True)} "
                f"({'+' if pnl >= 0 else ''}{100 * pnl / start:.1f}%)\n"
                f"📂 Posizioni aperte {len(self.positions)} · trade completati {len(self._completed_trade_rows())}")
        hhmm = datetime.fromtimestamp(now(), self.tz).strftime("%H:%M") if self.tz else time.strftime("%H:%M")
        head = tg.bot_header() if hasattr(tg, "bot_header") else "Paper bot"
        return f"📸 <b>{head}</b> · dashboard {hhmm}\n{line}", f"📊 <b>{head}</b> · riepilogo {hhmm}\n{line}"

    def screenshot_if_due(self):
        if not self.screenshot_active():
            self._shot_req.clear()
            return
        if self._shot_thread and self._shot_thread.is_alive():
            return
        t = now()
        last = self.meta.get("last_screenshot_ts") or 0
        due = max(last + 60 * float(self.shot["screenshot_interval_minutes"]),
                  self.session_start + float(self.shot["screenshot_first_delay_s"]))
        manual = self._shot_req.is_set()
        if not manual and t < due:
            return
        self._shot_req.clear()
        self.meta["last_screenshot_ts"] = t
        caption, summary = self._shot_texts()
        secrets = [os.getenv("DASHBOARD_PASSWORD", ""), os.getenv("TELEGRAM_BOT_TOKEN", "")]
        path = os.path.join(self.data, "dashboard_screenshot.png")

        def work():
            try:
                ok, why = screenshot.capture(self.shot, path)
                if ok:
                    sent, why = self.tg.send_photo(path, caption)
                    if sent:
                        self.shot_status.update(sent=self.shot_status["sent"] + 1, last_ok=now(), last_error=None)
                        log.info("Dashboard screenshot sent to Telegram (sendPhoto ok, %d KB%s)",
                                 os.path.getsize(path) // 1024, ", manual" if manual else "")
                        return
                    why = "invio foto fallito: " + why
                why = screenshot.safe_text(why, secrets)
                self.shot_status.update(fallback=self.shot_status["fallback"] + 1, last_error=why)
                log.warning("Dashboard screenshot not available (%s): text summary sent instead", why)
                self.tg.send(summary + f"\n<i>(screenshot non disponibile: {tg.html.escape(why)})</i>")
            except Exception as e:
                log.warning("screenshot worker error: %s", type(e).__name__)
        self._shot_thread = threading.Thread(target=work, name="screenshot", daemon=True)
        self._shot_thread.start()

    def housekeeping(self, force=False):
        try:
            self.screenshot_if_due()
        except Exception as e:
            log.warning("screenshot scheduling error: %s", type(e).__name__)
        dg = getattr(self, "disk_guard", None)
        if dg and not getattr(self, "_disk_thread_busy", False) and now() - dg.last_check >= dg.s["check_minutes"] * 60:
            def _dg():
                try:
                    dg.check()
                except Exception as e:
                    log.warning("disk guard error: %s", e)
                finally:
                    self._disk_thread_busy = False
            self._disk_thread_busy = True
            threading.Thread(target=_dg, name="disk-guard", daemon=True).start()
        o = self.ops
        hc = self.c.get("health") or {}
        t = now()
        if force or t - o["last_health"] >= 30:
            o["last_health"] = t
            try:
                after = hc.get("api_alert_after_s", 300)
                down = self.http.down_hosts(after)
                for host, h in down.items():
                    if host not in o["api_alerted"]:
                        o["api_alerted"][host] = t
                        self.tg.send(tg.msg_api_down(host, h.down_since, h.last_kind, h.consec), key="apidown:" + host)
                for host in list(o["api_alerted"]):
                    if host not in down:
                        hh = self.http.health.get(host)
                        if hh and hh.consec == 0:
                            since = o["api_alerted"].pop(host)
                            self.tg.send(tg.msg_api_up(host, t - since), key="apiup:" + host)
            except Exception as e:
                log.warning("health check error: %s", e)
            try:
                self.risk.tick(self.equity_value(), o.get("disk", {}).get("level"))
                for ev in self.risk.pop_events():
                    self.notify_risk(ev)
            except Exception as e:
                log.warning("risk tick error: %s", e)
        if force or t - o["last_disk"] >= hc.get("disk_check_seconds", 300):
            o["last_disk"] = t
            try:
                ds = disk_status(self.data, hc.get("disk_warn_mb", 300), hc.get("disk_critical_mb", 100))
                prev = (o.get("disk") or {}).get("level", "ok")
                o["disk"] = ds
                if ds["level"] in ("warn", "critical") and ds["level"] != prev:
                    self.tg.send(tg.msg_disk(ds), key="disk:" + ds["level"])
                self.tg.outlog.enabled = ds["level"] == "ok"
            except Exception as e:
                log.warning("disk check error: %s", e)
        bc = self.c.get("backup") or {}
        if bc.get("enabled", True) and (force or t - o["last_backup"] >= bc.get("interval_minutes", 60) * 60):
            o["last_backup"] = t
            self.save_state("dead" if self.meta.get("dead") else "running")
            r = self.backups.run()
            if r["ok"]:
                log.info("backup ok: %s (%d file, %d vecchi rimossi, %d in archivio)", r["dir"], r["files"], r["removed"], r["count"])
                day = datetime.now(self.tz).strftime("%Y-%m-%d") if self.tz else datetime.now().strftime("%Y-%m-%d")
                if bc.get("notify", "daily") == "every" or (bc.get("notify", "daily") == "daily" and o["backup_day_notified"] != day):
                    o["backup_day_notified"] = day
                    self.tg.send(tg.msg_backup(r, bc.get("interval_minutes", 60)), key="backup:" + day)
            else:
                o["backup_errors"] = o.get("backup_errors", 0) + 1
                # Retry in about one minute instead of waiting a full backup interval.
                o["last_backup"] = t - max(0, bc.get("interval_minutes", 60) * 60 - 60)
                log.error("backup snapshot failed (previous backups retained): %s", r.get("error", "?"))
                self.tg.send(tg.msg_backup_failed(r.get("error", "?")), key="backupfail:" + str(int(t // 3600)))
        url = os.getenv("DASHBOARD_HEALTH_URL", "").strip()
        if url and (force or t - o["last_dash"] >= hc.get("dashboard_check_seconds", 120)):
            o["last_dash"] = t
            r = self.http.request_ex(url, tries=1, deadline=8)
            okd = r.ok and isinstance(r.data, dict) and r.data.get("dashboard") == "ok"
            if okd:
                if o["dash_alerted"]:
                    self.tg.send(tg.msg_dashboard(True, t - (o["dash_fail_since"] or t)), key="dashup")
                o["dash_fail_since"], o["dash_alerted"] = None, False
            else:
                o["dash_fail_since"] = o["dash_fail_since"] or t
                if not o["dash_alerted"] and t - o["dash_fail_since"] >= hc.get("dashboard_alert_after_s", 300):
                    o["dash_alerted"] = True
                    self.tg.send(tg.msg_dashboard(False, t - o["dash_fail_since"], r.kind), key="dashdown")

    def loop_error(self, e):
        t = now()
        errs = [x for x in self.ops["loop_errors"] if t - x[0] < 600] + [(t, f"{type(e).__name__}: {str(e)[:120]}")]
        self.ops["loop_errors"] = errs[-50:]
        hc = self.c.get("health") or {}
        if len(errs) >= hc.get("persistent_errors_n", 5) and t - self.ops["last_err_alert"] > 3600:
            self.ops["last_err_alert"] = t
            self.tg.send(tg.msg_persistent_errors(len(errs), errs[-1][1]), key="errs:" + str(int(t // 3600)))

    # ---- watchdog
    def start_watchdog(self):
        limit = self.c.get("watchdog_seconds", 240)

        def emergency_exit(stalled):
            result = {"saved": False}
            def work():
                result["saved"] = self._save_state_with_retry("restarting", attempts=2, lock_timeout=5.0)
                if not result["saved"]:
                    log.critical("watchdog restart proceeding with last known-good state; latest in-memory changes may be lost")
                try:
                    self.tg.send(tg.msg_watchdog(stalled))
                    self.tg.persist_pending()
                    self.tg.flush(8)
                except Exception:
                    pass
            th = threading.Thread(target=work, name="watchdog-exit", daemon=True)
            th.start()
            th.join(15)
            try:
                for hd in logging.getLogger().handlers:
                    hd.flush()
            except Exception:
                pass
            os._exit(3 if result["saved"] else 4)

        def run():
            last_wall = now()
            while True:
                time.sleep(10)
                gap = now() - last_wall
                last_wall = now()
                if gap > 60:
                    log.warning("clock jumped %.0fs (host suspended?)", gap)
                    self.meta["stalls"].append({"from": now() - gap, "to": now(), "sec": round(gap), "kind": "host_pause"})
                    del self.meta["stalls"][:-200]
                    self.beat()
                    continue
                stalled = now() - self.heartbeat
                if stalled > limit and not self.stop:
                    log.critical("WATCHDOG: main loop stalled for %.0fs - saving state and exiting for restart", stalled)
                    self.meta["watchdog_restarts"] = self.meta.get("watchdog_restarts", 0) + 1
                    self.meta["stalls"].append({"from": self.heartbeat, "to": now(), "sec": round(stalled), "kind": "watchdog"})
                    emergency_exit(stalled)
                if self.stop and self._stop_ts and now() - self._stop_ts > self.c.get("shutdown_timeout_s", 25):
                    log.critical("shutdown took more than %ss - forcing exit", self.c.get("shutdown_timeout_s", 25))
                    try:
                        if not self.final_saved:
                            self.final_saved = self._save_state_with_retry("stopped", attempts=2, lock_timeout=3.0)
                            self.tg.persist_pending()
                    finally:
                        os._exit(0 if self.final_saved else 4)
        threading.Thread(target=run, name="watchdog", daemon=True).start()

    def request_stop(self, reason):
        if self.stop:
            log.warning("second stop signal: forced exit")
            try:
                saved = self._save_state_with_retry("stopped", attempts=2, lock_timeout=3.0)
                self.tg.persist_pending()
            finally:
                os._exit(130 if locals().get("saved", False) else 4)
        self.stop_reason, self.stop, self._stop_ts = reason, True, now()
        self.http.cancel()
        sd_notify("STOPPING=1")

    def _loop_tick(self):
        self.beat()
        if now() - self.ops["last_hb_write"] >= 5:
            self.write_heartbeat()
            sd_notify("WATCHDOG=1")

    def run(self):
        log.info("PAPER TRADING ONLY - no wallet, no keys, no real orders. Start balance EUR %.2f (= $%.2f at EUR/USD %.4f)",
                 self.c["starting_balance_eur"], self.c["starting_balance_usd"], self.meta.get("fx_start", 0))
        log.info("Risk manager mode: %s %s", self.risk.mode, self.risk.limits())
        if not self.vybe:
            log.warning("VYBE_API_KEY not set: copy-top-traders entry DISABLED; holder filters use RugCheck public API.")
        else:
            log.info("Vybe key found. Est. credits used this month: %s", self.vybe.state["month_credits"])
        self.save_state("starting")
        self.tg.send(tg.msg_start(self.cash, self.c["starting_balance_usd"], len(self.positions), self.resumed,
                                  bool(self.vybe) or self.birdeye_copy, self.c, self.exit_rules("copy_top_traders"),
                                  bool(self.jup), copy_src=("Vybe" if not self.birdeye_copy else
                                                            "Vybe + Birdeye" if self.vybe else "Birdeye"),
                                  antirug=self.antirug_mode if self.antirug else False,
                                  **({"antirug_enforce": self.antirug_enforce} if self.antirug and self.antirug.enforce
                                     else {})))
        self._antirug_mode_notice()
        if getattr(self, "_brain_startup_change", None):
            c = self._brain_startup_change
            self.tg.send(f"🧠 <b>Cervello</b> — nuovo limite all'entrata\n{tg.html.escape(c['reason'])}\n"
                         f"<b>Parametro cambiato:</b> {tg.html.escape(c['text'])}\n"
                         "<i>Simulazione: cambia solo i filtri del paper bot.</i>", key="brain:slip_init")
        if RESET_NOTE:
            self.tg.send(tg.msg_reset(RESET_NOTE), key="reset:" + str(RESET_NOTE.get("kind")))
        if self.recovered:
            self.tg.send(tg.msg_recovered(self.recovered, self.cash, len(self.positions), self.meta.get("restarts", 0)),
                         key="recovered:" + str(int(self.session_start)))
        self.start_watchdog()
        self.cmds.start()
        self.pumpfun.start()
        self.gmgn.start()
        self.mark_equity(force=True)
        sd_notify("READY=1")
        cycle_budget = self.c.get("api_cycle_budget_seconds", 12.0)
        self.http.begin_cycle_budget(cycle_budget)
        self.rpc.begin_cycle_budget(cycle_budget)
        try:
            self.manage()
        except Exception as e:
            log.exception("initial manage error: %s", e)
        finally:
            self.http.end_cycle_budget()
            self.rpc.end_cycle_budget()
        self.check_death()
        self.save_state("dead" if self.meta.get("dead") else "running")
        end = now() + self.c["run_minutes"] * 60 if self.c["run_minutes"] else float("inf")
        last_disc = 0
        save_every = self.c.get("state_save_seconds", 15)
        while not self.stop and now() < end:
            loop_start = now()
            self._loop_tick()
            if self.meta.get("dead"):
                self.cmds.run_actions()
                try:
                    self.housekeeping()
                except Exception as e:
                    log.exception("housekeeping error: %s", e)
                if now() - self.last_save >= 30:
                    self.save_state("dead")
                time.sleep(2)
                continue
            # All sequential provider calls in this trading pass share one deadline.
            # This prevents outages across multiple APIs from multiplying their
            # individual timeouts and delaying open-position management.
            cycle_budget = self.c.get("api_cycle_budget_seconds", 12.0)
            self.http.begin_cycle_budget(cycle_budget)
            self.rpc.begin_cycle_budget(cycle_budget)
            try:
                self.manage_if_due()
                if now() - last_disc >= self.c["poll_seconds"]:
                    last_disc = now()
                    self.discover()
                    log.info("watch=%d open=%d closed=%d cash=$%.2f equity=$%.2f ops/min=%d", len(self.watch),
                             len(self.positions), len(self._completed_trade_rows()), self.cash, self.equity_value(), self.http.ops_per_min())
                self.mark_equity()
                self.maybe_daily_summary()
            except Exception as e:
                log.exception("loop error: %s", e)
                self.loop_error(e)
            finally:
                self.http.end_cycle_budget()
                self.rpc.end_cycle_budget()
            try:
                self.housekeeping()
            except Exception as e:
                log.exception("housekeeping error: %s", e)
            self._loop_tick()
            if now() - self.last_save >= save_every:
                self.save_state("dead" if self.meta.get("dead") else "running")
            nap = max(1.0, self.c["price_poll_seconds"] - (now() - loop_start))
            t_end = now() + nap
            while now() < t_end and not self.stop:
                time.sleep(0.5)
                if now() - self.ops["last_hb_write"] >= 5:
                    self._loop_tick()
        self.shutdown(end)

    def shutdown(self, end=float("inf")):
        finite_run = bool(self.c["run_minutes"]) and now() >= end
        if not self.reset_pending and (finite_run or self.c.get("close_positions_on_stop")):
            snaps = self.ds.tokens(list(self.positions)) if self.positions else {}
            for m in list(self.positions):
                if m not in self.positions:
                    continue
                px = snaps.get(m, {}).get("price") or self.positions[m]["last"]
                self.try_sell(m, px, "end_of_run", 0.0)
            self.tg.send(tg.msg_daily(self.report(), "fine sessione"))
        reason = "durata impostata terminata" if finite_run else self.stop_reason
        save_status = "dead" if self.meta.get("dead") else "stopped"
        self.final_saved = self._save_state_with_retry(save_status, attempts=2, lock_timeout=3.0)
        if not self.final_saved:
            log.critical("shutdown completed without a confirmed final state save; process must exit non-zero")
        try:
            self.pumpfun.stop()
        except Exception:
            pass
        try:
            self.gmgn.stop()
        except Exception:
            pass
        self.write_heartbeat()
        self.tg.send(tg.msg_stop(self.cash, self.c["starting_balance_usd"], len(self.positions), reason))
        delivered = self.tg.flush(self.c.get("shutdown_flush_s", 12))
        self.tg.persist_pending()
        log.info("Stopped (%s). cash=$%.2f open=%d closed=%d telegram_flushed=%s", reason, self.cash,
                 len(self.positions), len(self._completed_trade_rows()), delivered)


def load_dotenv(path):
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


RESET_MARK = "reset.done"


def seed_brain_rules(data_dir):
    path = os.environ.get("BRAIN_SEED_FILE", "").strip()
    dst = os.path.join(data_dir, "brain.json")
    if not path or os.path.exists(dst):
        return False
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    try:
        seed = json.load(open(path))
        params, probs = validate_seed(seed)
        for pr in probs:
            log.warning("brain seed: %s", pr)
        st = {"records": [], "analyses": [], "changes": list(seed.get("changes") or [])[-300:], "params": params,
              "last_n": 0, "total_n": 0, "diagnosis": "Regole apprese caricate dal seed (%s): %s" % (
                  seed.get("created", "?"), ", ".join("%s=%s" % kv for kv in sorted(params.items()))),
              "pending": None, "seeded_from": seed.get("created", "?"), "seed_problems": probs,
              "seed_source_trades": seed.get("source_trades")}
        if "max_entry_slippage_pct" in params:
            st["slip_init_done"] = time.time()
        atomic_json(dst, st)
        log.info("Brain seeded from %s: %s", os.path.basename(path), sorted(params))
        return True
    except Exception as e:
        log.warning("brain seed %s not loaded: %s", path, e)
        return False


RESET_NOTE = {}


def reset_requested(data_dir, value):
    if not value or value.lower() in ("0", "false", "no", "off"):
        return False
    try:
        done = json.load(open(os.path.join(data_dir, RESET_MARK)))
    except Exception:
        done = {}
    if done.get("value") == value and (os.path.exists(os.path.join(data_dir, "state.json")) or done.get("source") == "telegram"):
        RESET_NOTE.update(kind="ignored", value=value, when=done.get("ts"))
        log.warning("RESET_STATE=%s is still set but was already applied on %s: IGNORED, resuming the saved state. "
                    "Set RESET_STATE=0 in .env (or another value to reset again).", value,
                    datetime.fromtimestamp(done.get("ts", 0)).strftime("%d/%m %H:%M"))
        return False
    return True


def archive_for_reset(data_dir, value):
    sp = os.path.join(data_dir, "state.json")
    arch = None
    if os.path.exists(sp):
        arch = sp + f".{int(now())}.reset"
        os.replace(sp, arch)
        for extra in (".bak",):
            if os.path.exists(sp + extra):
                os.replace(sp + extra, arch + extra)
        for f_path in (arch, arch + ".bak"):
            try:
                if os.path.exists(f_path):
                    os.chmod(f_path, 0o444)
            except OSError:
                pass
    hb = os.path.join(data_dir, "heartbeat.json")
    if os.path.exists(hb):
        os.remove(hb)
    if value:
        atomic_json(os.path.join(data_dir, RESET_MARK), {"value": value, "ts": now(), "archived": arch})
    RESET_NOTE.update(kind="reset", value=value or "--reset", archived=os.path.basename(arch) if arch else None)
    log.warning("RESET: starting a NEW simulation from the start balance; previous state archived as %s", arch or "(none)")


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))
    ap = argparse.ArgumentParser(description="Solana memecoin paper-trading bot (simulation only)")
    ap.add_argument("--config", default=os.path.join(root, "bot", "config.yaml"))
    ap.add_argument("--minutes", type=float, help="override run_minutes (0 = forever)")
    ap.add_argument("--data-dir", default=os.getenv("DATA_DIR", os.path.join(root, "data")))
    ap.add_argument("--reset", action="store_true", help="ignore saved state and start from the start balance")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    if a.minutes is not None:
        cfg["run_minutes"] = a.minutes
    os.makedirs(a.data_dir, exist_ok=True)
    reset_env = os.getenv("RESET_STATE", "").strip()
    fh = RotatingFileHandler(os.path.join(a.data_dir, "bot.log"), maxBytes=5_000_000, backupCount=5)
    handlers = [fh]
    if os.getenv("LOG_STDOUT", "1").lower() not in ("0", "false", "no"):
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.CRITICAL)
    lock = InstanceLock(a.data_dir)
    if not lock.acquire():
        log.critical("another paper bot is already running on %s (bot.lock busy): exiting", a.data_dir)
        sys.exit(2)
    pending_reset = os.path.join(a.data_dir, "reset.request.json")
    if os.path.exists(pending_reset):
        try:
            backups = reset_manager.create_reset_backups(root, a.data_dir)
            reset_manager.archive_and_clear_data(a.data_dir)
            log.warning("Recovered interrupted Telegram reset; backups=%s", backups["directory"])
        except Exception:
            log.exception("Pending Telegram reset could not be backed up; refusing to trade")
            lock.release()
            sys.exit(5)
    a.reset = a.reset or reset_requested(a.data_dir, reset_env)
    if a.reset:
        archive_for_reset(a.data_dir, reset_env)
    bot = PaperBot(cfg, a.data_dir, reset=a.reset)
    if os.getenv("STARTUP_CHECKS", "").strip().lower() in ("1", "true", "yes", "on"):
        import startup_checks
        res = startup_checks.run(bot)
        for c in res["checks"]:
            (log.critical if c["critical"] else log.warning if c["status"] != "OK" else log.info)(
                "STARTUP CHECK %s %s: %s - %s", c["n"], c["name"], c["status"], c["detail"])
        bot.tg.send(startup_checks.message(res), key="startup:checks")
        try:
            atomic_json(os.path.join(a.data_dir, "startup_checks.json"), res)
        except Exception:
            pass
        if res["critical"]:
            log.critical("REFUSING TO START: critical configuration invalid (%s)",
                         ", ".join(c["name"] for c in res["critical"]))
            bot.tg.flush(15) if hasattr(bot.tg, "flush") else time.sleep(5)
            lock.release()
            sys.exit(startup_checks.EX_CONFIG)

    def _stop(sig, _frm):
        bot.request_stop("arresto manuale" if sig == signal.SIGINT else "arresto/riavvio del servizio")
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        bot.run()
    finally:
        lock.release()
    if not bot.final_saved:
        log.critical("refusing post-shutdown reset/cleanup because final state was not persisted")
        sys.exit(4)
    reset_request = os.path.join(a.data_dir, "reset.request.json")
    if os.path.exists(reset_request):
        try:
            backups = reset_manager.create_reset_backups(root, a.data_dir)
            archived = reset_manager.archive_and_clear_data(a.data_dir)
            log.warning("TELEGRAM RESET COMPLETE: backups=%s data_archived=%s", backups["directory"], archived)
            try:
                bot.tg.send("♻️ <b>Reset completato</b> — backup del progetto e dei dati creati. "
                            "La prossima sessione ripartirà dal saldo iniziale configurato. Solo paper trading.",
                            key="reset:completed:" + os.path.basename(backups["directory"]))
                bot.tg.flush(5)
            except Exception:
                pass
        except Exception as exc:
            log.exception("TELEGRAM RESET FAILED: backup/clear aborted")
            try:
                bot.tg.send("🚨 <b>Reset NON completato</b>: backup fallito. I dati originali sono stati mantenuti. "
                            "Controllare i log prima di riprovare.", key="reset:failed")
                bot.tg.flush(5)
            except Exception:
                pass
        sys.exit(5)


if __name__ == "__main__":
    main()
