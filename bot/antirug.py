"""AntiRug Security Engine - independent pre-entry security gate (PAPER TRADING ONLY; spec: SPEC_ANTIRUG.txt).

Pipeline: 8 agents -> Grok Core (buy candidate) -> **AntiRug Security Gate** -> Risk Manager -> paper execution.
Modes (env ANTIRUG_MODE, default OFF; switching always needs explicit configuration):
 OFF the engine is not built: no extra API call, no state key, behaviour byte-identical to before.
 SHADOW every buy candidate is analysed and recorded (what it would have blocked, why, scores, checks passed /
 failed / unverified); NOTHING is blocked. Prices (and liquidity) followed at +1/5/15/60 min.
 ACTIVE blocks candidates over the risk thresholds. FAIL-CLOSED: if a mandatory check cannot be verified the buy is
 not authorised. An API error is NEVER a passed check.

RISK score 0-100 (0 = low risk, 100 = critical), per category and overall:
 0-19 basso · 20-39 moderato · 40-59 elevato · 60-79 molto elevato · 80-100 critico. Missing data -> UNKNOWN (never
 an artificially low score). HARD BLOCKS (absolute) always win over the score.
Categories (managed separately, as required by the spec):
 technical RugCheck: mint / freeze authority, dangerous token features, LP locked %, RugCheck risks
 liquidity current / usable liquidity, change over 1-5-15 min, outflow rate, source consistency, pool depth vs
 simulated size, Jupiter buy price impact
 concentration Birdeye /defi/v3/token/holder (mode=wallet: economic owners, not token accounts), top 5 / top 10,
 burn / LP / technical / program / official accounts excluded, uncertainty flagged
 sellability Jupiter QUOTES only: buy, then sell of the bought quantity to USDC (fallback SOL); four distinct
 metrics: slippage tolerance, price impact, estimated slippage, executed (simulated) slippage
 behavioral wallet intelligence: same funding source, simultaneous first buys, early-buy concentration,
 recent large-holder moves, simultaneous sells (weak), insiders, dev holding - every finding has
 evidence, source, timestamp, confidence
Keys: the Birdeye key lives only inside the Birdeye client header; nothing here logs or stores a key."""
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

try:
    from birdeye import RateLimiter
except Exception:  # pragma: no cover
    RateLimiter = None

log = logging.getLogger("paperbot.antirug")

MODES = ("off", "shadow", "active")
CATEGORIES = ("technical", "liquidity", "concentration", "sellability", "behavioral")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
BURN_OWNERS = {"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"}
RC = "https://api.rugcheck.xyz/v1"
# MANDATORY checks (fixed, not configurable: they can only be made stricter through the thresholds).
# ACTIVE: a FAIL or an UNKNOWN on ANY of them prevents the buy. SHADOW: recorded only. OFF: never run.
MANDATORY = (
    "authorities.mint",
    "authorities.freeze",
    "liquidity.minimum",
    "holders.top10",
    "sellability.buy_quote",
    "sellability.sell_route",
    "sellability.price_impact",
    "sellability.quoted_slippage",
    "sellability.quote_fresh",
)
_B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
LEVELS = ((80, "critico"), (60, "molto elevato"), (40, "elevato"), (20, "moderato"), (0, "basso"))

DEFAULTS = {
    "block_risk_score": 60,  # overall risk >= this -> BLOCK ("molto elevato" or worse)
    "weights": {"technical": 0.25, "liquidity": 0.20, "concentration": 0.20, "sellability": 0.25, "behavioral": 0.10},
    "concentration": {
        "limit": 20,
        "wallet_mode": True,
        "holder_infos": True,
        "top10_high_pct": 25,
        "top10_block_pct": 40,
        "top5_high_pct": 25,
        "single_high_pct": 15,
        "uncertain_single_pct": 10,
        "program_owners": [
            "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
            "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
        ],
        "official_wallets": [],  # known team / treasury / CEX wallets to exclude (manual list)
        "excluded_known_types": ["AMM", "LOCKER"],  # RugCheck knownAccounts types treated as non-people
    },
    "liquidity": {
        "min_usd": 10000,
        "good_usd": 50000,
        "drop_block_1m_pct": 20,
        "drop_block_5m_pct": 30,
        "drop_block_15m_pct": 50,
        "inconsistent_ratio": 3.0,
        "max_size_to_liquidity_pct": 2.0,
        "max_buy_impact_pct": 10,
        "birdeye_history": True,
        "max_age_s": 120,  # liquidity value older than this -> UNKNOWN (stale)
        "history_max_age_s": 300,  # Birdeye exit-liquidity point older than this is not used
    },
    "sellability": {
        "max_sell_slippage_pct": 10,
        "max_sell_impact_pct": 10,
        "max_route_hops": 3,
        "max_quote_age_s": 15,
    },
    "technical": {
        "lp_unlocked_below_pct": 50,
        "danger_risk_points": 25,
        "warn_risk_points": 8,
        "token2022_points": 15,
        "dangerous_feature_keywords": [
            "mint authority",
            "freeze authority",
            "transfer fee",
            "permanent delegate",
            "transfer hook",
            "non-transferable",
            "default account state",
            "close authority",
        ],
    },
    "behavioral": {
        "same_funder_min": 2,
        "simultaneous_window_s": 60,
        "simultaneous_min": 3,
        "recent_move_min": 10,
        "max_dev_pct": 5,
    },
    "cache_s": {"holders": 120, "summary": 600, "liquidity_history": 60},
    "rugcheck_rpm": 20,
    "max_wait_s": 1.0,
    "shadow_followups_min": [1, 5, 15, 60],
    "shadow_reevaluate_min": 60,
    "rug_like_pct": -50,
    "liquidity_loss_pct": -50,
    "drained_liq_usd": 1000,  # follow-up liquidity below this (pool started above it) = pool drained: unsellable
    "profitable_missed_pct": 20,
    "alerts": {"enabled": True, "dedupe_s": 21600, "sources_down_dedupe_s": 3600},
    "max_recent": 30,
}


def _f(x):
    try:
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return out


def _mand(result, detail, source=None, age_s=None, **extra):
    """One mandatory-check result: PASS (verified ok) / FAIL (verified disqualifying) / UNKNOWN (not verifiable)."""
    assert result in ("PASS", "FAIL", "UNKNOWN")
    return {
        "result": result,
        "detail": detail,
        "source": source,
        "age_s": None if age_s is None else round(float(age_s), 1),
        **extra,
    }


def parse_authority(report, key):
    """RugCheck authority field -> (PASS|FAIL|UNKNOWN, detail).
    Explicit null = revoked (PASS); a base58 address = active (FAIL); field missing, unparseable value, a report that
    is not a JSON object, or top-level / token.* values that disagree -> UNKNOWN. A missing field is NEVER 'revoked',
    and nothing else in the report (e.g. LP 100% locked) can change this result."""
    if not isinstance(report, dict):
        return "UNKNOWN", "RugCheck report unavailable or not a JSON object"
    tok = report.get("token") if isinstance(report.get("token"), dict) else {}
    vals = [report[key]] if key in report else []
    if key in tok:
        vals.append(tok[key])
    if not vals:
        return "UNKNOWN", f"{key} missing in RugCheck answer"
    res = []
    for v in vals:
        if v is None:
            res.append(("PASS", "revoked (null)"))
        elif isinstance(v, str) and 32 <= len(v) <= 44 and set(v) <= _B58:
            res.append(("FAIL", f"active ({v[:4]}…{v[-4:]})"))
        else:
            res.append(("UNKNOWN", f"{key} unparseable ({type(v).__name__})"))
    if len({r for r, _ in res}) > 1:
        return "UNKNOWN", f"{key}: report and report.token disagree"
    return res[0]


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ramp(x, lo, hi):
    """Risk 0 at x<=lo, 100 at x>=hi, linear between."""
    if x is None:
        return None
    if hi <= lo:
        return 100.0 if x >= hi else 0.0
    return round(100.0 * min(1.0, max(0.0, (x - lo) / (hi - lo))), 1)


def level(risk):
    if risk is None:
        return "UNKNOWN"
    for lo, name in LEVELS:
        if risk >= lo:
            return name
    return "basso"


def resolve_mode(env=None, cfg=None):
    """ANTIRUG_MODE=off|shadow|active (default off). Legacy alias: ANTIRUG_STRICT=1 -> active (only if
    ANTIRUG_MODE is not set)."""
    env = os.environ if env is None else env
    m = str(env.get("ANTIRUG_MODE", "") or (cfg or {}).get("mode", "") or "").strip().lower()
    if m in MODES:
        return m, None
    if m:
        return "off", f"ANTIRUG_MODE={m!r} non valido (usa off|shadow|active): AntiRug resta OFF"
    if str(env.get("ANTIRUG_STRICT", "0")).strip().lower() in ("1", "true", "yes", "on"):
        return "active", "ANTIRUG_STRICT=1 is a legacy alias: AntiRug runs in ACTIVE mode (prefer ANTIRUG_MODE=active)"
    return "off", None


# Hard blocks that may be ENFORCED on top of SHADOW (user decision 13/11/2026: only the verified top-10 holder
# concentration > top10_block_pct blocks; every other check stays observe-only). Fail-OPEN by design: the block
# needs a VERIFIED top-10 value above the threshold; UNKNOWN never blocks in this mode.
ENFORCEABLE = ("holders_top10_concentrated",)


