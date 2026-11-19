"""First-startup procedure for the VPS (STARTUP_CHECKS=1 in .env). Runs the 14 checks of the deployment spec, sends
the report to Telegram and REFUSES TO START (exit code 78, systemd does not restart it) when a CRITICAL check fails.
External APIs that are down are reported clearly but are not critical: the documented safety behaviour applies
(Birdeye down -> AntiRug uses RugCheck/RPC data and COPY uses the other sources; Jupiter down -> prices are ESTIMATED
or entries REJECTED by price integrity; RugCheck down -> AntiRug records UNKNOWN). No secret is ever logged."""
import logging
import os
import shutil
import time

import requests

import antirug as antirug_mod
import profiles

log = logging.getLogger("paperbot.startup")
EX_CONFIG = 78
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _scrub(s, env):
    s = str(s)
    for k in (
        "TELEGRAM_BOT_TOKEN",
        "BIRDEYE_API_KEY",
        "VYBE_API_KEY",
        "JUPITER_API_KEY",
        "GMGN_API_KEY",
        "PUMPPORTAL_API_KEY",
        "HELIUS_API_KEY",
        "CHAINSTACK_API_KEY",
        "DASHBOARD_PASSWORD",
    ):
        v = (env.get(k) or "").strip()
        if len(v) >= 6:
            s = s.replace(v, "***")
    return s[:160]


def _verdict(pct, result, source="birdeye"):
    return {
        "hard_blocks": ["holders_top10_concentrated"] if result == "FAIL" else [],
        "mandatory": {"holders.top10": {"result": result, "value_pct": pct, "source": source}},
    }


