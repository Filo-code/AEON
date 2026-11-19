#!/usr/bin/env python3
"""Read-only web dashboard for the PAPER bot. Reads DATA_DIR/state.json written by the bot.
It never talks to the bot or to any exchange: it cannot place, change or cancel anything."""
import base64, hmac, ipaddress, json, os, threading, time
from datetime import datetime
from flask import Flask, Response, jsonify, render_template, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path):
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def load_json_safe(path):
    """Safely load JSON file, returning None if missing or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


load_dotenv(os.path.join(ROOT, ".env"))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(ROOT, "data"))
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
USER = os.getenv("DASHBOARD_USER", "admin")
BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "AEON by Rekt")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TZ") or "Europe/Rome")
except Exception:
    TZ = None

WEAK = {"", "cambia-questa-password", "password", "admin", "123456"}


def check_password():
    """The dashboard is meant to be reachable from the internet: refuse to run without a real password."""
    if PASSWORD in WEAK or len(PASSWORD) < 8:
        raise SystemExit("ERRORE: imposta DASHBOARD_PASSWORD nel file .env (almeno 8 caratteri, non quella di esempio). "
                         "La dashboard non parte senza password.")


app = Flask(__name__, static_folder=os.path.join(os.path.dirname(__file__), "static"), template_folder=os.path.join(os.path.dirname(__file__), "templates"))

REASONS = {"stop_loss": "stop loss", "take_profit": "take profit", "trailing_stop": "trailing stop",
           "time_exit": "tempo scaduto", "time_exit_stale": "tempo scaduto (prezzo vecchio)",
           "no_price_data_assume_rug": "rug (nessun prezzo)", "end_of_run": "fine sessione",
           "manual_close": "chiusura manuale", "top_traders_sold": "i top trader hanno venduto",
           "unconfirmed_price_exit": "prezzo non confermato (Jupiter)"}
AGENTS = [  # key, label, subtitle, color
    ("stream", "STREAM", "new pairs", "#3ddc84"), ("trend", "TREND", "hot rank", "#3ddc84"),
    ("volume", "VOLUME", "vol spikes", "#b8e34a"), ("smart", "SMART$", "top traders", "#f5a623"),
    ("copy", "COPY", "mirror size", "#ff5fa2"), ("holders", "HOLDERS", "top-10 share", "#38bdf8"),
    ("whales", "WHALES", "whale moves", "#a78bfa"), ("dev", "DEV", "dev wallet", "#ff4d4f")]


FAILS = {}   # ip -> timestamps of failed logins (simple brute-force protection)
FAILS_LOCK = threading.Lock()   # waitress serves requests from several threads


PROXY_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                  "::1/128", "fc00::/7")]   # loopback + RFC1918 (docker) + ULA


def _is_proxy(ip):
    try:
        a = ipaddress.ip_address(ip)
        return any(a in n for n in PROXY_NETS)
    except ValueError:
        return False


def client_ip():
    """Real client IP for the brute-force limiter. Forwarded headers are trusted ONLY when the peer is our own
    reverse proxy (loopback/private: Caddy, cloudflared). Caddy APPENDS the real peer to X-Forwarded-For, so the
    LAST entry is the trustworthy one (the first can be forged by the client)."""
    ip = request.remote_addr or "?"
    if _is_proxy(ip):
        cf = request.headers.get("CF-Connecting-IP", "").strip()
        fwd = [x.strip() for x in request.headers.get("X-Forwarded-For", "").split(",") if x.strip()]
        if cf:
            ip = cf
        elif fwd:
            ip = fwd[-1]
    return ip


@app.before_request
def auth():
    if request.path == "/healthz":
        return None
    ip, t = client_ip(), time.time()
    with FAILS_LOCK:
        recent = [x for x in FAILS.get(ip, []) if t - x < 600]
        if len(recent) >= 10:
            return Response("Troppi tentativi, riprova tra 10 minuti", 429)
    h = request.headers.get("Authorization", "")
    if h.startswith("Basic "):
        try:
            u, p = base64.b64decode(h[6:]).decode().split(":", 1)
            if hmac.compare_digest(p.encode(), PASSWORD.encode()) and hmac.compare_digest(u.encode(), USER.encode()):
                return None
        except Exception:
            pass
        with FAILS_LOCK:
            recent.append(t)
            FAILS[ip] = recent
            if len(FAILS) > 5000:
                FAILS.clear()
    return Response("Password richiesta", 401, {"WWW-Authenticate": 'Basic realm="paper-bot"'})


@app.after_request
def security_headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("X-Frame-Options", "DENY")
    r.headers.setdefault("Referrer-Policy", "no-referrer")
    r.headers.setdefault("Cache-Control", "no-store")
    if request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure:
        r.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return r


def heartbeat_ts(st):
    """Liveness: heartbeat.json is rewritten every few seconds by the bot (state.json only every 15 s)."""
    hb = st.get("heartbeat_ts") or 0
    try:
        hb = max(hb, float(json.load(open(os.path.join(DATA_DIR, "heartbeat.json"))).get("ts") or 0))
    except Exception:
        pass
    return hb


def hhmm(ts):
    if not ts:
        return "--:--"
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def view():
    try:
        st = load_json_safe(os.path.join(DATA_DIR, "state.json"))
        if st is None:
            return {"ok": False, "error": "state.json non ancora creato: il bot sta partendo?"}
    except Exception as e:
        return {"ok": False, "error": f"state.json illeggibile: {type(e).__name__}"}
    try:
        result = _view(st)
        # Load Pump.fun state
        result["pumpfun"] = _normalize_pumpfun_state(
            load_json_safe(os.path.join(DATA_DIR, "pumpfun_state.json"))
        )
        # Load GMGN state
        result["gmgn"] = _normalize_gmgn_state(
            load_json_safe(os.path.join(DATA_DIR, "gmgn_state.json"))
        )
        # Source status summary
        result["sources"] = _build_source_status(result)
        return result
    except Exception as e:
        return {"ok": False, "error": f"stato non interpretabile ({type(e).__name__})"}


def _normalize_pumpfun_state(raw):
    """Normalize Pump.fun scanner state for dashboard."""
    if not raw:
        return {
            "enabled": False, "connected": False, "subscribed": False,
            "receiving_events": False, "state": "not_configured",
            "received": 0, "last_event_ts": None, "last_error": None,
            "updated_ts": None, "recent_events": [],
        }
    # Extract events and normalize field names for frontend
    events = raw.get("events", [])[:20]
    norm_events = []
    for e in events:
        norm_events.append({
            "source": "pumpfun",
            "event_type": e.get("kind") or e.get("event_type"),
            "token_symbol": e.get("symbol") or e.get("token_symbol"),
            "token_name": e.get("name") or e.get("token_name"),
            "token_mint": e.get("mint") or e.get("token_mint"),
            "pool_address": e.get("pool") or e.get("pool_address"),
            "received_at": e.get("ts") or e.get("received_at"),
            "block_time": e.get("block_time"),
            "price_usd": e.get("price_usd"),
            "liquidity_usd": e.get("liquidity_usd"),
            "volume_usd": e.get("volume_usd"),
            "data_quality": e.get("data_quality"),
            "verification_status": e.get("verification_status"),
        })
    return {
        "enabled": raw.get("enabled", False),
        "connected": raw.get("connected", False),
        "subscribed": raw.get("subscribed", False),
        "receiving_events": raw.get("received", 0) > 0,
        "state": raw.get("state", "unknown"),
        "received": raw.get("received", 0),
        "events_by_channel": raw.get("events_by_channel", {}),
        "last_event_ts": raw.get("last_event_ts"),
        "last_error": raw.get("last_error"),
        "updated_ts": raw.get("updated_ts"),
        "recent_events": norm_events,
    }


def _normalize_gmgn_state(raw):
    """Normalize GMGN scanner state for dashboard."""
    if not raw:
        return {
            "enabled": False, "connected": False, "subscribed": False,
            "state": "not_configured", "received": 0,
            "events_by_channel": {}, "last_event_ts": None,
            "last_error": None, "updated_ts": None,
            "recent_events": [],
        }
    events = raw.get("events", [])[:20]
    norm_events = []
    for e in events:
        norm_events.append({
            "source": "gmgn",
            "event_type": e.get("event_type"),
            "token_symbol": e.get("token_symbol"),
            "token_name": e.get("token_name"),
            "token_mint": e.get("token_mint"),
            "pool_address": e.get("pool_address"),
            "received_at": e.get("received_at"),
            "block_time": e.get("block_time"),
            "price_usd": e.get("price_usd"),
            "liquidity_usd": e.get("liquidity_usd"),
            "volume_usd": e.get("volume_usd"),
            "data_quality": e.get("data_quality"),
            "verification_status": e.get("verification_status"),
        })
    return {
        "enabled": raw.get("enabled", False),
        "connected": raw.get("connected", False),
        "subscribed": raw.get("subscribed", False),
        "state": raw.get("state", "unknown"),
        "received": raw.get("received", 0),
        "events_by_channel": raw.get("events_by_channel", {}),
        "last_event_ts": raw.get("last_event_ts"),
        "last_error": raw.get("last_error"),
        "updated_ts": raw.get("updated_ts"),
        "recent_events": norm_events,
    }


def _build_source_status(api_data):
    """Build unified source status from all data sources using actual provider telemetry."""
    pf = api_data.get("pumpfun", {})
    gm = api_data.get("gmgn", {})
    hosts = api_data.get("health", {}).get("hosts", {})

    def _host_status(hostname, fallback_sub=None):
        """Extract actual telemetry for an HTTP provider host. Never derive from ops_per_min."""
        h = hosts.get(hostname)
        if not h and fallback_sub:
            h = next((v for k, v in hosts.items() if fallback_sub in k), None)
        if not h:
            return {"connected": False, "state": "UNKNOWN", "last_event_ts": None, "last_error": None}
        consec = h.get("consec", 0)
        open_until = float(h.get("open_until", 0.0) or 0.0)
        last_ok = h.get("last_ok")
        last_kind = h.get("last_kind")
        now_ts = time.time()
        if now_ts < open_until or consec >= 5:
            return {"connected": False, "state": "ERROR", "last_event_ts": last_ok, "last_error": last_kind or "circuit_open"}
        elif last_ok is not None and (now_ts - last_ok <= 180):
            return {"connected": True, "state": "CONNECTED", "last_event_ts": last_ok, "last_error": None}
        elif last_ok is not None:
            return {"connected": False, "state": "STALE", "last_event_ts": last_ok, "last_error": last_kind}
        elif consec > 0:
            return {"connected": False, "state": "ERROR", "last_event_ts": None, "last_error": last_kind}
        else:
            return {"connected": False, "state": "UNKNOWN", "last_event_ts": None, "last_error": None}

    gt_info = _host_status("api.geckoterminal.com", "geckoterminal")
    ds_info = _host_status("api.dexscreener.com", "dexscreener")

    return [
        {
            "source": "gmgn",
            "label": "GMGN",
            "enabled": gm.get("enabled", False),
            "connected": gm.get("connected", False),
            "subscribed": gm.get("subscribed", False),
            "state": gm.get("state", "disabled"),
            "received": gm.get("received", 0),
            "events_by_channel": gm.get("events_by_channel", {}),
            "last_event_ts": gm.get("last_event_ts"),
            "last_error": gm.get("last_error"),
            "updated_ts": gm.get("updated_ts"),
        },
        {
            "source": "pumpfun",
            "label": "Pump.fun",
            "enabled": pf.get("enabled", False),
            "connected": pf.get("connected", False),
            "subscribed": pf.get("subscribed", False),
            "receiving_events": pf.get("receiving_events", False),
            "state": pf.get("state", "disabled"),
            "received": pf.get("received", 0),
            "events_by_channel": pf.get("events_by_channel", {}),
            "last_event_ts": pf.get("last_event_ts"),
            "last_error": pf.get("last_error"),
            "updated_ts": pf.get("updated_ts"),
        },
        {
            "source": "geckoterminal",
            "label": "GeckoTerminal",
            "enabled": True,
            "connected": gt_info["connected"],
            "subscribed": False,
            "state": gt_info["state"],
            "received": api_data.get("stats", {}).get("pools_seen", 0),
            "events_by_channel": {},
            "last_event_ts": gt_info["last_event_ts"],
            "last_error": gt_info["last_error"],
            "updated_ts": None,
        },
        {
            "source": "dexscreener",
            "label": "DexScreener",
            "enabled": True,
            "connected": ds_info["connected"],
            "subscribed": False,
            "state": ds_info["state"],
            "received": api_data.get("stats", {}).get("signals", 0),
            "events_by_channel": {},
            "last_event_ts": ds_info["last_event_ts"],
            "last_error": ds_info["last_error"],
            "updated_ts": None,
        },
    ]


def _is_completed_trade(t):
    """Return True if this closed record represents a full completed trade (not a partial exit leg)."""
    cat = t.get("counts_as_trade")
    if cat is not None:
        if isinstance(cat, str):
            return cat.strip().lower() not in ("false", "0", "no")
        return bool(cat)
    # counts_as_trade is missing (legacy record)
    partial = t.get("partial")
    if partial is True or (isinstance(partial, str) and partial.strip().lower() in ("true", "1", "yes")) or partial == 1:
        return False
    exit_reason = str(t.get("exit_reason", "")).strip().lower()
    if exit_reason in ("take_profit_1", "tp1", "partial_exit"):
        return False
    return True


def _rpc_health(rpc_raw):
    """Build a canonical RPC health object with response freshness verification.
    
    Distinguishes HEALTHY, DEGRADED, DOWN, and UNKNOWN.
    If required timestamps are not available, returns UNKNOWN.
    Never infers health from heartbeat or historical cumulative successes alone.
    """
    if not rpc_raw or not isinstance(rpc_raw, dict):
        return {"state": "unknown", "label": "UNKNOWN", "cls": "offline", "errors": 0, "ok": 0, "latency_ms": None}

    raw_state = str(rpc_raw.get("state", "")).lower()
    if raw_state in ("disconnected", "unreachable", "circuit_open", "down"):
        return {"state": "down", "label": "DOWN", "cls": "offline",
                "errors": rpc_raw.get("fail", 0), "ok": rpc_raw.get("ok", 0), "latency_ms": None}

    now_ts = time.time()
    ok = rpc_raw.get("ok", 0)
    fail = rpc_raw.get("fail", 0)

    # Extract timestamps from rpc_raw or provider stats
    last_success = rpc_raw.get("last_success_ts")
    last_failure = rpc_raw.get("last_failure_ts")
    providers = rpc_raw.get("providers", {})
    consec_max = rpc_raw.get("consecutive_failures", 0)
    latency = rpc_raw.get("current_latency_ms") or rpc_raw.get("avg_latency_ms")

    all_circuit_open = len(providers) > 0
    for pv in providers.values():
        consec_max = max(consec_max, pv.get("consecutive_failures", 0))
        if pv.get("last_success_ts"):
            last_success = max(last_success or 0, pv["last_success_ts"])
        if pv.get("last_failure_ts"):
            last_failure = max(last_failure or 0, pv["last_failure_ts"])
        if pv.get("current_latency_ms") is not None and pv["current_latency_ms"] > 0:
            latency = pv["current_latency_ms"]
        if not pv.get("circuit_open", False) and str(pv.get("state", "")).lower() not in ("disconnected", "circuit_open"):
            all_circuit_open = False

    # Hard down condition
    if all_circuit_open or (ok == 0 and fail > 0):
        return {"state": "down", "label": "DOWN", "cls": "offline",
                "errors": fail, "ok": ok, "latency_ms": None}

    # If required timestamps are not available, return UNKNOWN instead of inventing a healthy status
    if last_success is None:
        return {"state": "unknown", "label": "UNKNOWN", "cls": "offline",
                "errors": fail, "ok": ok, "latency_ms": round(latency, 1) if latency else None}

    # Verify freshness of last successful response
    age = now_ts - last_success

    if age > 180:
        # Stale (>3m)
        state = "down" if (last_failure and last_failure > last_success) else "degraded"
        label = "DOWN" if state == "down" else "DEGRADED"
        cls = "offline" if state == "down" else "warning"
    elif age > 60:
        # Moderately stale (>60s)
        state, label, cls = "degraded", "DEGRADED", "warning"
    elif consec_max >= 5 or (ok + fail > 0 and fail / (ok + fail) > 0.1):
        state, label, cls = "degraded", "DEGRADED", "warning"
    else:
        state, label, cls = "healthy", "HEALTHY", "ok"

    return {"state": state, "label": label, "cls": cls,
            "errors": fail, "ok": ok, "latency_ms": round(latency, 1) if latency else None,
            "last_success_age_s": round(age, 1)}


def _classify_brain_change(c):
    """Classify brain change record into REJECTED, ACCEPTED, or UNKNOWN.
    
    Rules:
    - rejected=true -> REJECTED
    - rejected=false -> ACCEPTED
    - missing rejected field -> UNKNOWN (unless explicit status 'confermata'/'annullata')
    """
    if "rejected" in c and c["rejected"] is not None:
        rej = c["rejected"]
        is_rej = rej.strip().lower() in ("true", "1", "yes") if isinstance(rej, str) else bool(rej)
        return "REJECTED" if is_rej else "ACCEPTED"
    if "accepted" in c and c["accepted"] is not None:
        acc = c["accepted"]
        is_acc = acc.strip().lower() in ("true", "1", "yes") if isinstance(acc, str) else bool(acc)
        return "ACCEPTED" if is_acc else "REJECTED"
    status = str(c.get("status", "")).strip().lower()
    if status in ("annullata", "rejected", "rollback", "rolled_back"):
        return "REJECTED"
    elif status in ("confermata", "accepted", "confirmed"):
        return "ACCEPTED"
    elif status in ("in verifica", "pending", "testing", "evaluating"):
        return "PENDING"
    else:
        # missing rejected field -> UNKNOWN
        return "UNKNOWN"


def _view(st):
    now = time.time()
    start = st["starting_balance_usd"]
    pos = st.get("positions", {})
    closed = st.get("closed", [])

    # ── Canonical financial metrics ──────────────────────────────────────────
    invested_capital = round(sum(float(p.get("cost_usd", 0.0)) for p in pos.values()), 4)
    open_val = round(sum(float(p.get("qty", 0.0)) * float(p.get("last", 0.0)) for p in pos.values()), 4)
    unrealized_pnl = round(open_val - invested_capital, 4)
    unrealized_pnl_pct = round(100.0 * unrealized_pnl / invested_capital, 2) if invested_capital > 0 else 0.0
    cash = st["cash"]
    equity = round(cash + open_val, 4)
    total_pnl = round(equity - float(start), 4)
    return_pct = round(100.0 * total_pnl / float(start), 2) if float(start) > 0 else 0.0

    # Realized PnL = sum of all closed execution PnLs (all legs including partials)
    realized_pnl = round(sum(float(t.get("pnl_usd", 0.0)) for t in closed), 4)

    # Fee aggregation
    fees_dex = round(sum(float(t.get("dex_fees_usd", 0.0)) for t in closed), 4)
    fees_net = round(sum(float(t.get("network_fees_usd", 0.0)) for t in closed), 4)
    fees_total = round(fees_dex + fees_net, 4)

    # ── Completed-trade metrics (corrected, partial-aware) ───────────────────
    completed_trades = [t for t in closed if _is_completed_trade(t)]
    partial_exits = [t for t in closed if not _is_completed_trade(t)]

    def _outcome_pnl(t):
        """Canonical outcome PnL for a completed trade (uses cumulative_pnl_usd when available)."""
        return float(t.get("cumulative_pnl_usd", t.get("pnl_usd", 0.0)))

    completed_wins = [t for t in completed_trades if _outcome_pnl(t) > 0]
    completed_losses = [t for t in completed_trades if _outcome_pnl(t) <= 0]
    ct = len(completed_trades)
    win_rate_canonical = round(100.0 * len(completed_wins) / ct, 1) if ct else None

    # Legacy counts (kept for backward-compat fields)
    n = len(closed)
    wins_legacy = [t for t in closed if t.get("pnl_usd", 0) > 0]

    # ── Strategy aggregation (completed-trade aware) ─────────────────────────
    strat_data = {}
    for t in completed_trades:
        k = t.get("entry_type", "unknown")
        s = strat_data.setdefault(k, {"wins": 0, "losses": 0, "pnl": 0.0, "hold_secs": []})
        if _outcome_pnl(t) > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["pnl"] = round(s["pnl"] + _outcome_pnl(t), 4)
        if t.get("hold_sec"):
            s["hold_secs"].append(t["hold_sec"])
    # Partial exits also contribute execution PnL to strategy cash flow
    for t in partial_exits:
        k = t.get("entry_type", "unknown")
        s = strat_data.setdefault(k, {"wins": 0, "losses": 0, "pnl": 0.0, "hold_secs": []})
        # Partial does not count as a win/loss outcome, but we track execution pnl for cash accounting
        s["partial_pnl"] = round(s.get("partial_pnl", 0.0) + float(t.get("pnl_usd", 0.0)), 4)

    exit_rules_cfg = st.get("exit_rules") or {}
    cfg_all = st.get("config", {})
    open_by_strategy = {}
    for p in pos.values():
        k = p.get("entry_type", "unknown")
        open_by_strategy[k] = open_by_strategy.get(k, 0) + 1

    STRATEGY_LABELS = {
        "volume_spike": "Volume Spike",
        "copy_top_traders": "Copy Top Traders",
        "stream": "Stream",
        "trend": "Trend",
        "holders": "Holders",
        "whales": "Whales",
        "dev": "Dev Wallet",
    }
    strategies_list = []
    all_strategy_keys = set(strat_data) | set(open_by_strategy) | set(exit_rules_cfg)
    for k in all_strategy_keys:
        sd = strat_data.get(k, {"wins": 0, "losses": 0, "pnl": 0.0, "hold_secs": []})
        st_trades = sd["wins"] + sd["losses"]
        avg_hold = round(sum(sd.get("hold_secs", [])) / len(sd["hold_secs"]), 0) if sd.get("hold_secs") else None
        strategies_list.append({
            "key": k,
            "name": STRATEGY_LABELS.get(k, k.replace("_", " ").title()),
            "enabled": k in exit_rules_cfg,
            "trades": st_trades,
            "total_executions": st_trades + len([t for t in partial_exits if t.get("entry_type") == k]),
            "wins": sd["wins"],
            "losses": sd["losses"],
            "win_rate": round(100.0 * sd["wins"] / st_trades, 1) if st_trades else None,
            "realized_pnl": sd["pnl"],
            "avg_pnl": round(sd["pnl"] / st_trades, 4) if st_trades else None,
            "avg_hold_sec": avg_hold,
            "open_positions": open_by_strategy.get(k, 0),
        })
    strategies_list.sort(key=lambda s: s["trades"], reverse=True)

    # Backward-compat by_type (uses all executions, not just completed trades)
    by_type, by_reason = {}, {}
    for t in closed:
        e = by_type.setdefault(t.get("entry_type", "unknown"), {"n": 0, "pnl": 0.0, "wins": 0})
        e["n"] += 1; e["pnl"] += t.get("pnl_usd", 0.0); e["wins"] += t.get("pnl_usd", 0.0) > 0
        reason_key = REASONS.get(t.get("exit_reason", ""), t.get("exit_reason", "?"))
        r = by_reason.setdefault(reason_key, {"n": 0, "pnl": 0.0})
        r["n"] += 1; r["pnl"] += t.get("pnl_usd", 0.0)

    # ── Brain intelligence ───────────────────────────────────────────────────
    brn_raw = st.get("brain") or {}
    changes_raw = brn_raw.get("changes", [])
    changes_enriched = [{**c, "classification": _classify_brain_change(c)} for c in changes_raw]
    accepted_changes = [c for c in changes_enriched if c["classification"] == "ACCEPTED"]
    rejected_changes = [c for c in changes_enriched if c["classification"] == "REJECTED"]
    unknown_changes = [c for c in changes_enriched if c["classification"] == "UNKNOWN"]
    pending_changes = [c for c in changes_enriched if c["classification"] == "PENDING"]
    pending = brn_raw.get("pending")
    brain_info = {
        "mode": brn_raw.get("mode", "legacy"),
        "phase": brn_raw.get("phase"),
        "enabled": brn_raw.get("enabled", False),
        "records": brn_raw.get("records", 0),
        "analysis_interval": brn_raw.get("analysis_interval"),
        "next_in": brn_raw.get("next_in"),
        "next_at_trade": (brn_raw.get("records", 0) + brn_raw.get("next_in", 0)) if brn_raw.get("next_in") is not None else None,
        "last_ts": brn_raw.get("last_ts"),
        "diagnosis": brn_raw.get("diagnosis", ""),
        "params": brn_raw.get("params", {}),
        "pending": pending,
        "changes": changes_enriched[-30:],
        "accepted_changes": accepted_changes[-20:],
        "rejected_changes": rejected_changes[-20:],
        "unknown_changes": unknown_changes[-20:],
        "unconfirmed_changes": unknown_changes[-20:],
        "pending_changes": pending_changes[-20:],
        "accepted_count": len(accepted_changes),
        "rejected_count": len(rejected_changes),
        "unknown_count": len(unknown_changes),
        "unconfirmed_count": len(unknown_changes),
        "pending_count": len(pending_changes),
        "locked": brn_raw.get("locked", []),
    }

    # ── AntiRug intelligence (mode-aware) ────────────────────────────────────
    ar_raw = st.get("antirug") or {}
    ar_mode_raw = str(ar_raw.get("mode", "off")).strip().lower()
    if ar_mode_raw in ("active", "on"):
        ar_mode = "active"
        ar_mode_label = "ACTIVE"
        ar_mode_desc = "Blocco attivo degli ordini a rischio"
    elif ar_mode_raw in ("shadow", "monitor", "observe"):
        ar_mode = "shadow"
        ar_mode_label = "SHADOW"
        ar_mode_desc = "Monitoraggio e osservazione (nessun blocco ordini)"
    else:
        ar_mode = "off"
        ar_mode_label = "OFF"
        ar_mode_desc = "Disattivato (nessun controllo applicato)"

    ar_recent_raw = ar_raw.get("recent", [])
    ar_recent = []
    for r in ar_recent_raw:
        decision = str(r.get("decision", "ALLOW")).strip().upper()
        if ar_mode == "off":
            action = "DISABLED"
            action_cls = "neutral"
        elif ar_mode == "shadow":
            if decision == "BLOCK":
                action = "OBSERVED"
                action_cls = "warning"
            else:
                action = "OBSERVED_ALLOW"
                action_cls = "neutral"
        else: # active
            if decision == "BLOCK":
                action = "BLOCKED"
                action_cls = "danger"
            else:
                action = "ALLOWED"
                action_cls = "success"
        ar_recent.append({**r, "effective_action": action, "effective_cls": action_cls})

    antirug_info = {
        "mode": ar_mode,
        "mode_label": ar_mode_label,
        "mode_desc": ar_mode_desc,
        "enforce": list(ar_raw.get("enforce", [])),
        "stats": ar_raw.get("stats", {}),
        "recent": ar_recent[-15:],
        "last_blocks": [r for r in ar_recent if r.get("effective_action") == "BLOCKED"][-8:],
        "last_observed": [r for r in ar_recent if r.get("effective_action") == "OBSERVED"][-8:],
        "pending_followups": ar_raw.get("pending_followups", 0),
        "completed": ar_raw.get("completed", 0),
    }

    # ── RPC health (canonical) ───────────────────────────────────────────────
    rpc_raw = st.get("rpc") or {}
    rpc_health = _rpc_health(rpc_raw)

    # ── Position enrichment ──────────────────────────────────────────────────
    positions_enriched = []
    for p in pos.values():
        last_px = float(p.get("last", 0.0))
        cost = float(p.get("cost_usd", 0.0))
        qty = float(p.get("qty", 0.0))
        entry_mid = float(p.get("entry_mid", 1.0)) or 1.0
        upnl = round(qty * last_px - cost, 4)
        positions_enriched.append({
            **p,
            "held_min": round((now - float(p.get("entry_ts", now))) / 60, 1),
            "chg_pct": round(100.0 * (last_px / entry_mid - 1), 2) if entry_mid else 0,
            "value": round(qty * last_px, 4),
            "entry_clock": hhmm(p.get("entry_ts")),
            "upnl": upnl,
            "adaptive_holding_enabled": p.get("adaptive_holding_enabled", False),
            "adaptive_holding_stage": p.get("adaptive_holding_stage", 0),
            "adaptive_holding_extensions": p.get("adaptive_holding_extensions", 0),
            "adaptive_holding_next_check": p.get("adaptive_holding_next_check"),
            "adaptive_holding_last_decision": p.get("adaptive_holding_last_decision"),
            "adaptive_holding_last_reason": p.get("adaptive_holding_last_reason"),
            "adaptive_holding_max_deadline": p.get("adaptive_holding_max_deadline"),
            "adaptive_holding_baseline_quote": p.get("adaptive_holding_baseline_quote"),
        })

    # ── Closed trade enrichment (with partial flag) ──────────────────────────
    closed_enriched = []
    for t in reversed(closed[-300:]):
        is_partial = not _is_completed_trade(t)
        cum_pnl = float(t.get("cumulative_pnl_usd", t.get("pnl_usd", 0.0)))
        closed_enriched.append({
            **t,
            "exit_clock": hhmm(t.get("exit_ts")),
            "entry_clock": hhmm(t.get("entry_ts")),
            "reason_it": REASONS.get(t.get("exit_reason", ""), t.get("exit_reason", "?")),
            "is_partial": is_partial,
            "trade_type_label": "USCITA PARZIALE" if is_partial else "CHIUSURA COMPLETA",
            "cumulative_pnl_display": cum_pnl,
            "is_winning": cum_pnl > 0,
        })

    eq = st.get("equity", [])
    hour_ago = [e for e in eq if e[0] <= now - 3600]
    ref = hour_ago[-1][1] if hour_ago else (eq[0][1] if eq else start)
    hb_age = now - heartbeat_ts(st)
    status = st.get("status", "?")
    live = status in ("running", "dead") and hb_age < 90
    first_ts = eq[0][0] if eq else st.get("meta", {}).get("created_ts", now)
    agents = []
    ag = st.get("agents", {})
    last_active = max(ag.items(), key=lambda kv: kv[1].get("ts", 0))[0] if ag else None
    for k, label, sub, color in AGENTS:
        a = ag.get(k, {"n": 0, "ok": 0, "last": "", "ts": 0})
        pctv = a["load"] if a.get("load") is not None else (round(100 * a["ok"] / a["n"]) if a["n"] else 0)
        agents.append({"key": k, "label": label, "sub": sub, "color": color, "n": a["n"], "pct": pctv,
                       "last": a.get("last") or "in attesa…", "active": k == last_active,
                       "idle": not a["n"]})
    stalls = st.get("meta", {}).get("stalls", [])
    return {
        "ok": True, "now": now, "paused": bool(st.get("paused")), "dead": bool(st.get("dead")),
        "fx": st.get("fx_usd_per_eur") or 1.0, "fx_now": st.get("fx_now"), "survival": st.get("survival"),
        "brain": brn_raw, "brain_info": brain_info, "clock": hhmm(now), "status": status,
        "live": live, "heartbeat_age": round(hb_age),
        # Canonical financial metrics
        "start": start, "cash": cash, "equity": equity, "open_value": open_val,
        "invested_capital": invested_capital,
        "unrealized_pnl": unrealized_pnl, "unrealized_pnl_pct": unrealized_pnl_pct,
        "realized_pnl": realized_pnl,
        "total_pnl": total_pnl, "return_pct": return_pct,
        "fees_dex": fees_dex, "fees_net": fees_net, "fees_total": fees_total,
        "realized_value": cash + sum(float(p.get("cost_usd", 0)) for p in pos.values()),
        "risk": st.get("risk"), "health": st.get("health"),
        # Legacy / compat fields
        "pnl": total_pnl, "mult": equity / float(start) if float(start) else 0, "change_1h": equity - ref,
        "realized": realized_pnl,
        # Canonical trade counts (partial-aware)
        "completed_trades_count": ct,
        "completed_wins_count": len(completed_wins),
        "completed_losses_count": len(completed_losses),
        "completed_win_rate": win_rate_canonical,
        "partial_exits_count": len(partial_exits),
        # Legacy trade counts (all executions, kept for compat)
        "trades": n, "wins": len(wins_legacy), "losses": n - len(wins_legacy),
        "win_rate": win_rate_canonical,  # upgraded to canonical
        "uptime_sec": now - first_ts, "first_ts": first_ts, "start_clock": hhmm(first_ts),
        "equity_curve": eq[-1500:],
        "positions": positions_enriched,
        "closed": closed_enriched,
        "events": [{**e, "clock": hhmm(e["ts"])} for e in reversed(st.get("events", [])[-40:])],
        "by_type": by_type, "by_reason": by_reason,
        "strategies": strategies_list,
        "avg_buy_slip": sum(float(t.get("buy_slip_pct", 0)) for t in closed) / n if n else None,
        "avg_sell_slip": sum(float(t.get("sell_slip_pct", 0)) for t in closed) / n if n else None,
        "slip_cost": sum(float(t.get("cost_usd", 0)) * (float(t.get("buy_slip_pct", 0)) + float(t.get("sell_slip_pct", 0))) / 100 for t in closed),
        "failed_exits": st.get("stats", {}).get("failed_exits", 0),
        "best": max(completed_trades, key=lambda t: _outcome_pnl(t)) if completed_trades else None,
        "worst": min(completed_trades, key=lambda t: _outcome_pnl(t)) if completed_trades else None,
        "coins": len({t.get("mint") for t in closed} | set(pos)),
        "agents": agents, "running": sum(1 for a in agents if not a["idle"]),
        "ops_per_min": st.get("ops_per_min", 0), "signals": st.get("stats", {}).get("signals", 0),
        "pools_seen": st.get("stats", {}).get("pools_seen", 0), "watch": st.get("watch_count", 0),
        "ticker": st.get("ticker", []), "meta": st.get("meta", {}), "stalls": stalls[-10:],
        "stall_total_min": round(sum(s.get("sec", 0) for s in stalls) / 60),
        "vybe": st.get("vybe"), "copy_traders": st.get("copy_traders", 0), "telegram": st.get("telegram"),
        "config": st.get("config", {}), "filtered": st.get("stats", {}).get("filtered_out", {}),
        "exit_rules": st.get("exit_rules") or {}, "jupiter": st.get("jupiter") or {},
        "rpc": rpc_raw, "rpc_health": rpc_health,
        "slip_src": st.get("meta", {}).get("slip_src") or {},
        "slip_src_closed": {"jupiter": sum((t.get("buy_slip_src") == "jupiter") + (t.get("sell_slip_src") == "jupiter") for t in closed),
                            "stimato": sum((t.get("buy_slip_src", "stimato") != "jupiter") + (t.get("sell_slip_src", "stimato") != "jupiter") for t in closed)},
        "antirug": antirug_info,
        **({} if not st.get("risk_profile") else {"risk_profile": st["risk_profile"]}),
        **({} if not st.get("price_provenance") else {"price_provenance": st["price_provenance"]}),
        **({} if not st.get("session") else {"session": st["session"]}),
        "adaptive_poll": st.get("adaptive_poll", {}),
        "recovery_mode": st.get("recovery_mode", {}),
        "non_liquidatable_value": st.get("non_liquidatable_value", 0),
        "executable_liquidation_value": st.get("executable_liquidation_value", 0),
        "smart_money": st.get("smart_money", {"enabled": False, "rankings": {}, "total_tracked": 0, "total_verified": 0}),
    }


@app.route("/")
def index():
    # visible brand only (UI text): BOT_DISPLAY_NAME, default 'Ez__Rekt Bot'; not part of the /api/view JSON
    name = os.getenv("BOT_DISPLAY_NAME")
    name = ("Ez__Rekt Bot" if name is None else name.strip()[:40]) or "Paper bot"
    return render_template("index.html", brand=name)


@app.route("/api/state")
def api_state():
    return jsonify(view())


@app.route("/healthz")
def healthz():
    """200 = the dashboard works. ?strict=1 -> 503 when the bot heartbeat is stale (for external monitors)."""
    v = view()
    ok = bool(v.get("ok") and v.get("live"))
    code = 503 if (request.args.get("strict") and not ok) else 200
    return jsonify({"dashboard": "ok", "bot_live": ok, "heartbeat_age": v.get("heartbeat_age")}), code


if __name__ == "__main__":
    check_password()
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8080")))


def create_app():
    """Factory for `waitress-serve --call dashboard.app:create_app`. Refuses to start without a password."""
    check_password()
    return app
