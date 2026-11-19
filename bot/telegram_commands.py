"""Telegram commands for the PAPER bot (Italian replies).

Long-polling getUpdates in a background thread. Only messages from TELEGRAM_CHAT_ID are accepted; everything
else is ignored. Read-only commands are answered directly; commands that change something (pause, close,
settings) are queued and executed by the bot's main loop, so they never race with trading logic.
Reliability (v4): the update offset is saved atomically BEFORE a command is executed (a restart can never
execute the same /chiuditutto twice), update_ids already seen are ignored, the same mutating command repeated
within 15 s is ignored, commands sent while the bot was down are answered (read-only) or politely refused
(mutating, older than 10 min), network errors back off exponentially.
Nothing here can touch real money: the bot has no wallet and no keys. "/chiudi" is a SIMULATED close.
"""
import html
import json
import logging
import math
import os
import queue
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import requests
import telegram_notify as tg
from storage import atomic_json
from telegram_notify import MOTIVI, eur, money, pct, price, usd

log = logging.getLogger("paperbot.commands")


def cfg_set(c, key, val):
    if "." in key:
        sec, k = key.split(".", 1)
        c.setdefault(sec, {})[k] = val
    else:
        c[key] = val


COMMANDS = [
    ("aiuto", "Elenco dei comandi"),
    ("stato", "Il bot è attivo? Da quanto, ultimo battito"),
    ("saldo", "Cassa, valore totale e P&L"),
    ("posizioni", "Posizioni aperte con P&L live"),
    ("trade", "Ultimi trade chiusi (es. /trade 10)"),
    ("oggi", "Riepilogo di oggi"),
    ("settimana", "Riepilogo settimanale"),
    ("pausa", "Ferma i NUOVI acquisti (le uscite continuano)"),
    ("riprendi", "Riattiva i nuovi acquisti"),
    ("chiudi", "Chiude (simulato) una posizione: /chiudi SIMBOLO"),
    ("chiuditutto", "Chiude (simulato) tutte le posizioni"),
    ("config", "Parametri principali"),
    ("imposta", "Cambia un parametro: /imposta stop_loss 15"),
    ("cervello", "Ultima diagnosi del cervello e parametri cambiati"),
    ("crediti", "Consumo crediti Vybe"),
    ("backup", "Elenco backup disponibili"),
    ("ripristina", "Ripristina da backup: /ripristina nome_file"),
    ("dashboard", "Link alla dashboard"),
    ("rischio", "Risk Manager: modalità, limiti, pausa"),
    ("salute", "Salute del sistema: API, disco, backup, Telegram"),
    ("log", "Ultimi log del bot"),
    ("errori", "Errori degli ultimi controlli API"),
    ("corsa", "Info sulle performance di rete"),
    ("reset", "Azzera la simulazione dopo backup e doppia conferma"),
    ("confermareset", "Conferma il reset: /confermareset CODICE"),
    ("pumpfun", "Stato scanner Pump.fun e ultimi eventi"),
    ("antirug", "Anti-Rug: /antirug active|shadow|status"),
    ("smartmoney", "Smart Money: /smartmoney status|top|wallet <addr>|signals [n]"),
    ("blacklist", "Creator e DEX in blacklist automatica 24h"),
]
SCREEN_CMD = ("screen", "Screenshot della dashboard adesso")
AGGR_CMD = ("aggr", "Profilo di rischio: /aggr aggressiva|bilanciata|conservativa")


def commands_for(bot):
    on = bool(getattr(bot, "screenshot_active", lambda: False)())
    prof = bool(getattr(bot, "profile_visible", False))
    return COMMANDS + ([SCREEN_CMD] if on else []) + ([AGGR_CMD] if prof else [])


MUTATING = {"pausa", "riprendi", "chiudi", "chiuditutto", "imposta", "aggr", "reset", "confermareset", "ripristina"}
STALE_MUTATING_S = 600


def _is_completed_trade(trade):
    return trade.get("counts_as_trade", not (trade.get("partial") or trade.get("exit_reason") == "take_profit_1")) is not False

# name -> (config key, min, max, unit, scale) ; value typed by the user in % or minutes
SETTABLE = {
    "stop_loss": ("stop_loss_pct", 1, 50, "%", 100),
    "take_profit": ("take_profit_pct", 1, 500, "%", 100),
    "tp1": ("partial_tp.tp1_pct", 5, 200, "% (take profit parziale 1)", 100),
    "tp2": ("partial_tp.tp2_pct", 10, 500, "% (take profit finale 2)", 100),
    "tp1_quota": ("partial_tp.tp1_sell_fraction", 10, 90, "% posizione venduta a TP1", 100),
    "max_hold": ("max_hold_minutes", 1, 240, " min", 1),
    "size": ("position_size_pct", 1, 50, "% del capitale", 100),
    "trailing": ("trailing_stop_pct", 0, 50, "%", 100),
    "slippage_max": ("max_entry_slippage_pct", 1, 30, "% (slippage max entrata)", 1),
    "crash_soglia": ("anti_panic.crash_drop_pct", 30, 95, "% crollo anti-panic", 100),
    "ricompra": ("reentry_cooldown_minutes", 0, 1440, " min (solo picchi di volume)", 1),
    "copy_stop_loss": ("copy_exits.stop_loss_pct", 1, 60, "% (copia top trader)", 100),
    "copy_max_hold": ("copy_exits.max_hold_minutes", 5, 1440, " min (copia top trader)", 1),
    "copy_trailing": ("copy_exits.trailing_stop_pct", 0, 60, "% (copia top trader)", 100),
    "copy_maggioranza": ("copy_exits.sold_majority_pct", 10, 90, "% dei top trader seguiti", 100),
}
ALIASES = {
    "stoploss": "stop_loss",
    "sl": "stop_loss",
    "takeprofit": "take_profit",
    "tp": "take_profit",
    "take_profit_1": "tp1",
    "take_profit_2": "tp2",
    "maxhold": "max_hold",
    "durata": "max_hold",
    "position_size": "size",
    "dimensione": "size",
    "taglia": "size",
    "slippage": "slippage_max",
    "max_slippage": "slippage_max",
    "cooldown": "ricompra",
    "reentry": "ricompra",
}


def dur(sec):
    sec = max(0, int(sec))
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h} h {m} min" if h else f"{m} min"


def _fmt_pf(pf):
    if pf is None:
        return "0.00"
    if isinstance(pf, float) and math.isinf(pf):
        return "∞"
    return f"{pf:.2f}"