def run(bot, env=os.environ, http_get=requests.get):
    out = []

    def add(n, name, status, detail, critical=False):
        out.append({
            "n": n,
            "name": name,
            "status": status,
            "detail": _scrub(detail, env),
            "critical": bool(critical and status == FAIL),
        })

    # 1. environment
    miss = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DASHBOARD_PASSWORD") if not (env.get(k) or "").strip()]
    bad = []
    if (env.get("DASHBOARD_PASSWORD") or "") and len(env["DASHBOARD_PASSWORD"]) < 8:
        bad.append("DASHBOARD_PASSWORD < 8 caratteri")
    mode, w1 = antirug_mod.resolve_mode(env, bot.c.get("antirug") or {})
    _enf, w2 = antirug_mod.resolve_enforce(env, bot.c.get("antirug") or {}, mode)
    if w1:
        bad.append(w1)
    # In ACTIVE mode, any legacy ANTIRUG_ENFORCE setting is safely ignored (ACTIVE already blocks everything)
    # and must not cause a critical startup failure (EX_CONFIG=78).
    if w2 and mode != "active":
        bad.append(w2)
    if (env.get("RISK_PROFILE") or "").strip() and not profiles.normalize(env["RISK_PROFILE"]):
        bad.append("RISK_PROFILE sconosciuto")
    try:
        start_bal = float(bot.c.get("starting_balance_eur") or 0)
    except (ValueError, TypeError):
        start_bal = 0
    if not (0 < start_bal):
        bad.append("starting_balance_eur non valido")
    try:
        sl = float(bot.c.get("stop_loss_pct") or 0)
        tp = float(bot.c.get("take_profit_pct") or 0)
        if not (0 < sl < 1 and 0 < tp < 5):
            bad.append("stop_loss_pct / take_profit_pct non validi")
    except (ValueError, TypeError):
        bad.append("stop_loss_pct / take_profit_pct non numerici")
    add(
        1,
        "Variabili d'ambiente e config",
        FAIL if (miss or bad) else OK,
        ("mancano: " + ", ".join(miss) + ". " if miss else "") + "; ".join(bad) or "complete e valide",
        critical=True,
    )
    # 2. Birdeye
    be = getattr(bot, "birdeye", None)
    if not be:
        add(2, "Birdeye", WARN, "spento o senza BIRDEYE_API_KEY: AntiRug usa RugCheck/RPC, COPY usa le altre fonti")
    else:
        try:
            p = be.price(SOL)
            add(2, "Birdeye", OK if p else WARN, "risponde (prezzo SOL ricevuto)" if p else "nessuna risposta valida ora")
        except Exception as e:
            add(2, "Birdeye", WARN, f"errore: {type(e).__name__}")
    # 3. Jupiter
    jq = None
    try:
        jq = bot.jup.quote(USDC, SOL, 10_000_000) if bot.jup else None
    except Exception:
        jq = None
    add(
        3,
        "Jupiter",
        OK if jq else WARN,
        "quota USDC→SOL ricevuta"
        if jq
        else "non raggiungibile: prezzi STIMATI o entrate RIFIUTATE dall'integrità prezzi",
    )
    # 4. RugCheck
    try:
        rc = bot.rc.report(BONK) if hasattr(bot.rc, "report") else None
    except Exception:
        rc = None
    add(4, "RugCheck", OK if rc else WARN, "report ricevuto" if rc else "non raggiungibile: AntiRug registra UNKNOWN")
    # 5. Telegram
    tok = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not tok:
        add(5, "Telegram", FAIL, "token mancante", critical=True)
    else:
        try:
            r = http_get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10)
            ok = r.status_code == 200 and (r.json() or {}).get("ok")
            add(
                5,
                "Telegram",
                OK if ok else FAIL,
                "bot raggiungibile" if ok else f"getMe HTTP {r.status_code} (token errato?)",
                critical=r.status_code in (401, 404),
            )
        except Exception as e:
            add(5, "Telegram", WARN, f"rete non disponibile ({type(e).__name__}): i messaggi restano in coda")
    # 6. learned rules
    st = bot.brain.st if hasattr(bot, "brain") else {}
    probs = st.get("seed_problems") or []
    params = st.get("params") or {}
    applied = all(bot.c.get(k) == v for k, v in params.items())
    add(
        6,
        "Regole imparate",
        OK if applied and not probs else WARN,
        f"{', '.join(f'{k}={v}' for k, v in sorted(params.items())) or 'nessuna'}"
        + (f"; dal seed ({st.get('seeded_from')}, {st.get('seed_source_trades')} trade)" if st.get("seeded_from") else "")
        + (f"; problemi: {'; '.join(probs)}" if probs else "")
        + ("" if applied else "; NON applicate al config"),
    )
    # 7-8. session
    start = bot.c["starting_balance_usd"]
    fresh = not bot.closed and not bot.positions and abs(bot.cash - start) < 1e-6
    sess = bot.meta.get("session") or {}
    add(
        7,
        "Sessione paper",
        OK,
        (
            f"NUOVA sessione '{sess.get('name', '-')}' da €{bot.c['starting_balance_eur']:.2f}"
            if fresh
            else f"sessione ripresa ({len(bot.closed)} trade, {len(bot.positions)} aperte)"
        ),
    )
    old_pos = bool(bot.positions) and fresh
    add(
        8,
        "Nessuna posizione vecchia importata",
        OK if (not bot.positions or bot.resumed) else FAIL,
        "0 posizioni" if not bot.positions else f"{len(bot.positions)} posizioni della sessione corrente",
        critical=old_pos,
    )
    # 9-10. AntiRug
    ar = getattr(bot, "antirug", None)
    want = mode == "shadow" and "holders_top10_concentrated" in (_enf or ())
    try:
        conc = (bot.c.get("antirug") or {}).get("concentration") or {}
        thr_pct = float(conc.get("top10_block_pct") or 40)
        assert 0 <= thr_pct <= 100
    except (ValueError, TypeError, AssertionError):
        thr_pct = 40.0
    add(
        9,
        "Configurazione AntiRug",
        OK if ar else WARN,
        f"modo {bot.antirug_mode}, enforce {list(getattr(ar, 'enforce', ()) or ()) or '-'}, soglia top10 "
        f"{thr_pct:.0f}%"
        + ("" if want or bot.antirug_mode == "active" else " (diverso dalla config approvata shadow+top10)"),
    )
    if ar and ar.enforce:
        t41 = ar.enforced_hits(_verdict(thr_pct + 1, "FAIL")) == ["holders_top10_concentrated"]
        t40 = ar.enforced_hits(_verdict(thr_pct, "FAIL")) == []
        tun = ar.enforced_hits(_verdict(None, "UNKNOWN")) == []
        ok = t41 and t40 and tun
        add(
            10,
            "Eccezione top10 > soglia",
            OK if ok else FAIL,
            f"{thr_pct + 1:.0f}% verificato → blocca: {t41}; {thr_pct:.0f}% → non blocca: {t40}; dato UNKNOWN → non blocca: {tun}",
            critical=True,
        )
    else:
        add(
            10,
            "Eccezione top10 > soglia",
            WARN if bot.antirug_mode != "active" else OK,
            "non attiva (ANTIRUG_ENFORCE vuoto)" if bot.antirug_mode != "active" else "ACTIVE: blocca già tutto",
        )
    # 11. COPY <- Birdeye
    if getattr(bot, "birdeye_copy", False):
        try:
            bt = be.best_traders(("today",), 1)
            add(
                11,
                "COPY riceve Birdeye",
                OK if bt else WARN,
                f"{len(bt)} wallet top trader da Birdeye" if bt else "nessun wallet ora: COPY usa Vybe/holder/GeckoTerminal",
            )
        except Exception as e:
            add(11, "COPY riceve Birdeye", WARN, f"errore {type(e).__name__}")
    else:
        add(11, "COPY riceve Birdeye", WARN, "Birdeye non attivo per COPY")
    # 12. dashboard
    port = (env.get("PORT") or "8080").strip()
    hz = (env.get("DASHBOARD_HEALTH_URL") or "").strip()  # Docker: http://dashboard:8080/healthz
    try:
        r = http_get(hz or f"http://127.0.0.1:{port}/", timeout=5)
        good = r.status_code == (200 if hz else 401)
        add(
            12,
            "Dashboard",
            OK if good else WARN,
            ("attiva (healthz)" if hz else "attiva e protetta da password (401 senza login)")
            if good
            else f"HTTP {r.status_code}",
        )
    except Exception:
        add(12, "Dashboard", WARN, f"non ancora raggiungibile su 127.0.0.1:{port} (servizio separato, parte da solo)")
    # 13. health
    try:
        u = shutil.disk_usage(bot.data)
        pct = 100 * (u.total - u.free) / u.total
        w = os.access(bot.data, os.W_OK)
        add(
            13,
            "Salute",
            OK if (w and pct < 90) else (FAIL if not w else WARN),
            f"data/ scrivibile: {w}; disco usato {pct:.0f}%",
            critical=not w,
        )
    except Exception as e:
        add(13, "Salute", FAIL, f"errore {type(e).__name__}", critical=True)
    critical = [c for c in out if c["critical"]]
    return {"checks": out, "critical": critical, "ts": time.time()}


def message(res):
    icon = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
    lines = ["🧪 <b>Controlli di avvio</b> (VPS)"]
    for c in res["checks"]:
        lines.append(f"{icon[c['status']]} {c['n']}. {c['name']}: {c['detail']}")
    lines.append(
        "❌ <b>AVVIO RIFIUTATO</b>: configurazione critica non valida. Correggi il .env e riavvia."
        if res["critical"]
        else "14. Avvio completato: il bot gira (solo simulazione)."
    )
    return "\n".join(lines)