def resolve_enforce(env=None, cfg=None, mode="off"):
    """ANTIRUG_ENFORCE=holders_top10_concentrated (comma list; or antirug.enforce in config). Only used in SHADOW."""
    env = os.environ if env is None else env
    raw = env.get("ANTIRUG_ENFORCE")
    if raw is None:
        raw = (cfg or {}).get("enforce") or ""
    names = [x.strip().lower() for x in (raw if isinstance(raw, (list, tuple)) else str(raw).split(",")) if str(x).strip()]
    bad = [x for x in names if x not in ENFORCEABLE]
    good = tuple(dict.fromkeys(x for x in names if x in ENFORCEABLE))
    warn = None
    if bad:
        warn = f"ANTIRUG_ENFORCE: ignorati valori non supportati {bad} (supportati: {list(ENFORCEABLE)})"
    if good and mode != "shadow":
        return (), (
            f"ANTIRUG_ENFORCE={','.join(good)} ignorato: vale solo con ANTIRUG_MODE=shadow"
            + (" (ACTIVE blocca già tutto)" if mode == "active" else "")
        )
    return good, warn


def previous_mode(data_dir):
    """Mode recorded by the last run that had the engine on (None if the engine was never used)."""
    try:
        return json.load(open(os.path.join(data_dir, "antirug_state.json"))).get("mode")
    except Exception:
        return None


def record_off(data_dir):
    """Called only in OFF mode and only when a previous run used the engine: remember the switch to OFF."""
    p = os.path.join(data_dir, "antirug_state.json")
    try:
        d = json.load(open(p))
        d["mode"] = "off"
        from storage import atomic_json

        atomic_json(p, d, indent=None)
    except Exception as e:
        log.warning("cannot record AntiRug OFF: %s", e)


def mode_change_text(prev, new):
    names = {
        "off": "OFF (nessun controllo)",
        "shadow": "SHADOW (osserva, non blocca)",
        "active": "ACTIVE (blocca, fail-closed)",
    }
    return (
        f"🛡 <b>AntiRug: modalità cambiata</b>\n{names.get(prev or 'off', prev)} → <b>{names.get(new, new)}</b>\n"
        "<i>Solo simulazione</i>"
    )