class Commands:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, bot, notifier):
        self.bot, self.tg = bot, notifier
        self.token, self.chat_id = notifier.token, notifier.chat_id
        self.enabled = not notifier.dry and bot.c.get("telegram_commands", True)
        self.actions = queue.Queue()
        self.offset_path = os.path.join(bot.data, "telegram_offset.json")
        self.s = requests.Session()
        self.started = time.time()
        self.seen = deque(maxlen=500)
        self.last_cmd = {}
        self.poll_errors = 0
        self.reset_code = None
        self.reset_expires = 0

    # ---------------------------------------------------------------- plumbing
    def _api(self, method, payload, timeout=(5, 15)):
        r = self.s.post(self.API.format(token=self.token, method=method), json=payload, timeout=timeout)
        try:
            d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": False}
        except ValueError:
            d = {"ok": False}
        return d if isinstance(d, dict) else {"ok": False}

    def _save_offset(self, offset):
        try:
            atomic_json(self.offset_path, {"offset": offset, "ts": time.time()}, indent=None)
            return True
        except Exception as e:
            log.error("CRITICAL: cannot save Telegram update offset; command will not execute and may be replayed: %s", e)
            return False

    def start(self):
        if not self.enabled:
            log.info("Telegram commands disabled (no token/chat id)")
            return
        threading.Thread(target=self._poll, name="telegram-commands", daemon=True).start()

    def register(self):
        try:
            res = self._api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
            log.info("Telegram setMyCommands: %s", "ok" if res.get("ok") else res.get("description"))
        except Exception as e:
            log.warning("setMyCommands failed: %s", type(e).__name__)

    def load_offset(self):
        try:
            d = json.load(open(self.offset_path, encoding="utf-8"))
            off = d.get("offset")
        except Exception as e:
            log.warning("telegram_offset.json non leggibile (%s): riparto da 0", type(e).__name__)
            return 0
        if isinstance(off, bool) or not isinstance(off, int) or off < 0:
            log.warning(
                "telegram_offset.json offset non valido (%r): riparto da 0 (comandi duplicati protetti "
                "dal guardiano di 15 s e dalla freschezza del messaggio)",
                off,
            )
            return 0
        return off

    def _backoff(self):
        self.poll_errors += 1
        time.sleep(min(120, 5 * 2 ** min(self.poll_errors - 1, 5)) * random.uniform(0.7, 1.3))

    def process_updates(self, updates, offset):
        for u in updates if isinstance(updates, list) else []:
            if not isinstance(u, dict) or not isinstance(u.get("update_id"), int):
                continue
            uid = u["update_id"]
            if uid < offset or uid in self.seen:
                continue
            next_offset = uid + 1
            # Persist the acknowledgement BEFORE execution. If disk I/O fails, leave
            # this update unacknowledged and retry it on the next poll.
            if not self._save_offset(next_offset):
                break
            offset = next_offset
            self.seen.append(uid)
            try:
                self._handle(u.get("message") or {})
            except Exception as e:
                log.exception("command error: %s", e)
                self.reply("⚠️ Errore interno nel comando, il bot continua a funzionare.")
        return offset

    def _poll(self):
        self.register()
        offset = self.load_offset()
        while True:
            try:
                res = self._api(
                    "getUpdates",
                    {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                    timeout=(5, 35),
                )
                if not res.get("ok"):
                    log.warning("getUpdates not ok: %s", str(res.get("description", ""))[:100])
                    self._backoff()
                    continue
                self.poll_errors = 0
                offset = self.process_updates(res.get("result", []), offset)
            except Exception as e:
                log.warning("getUpdates: %s", type(e).__name__)
                self._backoff()

    def reply(self, text):
        self.tg.send(text)

    def _handle(self, msg):
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if chat != str(self.chat_id):
            log.warning("Ignored command from unauthorised chat %s", chat)
            return
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()
        args = parts[1:]
        age = time.time() - msg.get("date", 0)
        if msg.get("date", 0) < self.started - 120:
            if cmd in MUTATING and age > STALE_MUTATING_S:
                log.info("Ignored stale mutating command sent while the bot was down: %s", text[:30])
                self.reply(
                    f"⏳ Comando <code>{html.escape(text[:40])}</code> ignorato: era stato inviato "
                    f"{int(age // 60)} min fa mentre il bot era spento/riavviato. Se serve ancora, rimandalo."
                )
                return
        if cmd in MUTATING:
            norm = " ".join([cmd] + [a.lower() for a in args])
            last = self.last_cmd.get(norm, 0)
            if time.time() - last < 15:
                log.info("Ignored repeated command within 15 s: %s", norm[:40])
                self.reply(
                    f"⏳ Comando <code>{html.escape(text[:40])}</code> già in elaborazione: "
                    f"viene eseguito una sola volta. Se non vedi l'effetto, attendi qualche secondo."
                )
                return
            self.last_cmd[norm] = time.time()
        log.info("Telegram command: /%s %s", cmd, " ".join(args)[:40])
        self.reply(self.answer(cmd, args))

    # ---------------------------------------------------------------- commands
    def answer(self, cmd, args):
        lk = getattr(self.bot, "lock", None)
        got = lk.acquire(timeout=5) if lk else False
        if lk is not None and not got:
            return (
                "⏳ Il bot è occupato (chiusura/backup in corso): riprova tra qualche secondo. "
                "Nulla è stato eseguito."
            )
        try:
            return self._answer(cmd, args)
        finally:
            if got:
                lk.release()

    def _answer(self, cmd, args):
        b = self.bot
        if cmd in ("start", "aiuto", "help"):
            name = tg.display_name()
            return (
                (f"🤖 <b>{html.escape(name)} · paper bot Solana</b>" if name else "🤖 <b>Paper bot Solana</b>")
                + " (solo simulazione)\n\n"
                + "\n".join(f"/{c} — {html.escape(d)}" for c, d in commands_for(self.bot))
                + "\n\nParametri per /imposta: "
                + ", ".join(SETTABLE)
                + "\n<i>Nessun wallet, nessuna chiave, nessun ordine reale.</i>"
            )
        if cmd == "blacklist":
            bl = getattr(b, "blacklist", None)
            if not bl or not getattr(bl, "entries", None):
                return "🚫 <b>Blacklist automatica 24h</b>: nessuna entità bloccata al momento."
            now = time.time()
            active = {k: v for k, v in bl.entries.items() if now < v.get("expires_at", 0)}
            if not active:
                return "🚫 <b>Blacklist automatica 24h</b>: nessuna entità bloccata al momento."
            lines = [f"🚫 <b>Blacklist automatica attiva ({len(active)})</b>:"]
            for k, v in list(active.items())[:15]:
                rem_h = max(0.1, (v["expires_at"] - now) / 3600.0)
                lines.append(
                    f"• <code>{html.escape(k[:26])}</code> · {html.escape(str(v.get('reason', '?')))} · scade tra {eur(rem_h, 1)} h"
                )
            return "\n".join(lines)
        if cmd == "reset":
            if args and args[0].lower() in ("annulla", "cancel"):
                self.reset_code, self.reset_expires = None, 0
                return "✅ Reset annullato."
            if self.reset_code and time.time() < self.reset_expires:
                return (
                    "⚠️ Esiste già una richiesta di reset in attesa. "
                    f"Per confermarla usa <code>/confermareset {self.reset_code}</code> oppure <code>/reset annulla</code>."
                )
            self.reset_code = f"{random.SystemRandom().randint(0, 999999):06d}"
            self.reset_expires = time.time() + 300
            open_positions = len(b.positions)
            return (
                "🚨 <b>RESET TOTALE DELLA SIMULAZIONE</b>\n\n"
                "Verrà creato un backup del codice/configurazione e di tutti i dati (saldo, trade, posizioni, Brain, log). "
                "Poi il processo si riavvierà dal saldo iniziale configurato. Le posizioni aperte resteranno nello storico backup, "
                "non verranno vendute.\n\n"
                f"Posizioni aperte: <b>{open_positions}</b> · saldo iniziale: <b>{money(b.c['starting_balance_usd'])}</b>.\n"
                "Il file .env è incluso nel backup server-side con permessi riservati; non viene inviato su Telegram.\n\n"
                f"Per procedere entro 5 minuti: <code>/confermareset {self.reset_code}</code>\n"
                "Per annullare: <code>/reset annulla</code>."
            )
        if cmd == "confermareset":
            if not args or not self.reset_code or time.time() > self.reset_expires or args[0] != self.reset_code:
                self.reset_code, self.reset_expires = None, 0
                return "❌ Codice errato o scaduto. Nessun dato è stato modificato. Se vuoi riprovare usa /reset."
            self.reset_code, self.reset_expires = None, 0
            self.actions.put(("reset", None))
            return (
                "⏳ Reset confermato. Il bot salva lo stato, crea due backup (codice/configurazione e dati), "
                "azzera la directory dati e si riavvia automaticamente. Nessuna transazione reale."
            )
        if cmd == "pumpfun":
            pf = getattr(b, "pumpfun", None)
            if pf is None:
                return "Pump.fun scanner non inizializzato."
            st = pf.snapshot()
            recent = st.get("events", [])[:5]
            by_ch = st.get("events_by_channel", {}) or {}
            lines = [
                f"🟢 <b>Pump.fun Intelligence</b>: {html.escape(st.get('state', 'unknown'))}",
                f"Nuovi token {by_ch.get('new_token', 0)} · migrazioni {by_ch.get('migration', 0)} · eventi {st.get('received', 0)}",
            ]
            for ev in recent:
                lines.append(
                    f"• {html.escape(ev.get('token_symbol') or ev.get('token_name') or ev.get('token_mint', '')[:8])}"
                    f" — {ev.get('event_type', '')}"
                )
            if st.get("last_error"):
                lines.append("⚠️ " + html.escape(st["last_error"][:140]))
            if not st.get("enabled"):
                lines.append("Configura PUMPPORTAL_API_KEY nel .env per attivare lo stream.")
            return "\n".join(lines)
        if cmd == "antirug":
            if not args:
                return "Uso: <code>/antirug active|shadow|status</code>"
            ar = getattr(b, "antirug", None)
            if not ar:
                return "⚠️ Anti-Rug engine non inizializzato (manca BIRDEYE_API_KEY o ANTIRUG_MODE=off)."
            sub = args[0].lower()
            if sub == "active":
                if ar.mode == "active":
                    return "ℹ️ Anti-Rug è già in modalità ACTIVE."
                return (
                    "⚠️ Per passare a ACTIVE è necessario riavviare il bot con <code>ANTIRUG_MODE=active</code> nel file .env.\n"
                    f"Modalità attuale: <b>{ar.mode.upper()}</b>.\n"
                    "Dopo il riavvio, Anti-Rug bloccherà le entrate che falliscono i controlli obbligatori."
                )
            elif sub == "shadow":
                if ar.mode == "shadow":
                    return "ℹ️ Anti-Rug è già in modalità SHADOW."
                return (
                    "⚠️ Per passare a SHADOW è necessario riavviare il bot con <code>ANTIRUG_MODE=shadow</code> nel file .env.\n"
                    f"Modalità attuale: <b>{ar.mode.upper()}</b>.\n"
                    "Dopo il riavvio, Anti-Rug osserverà e registrerà senza bloccare."
                )
            elif sub == "status":
                st = ar.stats
                api = getattr(ar, "api", {}) or {}
                enforce = getattr(ar, "enforce", ())
                lines = [
                    "🛡 <b>Anti-Rug Status</b>",
                    f"Modalità: <b>{ar.mode.upper()}</b>",
                    f"Enforced: {', '.join(enforce) if enforce else 'nessuno'}",
                    f"Analizzati: {st['evaluated']} · Autorizzati: {st['allow']} · Bloccati: {st['block']} · Non verificabili: {st['unknown']}",
                    f"Critici: {st.get('critical', 0)}",
                    f"Hard blocks: {dict(st.get('hard', {}))}",
                    f"API: Birdeye={'ok' if api.get('birdeye_holders', {}).get('ok') else 'fail'}, "
                    f"RugCheck={'ok' if api.get('rugcheck_report', {}).get('ok') else 'fail'}, "
                    f"Jupiter={'ok' if api.get('jupiter', {}).get('ok') else 'fail'}",
                ]
                if ar.mode == "shadow" and enforce:
                    lines.append("Nota: SHADOW + top10 enforced = blocco SOLO top10 holder > 40% (dato verificato).")
                return "\n".join(lines)
            else:
                return "Sottocomando non valido. Usa: active, shadow, status"
        if cmd == "smartmoney":
            return self._answer_smartmoney(args)
        if cmd == "stato":
            hb = time.time() - b.heartbeat
            up = time.time() - b.session_start
            stalls = b.meta.get("stalls", [])
            st = (
                "💀 MORTO (capitale esaurito)"
                if b.meta.get("dead")
                else "⏸ in pausa (nuovi acquisti fermi)"
                if b.meta.get("paused")
                else "✅ attivo"
            )
            return (
                f"📡 <b>Stato</b>: {st}\n"
                f"⏱ In esecuzione da {dur(up)} · ultimo battito {int(hb)} s fa\n"
                f"🔁 Riavvii {b.meta.get('restarts', 0)} (watchdog {b.meta.get('watchdog_restarts', 0)}) · "
                f"blocchi totali {dur(sum(s.get('sec', 0) for s in stalls))}\n"
                f"👀 Coppie in osservazione {len(b.watch)} · chiamate API/min {b.http.ops_per_min()}\n"
                f"📂 Posizioni aperte {len(b.positions)} · trade completati {sum(1 for t in b.closed if t.get('counts_as_trade', True) is not False)}\n"
                + tg.survival_line(b.survival_status())
                + (
                    f"\n🛡 Risk Manager: {b.risk.mode}"
                    + (f" · PAUSA ({html.escape(b.risk.pause_info()['reason'])})" if b.risk.paused else "")
                    if hasattr(b, "risk")
                    else ""
                )
                + (
                    f"\n🎚 Profilo di rischio: <b>{b.profile_info()['label']}</b>"
                    if getattr(b, "profile_visible", False)
                    else ""
                )
            )
        if cmd == "saldo":
            with b.lock:
                start, eq, cash = b.c["starting_balance_usd"], b.equity_value(), b.cash
                real = sum(t["pnl_usd"] for t in b.closed)
                return (
                    f"💼 <b>Saldo (simulato)</b>\n"
                    f"Cassa {money(cash)} · in posizioni {money(eq - cash)}\n"
                    f"Totale <b>{money(eq)}</b> su {money(start)} → {money(eq - start, sign=True)} "
                    f"({pct(100 * (eq / start - 1), 1)}) · {eur(eq / start, 2)}×\n"
                    f"Realizzato {money(real, sign=True)}"
                    + (
                        (
                            lambda pv: f"\n🔎 Verificati (Jupiter): {pv['VERIFIED']['n']} trade, {money(pv['VERIFIED']['pnl_usd'], sign=True)}"
                            f" · Stimati: {pv['ESTIMATED']['n']} trade, {money(pv['ESTIMATED']['pnl_usd'], sign=True)}"
                            f" · Rifiutati (prezzo): {pv['REJECTED']['n']}"
                        )(b.provenance_summary())
                        if getattr(b, "provenance_on", False)
                        else ""
                    )
                )
        if cmd == "posizioni":
            with b.lock:
                ps = list(b.positions.values())
                if not ps:
                    return "📂 Nessuna posizione aperta."
                out = ["📂 <b>Posizioni aperte</b> (prezzi aggiornati all'ultimo controllo)"]
                for p in ps:
                    val = p["qty"] * p["last"]
                    chg = 100 * (p["last"] / p["entry_mid"] - 1)
                    tr = p.get("track") or {}
                    flags = []
                    if p.get("tp1_hit"):
                        flags.append("TP1✅")
                    if p.get("breakeven_activated"):
                        flags.append(f"BE@{tg.price(p.get('breakeven_price', 0))}")
                    if p.get("anti_panic_active"):
                        flags.append("ANTI-PANIC🛡")
                    if p.get("tp2_done"):
                        flags.append("TP2✅")
                    flag_s = f" [{' '.join(flags)}]" if flags else ""
                    # Show remaining qty and original qty if TP1 hit
                    qty_info = f"qty {p['qty']:.6f}"
                    if p.get("tp1_hit"):
                        qty_info += f" (orig {p.get('original_qty', p['qty']):.6f}, rem {p.get('remaining_qty', p['qty']):.6f})"
                    out.append(
                        f"• {tg._tok(p['name'])}{flag_s} ({'copia' if p['entry_type'] == 'copy_top_traders' else 'volume'}): "
                        f"{money(p['cost_usd'])} → {money(val)} "
                        f"({money(val - p['cost_usd'], sign=True)}, prezzo {pct(chg, 1)}) · "
                        f"{qty_info} · "
                        f"{dur(time.time() - p['entry_ts'])}"
                        + (f" · top trader usciti {tr.get('sold', 0)}/{tr['n']}" if tr.get("n") else "")
                    )
                return "\n".join(out)
        if cmd == "trade":
            n = 5
            if args and args[0].isdigit():
                n = max(1, min(20, int(args[0])))
            completed = [t for t in b.closed if _is_completed_trade(t)]
            cl = completed[-n:][::-1]
            if not cl:
                return "📜 Nessun trade completato ancora."
            out = [f"📜 <b>Ultimi {len(cl)} trade completati</b>"]
            for t in cl:
                hm = datetime.fromtimestamp(t.get("exit_ts", 0), b.tz).strftime("%d/%m %H:%M")
                logical_pnl = float(t.get("cumulative_pnl_usd", t["pnl_usd"]))
                logical_pct = float(t.get("cumulative_pnl_pct", t["pnl_pct"]))
                out.append(
                    f"{'✅' if logical_pnl > 0 else '❌'} {hm} {tg._tok(t['token'])} "
                    f"{money(logical_pnl, sign=True)} ({pct(logical_pct, 0)}) · "
                    f"{MOTIVI.get(t['exit_reason'], t['exit_reason']).split(' (')[0]}"
                )
            return "\n".join(out)
        if cmd == "oggi":
            midnight = datetime.now(b.tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            executions = [t for t in b.closed if t.get("exit_ts", 0) >= midnight]
            cl = [t for t in executions if _is_completed_trade(t)]
            if not executions:
                return f"📅 Oggi nessuna uscita registrata. Posizioni aperte: {len(b.positions)}."
            w = sum(1 for t in cl if float(t.get("cumulative_pnl_usd", t["pnl_usd"])) > 0)
            pnl = sum(t["pnl_usd"] for t in executions)  # sum each realized execution exactly once
            fees = sum(t["dex_fees_usd"] + t["network_fees_usd"] for t in executions)
            best = max(cl, key=lambda t: float(t.get("cumulative_pnl_usd", t["pnl_usd"])), default=None)
            worst = min(cl, key=lambda t: float(t.get("cumulative_pnl_usd", t["pnl_usd"])), default=None)
            win_rate = pct(100 * w / len(cl), 0, False) if cl else "—"
            extremes = (f"\n🏆 {tg._tok(best['token'])} {money(best.get('cumulative_pnl_usd', best['pnl_usd']), sign=True)} · "
                        f"💀 {tg._tok(worst['token'])} {money(worst.get('cumulative_pnl_usd', worst['pnl_usd']), sign=True)}"
                        if best and worst else "")
            return (
                f"📅 <b>Oggi</b>: {len(cl)} trade completati · vinti {w} · win rate {win_rate}\n"
                f"P&amp;L realizzato {money(pnl, sign=True)} · commissioni {money(fees)}"
                + extremes + f"\nPosizioni aperte: {len(b.positions)}"
            )
        if cmd == "settimana":
            week_start = datetime.now(b.tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
            executions = [t for t in b.closed if t.get("exit_ts", 0) >= week_start.timestamp()]
            cl = [t for t in executions if _is_completed_trade(t)]
            if not executions:
                return f"📅 Nessuna uscita questa settimana. Posizioni aperte: {len(b.positions)}."
            w = sum(1 for t in cl if float(t.get("cumulative_pnl_usd", t["pnl_usd"])) > 0)
            pnl = sum(t["pnl_usd"] for t in executions)
            fees = sum(t["dex_fees_usd"] + t["network_fees_usd"] for t in executions)
            best = max(cl, key=lambda t: float(t.get("cumulative_pnl_usd", t["pnl_usd"])), default=None)
            worst = min(cl, key=lambda t: float(t.get("cumulative_pnl_usd", t["pnl_usd"])), default=None)
            win_rate = pct(100 * w / len(cl), 0, False) if cl else "—"
            extremes = (f"\n🏆 {tg._tok(best['token'])} {money(best.get('cumulative_pnl_usd', best['pnl_usd']), sign=True)} · "
                        f"💀 {tg._tok(worst['token'])} {money(worst.get('cumulative_pnl_usd', worst['pnl_usd']), sign=True)}"
                        if best and worst else "")
            return (
                f"📅 <b>Settimana corrente</b>: {len(cl)} trade completati · vinti {w} · win rate {win_rate}\n"
                f"P&amp;L realizzato {money(pnl, sign=True)} · commissioni {money(fees)}"
                + extremes
            )
        if cmd == "pausa":
            self.actions.put(("pause", None))
            return "⏳ Pausa in corso (eseguita dal ciclo principale)…"
        if cmd == "riprendi":
            self.actions.put(("resume", args))
            return "⏳ Ripresa in corso (eseguita dal ciclo principale)…"
        if cmd == "chiudi":
            if not args:
                return "Scrivi il simbolo, es. <code>/chiudi AGI</code>. Vedi /posizioni."
            sym = args[0].lstrip("$").lower()
            if not sym:
                return (
                    f"Simbolo non valido: <code>{html.escape(args[0])}</code>. "
                    f"Scrivi il simbolo, es. <code>/chiudi AGI</code>. Vedi /posizioni."
                )
            match = [m for m, p in b.positions.items() if str(p["name"]).lower() == sym or m.lower().startswith(sym)]
            if not match:
                return f"Nessuna posizione aperta su ${html.escape(args[0].lstrip('$'))}. Vedi /posizioni."
            name = (b.positions.get(match[0]) or {}).get("name", sym)
            self.actions.put(("close", match[0]))
            return f"⏳ Chiudo (simulato) {tg._tok(name)} al prossimo controllo prezzi…"
        if cmd == "chiuditutto":
            if not b.positions:
                return "📂 Nessuna posizione aperta."
            self.actions.put(("close_all", None))
            return f"⏳ Chiudo (simulato) {len(b.positions)} posizioni al prossimo controllo prezzi…"
        if cmd == "config":
            c = b.c
            cr = b.exit_rules("copy_top_traders")
            jc = c.get("jupiter") or {}
            ptp = c.get("partial_tp") or {}
            ap = c.get("anti_panic") or {}
            ptp_str = (
                f"TP1 {pct(100 * ptp.get('tp1_pct', 0.25), 0, False)} ({eur(100 * ptp.get('tp1_sell_fraction', 0.5), 0)}%) · "
                f"TP2 {pct(100 * ptp.get('tp2_pct', c['take_profit_pct']), 0, False)} · "
                f"BE={ptp.get('move_sl_to_breakeven', True)}"
                if ptp.get("enabled")
                else f"take_profit {pct(100 * c['take_profit_pct'], 0, False)}"
            )
            return (
                f"⚙️ <b>Parametri</b>\n"
                f"<b>Picco di volume</b>: stop_loss {pct(100 * c['stop_loss_pct'], 0, False)} · {ptp_str} · "
                f"trailing {pct(100 * c['trailing_stop_pct'], 0, False)} (da +{pct(100 * c['trailing_activate_pct'], 0, False)}) · "
                f"max_hold {c['max_hold_minutes']} min · ricompra stesso token dopo {c.get('reentry_cooldown_minutes') or 0} min\n"
                f"<b>Copia top trader</b>: {tg.rules_line(cr)} · nessun blocco di ricompra\n"
                f"Esecuzione: {'quote Jupiter (fallback modello)' if jc.get('enabled', True) else 'modello liquidità'} · "
                f"polling {c.get('price_poll_seconds', 2.5)}s · slippage max entrata {c.get('max_entry_slippage_pct', 7.0)}%\n"
                f"size {pct(100 * c['position_size_pct'], 0, False)} del capitale "
                f"(minimo {money(b.min_trade_usd())}) · posizioni: "
                f"{('max ' + str(c['max_open_positions'])) if c.get('max_open_positions') else 'nessun limite rigido'}\n"
                f"Anti-Panic crolli >{eur(100 * ap.get('crash_drop_pct', 0.70), 0)}%: {'attivo' if ap.get('enabled') else 'spento'}\n"
                f"Volume: ×{c['spike_multiplier']} in 5 min, minimo {usd(c['min_volume_5m_usd'], 0)}\n"
                f"Filtri: dev ≤ {c['max_dev_holding_pct']}%, top10 ≤ {c['max_top10_pct']}%, balena ≥ {usd(c['whale_buy_usd'], 0)}\n"
                f"Copia top trader: {'attiva' if b.top_traders else 'non attiva'} (min {c['min_top_traders_in']})\n"
                f"Nuovi acquisti: {'IN PAUSA' if b.meta.get('paused') else 'attivi'}\n"
                f"Modificabili con /imposta: {', '.join(SETTABLE)}"
            )
        if cmd == "imposta":
            if len(args) < 2:
                return (
                    "Uso: <code>/imposta parametro valore</code>\nes. <code>/imposta stop_loss 15</code> (=−15%), "
                    "<code>/imposta tp1 35</code>, <code>/imposta tp2 100</code>, "
                    "<code>/imposta slippage_max 7</code> (%), <code>/imposta max_hold 20</code> (minuti), "
                    "<code>/imposta size 15</code> (% della cassa), <code>/imposta trailing 10</code>, "
                    "<code>/imposta ricompra 30</code> (min), <code>/imposta copy_stop_loss 25</code>"
                )
            name = ALIASES.get(args[0].lower(), args[0].lower())
            if name not in SETTABLE:
                return f"Parametro non modificabile. Ammessi: {', '.join(SETTABLE)}"
            key, lo, hi, unit, scale = SETTABLE[name]
            raw_value = str(args[1]).strip()
            # Accept one optional trailing percent sign and Italian decimal comma.
            if raw_value.endswith("%"):
                raw_value = raw_value[:-1].strip()
            if "%" in raw_value:
                return "Valore non valido: usa un numero, es. 15 oppure 15,5."
            try:
                v = float(raw_value.replace(",", "."))
            except (TypeError, ValueError, OverflowError):
                return "Valore non valido: scrivi un numero, es. 15"
            if not math.isfinite(v):
                return "Valore non valido: sono ammessi solo numeri finiti."
            if not lo <= v <= hi:
                return f"Valore fuori dai limiti di sicurezza per {name}: tra {lo} e {hi}{unit}."
            # Minute/count settings are integral; percentages and slippage may be decimal.
            if scale == 1 and key != "max_entry_slippage_pct" and not v.is_integer():
                return f"{name} richiede un numero intero."
            if scale != 1:
                val = v / scale
            else:
                val = v if key == "max_entry_slippage_pct" else int(v)
            note = ""
            rk = getattr(b, "risk", None)
            if (
                key == "position_size_pct"
                and rk is not None
                and rk.L.get("max_position_pct")
                and val > rk.L["max_position_pct"]
            ):
                val = rk.L["max_position_pct"]
                note = f" (limitato dal Risk Manager al {pct(100 * val, 0, False)})"
            disp_v = val * scale
            self.actions.put(("set", (key, val)))
            return f"✅ {name} impostato a {eur(disp_v, 1 if disp_v % 1 else 0)}{unit} (vale anche dopo i riavvii).{note}"
        if cmd == "cervello":
            br = b.brain
            sm = br.summary()
            if not sm["diagnosis"]:
                return (
                    f"🧠 Il cervello sta ancora raccogliendo dati: {sm['records']} trade registrati, "
                    f"prima analisi tra {sm['next_in']} trade."
                )
            last = br.st["analyses"][-1]
            txt = tg.msg_brain(last, "🧠 <b>Cervello</b> — ultima diagnosi")
            older = [c for c in sm["changes"] if c not in last.get("changes", [])][-5:]
            if older:
                txt += "\n\n<b>Modifiche precedenti:</b>\n" + "\n".join(
                    f"• {datetime.fromtimestamp(c['ts'], b.tz).strftime('%d/%m %H:%M')} {html.escape(c['text'])}"
                    for c in older
                )
            return txt + f"\nProssima analisi tra {sm['next_in']} trade."
        if cmd == "crediti":
            if not b.vybe:
                return "🔑 Vybe non configurato (manca VYBE_API_KEY): la copia dei top trader è spenta."
            u = b.vybe.usage()
            tot = b.c["vybe_monthly_credits"]
            return (
                f"🔑 <b>Crediti Vybe</b> (stima locale)\n"
                f"Mese: {eur(u['estimated_credits_used_this_month'], 0)} su {eur(tot, 0)} · oggi {eur(u['credits_used_today'], 0)} "
                f"(tetto {eur(b.c['vybe_daily_credit_cap'], 0)}/giorno, stop al {pct(100 * b.c['vybe_monthly_use_pct'], 0, False)} del mese)\n"
                f"Lista top trader: {len(b.top_traders)} wallet (aggiornata ogni {b.c['top_traders_refresh_minutes'] // 60} h)"
                + (f"\nEndpoint disattivati: {', '.join(u['disabled_endpoints'])}" if u["disabled_endpoints"] else "")
            )
        if cmd == "aggr":
            import profiles

            if args:
                p = b.set_profile(args[0])
                if not p:
                    return "Profilo sconosciuto. Usa: /aggr aggressiva · /aggr bilanciata · /aggr conservativa"
                pi = b.profile_info()
                return (
                    f"🎚 Profilo di rischio ora: <b>{pi['label']}</b> (salvato)\n{html.escape(pi['desc'])}\n"
                    "Filtri d'entrata, size e slippage: subito."
                    + (
                        "\n⚠️ La parte AntiRug / Risk Manager vale dal prossimo riavvio "
                        "(<code>systemctl restart paper-bot</code>)."
                        if pi["restart_needed"]
                        else ""
                    )
                )
            pi = b.profile_info()
            return (
                f"🎚 Profilo di rischio: <b>{pi['label']}</b> ({html.escape(pi['source'])})\n\n"
                + "\n".join(f"• <b>{n}</b>: {html.escape(profiles.describe(n))}" for n in profiles.PROFILES)
                + "\n\nCambia con /aggr aggressiva · /aggr bilanciata · /aggr conservativa"
            )
        if cmd == "backup":
            if not hasattr(b, "backups") or not b.backups:
                return "📁 Nessun backup disponibile."
            entries = []
            for name in b.backups.list():
                d = os.path.join(b.backups.dir, name)
                try:
                    ts = os.path.getmtime(d)
                    size = sum(
                        os.path.getsize(os.path.join(d, f))
                        for f in os.listdir(d)
                        if os.path.isfile(os.path.join(d, f))
                    )
                except OSError:
                    ts, size = 0, 0
                entries.append({"name": name, "ts": ts, "size": size})
            if not entries:
                return "📁 Nessun backup conservato."
            out = ["📁 <b>Backup disponibili</b> (ultimi 10):"]
            for bk in sorted(entries, key=lambda x: x["ts"], reverse=True)[:10]:
                ts = datetime.fromtimestamp(bk["ts"], b.tz).strftime("%d/%m %H:%M") if bk["ts"] else "?"
                sz = f"{money(bk['size'], 0)}" if bk["size"] else "?"
                out.append(f"• {ts} — {html.escape(bk['name'])} ({sz})")
            return "\n".join(out)
        if cmd == "ripristina":
            if not args:
                return "Usa: <code>/ripristina nome_file</code> — vedi /backup per l'elenco."
            fname = html.escape(args[0])
            return (
                f"📁 <b>Ripristino da backup</b>\n\n"
                f"Per ripristinare, esegui da terminale:\n"
                f"<code>aeonctl restore /opt/aeon-by-rekt/backups-manuali/{fname}</code>\n\n"
                f"oppure\n\n"
                f"<code>bash /opt/aeon-by-rekt/scripts/restore.sh /opt/aeon-by-rekt/backups-manuali/{fname}</code>\n\n"
                f"⚠️ Il bot deve essere fermato prima del ripristino."
            )
        if cmd == "log":
            log_file = os.path.join(b.data, "bot.log")
            if not os.path.exists(log_file):
                return "📝 Nessun file di log trovato."
            try:
                with open(log_file) as f:
                    lines = f.readlines()[-20:]
                return "📝 <b>Ultimi log</b>:\n<pre>" + html.escape("".join(lines)) + "</pre>"
            except Exception as e:
                return f"⚠️ Impossibile leggere i log: {html.escape(str(e)[:100])}"
        if cmd == "errori":
            h = b.http.health_report() if hasattr(b.http, "health_report") else {}
            if not h:
                return "📊 Nessuna info sui controlli API disponibile."
            bad = {k: v for k, v in h.items() if v.get("consec", 0) > 0}
            if not bad:
                return "✅ Tutte le API sono attive, nessun errore."
            out = ["⚠️ <b>Errori API</b>:"]
            for k, v in bad.items():
                out.append(f"• {k}: {v['consec']} errori consecutive ({v['last_kind']})")
            return "\n".join(out)
        if cmd == "corsa":
            ops = b.http.ops_per_min() if hasattr(b.http, "ops_per_min") else 0
            return (
                f"📊 <b>Performance rete</b>\n"
                f"Richieste/min: {ops}\n"
                f"chiamate API totali per host: {sum((b.http.ok or {}).values()) + sum((b.http.err or {}).values())}"
            )
        if cmd == "screen":
            if not getattr(b, "screenshot_active", lambda: False)():
                return "📸 Screenshot disattivato (telegram.hourly_screenshot: false o Telegram non configurato)."
            b.request_screenshot()
            return "📸 Screenshot della dashboard in preparazione: arriva tra poco."
        if cmd == "dashboard":
            url = os.getenv("DASHBOARD_URL", "").strip()
            return (
                f"📊 Dashboard: {html.escape(url)}"
                if url
                else "📊 Imposta DASHBOARD_URL nel file .env (es. https://bot.tuodominio.it o http://IP:8080) e riavvia."
            )
        if cmd == "rischio":
            rk = getattr(b, "risk", None)
            if rk is None:
                return "🛡 Risk Manager non attivo."
            L = rk.limits()
            fmtp = lambda x: "nessuno" if not x else pct(100 * x, 0, False)
            p = rk.pause_info()
            return (
                f"🛡 <b>Risk Manager</b> — modalità <b>{rk.mode}</b>"
                + (" (regole sopravvivenza: muore solo a 0)" if rk.mode == "survival" else " (limiti prudenti)")
                + "\n"
                f"Posizioni max: {L['max_open_positions'] or 'nessun limite'} · esposizione max: {fmtp(L['max_exposure_pct'])} · "
                f"size max: {fmtp(L['max_position_pct'])}\n"
                f"Perdita giornaliera max: {fmtp(L['max_daily_loss_pct'])} · drawdown max: {fmtp(L['max_drawdown_pct'])} · "
                f"perdite di fila max: {L['max_consecutive_losses'] or 'nessun limite'}\n"
                f"Perdite di fila ora: {rk.s['consec_losses']} · massimo capitale: {money(rk.s['peak_equity'] or 0)}\n"
                + (f"⏸ PAUSA: {html.escape(p['reason'])} — {html.escape(p.get('text', ''))}" if p else "Nessuna pausa attiva.")
                + "\n<i>Il cervello non può modificare questi limiti.</i>"
            )
        if cmd == "salute":
            h = b.http.health_report() if hasattr(b.http, "health_report") else {}
            bad = [f"{k}: {v['last_kind']} ×{v['consec']}" for k, v in h.items() if v.get("consec")]
            ds = (getattr(b, "ops", {}) or {}).get("disk") or {}
            ts = b.tg.status() if hasattr(b.tg, "status") else {}
            nb = len(b.backups.list()) if hasattr(b, "backups") else 0
            return (
                "🩺 <b>Salute</b>\nAPI: "
                + ("tutte ok" if not bad else html.escape("; ".join(bad)))
                + "\n"
                f"Disco: {ds.get('free_mb', '?')} MB liberi ({ds.get('level', '?')}) · backup conservati: {nb}\n"
                f"Telegram: inviati {ts.get('sent', 0)}, in coda {ts.get('pending', 0)}, falliti {ts.get('failed', 0)}\n"
                f"Watchdog riavvii: {b.meta.get('watchdog_restarts', 0)} · riavvii totali: {b.meta.get('restarts', 0)}"
                + self._birdeye_line(b)
            )
        return "Comando sconosciuto. Scrivi /aiuto per l'elenco."

    def _answer_smartmoney(self, args):
        """Handle /smartmoney command for wallet intelligence."""
        b = self.bot
        wi = getattr(b, "wallet_intelligence", None)

        if not wi:
            return "⚠️ Wallet Intelligence non inizializzato."

        if not args:
            stats = wi.get_stats()
            cats = stats.get("categories", {})
            lines = [
                "🧠 <b>Smart Money Intelligence 2.0</b>",
                f"Wallet tracciati: {stats['total_tracked']}",
                f"Trade totali: {stats['total_trades']}",
                f"Categorie: ELITE {cats.get('ELITE', 0)} · WEEKLY_RISING {cats.get('WEEKLY_RISING', 0)} · HIGH_RISK {cats.get('HIGH_RISK', 0)} · UNVERIFIED {cats.get('UNVERIFIED', 0)}",
            ]

            if stats.get("rankings"):
                r = stats["rankings"]
                lines.append(
                    f"\nRankings aggiornati: {datetime.fromtimestamp(r['updated_timestamp']).strftime('%d/%m %H:%M')}"
                )
                for name, wallets in r.get("rankings", {}).items():
                    if wallets:
                        lines.append(f" {name}: {len(wallets)} wallet")

            return "\n".join(lines)

        sub = args[0].lower()

        if sub == "top":
            limit = 10
            if len(args) > 1 and args[1].isdigit():
                limit = min(int(args[1]), 50)

            category = None
            if len(args) > 2:
                cat = args[2].upper()
                if cat in ["ELITE", "WEEKLY_RISING", "HIGH_RISK", "UNVERIFIED"]:
                    category = cat

            top = wi.get_top_wallets(limit, category)
            if not top:
                return "Nessun wallet trovato per i criteri specificati."

            lines = [f"🏆 <b>Top {len(top)} Wallet{' (' + category + ')' if category else ''}</b>"]
            for i, (wallet, metrics) in enumerate(top, 1):
                lines.append(
                    f"{i}. <code>{wallet[:8]}...{wallet[-6:]}</code> "
                    f"{metrics.classification} · Score {metrics.score:.2f} · "
                    f"P&L {money(metrics.realized_pnl_usd, sign=True)} · "
                    f"WR {metrics.win_rate:.0%} · PF {_fmt_pf(metrics.profit_factor)} · "
                    f"DD {metrics.max_drawdown_pct:.1f}% · "
                    f"Trade {metrics.verified_trades}/{metrics.total_trades}"
                )
            return "\n".join(lines)

        elif sub == "wallet":
            if len(args) < 2:
                return "Uso: <code>/smartmoney wallet <address></code>"

            wallet = args[1].strip()
            metrics = wi.get_wallet_metrics(wallet)
            if not metrics:
                return f"Wallet {html.escape(wallet[:8])}... non trovato o senza dati sufficienti."

            lines = [
                "🧠 <b>Wallet Intelligence</b>",
                f"Address: <code>{html.escape(metrics.wallet_address)}</code>",
                f"Categoria: <b>{metrics.classification}</b>",
                f"Score: {metrics.score:.2f}/1.00",
                f"Trade totali: {metrics.total_trades} (verificati: {metrics.verified_trades})",
                f"P&L realizzato: {money(metrics.realized_pnl_usd, sign=True)}",
                f"Win Rate: {metrics.win_rate:.1%}",
                f"Profit Factor: {_fmt_pf(metrics.profit_factor)}",
                f"Expectancy: {metrics.expectancy_per_trade_pct:.2f}% per trade",
                f"Max Drawdown: {metrics.max_drawdown_pct:.1f}%",
                f"Max perdite consecutive: {metrics.max_consecutive_losses}",
                f"Tempo medio holding: {metrics.avg_holding_time_minutes:.1f} min",
                f"Token unici: {metrics.unique_tokens}",
            ]

            if metrics.weekly_trades > 0:
                lines.append(
                    f"\n📅 <b>7 giorni</b>: {metrics.weekly_trades} trade · P&L {money(metrics.weekly_pnl_usd, sign=True)} · WR {metrics.weekly_win_rate:.0%} · PF {_fmt_pf(metrics.weekly_profit_factor)}"
                )

            if metrics.monthly_trades > 0:
                lines.append(
                    f"\n📅 <b>30 giorni</b>: {metrics.monthly_trades} trade · P&L {money(metrics.monthly_pnl_usd, sign=True)} · WR {metrics.monthly_win_rate:.0%} · PF {_fmt_pf(metrics.monthly_profit_factor)}"
                )

            lines.append(f"\nFreschezza dati: {metrics.data_freshness_hours:.1f}h · Sconosciuti: {metrics.unknown_ratio:.0%}")

            trades = wi.get_wallet_trades(wallet)
            if trades:
                lines.append("\n📋 <b>Ultimi 5 trade</b>:")
                for t in trades[-5:]:
                    dt = datetime.fromtimestamp(t.sell_timestamp)
                    lines.append(
                        f" {dt.strftime('%d/%m %H:%M')} {html.escape(t.token_symbol)} {money(t.realized_pnl_usd, sign=True)} ({t.realized_pnl_pct:+.1f}%)"
                    )

            return "\n".join(lines)

        elif sub == "signals":
            limit = 10
            if len(args) > 1 and args[1].isdigit():
                limit = max(1, min(int(args[1]), 50))

            stats = wi.get_stats()
            cats = stats.get("categories", {})

            lines = [
                "📊 <b>Smart Money Signals</b>",
                f"Wallet: ELITE {cats.get('ELITE', 0)} · RISING {cats.get('WEEKLY_RISING', 0)} · RISK {cats.get('HIGH_RISK', 0)} · UNVERIFIED {cats.get('UNVERIFIED', 0)}",
                f"Trade totali: {stats['total_trades']}",
            ]

            copy_trades = [m for m in wi.get_all_wallet_metrics().values() if m.verified_trades > 0][:limit]

            if copy_trades:
                lines.append(f"\n📋 <b>Wallet con trade recenti (top {len(copy_trades)})</b>:")
                for metrics in copy_trades:
                    lines.append(
                        f" <code>{metrics.wallet_address[:8]}...</code> {metrics.classification} · "
                        f"P&L {money(metrics.realized_pnl_usd, sign=True)} · "
                        f"Score {metrics.score:.2f}"
                    )

            return "\n".join(lines)

        else:
            return "Sottocomando non valido. Usa: /smartmoney status|top|wallet <address>|signals"

    @staticmethod
    def _birdeye_line(b):
        be = getattr(b, "birdeye", None)
        ar = getattr(b, "antirug", None)
        if not be:
            if not ar:
                return ""
            st = ar.stats
            return (
                f"\nAntiRug {ar.mode.upper()}{' + top10 applicato' if getattr(ar, 'enforce', ()) else ''}: "
                f"analizzati {st['evaluated']}, autorizzati {st['allow']}, "
                f"bloccati {st['block']}, non verificabili {st['unknown']}, critici {st.get('critical', 0)}"
            )
        st = be.status()
        return (
            f"\nBirdeye: ok {st['ok']}, errori {st['fail']}, saltate per limite {st['skipped_rate_limit']}"
            + (f", in pausa {st['paused_s']} s" if st["paused_s"] else "")
            + (
                f", endpoint disattivati {len(st.get('disabled_endpoints') or {})}"
                if st.get("disabled_endpoints")
                else ""
            )
            + f" · copia {'sì' if getattr(b, 'birdeye_copy', False) else 'no'}"
            + f" · AntiRug {(getattr(b, 'antirug_mode', 'off') or 'off').upper()}"
            + (" + top10 applicato" if getattr(ar, "enforce", ()) else "")
        )

    # ---------------------------------------------------------------- executed by the main loop
    def _pause(self):
        b = self.bot
        if b.meta.get("paused"):
            return
        b.meta["paused"] = True
        log.info("Telegram /pausa: new buys paused")
        self.reply(
            "⏸ Pausa: nessun nuovo acquisto. Le posizioni aperte restano gestite (stop, target, tempo). "
            "/riprendi per ripartire."
        )

    def _resume(self, args):
        b = self.bot
        if b.meta.get("dead"):
            self.reply(
                "💀 Il bot è MORTO (capitale esaurito): non può più comprare. "
                "Nuova vita: RESET_STATE=1 e riavvio."
            )
            return
        rk = getattr(b, "risk", None)
        force = bool(args) and args[0].lower() in ("forza", "force", "si", "sì")
        if rk is not None and rk.paused:
            ok, why = rk.user_resume(force=force)
            if not ok:
                p = rk.pause_info()
                self.reply(
                    f"🛡 Il Risk Manager ha messo in pausa i nuovi acquisti ({html.escape(p['reason'])}: "
                    f"{html.escape(p.get('text', ''))}). Per forzare la ripresa scrivi <code>/riprendi forza</code>."
                )
                return
            self.reply("▶️ Nuovi acquisti riattivati (ripresa controllata dal Risk Manager).")
            return
        if b.meta.get("paused"):
            b.meta["paused"] = False
            log.info("Telegram /riprendi: new buys resumed")
            self.reply("▶️ Nuovi acquisti riattivati.")

    def run_actions(self):
        b = self.bot
        while True:
            try:
                kind, arg = self.actions.get_nowait()
            except queue.Empty:
                return
            try:
                # All queued Telegram mutations share PaperBot's RLock with the trading loop.
                # RLock is required because sell()/request_stop()/save paths may re-enter it.
                with b.lock:
                    if kind == "pause":
                        self._pause()
                    elif kind == "resume":
                        self._resume(arg)
                    elif kind == "set":
                        key, val = arg
                        cfg_set(b.c, key, val)
                        b.meta.setdefault("overrides", {})[key] = val
                        if hasattr(b, "brain") and hasattr(b.brain, "lock_param"):
                            b.brain.lock_param(key)
                        log.info("Setting changed via Telegram: %s=%s", key, val)
                    elif kind == "reset":
                        atomic_json(
                            os.path.join(b.data, "reset.request.json"),
                            {"requested_ts": time.time(), "source": "telegram", "confirmed": True},
                        )
                        b.reset_pending = True
                        b.request_stop("reset totale richiesto da Telegram")
                        log.warning("Full paper reset requested via Telegram; graceful shutdown started")
                        self.reply("🛑 Reset in corso: arresto controllato e backup prima di azzerare i dati…")
                        return
                    elif kind in ("close", "close_all"):
                        mints = [arg] if kind == "close" else list(b.positions)
                        mints = [m for m in mints if m in b.positions]
                        snaps = b.ds.tokens(mints) if mints else {}
                        ok_n, bad, closed_names = 0, [], []
                        for m in mints:
                            label = str((b.positions.get(m) or {}).get("name", m[:8]))
                            try:
                                # Re-check while holding the bot lock: positions may have changed
                                # between command enqueue and execution.
                                if m not in b.positions:
                                    continue
                                px = (snaps.get(m) or {}).get("price") or b.positions[m]["last"]
                                stale = not (snaps.get(m) or {}).get("price")
                                b.sell(m, px, "manual_close")

                                # A successful call is not proof of a successful exit:
                                # sell() can return early when Jupiter has no valid quote.
                                if m not in b.positions:
                                    ok_n += 1
                                    closed_names.append(label)
                                else:
                                    bad.append(f"{label}: posizione ancora aperta")
                                if stale:
                                    log.warning(
                                        "/chiuditutto %s: prezzo snapshot mancante, chiuso all'ultimo prezzo noto", m[:8]
                                    )
                            except Exception as e:
                                if m not in b.positions:
                                    ok_n += 1
                                    closed_names.append(label)
                                else:
                                    bad.append(f"{label}: {type(e).__name__}")

                        if kind == "close_all":
                            msg = f"✅ Chiuse (simulato) {ok_n} posizioni"
                            if bad:
                                msg += f" · ❌ {len(bad)} non chiuse: " + "; ".join(bad)
                            self.reply(msg + ".")
                        elif ok_n:
                            self.reply(
                                f"✅ Posizione {html.escape(', '.join(closed_names))} chiusa (simulato)."
                            )
                        elif bad:
                            self.reply(f"❌ Chiusura non riuscita: {'; '.join(bad)}")
            except Exception as e:
                log.exception("action %s error: %s", kind, e)