class AntiRugEngine:
    def __init__(
        self,
        cfg,
        mode,
        *,
        http=None,
        birdeye=None,
        rc_report=None,
        jup_quote=None,
        decimals=None,
        slippage_bps=None,
        sol_price=None,
        clock=time.time,
        data_dir=None,
        notify=None,
        enforce=(),
    ):
        assert mode in ("shadow", "active"), "OFF mode never builds the engine"
        self.enforce = tuple(x for x in (enforce or ()) if x in ENFORCEABLE) if mode == "shadow" else ()
        self.c = _merge(DEFAULTS, cfg or {})
        self.mode, self.h, self.be = mode, http, birdeye
        self.rc_report, self.jup_quote, self.decimals = rc_report, jup_quote, decimals
        self.slippage_bps, self.sol_price = slippage_bps, sol_price
        self.clock, self.notify, self.data_dir = clock, notify, data_dir
        self.cache = {}
        self.seen = {}  # (kind, mint) -> time the engine received a non-cached answer (freshness)
        self.liq = {}
        self.rc_rl = RateLimiter(self.c["rugcheck_rpm"]) if RateLimiter else None
        self.stats = {
            "evaluated": 0,
            "allow": 0,
            "block": 0,
            "unknown": 0,
            "errors": 0,
            "rejected_active": 0,
            "critical": 0,
            "levels": {n: 0 for _, n in LEVELS},
            "hard": {},
        }
        self.api = {
            k: {"ok": 0, "fail": 0, "skipped": 0, "last_ok": None, "last_error": None}
            for k in (
                "rugcheck_report",
                "rugcheck_summary",
                "birdeye_holders",
                "birdeye_price",
                "birdeye_liq_history",
                "jupiter",
            )
        }
        if self.enforce:  # only then: plain SHADOW state stays exactly as before
            self.stats["rejected_enforced"] = 0
        self.recent = deque(maxlen=int(self.c["max_recent"]))
        self.shadow, self.completed, self.alerted = {}, [], {}
        self.prev_mode = None
        self.lock = threading.RLock()
        self.path_state = os.path.join(data_dir, "antirug_state.json") if data_dir else None
        self.path_log = os.path.join(data_dir, "antirug_shadow.jsonl") if data_dir else None
        self.path_verdicts = os.path.join(data_dir, "antirug_verdicts.jsonl") if data_dir else None
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self):
        if not self.path_state:
            return
        try:
            d = json.load(open(self.path_state))
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("AntiRug state unreadable (%s): starting empty", e)
            return
        self.prev_mode = d.get("mode")
        self.shadow = {k: v for k, v in (d.get("shadow") or {}).items() if isinstance(v, dict)}
        for k, v in (d.get("stats") or {}).items():
            if k in self.stats and type(v) is type(self.stats[k]):
                self.stats[k] = v
        self.alerted = {
            k: v
            for k, v in (d.get("alerted") or {}).items()
            if self.clock() - v < self.c["alerts"]["dedupe_s"]
        }
        self.completed = list(d.get("completed") or [])

    def save(self):
        if not self.path_state:
            return
        try:
            from storage import atomic_json

            with self.lock:
                atomic_json(
                    self.path_state,
                    {
                        "mode": self.mode,
                        "shadow": self.shadow,
                        "stats": self.stats,
                        "alerted": self.alerted,
                        "completed": self.completed[-500:],
                    },
                    indent=None,
                )
        except Exception as e:
            log.warning("cannot save AntiRug state: %s", e)

    def _append(self, path, rec):
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
            if os.path.getsize(path) > 20 * 1024 * 1024:  # bounded: rotate once at 20 MB
                os.replace(path, path + ".1")
        except Exception as e:
            log.warning("AntiRug log write failed: %s", e)

    # ------------------------------------------------------------------ api bookkeeping / caches
    def _api(self, name, ok, err=None, skipped=False):
        a = self.api[name]
        if skipped:
            a["skipped"] += 1
            a["last_error"] = err or "rate_limit"
        elif ok:
            a["ok"] += 1
            a["last_ok"] = _iso(self.clock())
        else:
            a["fail"] += 1
            a["last_error"] = err or "error"

    def _cached(self, kind, mint, ttl, fn):
        """TTL cache: expired data is never reused (a failed refresh returns None = UNKNOWN, not the old value)."""
        k = (kind, mint)
        hit = self.cache.get(k)
        if hit and self.clock() - hit[0] < ttl:
            return hit[1]
        self.cache.pop(k, None)
        v = fn()
        if v is not None:
            self.cache[k] = (self.clock(), v)
            if len(self.cache) > 4000:
                self.cache = dict(list(self.cache.items())[-2000:])
        return v

    def _age(self, kind, mint):
        hit = self.cache.get((kind, mint)) or self.seen.get((kind, mint))
        return None if not hit else max(0.0, self.clock() - (hit[0] if isinstance(hit, tuple) else hit))

    def observe_liquidity(self, mint, usd, ts=None):
        usd = _f(usd)
        if usd is None or usd < 0:
            return
        dq = self.liq.setdefault(mint, deque(maxlen=120))
        t = self.clock() if ts is None else ts
        if dq and abs(t - dq[-1][0]) < 5:
            dq[-1] = (t, usd)
        else:
            dq.append((t, usd))
        if len(dq) > 1 and dq[-1][0] < dq[-2][0]:  # keep samples in time order
            srt = sorted(dq)
            dq.clear()
            dq.extend(srt)
        if len(self.liq) > 3000:
            for m in list(self.liq)[:1500]:
                self.liq.pop(m, None)

    # ------------------------------------------------------------------ sources
    def _report(self, mint):
        if not self.rc_report:
            return None
        try:
            d = self.rc_report(mint)
        except Exception as e:
            self._api("rugcheck_report", False, type(e).__name__)
            return None
        ok = isinstance(d, dict)
        self._api("rugcheck_report", ok, None if ok else ("malformed" if d is not None else "no_answer"))
        if ok:
            self.seen[("rc_report", mint)] = self.clock()
            if len(self.seen) > 4000:
                self.seen = dict(list(self.seen.items())[-2000:])
        return d if ok else None

    def _rc_summary(self, mint):
        if not self.h:
            return None

        def get():
            if self.rc_rl and not self.rc_rl.acquire(self.c["max_wait_s"]):
                self._api("rugcheck_summary", False, skipped=True)
                return None
            d = self.h.get(f"{RC}/tokens/{mint}/report/summary")
            ok = isinstance(d, dict) and not d.get("error")
            self._api("rugcheck_summary", ok, None if ok else "no_answer")
            return d if ok else None

        return self._cached("rc_summary", mint, self.c["cache_s"]["summary"], get)

    def _be_holders(self, mint):
        if not self.be:
            return None
        cc = self.c["concentration"]

        def get():
            p = {"address": mint, "offset": 0, "limit": int(cc["limit"])}
            if cc.get("wallet_mode", True):
                p["mode"] = "wallet"
            if cc.get("holder_infos", True):
                p["get_holder_infos"] = "true"
            d = self.be._get("/defi/v3/token/holder", p, ttl=self.c["cache_s"]["holders"])
            ok = isinstance(d, dict)
            self._api("birdeye_holders", ok, None if ok else (getattr(self.be, "last_error", "") or "no_answer"))
            return d if ok else None

        return self._cached("be_holders", mint, self.c["cache_s"]["holders"], get)

    def _be_liq_history(self, mint):
        if not (self.be and self.c["liquidity"].get("birdeye_history")):
            return None

        def get():
            d = self.be._get(
                "/defi/v3/liquidity/history/token",
                {
                    "address": mint,
                    "resolution": "1m",
                    "direction": "back",
                    "count": 16,
                    "time": int(self.clock()),
                },
                ttl=self.c["cache_s"]["liquidity_history"],
            )
            items = d.get("items") if isinstance(d, dict) else None
            ok = isinstance(items, list)
            self._api(
                "birdeye_liq_history",
                ok,
                None
                if ok
                else (
                    "endpoint_disabled"
                    if getattr(self.be, "endpoint_disabled", lambda _p: False)("/defi/v3/liquidity/history/token")
                    else "no_answer"
                ),
            )
            if not ok:
                return None
            return sorted(
                (int(i["unix_time"]), _f(i.get("liquidity_usd")), _f(i.get("exit_liquidity_usd")))
                for i in items
                if isinstance(i, dict)
                and _f(i.get("unix_time")) is not None
                and _f(i.get("liquidity_usd")) is not None
            )

        return self._cached("be_liq", mint, self.c["cache_s"]["liquidity_history"], get)

    def _quotes(self, snap, usd_in):
        """Jupiter buy + sell quotes (read-only). Returns dict with status / reasons."""
        out = {"buy": None, "sell": None, "sell_to": None, "reason_buy": "", "reason_sell": "", "qty": None}
        mid = _f(snap.get("price"))
        if not self.jup_quote or not mid or mid <= 0 or not usd_in or usd_in <= 0:
            out["reason_buy"] = "not_possible"
            return out
        dec = self.decimals(snap["mint"]) if self.decimals else None
        if dec is None:
            out["reason_buy"] = "no_decimals"
            return out
        qb, rb = self.jup_quote(USDC, snap["mint"], round(usd_in * 1e6))
        out["reason_buy"] = rb
        if not qb:
            self._api("jupiter", rb == "no_route", rb)
            return out
        out["buy"], out["qty"] = qb, qb["out"] / 10 ** dec
        qs, rs = self.jup_quote(snap["mint"], USDC, int(qb["out"]))
        to = "USDC"
        if not qs and rs == "no_route":  # spec: route to SOL *or* USDC
            qs2, rs2 = self.jup_quote(snap["mint"], SOL, int(qb["out"]))
            if qs2:
                qs, rs, to = qs2, "", "SOL"
            else:
                rs = rs2 or rs
        out["sell"], out["reason_sell"], out["sell_to"] = qs, rs, (to if qs else None)
        self._api("jupiter", bool(qs) or rs == "no_route", None if qs else rs)
        return out

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _cat(risk, metrics, checks, hard, flags=(), mand=None):
        r = None if risk is None else round(min(100.0, max(0.0, float(risk))), 1)
        if hard:
            r = 100.0
        return {
            "status": "OK",
            "risk": r,
            "level": level(r),
            "metrics": metrics,
            "checks": checks,
            "flags": list(flags),
            "hard_blocks": hard,
            "mandatory": mand or {},
        }

    @staticmethod
    def _unknown(why, checks=None, mand_names=(), metrics=None, source=None):
        return {
            "status": "UNKNOWN",
            "risk": None,
            "level": "UNKNOWN",
            "metrics": metrics or {},
            "checks": checks or [{"check": "data", "result": "unverified", "detail": why}],
            "flags": [why],
            "hard_blocks": [],
            "mandatory": {n: _mand("UNKNOWN", why, source) for n in mand_names},
        }

    @staticmethod
    def _chk(name, result, detail=""):
        return {"check": name, "result": result, "detail": detail}

    def _excluded(self, report, snap):
        cc = self.c["concentration"]
        why = {a: "burn" for a in BURN_OWNERS}
        for a in cc.get("program_owners") or []:
            why[a] = "program"
        for a in cc.get("official_wallets") or []:
            why[a] = "official"
        if snap.get("pool"):
            why[snap["pool"]] = "lp_pool"
        if isinstance(report, dict):
            types = {str(t).upper() for t in cc.get("excluded_known_types") or []}
            for k, v in (report.get("knownAccounts") or {}).items():
                t = str((v or {}).get("type", "")).upper() if isinstance(v, dict) else ""
                if t in types:
                    why[k] = "lp_pool" if t == "AMM" else "technical:" + t.lower()
            for m in report.get("markets") or []:
                if isinstance(m, dict):
                    for key in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
                        if isinstance(m.get(key), str):
                            why[m[key]] = "lp_pool"
        return why

    # ------------------------------------------------------------------ categories
    def cat_concentration(self, snap, report, hinfo, be_items):
        cc = self.c["concentration"]
        why = self._excluded(report, snap)
        d = be_items
        items = [i for i in (d.get("items") or []) if isinstance(i, dict)] if d else []
        # the live API (13/11/2026) answers "top10_hold_percent"; older docs/fixtures use "top10HoldPercent"
        top10_raw = (
            next(
                (
                    v
                    for v in (_f(d.get(k)) for k in ("top10_hold_percent", "top10HoldPercent", "top10HolderPercent"))
                    if v is not None
                ),
                None,
            )
            if d
            else None
        )
        owners, excluded, src, unit = {}, [], None, None
        if items and top10_raw and 0 < top10_raw <= 100 and len(items) >= 10:
            unit = "ui_amount" if all(_f(i.get("ui_amount")) is not None for i in items) else "amount"
            items = sorted(items, key=lambda i: _f(i.get(unit)) or 0, reverse=True)  # never trust the API order
            amt10 = sum(_f(i.get(unit)) or 0 for i in items[:10])
            supply = amt10 / (top10_raw / 100.0) if amt10 > 0 else None  # unit-free: ratio of Birdeye's own numbers
            if supply:
                src = "birdeye:/defi/v3/token/holder" + (" mode=wallet" if cc.get("wallet_mode", True) else "")
                for i in items:
                    own, acc = i.get("owner"), i.get("token_account")
                    pct = 100.0 * (_f(i.get(unit)) or 0) / supply
                    tag = why.get(own) or why.get(acc)
                    if tag:
                        excluded.append({"owner": own, "pct": round(pct, 2), "type": tag})
                    else:
                        owners[own] = owners.get(own, 0.0) + pct  # economic owner, not token account
        if not owners and isinstance(report, dict) and report.get("topHolders"):
            src = "rugcheck:/v1/tokens/{mint}/report topHolders"
            for h in report.get("topHolders") or []:
                if not isinstance(h, dict):
                    continue
                own, acc, pct = h.get("owner"), h.get("address"), _f(h.get("pct")) or 0
                tag = why.get(own) or why.get(acc)
                if tag:
                    excluded.append({"owner": own, "pct": round(pct, 2), "type": tag})
                else:
                    owners[own] = owners.get(own, 0.0) + pct
        if not owners:
            return self._unknown(
                "no holder data (Birdeye and RugCheck unavailable or malformed)",
                mand_names=("holders.top10",),
            )
        shares = sorted(owners.values(), reverse=True)
        top10, top5, big = sum(shares[:10]), sum(shares[:5]), shares[0]
        flags, checks, hard = [], [], []
        if report is None:
            flags.append("lp/technical accounts not identifiable (RugCheck report missing)")
        big_unknown = [p for p in shares[:3] if p >= cc["uncertain_single_pct"]]
        if big_unknown:
            flags.append(
                f"uncertain: {len(big_unknown)} holder >= {cc['uncertain_single_pct']}% not classified "
                "(whale, unlisted pool/bonding curve or official wallet: not assumed to be an insider)"
            )
        uncertain = bool(report is None or big_unknown)
        # Only VERIFIED non-economic accounts are excluded (burn, pool / vault, program authority, RugCheck AMM/LOCKER,
        # configured official wallets). Unknown accounts are COUNTED as holders: the block is never skipped because
        # an account *might* be technical; the uncertainty is reported instead.
        checks.append(
            self._chk(
                "top10",
                "fail" if top10 > cc["top10_block_pct"] else "warn" if top10 > cc["top10_high_pct"] else "pass",
                f"{top10:.1f}%",
            )
        )
        checks.append(self._chk("top5", "warn" if top5 > cc["top5_high_pct"] else "pass", f"{top5:.1f}%"))
        checks.append(self._chk("largest_wallet", "warn" if big > cc["single_high_pct"] else "pass", f"{big:.1f}%"))
        checks.append(
            self._chk(
                "account_classification",
                "unverified" if uncertain else "pass",
                "; ".join(flags) or f"{len(excluded)} accounts excluded",
            )
        )
        fail = top10 > cc["top10_block_pct"]
        if fail:
            hard.append("holders_top10_concentrated")
        mand = {
            "holders.top10": _mand(
                "FAIL" if fail else "PASS",
                f"top10 {top10:.2f}% (warn > {cc['top10_high_pct']}%, block > "
                f"{cc['top10_block_pct']}%)" + ("; classification uncertain" if uncertain else ""),
                src,
                self._age("be_holders", snap["mint"])
                if (src or "").startswith("birdeye")
                else self._age("rc_report", snap["mint"]),
                value_pct=round(top10, 2),
            )
        }
        risk = max(
            ramp(top10, cc["top10_high_pct"] / 2, cc["top10_block_pct"] + 10),
            ramp(top5, cc["top5_high_pct"] / 2, cc["top5_high_pct"] * 1.6),
            ramp(big, cc["single_high_pct"] / 2, cc["single_high_pct"] * 2),
        )
        n_holders = d.get("holder") if d else None
        return self._cat(
            risk,
            {
                "top10_pct": round(top10, 2),
                "top5_pct": round(top5, 2),
                "largest_pct": round(big, 2),
                "top10_raw_pct": top10_raw,
                "owners_counted": len(owners),
                "excluded": excluded[:10],
                "source": src,
                "holder_count": n_holders,
                "holder_count_kind": (
                    ("wallet owners" if (src or "").endswith("mode=wallet") else "token accounts")
                    if n_holders is not None
                    else None
                ),
                "uncertain": uncertain,
            },
            checks,
            hard,
            flags,
            mand,
        )

    def _change(self, pts, now_usd, minutes, now=None):
        if not pts or now_usd is None:
            return None
        now = self.clock() if now is None else now
        # only a REAL sample close to the window start is used (no interpolation / extrapolation, nothing invented)
        target, tol = now - minutes * 60, max(20.0, minutes * 60 * 0.2)
        best = min(pts, key=lambda p: abs(p[0] - target))
        if abs(best[0] - target) > tol or not best[1] or best[0] >= now:
            return None
        return round(100.0 * (now_usd / best[1] - 1), 2)

    def _liq_value(self, snap):
        """DexScreener value with its state: ok / missing / invalid / stale (needs snap['liquidity_ts'])."""
        lc = self.c["liquidity"]
        raw = snap.get("liquidity")
        v = _f(raw)
        if raw is None or (v == 0 and raw in (0, 0.0)):  # DexScreener pair without a liquidity object -> 0.0
            return None, "missing", None
        if v is None or v < 0 or isinstance(raw, bool):
            return None, "invalid", None
        ts = _f(snap.get("liquidity_ts"))
        if ts is None:
            return None, "stale", None  # freshness not provable -> treated as stale
        age = self.clock() - ts
        if age > lc["max_age_s"] or age < -5:
            return None, "stale", age
        return v, "ok", age

    def cat_liquidity(self, snap, usd_in, quotes):
        """Minimum usable liquidity is an INDEPENDENT hard block (spec phase 2). Exactly at the minimum passes
        ("below the minimum" blocks: usable < min_usd)."""
        lc = self.c["liquidity"]
        names = ("liquidity.minimum",)
        cur, state, age = self._liq_value(snap)
        be_liq = None
        if self.be:
            pr = self.be.price(snap["mint"], with_liquidity=True)
            self._api("birdeye_price", pr is not None, None if pr else "no_answer")
            be_liq = _f(pr[1]) if pr else None
            if be_liq is not None and be_liq <= 0:
                be_liq = None
        src = "dexscreener"
        if cur is None and be_liq is not None:
            cur, src, age = be_liq, "birdeye:/defi/price", 0.0  # fetched now (client cache <= 10 s)
        if cur is None:
            why = {
                "missing": "liquidity missing",
                "invalid": "liquidity value invalid",
                "stale": "liquidity data stale or without timestamp",
            }[state]
            return self._unknown(
                f"{why} (DexScreener) and no Birdeye value",
                mand_names=names,
                source="dexscreener",
                metrics={
                    "liquidity_state": state,
                    "age_s": None if age is None else round(age, 1),
                    "threshold_usd": lc["min_usd"],
                },
            )
        hist = self._be_liq_history(snap["mint"]) or []
        exit_liq = None
        if hist and hist[-1][2] is not None and hist[-1][2] >= 0:
            if self.clock() - hist[-1][0] <= lc["history_max_age_s"]:
                exit_liq = hist[-1][2]
        usable = min(x for x in (cur, exit_liq) if x is not None)
        local = list(self.liq.get(snap["mint"]) or [])
        ch, csrc = {}, {}
        for mnt in (1, 5, 15):
            v = self._change(local, cur, mnt)
            if v is not None:
                csrc[f"{mnt}m"] = "local_dexscreener"
            elif hist:
                v = self._change([(t, l) for t, l, _ in hist], hist[-1][1], mnt, now=hist[-1][0])
                if v is not None:
                    csrc[f"{mnt}m"] = "birdeye:/defi/v3/liquidity/history/token"
            ch[f"{mnt}m"] = v
        rates = [-ch[f"{m}m"] / m for m in (1, 5, 15) if ch[f"{m}m"] is not None]
        outflow = round(max(rates), 2) if rates else None  # % of liquidity leaving per minute
        size_pct = round(100.0 * usd_in / usable, 3) if usd_in and usable else None
        imp = _f((quotes.get("buy") or {}).get("impact_pct"))
        checks, hard, flags = [], [], []
        below = usable < lc["min_usd"]
        detail = f"{usable:,.0f} USD usable, threshold {lc['min_usd']:,.0f} USD" + (
            " -> below minimum" if below else ""
        )
        checks.append(self._chk("minimum_liquidity", "fail" if below else "pass", detail))
        mand = {
            "liquidity.minimum": _mand(
                "FAIL" if below else "PASS",
                detail,
                src + ("+birdeye_exit" if exit_liq is not None else ""),
                age,
                value_usd=round(usable, 2),
                threshold_usd=lc["min_usd"],
                reason="liquidity_below_minimum" if below else "ok",
            )
        }
        if below:
            hard.append("liquidity_below_minimum")
        drop_fail = []
        for mnt in (1, 5, 15):
            v = ch[f"{mnt}m"]
            if v is not None and v <= -abs(lc[f"drop_block_{mnt}m_pct"]):
                drop_fail.append(f"liquidity_drop_{mnt}m")
        hard += drop_fail
        checks.append(
            self._chk(
                "liquidity_trend",
                "fail" if drop_fail else "unverified" if not rates else "pass",
                json.dumps(ch),
            )
        )
        if not rates:
            flags.append("liquidity_history_insufficient")
        if be_liq and cur and src == "dexscreener":
            ratio = max(be_liq, cur) / max(min(be_liq, cur), 1e-9)
            bad = ratio > lc["inconsistent_ratio"]
            checks.append(self._chk("source_consistency", "fail" if bad else "pass", f"DexScreener/Birdeye x{ratio:.1f}"))
            if bad:
                hard.append("liquidity_inconsistent")
        else:
            checks.append(self._chk("source_consistency", "unverified", "only one liquidity source"))
        shallow = (size_pct is not None and size_pct > lc["max_size_to_liquidity_pct"]) or (
            imp is not None and abs(imp) > lc["max_buy_impact_pct"]
        )
        checks.append(
            self._chk(
                "pool_depth",
                "fail" if shallow else "pass" if imp is not None else "unverified",
                f"size {size_pct}% of liquidity, buy impact {imp}%",
            )
        )
        if shallow:
            hard.append("pool_too_shallow")
        risk = max(
            ramp(-usable, -lc["good_usd"], -lc["min_usd"]) * 0.7,
            ramp(outflow or 0, 1, lc["drop_block_5m_pct"] / 5),
            ramp(size_pct or 0, lc["max_size_to_liquidity_pct"] / 4, lc["max_size_to_liquidity_pct"]),
        )
        return self._cat(
            risk,
            {
                "usd": round(cur),
                "source": src,
                "usable_usd": round(usable),
                "threshold_usd": lc["min_usd"],
                "liquidity_state": "ok",
                "age_s": None if age is None else round(age, 1),
                "exit_liquidity_usd": None if exit_liq is None else round(exit_liq),
                "birdeye_usd": None if be_liq is None else round(be_liq),
                "change_pct": ch,
                "change_source": csrc,
                "outflow_pct_per_min": outflow,
                "size_to_liquidity_pct": size_pct,
                "buy_price_impact_pct": imp,
            },
            checks,
            hard,
            flags,
            mand,
        )

    def cat_sellability(self, snap, usd_in, q):
        """Jupiter QUOTES only (spec phase 5). Four distinct numbers: slippage TOLERANCE (what we would allow),
        PRICE IMPACT (Jupiter's estimate), QUOTED slippage (quote vs market price) and EXECUTED slippage (only after a
        paper fill). A route now does not guarantee the token will be sellable later."""
        sc = self.c["sellability"]
        names = (
            "sellability.buy_quote",
            "sellability.sell_route",
            "sellability.price_impact",
            "sellability.quoted_slippage",
            "sellability.quote_fresh",
        )
        src = "jupiter:/swap/v1/quote"
        if q["buy"] is None:
            if q["reason_buy"] == "no_route":
                mand = {
                    n: _mand("FAIL" if n == "sellability.buy_quote" else "UNKNOWN", "no buy route", src)
                    for n in names
                }
                return self._cat(
                    100,
                    {"route_buy": False},
                    [self._chk("buy_route", "fail", "no route")],
                    ["no_buy_route"],
                    (),
                    mand,
                )
            return self._unknown(
                f"Jupiter buy quote unavailable ({q['reason_buy'] or 'unknown'})",
                mand_names=names,
                source=src,
            )
        if q["sell"] is None:
            if q["reason_sell"] == "no_route":
                mand = {n: _mand("UNKNOWN", "no sell route", src) for n in names}
                mand["sellability.buy_quote"] = _mand("PASS", "buy quote ok", src, q["buy"].get("age_s"))
                mand["sellability.sell_route"] = _mand("FAIL", "no route to USDC or SOL", src)
                return self._cat(
                    100,
                    {"route_buy": True, "route_sell": False},
                    [self._chk("sell_route", "fail", "no route to USDC or SOL")],
                    ["not_sellable"],
                    (),
                    mand,
                )
            mand = {
                n: _mand("UNKNOWN", f"Jupiter sell quote unavailable ({q['reason_sell'] or 'unknown'})", src)
                for n in names
            }
            mand["sellability.buy_quote"] = _mand("PASS", "buy quote ok", src, q["buy"].get("age_s"))
            return {
                **self._unknown(f"Jupiter sell quote unavailable ({q['reason_sell'] or 'unknown'})"),
                "mandatory": mand,
            }
        qb, qs, qty, mid = q["buy"], q["sell"], q["qty"], _f(snap["price"])
        ages = [_f(qq.get("age_s")) for qq in (qb, qs)]
        if any(a is None for a in ages) or max(ages) > sc["max_quote_age_s"]:
            why = (
                "quote age not reported"
                if any(a is None for a in ages)
                else f"quote {max(ages):.0f}s old > {sc['max_quote_age_s']}s"
            )
            mand = {n: _mand("UNKNOWN", "stale quote: " + why, src) for n in names}
            mand["sellability.quote_fresh"] = _mand(
                "UNKNOWN", why, src, None if any(a is None for a in ages) else max(ages)
            )
            return {
                **self._unknown("Jupiter quote stale (" + why + "): stale data never authorises a buy"),
                "mandatory": mand,
            }
        out_buy, out_sell = _f(qb.get("out")), _f(qs.get("out"))
        if not out_buy or out_buy <= 0 or out_sell is None or out_sell < 0 or not mid or not qty:
            return self._unknown("Jupiter quote malformed (outAmount)", mand_names=names, source=src)
        if q["sell_to"] == "SOL":
            sp = _f(self.sol_price()) if self.sol_price else None
            if not sp:
                return self._unknown("sell route only to SOL and SOL price unknown", mand_names=names, source=src)
            usd_back = out_sell / 1e9 * sp
        else:
            usd_back = out_sell / 1e6
        est_buy = 100 * ((usd_in / qty) / mid - 1)
        est_sell = 100 * (1 - (usd_back / qty) / mid)
        rt = 100.0 * (1 - usd_back / usd_in)
        imp = _f(qs.get("impact_pct"))
        hops = len([x for x in str(qs.get("route") or "").split("+") if x])
        m = {
            "route_buy": True,
            "route_sell": True,
            "sell_to": q["sell_to"],
            "route": qs.get("route"),
            "hops": hops,
            "representative_qty": qty,
            "usd_in": round(usd_in, 4),
            "quoted_out_buy_raw": int(out_buy),
            "quoted_out_sell_raw": int(out_sell),
            "quote_age_buy_s": ages[0],
            "quote_age_sell_s": ages[1],
            "slippage_tolerance_bps": self.slippage_bps,  # what we ALLOW (sent to Jupiter)
            "price_impact_buy_pct": qb.get("impact_pct"),
            "price_impact_sell_pct": imp,  # Jupiter's number
            "quoted_slippage_buy_pct": round(est_buy, 2),  # quote vs market price
            "quoted_slippage_sell_pct": round(est_sell, 2),
            "estimated_slippage_buy_pct": round(est_buy, 2),  # same numbers, legacy names
            "estimated_slippage_sell_pct": round(est_sell, 2),
            "estimated_exit_usd": round(usd_back, 4),
            "roundtrip_loss_pct": round(rt, 2),
            "executed_slippage_pct": None,  # only after a (paper) fill
            "note": "a route today does not guarantee the token will be sellable later",
        }
        checks = [self._chk("sell_route", "pass", f"to {q['sell_to']}")]
        hard, flags = [], []
        bad_slip = est_sell > sc["max_sell_slippage_pct"]
        checks.append(self._chk("sell_slippage", "fail" if bad_slip else "pass", f"{est_sell:.1f}%"))
        if bad_slip:
            hard.append("sell_slippage_excessive")
        bad_imp = imp is not None and abs(imp) > sc["max_sell_impact_pct"]
        checks.append(
            self._chk(
                "sell_price_impact",
                "fail" if bad_imp else "pass" if imp is not None else "unverified",
                f"{imp}%",
            )
        )
        if bad_imp:
            hard.append("sell_price_impact_excessive")
        odd = hops > sc["max_route_hops"] or not qs.get("route")
        checks.append(self._chk("route_shape", "warn" if odd else "pass", f"{hops} hop(s)"))
        if odd:
            flags.append("anomalous route")
        a = max(ages)
        mand = {
            "sellability.buy_quote": _mand("PASS", f"buy quote ok, out {int(out_buy)}", src, ages[0]),
            "sellability.sell_route": _mand(
                "PASS", f"sell route to {q['sell_to']}, out {int(out_sell)}", src, ages[1]
            ),
            "sellability.price_impact": _mand(
                "UNKNOWN" if imp is None else "FAIL" if bad_imp else "PASS",
                f"sell price impact {imp}% (max {sc['max_sell_impact_pct']}%)",
                src,
                ages[1],
            ),
            "sellability.quoted_slippage": _mand(
                "FAIL" if bad_slip else "PASS",
                f"quoted sell slippage {est_sell:.2f}% (max {sc['max_sell_slippage_pct']}%)",
                src,
                ages[1],
            ),
            "sellability.quote_fresh": _mand("PASS", f"quotes {a:.1f}s old (max {sc['max_quote_age_s']}s)", src, a),
        }
        risk = max(ramp(est_sell, 2, sc["max_sell_slippage_pct"]), ramp(rt, 5, 30), 20 if odd else 0)
        return self._cat(risk, m, checks, hard, flags, mand)

    def cat_technical(self, snap, report):
        """Authorities are validated INDEPENDENTLY of each other and of the LP lock (spec phase 1)."""
        tc = self.c["technical"]
        src = "rugcheck:/v1/tokens/{mint}/report"
        rep_age = self._age("rc_report", snap["mint"])
        mand, checks, hard, flags, m, risk = {}, [], [], [], {}, 0.0
        for name, key in (("mint_authority", "mintAuthority"), ("freeze_authority", "freezeAuthority")):
            res, detail = parse_authority(report, key)
            m[name] = {"PASS": False, "FAIL": True}.get(res)  # True active / False revoked / None unknown
            mand["authorities." + name.split("_")[0]] = _mand(res, detail, src, rep_age)
            checks.append(self._chk(name, {"PASS": "pass", "FAIL": "fail"}.get(res, "unverified"), detail))
            if res == "FAIL":
                hard.append(f"{name}_active")
        summ = self._rc_summary(snap["mint"])
        if not isinstance(report, dict) and not summ:
            return {
                **self._unknown("RugCheck report and summary unavailable", checks),
                "mandatory": mand,
                "metrics": m,
            }
        rlist = (summ or {}).get("risks") or (report.get("risks") if isinstance(report, dict) else None) or []
        risks = [r for r in (rlist if isinstance(rlist, list) else []) if isinstance(r, dict)]
        m["rugcheck_risks"] = [{"name": r.get("name"), "level": r.get("level")} for r in risks][:12]
        kws = [k.lower() for k in tc["dangerous_feature_keywords"]]
        danger_feat = [
            r.get("name")
            for r in risks
            if str(r.get("level", "")).lower() == "danger"
            and any(k in str(r.get("name", "")).lower() for k in kws)
        ]
        if danger_feat:
            hard.append("dangerous_token_feature")
        checks.append(
            self._chk(
                "dangerous_features",
                "fail" if danger_feat else "pass" if (summ or report) else "unverified",
                ", ".join(map(str, danger_feat)) or "none reported by RugCheck",
            )
        )
        for r in risks:
            lvl = str(r.get("level", "")).lower()
            risk += tc["danger_risk_points"] if lvl == "danger" else tc["warn_risk_points"] if lvl == "warn" else 0
        prog = (summ or {}).get("tokenProgram")
        m["token_program"] = prog
        if prog == TOKEN_2022:
            risk += tc["token2022_points"]
            flags.append("Token-2022: extensions not fully verifiable via RugCheck summary")
            checks.append(self._chk("token_extensions", "unverified", "Token-2022"))
        lp = _f((summ or {}).get("lpLockedPct"))
        m["lp_locked_pct"] = lp
        if lp is None:
            checks.append(self._chk("lp_locked", "unverified", "lpLockedPct not available"))
        else:
            unl = lp < tc["lp_unlocked_below_pct"]
            checks.append(self._chk("lp_locked", "warn" if unl else "pass", f"{lp:.0f}% locked"))
            if unl:
                risk += 30
                flags.append(f"LP locked only {lp:.0f}% (high risk, not an automatic block)")
        checks.append(
            self._chk(
                "lp_lock_duration_controller",
                "unverified",
                "not exposed by the documented free RugCheck endpoints",
            )
        )
        m["rugcheck_score_normalised"] = (summ or {}).get(
            "score_normalised", report.get("score_normalised") if isinstance(report, dict) else None
        )
        if isinstance(report, dict) and report.get("rugged") is True:
            hard.append("rugcheck_rugged")
        m["source"] = "rugcheck:/v1/tokens/{mint}/report + /report/summary"
        if all(v["result"] == "UNKNOWN" for v in mand.values()) and lp is None and not risks:
            return {
                **self._unknown("RugCheck answered without authority / LP / risk fields", checks),
                "mandatory": mand,
                "metrics": m,
            }
        return self._cat(risk, m, checks, hard, flags, mand)

    def cat_behavioral(self, snap, report, hinfo, ctx, be_items):
        bc = self.c["behavioral"]
        t = _iso(self.clock())
        ev, risk, checks, have = [], 0.0, [], False

        def add(signal, value, evidence, source, conf, points=0.0):
            nonlocal risk
            ev.append(
                {
                    "signal": signal,
                    "value": value,
                    "evidence": evidence,
                    "source": source,
                    "ts": t,
                    "confidence": conf,
                }
            )
            risk += points

        why = self._excluded(report, snap)
        items = [
            i
            for i in ((be_items or {}).get("items") or [])
            if isinstance(i, dict) and not (why.get(i.get("owner")) or why.get(i.get("token_account")))
        ]
        src_be = "birdeye:/defi/v3/token/holder mode=wallet get_holder_infos"
        if items:
            funders = {}
            for i in items:
                fu = (i.get("funding") or {}).get("funder") if isinstance(i.get("funding"), dict) else None
                if fu:
                    funders.setdefault(fu, []).append(i.get("owner"))
            if any(isinstance(i.get("funding"), dict) for i in items):
                have = True
                groups = {k: v for k, v in funders.items() if len(v) >= bc["same_funder_min"]}
                n = sum(len(v) for v in groups.values())
                checks.append(self._chk("same_funding_source", "fail" if groups else "pass", f"{n} linked wallets"))
                if groups:
                    add(
                        "same_funding_source",
                        n,
                        f"{n} top holders funded by {len(groups)} common wallet(s)",
                        src_be + " funding.funder",
                        0.7 if n >= 3 else 0.5,
                        30 if n >= 3 else 15,
                    )
            else:
                checks.append(self._chk("same_funding_source", "unverified", "funding not returned"))
            firsts = sorted(_f(i.get("firstTradeUnixTime")) for i in items if _f(i.get("firstTradeUnixTime")))
            if firsts:
                have = True
                w, best = bc["simultaneous_window_s"], 0
                for k, x in enumerate(firsts):
                    best = max(best, sum(1 for y in firsts[k:] if y - x <= w))
                sim = best >= bc["simultaneous_min"]
                checks.append(
                    self._chk(
                        "simultaneous_buys",
                        "fail" if sim else "pass",
                        f"{best} top holders first bought within {w}s",
                    )
                )
                if sim:
                    add(
                        "simultaneous_first_buys",
                        best,
                        f"{best} top holders made their first trade within {w}s "
                        "(possible bundle / coordinated early buying)",
                        src_be + " firstTradeUnixTime",
                        0.5,
                        20,
                    )
            else:
                checks.append(self._chk("simultaneous_buys", "unverified", "no first-trade times"))
            now = self.clock()
            recent = [
                i
                for i in items
                if _f(i.get("lastTradeUnixTime")) and now - _f(i.get("lastTradeUnixTime")) <= bc["recent_move_min"] * 60
            ]
            sellers = [i for i in recent if (_f(i.get("avgSellPrice")) or 0) > 0]
            if any(_f(i.get("lastTradeUnixTime")) for i in items):
                have = True
                checks.append(
                    self._chk(
                        "recent_large_holder_moves",
                        "warn" if recent else "pass",
                        f"{len(recent)} top holders traded in the last {bc['recent_move_min']} min",
                    )
                )
                if recent:
                    add(
                        "recent_large_holder_moves",
                        len(recent),
                        f"{len(recent)} top holders traded recently",
                        src_be + " lastTradeUnixTime",
                        0.4,
                        5 * min(len(recent), 4),
                    )
                checks.append(
                    self._chk(
                        "simultaneous_sells",
                        "warn" if len(sellers) >= bc["simultaneous_min"] else "pass",
                        f"{len(sellers)} recent holders with sells (weak signal)",
                    )
                )
                if len(sellers) >= bc["simultaneous_min"]:
                    add(
                        "simultaneous_sells",
                        len(sellers),
                        f"{len(sellers)} top holders traded recently and have "
                        "sold before (weak: Birdeye gives no per-trade side here)",
                        src_be + " avgSellPrice",
                        0.3,
                        15,
                    )
            else:
                checks.append(self._chk("simultaneous_sells", "unverified", "no trade times"))
        else:
            for c in ("same_funding_source", "simultaneous_buys", "simultaneous_sells"):
                checks.append(self._chk(c, "unverified", "Birdeye wallet-mode holder data unavailable"))
        if hinfo and hinfo.get("dev_pct") is not None:
            have = True
            dev = _f(hinfo.get("dev_pct")) or 0.0
            add(
                "dev_holding_pct",
                round(dev, 3),
                f"creator holds {dev:.2f}% of supply",
                "rugcheck:/v1/tokens/{mint}/report creator/topHolders",
                0.7,
                25 if dev > bc["max_dev_pct"] else 0,
            )
        if isinstance(report, dict):
            hs = [h for h in report.get("topHolders") or [] if isinstance(h, dict)]
            if any("insider" in h for h in hs):
                have = True
                ins = [h for h in hs if h.get("insider")]
                pct = sum(_f(h.get("pct")) or 0 for h in ins)
                add(
                    "insider_holders",
                    len(ins),
                    f"{len(ins)} top holders flagged insider ({pct:.1f}% of supply)",
                    "rugcheck:/v1/tokens/{mint}/report topHolders[].insider (not in the public schema)",
                    0.4,
                    30 if pct >= 10 else 15 if ins else 0,
                )
            if "graphInsidersDetected" in report:
                have = True
                g = int(_f(report.get("graphInsidersDetected")) or 0)
                add(
                    "insider_network",
                    g,
                    f"{g} wallets linked in RugCheck's insider graph",
                    "rugcheck:/v1/tokens/{mint}/report graphInsidersDetected (not in the public schema)",
                    0.4,
                    20 if g >= 5 else 0,
                )
        tags = self.be.token_trader_tags(snap["mint"]) if self.be and hasattr(self.be, "token_trader_tags") else None
        if tags is not None:
            have = True
            bad = sorted({x for x in tags if any(k in x for k in ("sniper", "bundler", "insider"))})
            add(
                "trader_tags",
                bad,
                f"top traders of the token tagged {bad or 'nothing risky'}",
                "birdeye:/defi/v2/tokens/top_traders tags",
                0.5,
                15 if bad else 0,
            )
        tt = (ctx or {}).get("top_traders_in")
        if tt:
            have = True
            add(
                "smart_wallets_in",
                tt,
                f"{tt} tracked top-trader wallets hold the token",
                "vybe/birdeye top-trader list",
                0.6,
            )
        if not have:
            return self._unknown("no wallet data", checks)
        return self._cat(risk, {"evidence": ev}, checks, [])

    # ------------------------------------------------------------------ main
    def evaluate(self, snap, usd_in, hinfo=None, ctx=None):
        """Full verdict. Never raises: an internal error makes the category UNKNOWN (never a passed check)."""
        t0 = self.clock()
        mint, cats, err = snap["mint"], {}, None
        report = self._report(mint)
        be_items = None
        try:
            be_items = self._be_holders(mint)
        except Exception as e:
            err = f"birdeye: {type(e).__name__}"
        try:
            quotes = self._quotes(snap, usd_in)
        except Exception as e:
            err = err or f"jupiter: {type(e).__name__}"
            quotes = {"buy": None, "sell": None, "reason_buy": "error", "reason_sell": "error", "qty": None}
        for name, fn in (
            ("technical", lambda: self.cat_technical(snap, report)),
            ("liquidity", lambda: self.cat_liquidity(snap, usd_in, quotes)),
            ("concentration", lambda: self.cat_concentration(snap, report, hinfo, be_items)),
            ("sellability", lambda: self.cat_sellability(snap, usd_in, quotes)),
            ("behavioral", lambda: self.cat_behavioral(snap, report, hinfo, ctx, be_items)),
        ):
            try:
                cats[name] = fn()
            except Exception as e:
                log.warning("AntiRug %s check error for %s: %s", name, mint[:8], e)
                cats[name] = self._unknown(f"internal error: {type(e).__name__}")
                err = err or f"{name}: {type(e).__name__}"
        hard = [h for c in cats.values() for h in c["hard_blocks"]]
        # --- mandatory checks: every one must be explicitly PASS; anything not produced is UNKNOWN
        mand = {}
        for c in cats.values():
            mand.update(c.get("mandatory") or {})
        for n in MANDATORY:
            if n not in mand:
                mand[n] = _mand("UNKNOWN", "check not evaluated")
        mand = {n: mand[n] for n in MANDATORY}
        m_fail = [n for n, x in mand.items() if x["result"] == "FAIL"]
        m_unk = [n for n, x in mand.items() if x["result"] == "UNKNOWN"]
        # --- risk score: SEPARATE from the blocking conditions; it can add a block, never remove one
        w = self.c["weights"]
        known = {k: c["risk"] for k, c in cats.items() if c["status"] == "OK" and c["risk"] is not None}
        tw = sum(w.get(k, 0) for k in known)
        score = round(sum(w.get(k, 0) * v for k, v in known.items()) / tw, 1) if tw else None
        risk = max(score or 0, 80.0) if hard else score  # shown level: an absolute block is at least "critico"
        unknown = [k for k, c in cats.items() if c["status"] == "UNKNOWN"]
        if hard or m_fail:
            decision, basis = "BLOCK", "hard_block"
            reason = "hard:" + (hard[0] if hard else m_fail[0])
        elif m_unk:
            decision, basis, reason = "UNKNOWN", "mandatory_unknown", "unverified:" + ",".join(m_unk)
        elif score is None:
            decision, basis, reason = "UNKNOWN", "no_score", "unverified:" + ",".join(unknown)
        elif score >= self.c["block_risk_score"]:
            decision, basis, reason = "BLOCK", "risk_score", f"risk:{score:.0f}"
        else:
            decision, basis, reason = "ALLOW", "all_mandatory_pass", "ok"
        block_reasons = list(
            dict.fromkeys(
                hard
                + [f"mandatory_fail:{n}" for n in m_fail]
                + [f"mandatory_unknown:{n}" for n in m_unk]
                + ([reason] if basis == "risk_score" else [])
            )
        )
        n_ok = sum(1 for x in mand.values() if x["result"] != "UNKNOWN")
        uncertain = bool(cats.get("concentration", {}).get("metrics", {}).get("uncertain"))
        confidence = "low" if m_unk else "medium" if (uncertain or unknown) else "high"
        g = lambda k, f: (cats.get(k) or {}).get("metrics", {}).get(f)
        checks = [{"category": k, **ch} for k, c in cats.items() for ch in c["checks"]]
        v = {
            "mint": mint,
            "name": snap.get("name"),
            "ts": round(t0, 3),
            "time": _iso(t0),
            "mode": self.mode,
            "decision": decision,
            "decision_basis": basis,
            "reason": reason,
            "block_reasons": block_reasons,
            "risk": risk,
            "score": score,
            "level": level(risk),
            "hard_blocks": hard,
            "mandatory": mand,
            "mandatory_verified": f"{n_ok}/{len(mand)}",
            "confidence": confidence,
            "sources": {k: (c.get("metrics") or {}).get("source") for k, c in cats.items()}
            | {"sellability": "jupiter:/swap/v1/quote", "authorities": "rugcheck:/v1/tokens/{mint}/report"},
            "freshness_s": {
                "liquidity": g("liquidity", "age_s"),
                "quote_buy": g("sellability", "quote_age_buy_s"),
                "quote_sell": g("sellability", "quote_age_sell_s"),
                "rugcheck_report": self._age("rc_report", mint),
                "rugcheck_summary": self._age("rc_summary", mint),
                "birdeye_holders": self._age("be_holders", mint),
            },
            "unknown": unknown,
            "categories": cats,
            "price": snap.get("price"),
            "liquidity": snap.get("liquidity"),
            "entry_type": (ctx or {}).get("entry_type"),
            "signal": (ctx or {}).get("signal"),
            "core_decision": "BUY",
            "checks_passed": [f"{c['category']}.{c['check']}" for c in checks if c["result"] == "pass"],
            "checks_failed": [f"{c['category']}.{c['check']}" for c in checks if c["result"] == "fail"],
            "checks_unverified": [f"{c['category']}.{c['check']}" for c in checks if c["result"] == "unverified"],
            "error": err,
            "elapsed_s": round(self.clock() - t0, 3),
        }
        with self.lock:
            self.stats["evaluated"] += 1
            self.stats[decision.lower()] += 1
            if err:
                self.stats["errors"] += 1
            if v["level"] in self.stats["levels"]:
                self.stats["levels"][v["level"]] += 1
            if v["level"] == "critico":
                self.stats["critical"] += 1
            for h in hard:
                self.stats["hard"][h] = self.stats["hard"].get(h, 0) + 1
            self.recent.append(self._card(v))
        self._append(self.path_verdicts, v)
        return v

    @staticmethod
    def _card(v):
        """Compact per-token card for the dashboard: Birdeye / Jupiter / RugCheck results."""
        c = v["categories"]
        g = lambda k: c.get(k, {}).get("metrics", {})
        return {
            "time": v["time"],
            "name": v["name"],
            "mint": v["mint"],
            "decision": v["decision"],
            "reason": v["reason"],
            "risk": v["risk"],
            "level": v["level"],
            "hard_blocks": v["hard_blocks"],
            "unknown": v["unknown"],
            "entry_type": v["entry_type"],
            "risks": {k: x.get("risk") for k, x in c.items()},
            "birdeye": {
                k: g("concentration").get(k)
                for k in ("top10_pct", "top5_pct", "largest_pct", "source", "uncertain")
            }
            | {"liquidity_usd": g("liquidity").get("usd"), "change_pct": g("liquidity").get("change_pct")},
            "jupiter": {
                k: g("sellability").get(k)
                for k in (
                    "route_sell",
                    "sell_to",
                    "estimated_slippage_sell_pct",
                    "price_impact_sell_pct",
                    "roundtrip_loss_pct",
                )
            },
            "rugcheck": {
                k: g("technical").get(k)
                for k in ("mint_authority", "freeze_authority", "lp_locked_pct", "rugcheck_score_normalised")
            },
            "failed": v["checks_failed"][:8],
            "unverified": v["checks_unverified"][:8],
            "mandatory": {k: x["result"] for k, x in (v.get("mandatory") or {}).items()},
            "confidence": v.get("confidence"),
        }

    def enforced_hits(self, v):
        """Enforced hard blocks present in a verdict, each backed by VERIFIED data (fail-open on UNKNOWN)."""
        out = []
        if "holders_top10_concentrated" in self.enforce and "holders_top10_concentrated" in (
            v.get("hard_blocks") or []
        ):
            m = (v.get("mandatory") or {}).get("holders.top10") or {}
            val = _f(m.get("value_pct"))
            if m.get("result") == "FAIL" and val is not None and val > self.c["concentration"]["top10_block_pct"]:
                out.append("holders_top10_concentrated")
        return out

    def gate(self, v):
        """(allow, reject_reason). SHADOW never blocks (except the enforced checks, see ENFORCEABLE); ACTIVE is
        fail-closed."""
        if self.mode != "active":
            hits = self.enforced_hits(v) if self.enforce else []
            if not hits:
                return True, None
            v["enforced"] = hits
            m = v["mandatory"]["holders.top10"]
            with self.lock:
                self.stats["rejected_enforced"] = self.stats.get("rejected_enforced", 0) + 1
            self._append(
                self.path_verdicts,
                {
                    "kind": "enforced_block",
                    "ts": v["ts"],
                    "time": v["time"],
                    "mint": v["mint"],
                    "name": v.get("name"),
                    "mode": self.mode,
                    "enforce": list(self.enforce),
                    "enforced": hits,
                    "entry_type": v.get("entry_type"),
                    "top10_pct": m.get("value_pct"),
                    "threshold_pct": self.c["concentration"]["top10_block_pct"],
                    "source": m.get("source"),
                    "age_s": m.get("age_s"),
                    "detail": m.get("detail"),
                },
            )
            return False, "antirug_" + hits[0]
        if v.get("decision") == "ALLOW":
            return True, None
        with self.lock:
            self.stats["rejected_active"] += 1
        if v.get("decision") == "UNKNOWN":
            return False, "antirug_unknown"
        r = v.get("reason") or ""
        return False, ("antirug_" + r.split(":", 1)[1]) if r.startswith("hard:") else "antirug_high_risk"

    # ------------------------------------------------------------------ alerts
    def conditions(self, v):
        """Alert conditions of a verdict (deduped per token + condition)."""
        h = set(v["hard_blocks"])
        cond = []
        if v["decision"] == "BLOCK":
            cond.append("blocked")
        if v["level"] == "critico":
            cond.append("critical")
        if any(x.startswith("liquidity_drop") for x in h):
            cond.append("liquidity_falling")
        if h & {"not_sellable", "no_buy_route"}:
            cond.append("not_sellable")
        return cond

    def maybe_alert(self, v):
        a = self.c["alerts"]
        if not (a.get("enabled", True) and self.notify):
            return None
        now = self.clock()
        if v["decision"] == "UNKNOWN":
            key, ttl = "antirug:sources_down", a["sources_down_dedupe_s"]
            if now - self.alerted.get(key, 0) < ttl:
                return None
            self.alerted[key] = now
            down = [k for k, x in self.api.items() if x["fail"] or x["skipped"]]
            text = (
                f"🛡 <b>AntiRug {self.mode.upper()}: API di sicurezza non disponibili</b>\n"
                f"Controlli non verificati: {', '.join(v['unknown'])} · fonti con errori: {', '.join(down) or '—'}\n"
                + (
                    "Le nuove entrate sono BLOCCATE finché i dati tornano (fail-closed)."
                    if self.mode == "active"
                    else "SHADOW: continuo a registrare i segnali con i controlli non verificati."
                )
                + "\n<i>Solo simulazione</i>"
            )
            self._send(text, key)
            return text
        new = [c for c in self.conditions(v) if now - self.alerted.get(f"antirug:{v['mint']}:{c}", 0) >= a["dedupe_s"]]
        if not new:
            return None
        for c in new:
            self.alerted[f"antirug:{v['mint']}:{c}"] = now
        if len(self.alerted) > 2000:
            self.alerted = {k: x for k, x in self.alerted.items() if now - x < a["dedupe_s"]}
        text = self.alert_text(v, new)
        self._send(text, f"antirug:{v['mint']}:{'+'.join(new)}")
        return text

    def _send(self, text, key):
        try:
            self.notify(text, key)
        except Exception as e:
            log.warning("AntiRug alert failed: %s", e)

    def alert_text(self, v, conds):
        import html

        if v.get("enforced"):
            name = html.escape(str(v.get("name") or v["mint"][:6]))
            m = (v.get("mandatory") or {}).get("holders.top10") or {}
            return (
                f"🛡 <b>AntiRug ha BLOCCATO ${name}</b> (SHADOW + top10 applicato)\n"
                f"Top 10 holder {m.get('value_pct')}% > {self.c['concentration']['top10_block_pct']}% "
                f"(dato verificato: {html.escape(str(m.get('source') or '?'))})\n"
                "Gli altri controlli restano solo osservazione.\n<i>Solo simulazione</i>"
            )
        lab = {
            "blocked": "token bloccato" if self.mode == "active" else "avrebbe bloccato (SHADOW)",
            "critical": "rischio CRITICO",
            "liquidity_falling": "liquidità in calo",
            "not_sellable": "NON vendibile",
        }
        name = html.escape(str(v.get("name") or v["mint"][:6]))
        head = (
            f"🛡 <b>AntiRug ha BLOCCATO ${name}</b>"
            if self.mode == "active" and "blocked" in conds
            else f"👁 <b>AntiRug (SHADOW) ${name}</b> · nessuna azione, solo osservazione"
            if self.mode == "shadow"
            else f"🛡 <b>AntiRug ${name}</b>"
        )
        cats = " · ".join(f"{k[:5]} {'?' if c['risk'] is None else int(c['risk'])}" for k, c in v["categories"].items())
        return (
            f"{head}\n{', '.join(lab[c] for c in conds)}\n"
            f"Rischio {'?' if v['risk'] is None else int(v['risk'])}/100 ({v['level']}) · esito {v['decision']}\n"
            f"Motivo: {html.escape(', '.join(v['hard_blocks']) or v['reason'])}\n{cats}\n<i>Solo simulazione</i>"
        )

    # ------------------------------------------------------------------ shadow follow-ups / validation
    def track(self, v, entered):
        mint = v["mint"]
        sm = v["categories"].get("sellability", {}).get("metrics", {})
        with self.lock:
            old = self.shadow.get(mint)
            if old and v["ts"] - old["ts"] < self.c["shadow_reevaluate_min"] * 60:
                old["entered"] = old["entered"] or bool(entered)
                return
            if old:
                self._finish(mint)
            self.shadow[mint] = {
                "mint": mint,
                "name": v.get("name"),
                "ts": v["ts"],
                "time": v["time"],
                "price0": v.get("price"),
                "liq0": v.get("liquidity"),
                "signal": v.get("signal"),
                "entry_type": v.get("entry_type"),
                "core_decision": v.get("core_decision"),
                "decision": v["decision"],
                "reason": v["reason"],
                "risk": v["risk"],
                "level": v["level"],
                "hard": v["hard_blocks"],
                "mode": self.mode,
                "checks_passed": v["checks_passed"],
                "checks_failed": v["checks_failed"],
                "checks_unverified": v["checks_unverified"],
                "entered": bool(entered),
                "est_buy_slip_pct": sm.get("estimated_slippage_buy_pct"),
                "est_sell_slip_pct": sm.get("estimated_slippage_sell_pct"),
                "fu": {},
                "fu_liq": {},
                "hypo_pnl_pct": {},
                "vanished": False,
                "rug_observed": False,
                "liquidity_loss_observed": False,
                "executed_slippage_pct": None,
            }
            if v.get("enforced"):  # enforced SHADOW block: same +1/5/15/60 min follow-up, flagged
                self.shadow[mint]["enforced"] = list(v["enforced"])

    def note_entry(self, mint, executed_slip_pct, src):
        with self.lock:
            r = self.shadow.get(mint)
            if r:
                r["entered"] = True
                r["executed_slippage_pct"] = executed_slip_pct
                r["executed_slippage_src"] = src

    def due_followups(self):
        now = self.clock()
        with self.lock:
            return [
                m
                for m, r in self.shadow.items()
                if any(str(h) not in r["fu"] and now >= r["ts"] + h * 60 for h in self.c["shadow_followups_min"])
            ]

    def record_prices(self, snaps, answered=True):
        """snaps: mint -> {"price", "liquidity"} (or a bare price). Missing = not listed any more.
        answered=False (outage) -> nothing recorded (an outage is not a rug)."""
        if not answered:
            return
        now = self.clock()
        with self.lock:
            for m, r in list(self.shadow.items()):
                p0, l0 = _f(r.get("price0")), _f(r.get("liq0"))
                s = snaps.get(m)
                p = _f(s.get("price") if isinstance(s, dict) else s)
                liq = _f(s.get("liquidity")) if isinstance(s, dict) else None
                for h in self.c["shadow_followups_min"]:
                    k = str(h)
                    if k in r["fu"] or now < r["ts"] + h * 60:
                        continue
                    if p is None:
                        r["vanished"] = True
                        r["fu"][k] = -100.0
                    elif p0:
                        r["fu"][k] = round(100.0 * (p / p0 - 1), 2)
                    if liq is not None:
                        r["fu_liq"][k] = round(liq)
                        if l0 and 100.0 * (liq / l0 - 1) <= self.c["liquidity_loss_pct"]:
                            r["liquidity_loss_observed"] = True
                    dl = self.c.get("drained_liq_usd") or 0
                    drained = bool(dl and liq is not None and liq < dl and (l0 is None or l0 >= dl))
                    if k in r["fu"]:  # hypothetical trade incl. estimated slippage both legs
                        bs, ss = (r.get("est_buy_slip_pct") or 0) / 100, (r.get("est_sell_slip_pct") or 0) / 100
                        r["hypo_pnl_pct"][k] = round(100.0 * ((1 + r["fu"][k] / 100) * (1 - ss) / (1 + bs) - 1), 2)
                        if r["fu"][k] <= self.c["rug_like_pct"]:
                            r["rug_observed"] = True
                    if drained or r.get("pool_drained"):  # LP pulled: the last mid is frozen, nothing can be sold
                        r["pool_drained"] = True  # (live case 人生曲线 13/11: +80% mid on a $13 pool)
                        r["rug_observed"] = True
                        if k in r["fu"]:
                            r["hypo_pnl_pct"][k] = -100.0
                    if r["vanished"]:
                        r["rug_observed"] = True
                if all(str(h) in r["fu"] for h in self.c["shadow_followups_min"]):
                    self._finish(m)

    def _finish(self, mint):
        r = self.shadow.pop(mint, None)
        if not r:
            return
        r["done_ts"] = round(self.clock(), 3)
        self.completed.append(
            {
                k: r.get(k)
                for k in (
                    "mint",
                    "name",
                    "ts",
                    "decision",
                    "reason",
                    "risk",
                    "level",
                    "hard",
                    "mode",
                    "entered",
                    "fu",
                    "hypo_pnl_pct",
                    "vanished",
                    "rug_observed",
                    "liquidity_loss_observed",
                    "executed_slippage_pct",
                )
            }
            | ({"enforced": r["enforced"]} if r.get("enforced") else {})
            | ({"pool_drained": True} if r.get("pool_drained") else {})
        )
        del self.completed[:-500]
        self._append(self.path_log, r)

    def comparison(self):
        """Blocked vs allowed (vs unknown): performance, profitable opportunities lost, rugs avoided.
        A high block count alone says nothing: look at missed_profitable vs rugs_avoided."""
        rows = list(self.completed) + list(self.shadow.values())
        hz = self.c["shadow_followups_min"]
        out = {}
        groups = ("BLOCK", "ALLOW", "UNKNOWN") + (("ENFORCED",) if self.enforce else ())
        for grp in groups:
            g = [r for r in rows if (r.get("enforced") if grp == "ENFORCED" else r["decision"] == grp)]
            d = {"n": len(g)}
            for h in hz:
                vals = [r["fu"][str(h)] for r in g if str(h) in r.get("fu", {})]
                hyp = [r["hypo_pnl_pct"][str(h)] for r in g if str(h) in (r.get("hypo_pnl_pct") or {})]
                d[f"avg_{h}m"] = round(sum(vals) / len(vals), 2) if vals else None
                d[f"hypo_{h}m"] = round(sum(hyp) / len(hyp), 2) if hyp else None
                d[f"n_{h}m"] = len(vals)
            d["rug_like"] = sum(1 for r in g if r.get("rug_observed"))
            d["liquidity_loss"] = sum(1 for r in g if r.get("liquidity_loss_observed"))
            seen = [r for r in g if r.get("fu")]
            d["rug_like_pct"] = round(100.0 * d["rug_like"] / len(seen), 1) if seen else None
            last = str(hz[-1])
            d["profitable"] = sum(
                1 for r in g if (r.get("hypo_pnl_pct") or {}).get(last, -1e9) >= self.c["profitable_missed_pct"]
            )
            out[grp] = d
        out["rugs_avoided"] = out["BLOCK"]["rug_like"]
        out["profitable_missed"] = out["BLOCK"]["profitable"]
        out["rugs_allowed"] = out["ALLOW"]["rug_like"]
        if self.enforce:  # entries REALLY refused by the enforced check (observe-only statistics)
            out["enforced_rugs_avoided"] = out["ENFORCED"]["rug_like"]
            out["enforced_profitable_missed"] = out["ENFORCED"]["profitable"]
        return out

    def summary(self):
        with self.lock:
            return {
                "mode": self.mode,
                **({"enforce": list(self.enforce)} if self.enforce else {}),
                "stats": json.loads(json.dumps(self.stats)),
                "recent": list(self.recent)[-15:],
                "last_blocks": [r for r in self.recent if r["decision"] == "BLOCK"][-8:],
                "pending_followups": len(self.shadow),
                "completed": len(self.completed),
                "comparison": self.comparison(),
                "block_risk_score": self.c["block_risk_score"],
                "mandatory": list(MANDATORY),
                "api": json.loads(json.dumps(self.api)),
                "sources": {
                    "birdeye": bool(self.be),
                    "rugcheck": bool(self.rc_report),
                    "jupiter": bool(self.jup_quote),
                },
            }
