"""Telegram notifications for the PAPER bot (Italian messages).

- Non-blocking: send() only enqueues; a background thread delivers.
- Persistent: undelivered messages are kept in data/telegram_pending.json and delivered after a restart.
- Confirmed: a message counts as sent only when Telegram answers ok=true with a message_id.
- Retries with exponential back-off + jitter (honours 429 retry_after); 4xx (bad token/chat) is not retried;
  an HTML formatting error is retried once as plain text. Messages > 4000 chars are split.
- Deduplication: send(text, key=...) drops a message whose key was already sent recently (alerts).
- flush() waits until the queue is really empty (including the message being delivered).
- Never raises into the bot. Dry mode (no token/chat id): messages only go to data/telegram_outbox.log.
"""
import html
import json
import logging
import os
import queue
import random
import re
import threading
import time
import requests

from storage import CappedLog, atomic_json

log = logging.getLogger("paperbot.telegram")
MAX_LEN = 4000

MOTIVI = {
    "stop_loss": "stop loss",
    "take_profit": "take profit",
    "take_profit_1": "take profit 1 (uscita parziale 50%, SL a breakeven)",
    "take_profit_2": "take profit 2 (target finale raggiunto)",
    "breakeven_stop": "stop a breakeven (dopo TP1)",
    "trailing_stop": "trailing stop (il prezzo è sceso dal massimo)",
    "time_exit": "tempo scaduto",
    "time_exit_stale": "tempo scaduto (prezzo non aggiornato, ultimo prezzo noto)",
    "no_price_data_assume_rug": "nessun prezzo disponibile: probabile rug pull",
    "crash_liquidity_evaporated": "crollo >70% con liquidità evaporata (uscita di emergenza)",
    "anti_panic_rebound_exit": "rimbalzo tecnico post-crollo (anti-panic sell)",
    "crash_rebound_timeout": "timeout attesa rimbalzo post-crollo (anti-panic)",
    "preventive_slippage_limit": "slippage preventivo oltre soglia massima",
    "blacklisted_entity": "creator o DEX in blacklist automatica 24h",
    "end_of_run": "fine sessione",
    "manual_close": "chiusura manuale (comando Telegram)",
    "top_traders_sold": "i top trader hanno venduto",
    "unconfirmed_price_exit": "prezzo non confermato da Jupiter (uscita al valore eseguibile)",
}

SLIP_SRC = {
    "jupiter": "slippage da quote Jupiter",
    "jupiter_v6": "slippage da quote Jupiter",
    "liquidity_model": "slippage stimato (modello liquidità)",
    "fallback_model": "slippage stimato (fallback modello liquidità)",
    "stimato": "slippage stimato (modello liquidità)",
}


def _src(src):
    if not src:
        return "stimato"
    if src in ("jupiter", "jupiter_v6"):
        return "quote Jupiter"
    if src in ("liquidity_model", "fallback_model", "stimato"):
        return "modello liquidità"
    return str(src)


def slip_src(src):
    return SLIP_SRC.get(src or "stimato", f"slippage ({src})")


def rules_line(r):
    """Exit rules of one position (they differ by entry type)."""
    parts = [f"stop {pct(-100 * r['stop_loss_pct'], 0)}"]
    if r.get("take_profit_1_pct"):
        parts.append(f"TP1 {pct(100 * r['take_profit_1_pct'], 0)} (50%)")
    if r.get("take_profit_pct"):
        lbl = "TP2" if r.get("take_profit_1_pct") else "target"
        parts.append(f"{lbl} {pct(100 * r['take_profit_pct'], 0)}")
    if r.get("trailing_stop_pct"):
        parts.append(f"trailing {pct(100 * r['trailing_stop_pct'], 0, False)} dopo {pct(100 * r['trailing_activate_pct'], 0)}")
    if r.get("follow_wallets"):
        parts.insert(0, f"uscita quando più del {pct(100 * r['sold_majority_pct'], 0, False)} dei top trader seguiti vende almeno il {pct(100 * r['reduced_pct'], 0, False)}")
        parts.append("nessuna scadenza temporale")
    else:
        parts.append(f"max {eur(r['max_hold_minutes'], 0)} min")
    return " · ".join(parts)


TIPI = {"volume_spike": "picco di volume", "copy_top_traders": "copia dei top trader"}


def eur(x, dec=2):
    """Italian number format: 1234.5 -> '1.234,50'."""
    x = _num(x)
    s = f"{abs(x):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if x < 0 else "") + s


def _num(x):
    """Any non-numeric / missing value (None, "", garbage, NaN) -> 0.0."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0


def _div(a, b):
    """a / b, guarded: a missing or zero denominator -> 0.0."""
    a, b = _num(a), _num(b)
    return a / b if b else 0.0


def _token_qty(value, decimals=None):
    """Format SPL token quantities without hiding sub-micro balances."""
    value = _num(value)
    try:
        places = int(decimals) if decimals is not None else 12
    except (TypeError, ValueError, OverflowError):
        places = 12
    places = max(0, min(18, places))
    rendered = f"{value:,.{places}f}"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if not rendered or rendered == "-0":
        rendered = "0"
    return rendered.replace(",", "X").replace(".", ",").replace("X", ".")


CUR = {"rate": 1.0, "sym": "$"}


def _rate():
    try:
        r = float(CUR.get("rate"))
    except (TypeError, ValueError):
        return 1.0
    return r if r else 1.0


def set_currency(usd_per_unit, sym):
    CUR["rate"], CUR["sym"] = _num(usd_per_unit) or 1.0, (sym or "$")[:8]


def usd(x, dec=2, sign=False):
    """Market data in US dollars (volumes, whale buys)."""
    s = eur(x, dec) + " $"
    return ("+" + s) if sign and x > 0 else s


def money(x, dec=2, sign=False):
    """Account amounts (USD internally) shown in the display currency (EUR)."""
    x = _num(x) / _rate()
    s = eur(x, dec) + " " + (CUR.get("sym") or "$")[:8]
    return ("+" + s) if sign and x > 0 else s


def pct(x, dec=1, sign=True):
    """Percentage."""
    x = _num(x)
    s = eur(x, dec) + "%"
    return ("+" + s) if sign and x > 0 else s


def price(x):
    x = _num(x)
    if not x:
        return "0"
    if x >= 1:
        return eur(x, 4)
    s = f"{x:.10f}".rstrip("0")
    i = next((k for k, ch in enumerate(s) if ch not in "0."), len(s))
    return s[: i + 4].replace(".", ",")


def split_text(text, n=MAX_LEN):
    """Split a long message into Telegram-sized chunks without empty fragments."""
    if len(text) <= n:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > n:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:n])
            line = line[n:]
        if len(cur) + len(line) + 1 > n:
            if cur:
                parts.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        parts.append(cur)
    return parts


class Telegram:
    API = "https://api.telegram.org/bot{token}/sendMessage"
    MAX_AGE = 24 * 3600
    RESTORED_MAX_AGE = 2 * 3600
    DEDUPE_S = 600

    def __init__(self, token=None, chat_id=None, outbox_path=None, enabled=True, pending_path=None, max_pending=500):
        self.token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        self.enabled = enabled
        self.dry = not (self.token and self.chat_id)
        self.outbox = outbox_path
        self.outlog = CappedLog(outbox_path, max_bytes=2_000_000, backups=1)
        self.pending_path = pending_path
        self.max_pending = max_pending
        self.q = queue.Queue()
        self.sent = self.failed = self.dropped = self.deduped = 0
        self.last_ok_ts = None
        self.last_error = ""
        self.s = requests.Session()
        self._lock = threading.Lock()
        self._pending = {}
        self._keys = {}
        self._seq = 0
        self.persist_errors = 0
        self.persist_last_error = None
        self._stop = threading.Event()
        self._load_pending()
        self._t = threading.Thread(target=self._worker, name="telegram", daemon=True)
        self._t.start()
        if self.dry:
            log.warning(
                "Telegram: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID non impostati -> modalità prova "
                "(messaggi scritti solo in %s)",
                outbox_path,
            )

    def _pending_is_fresh(self, item, at=None, max_age=None):
        """Reject malformed/future/expired queue timestamps instead of replaying stale alerts."""
        try:
            ts = float(item.get("ts"))
            current = time.time() if at is None else float(at)
            age_limit = self.MAX_AGE if max_age is None else max_age
            return 0 < ts <= current + 60 and current - ts <= age_limit
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False

    def _load_pending(self):
        if not self.pending_path or not os.path.exists(self.pending_path):
            return
        try:
            with open(self.pending_path, encoding="utf-8") as fh:
                d = json.load(fh)
            current = time.time()
            self._keys = {
                k: v
                for k, v in (d.get("keys") or {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                and 0 <= current - v < self.DEDUPE_S
            }
            items = d.get("pending") or []
            valid = [
                it for it in items
                if isinstance(it, dict) and it.get("id")
                and self._pending_is_fresh(it, current, self.RESTORED_MAX_AGE)
            ]
            expired = len(items) - len(valid)
            valid.sort(key=lambda x: x.get("seq", 0) if isinstance(x.get("seq", 0), (int, float)) else 0)
            for it in valid[-self.max_pending:]:
                it["restored"] = True
                it["restored_at"] = current
                seq = it.get("seq", 0)
                self._seq = max(self._seq, seq if isinstance(seq, int) and not isinstance(seq, bool) else 0)
                self._pending[it["id"]] = it
                self.q.put(it)
            if expired:
                log.info("Telegram: %d messaggi pendenti scartati (timestamp non valido o oltre 2h)", expired)
            if valid:
                log.info("Telegram: %d messaggi non consegnati ripresi dal disco", len(self._pending))
        except Exception as e:
            log.warning("telegram_pending.json illeggibile (%s): ignorato", e)

    def persist_pending(self):
        if not self.pending_path:
            return
        with self._lock:
            current = time.time()
            fresh = {k: v for k, v in self._pending.items() if self._pending_is_fresh(v, current)}
            self._pending = fresh
            data = {"pending": list(self._pending.values()), "keys": self._keys}
        try:
            atomic_json(self.pending_path, data, indent=None, lock_timeout=3)
            self.persist_last_error = None
            return True
        except Exception as e:
            self.persist_errors += 1
            self.persist_last_error = f"{type(e).__name__}: {e}"[:240]
            log.error("CRITICAL: cannot persist telegram queue (pending messages may be lost on restart): %s", e)
            return False

    def send(self, text, key=None):
        if not self.enabled or not text:
            return False
        try:
            now = time.time()
            with self._lock:
                if key and now - self._keys.get(key, 0) < self.DEDUPE_S:
                    self.deduped += 1
                    return False
                if len(self._pending) >= self.max_pending:
                    self.dropped += 1
                    log.warning("Telegram queue full (%d), message dropped", self.max_pending)
                    return False
                items = []
                for part in split_text(text):
                    self._seq += 1
                    it = {
                        "id": f"{int(now * 1000)}-{self._seq}",
                        "seq": self._seq,
                        "text": part,
                        "ts": now,
                        "key": key,
                        "attempts": 0,
                    }
                    self._pending[it["id"]] = it
                    items.append(it)
            for it in items:
                self.q.put_nowait(it)
            if not self.dry:
                self.persist_pending()
            return True
        except Exception as e:
            log.warning("Telegram send() error: %s", e)
            return False

    def send_photo(self, path, caption="", timeout=60):
        """Synchronous sendPhoto (call it from a worker thread)."""
        if not self.enabled:
            return False, "telegram disattivato"
        if self.dry:
            self._write_outbox(f"[foto {os.path.basename(path)}] {caption}")
            return True, "dry"
        url = self.API.format(token=self.token).replace("/sendMessage", "/sendPhoto")
        last = ""
        for attempt in range(3):
            try:
                with open(path, "rb") as f:
                    r = self.s.post(
                        url,
                        data={"chat_id": self.chat_id, "caption": caption[:1000], "parse_mode": "HTML"},
                        files={"photo": ("dashboard.png", f, "image/png")},
                        timeout=(5, timeout),
                    )
                body = {}
                try:
                    body = r.json()
                except ValueError:
                    pass
                if r.status_code == 200 and body.get("ok") is True:
                    self.last_ok_ts = time.time()
                    self._write_outbox(f"[foto inviata] {caption}")
                    return True, "ok"
                last = f"HTTP {r.status_code} {str(body.get('description', ''))[:120]}"
                if r.status_code == 429:
                    time.sleep(min(30.0, float((body.get("parameters") or {}).get("retry_after", 5))))
                    continue
                if 400 <= r.status_code < 500:
                    break
            except (requests.RequestException, OSError) as e:
                last = type(e).__name__
            time.sleep(2 * (attempt + 1))
        return False, last

    def flush(self, timeout=15):
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(0.05)
        return self.q.unfinished_tasks == 0

    def status(self):
        return {
            "dry": self.dry,
            "sent": self.sent,
            "failed": self.failed,
            "pending": len(self._pending),
            "dropped": self.dropped,
            "deduped": self.deduped,
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
            "persist_errors": self.persist_errors,
            "persist_last_error": self.persist_last_error,
        }

    def _worker(self):
        while True:
            it = self.q.get()
            try:
                ok = self._deliver(it)
                with self._lock:
                    self._pending.pop(it["id"], None)
                if not ok:
                    self.failed += 1
                if not self.dry:
                    self.persist_pending()
            except Exception as e:
                self.failed += 1
                log.warning("Telegram unexpected error: %s", e)
            finally:
                self.q.task_done()

    def _write_outbox(self, text):
        self.outlog.write(
            time.strftime("%Y-%m-%d %H:%M:%S ") + ("[DRY] " if self.dry else "") + text.replace("\n", "\n    ") + "\n"
        )

    def _deliver(self, it):
        restored = bool(it.get("restored"))
        max_age = self.RESTORED_MAX_AGE if restored else self.MAX_AGE
        if not self._pending_is_fresh(it, max_age=max_age):
            limit_label = "2 h" if restored else "24 h"
            log.warning("Telegram: messaggio pendente non valido o oltre %s scartato", limit_label)
            return False
        text = it["text"]
        if restored:
            text = (
                f"⏳ <i>messaggio ripreso dalla coda dopo un riavvio (creato alle "
                f"{time.strftime('%H:%M', time.localtime(it['ts']))})</i>\n"
                + text
            )
        if self.dry:
            self._write_outbox(text)
            self.sent += 1
            return True
        url = self.API.format(token=self.token)
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        attempt, plain_tried = 0, False
        while True:
            if not self._pending_is_fresh(it, max_age=max_age):
                log.warning("Telegram: messaggio più vecchio della finestra consentita scartato")
                return False
            attempt += 1
            it["attempts"] = it.get("attempts", 0) + 1
            delay = min(60.0, 2.0 * 2 ** min(attempt - 1, 5)) * random.uniform(0.7, 1.3)
            try:
                r = self.s.post(url, json=payload, timeout=(5, 10))
                body = {}
                try:
                    body = r.json()
                except ValueError:
                    pass
                if r.status_code == 200 and body.get("ok") is True:
                    self.last_msg_id = (
                        (body.get("result") or {}).get("message_id") if isinstance(body.get("result"), dict) else None
                    )
                    self.sent += 1
                    self.last_ok_ts = time.time()
                    if it.get("key"):
                        with self._lock:
                            self._keys[it["key"]] = time.time()
                    self._write_outbox(text)
                    return True
                if r.status_code == 429:
                    try:
                        delay = float((body.get("parameters") or {}).get("retry_after") or delay)
                    except (TypeError, ValueError):
                        delay = min(delay, 30.0)
                    self.last_error = "429"
                elif r.status_code == 400 and "parse" in str(body.get("description", "")).lower() and not plain_tried:
                    plain_tried = True
                    payload = {
                        "chat_id": self.chat_id,
                        "text": re.sub(r"<[^>]+>", "", html.unescape(text)),
                        "disable_web_page_preview": True,
                    }
                    continue
                elif 400 <= r.status_code < 500:
                    self.last_error = f"HTTP {r.status_code}"
                    log.warning(
                        "Telegram rifiuta il messaggio: HTTP %s %s", r.status_code, str(body.get("description", ""))[:150]
                    )
                    return False
                else:
                    self.last_error = f"HTTP {r.status_code}" if r.status_code != 200 else "risposta senza ok=true"
            except requests.RequestException as e:
                self.last_error = type(e).__name__
                if attempt <= 3 or attempt % 20 == 0:
                    log.warning("Telegram errore di rete (tentativo %d): %s", attempt, type(e).__name__)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:60]}"
                if attempt <= 3:
                    log.warning("Telegram eccezione non di rete (tentativo %d): %s", attempt, e)
                else:
                    log.error("Telegram eccezione persistente su messaggio %s: %s", it.get("id"), e)
            if self._stop.wait(min(delay, 60)):
                return False


# ------------------------------------------------------------------ message builders (Italian)
def _tok(name):
    return "$" + html.escape(str(name or "?"))


def msg_buy(p, cash, n_open, rules):
    i = p.get("info") or {}
    if p["entry_type"] == "volume_spike":
        why = (
            f"picco di volume {eur(i.get('vol_ratio', 0), 0)}× in 5 min "
            f"(volume 5m {usd(i.get('vol_m5', 0), 0)}), coppia nata da {eur(i.get('age_min', 0), 0)} min, "
            f"dev {pct(i.get('dev_pct', 0), 1, False)}, top10 {pct(i.get('top10_pct', 0), 0, False)}"
            + (f", balena {usd(i['whale_buy_usd'], 0)}" if i.get("whale_buy_usd") else "")
        )
    else:
        why = (
            f"{i.get('top_traders_in', '?')} top trader già dentro, coppia nata da "
            f"{eur(i.get('age_min', 0), 0)} min, top10 {pct(i.get('top10_pct', 0), 0, False)}"
        )
    c = p["costs"]
    jup = c.get("buy_slip_src") in ("jupiter", "jupiter_v6")
    tr = p.get("track") or {}
    follow = (
        f"👛 Seguo {tr.get('n', 0)} wallet di top trader (saldi on-chain)\n"
        if p["entry_type"] == "copy_top_traders" and rules.get("follow_wallets")
        else ""
    )
    lat_parts = []
    if p.get("decision_latency_ms") is not None:
        lat_parts.append(f"decisione {eur(p['decision_latency_ms'], 0)} ms")
    if p.get("quote_latency_ms") is not None:
        lat_parts.append(f"quote {eur(p['quote_latency_ms'], 0)} ms")
    lat_line = f"⚡ Telemetria: {' · '.join(lat_parts)}\n" if lat_parts else ""
    return (
        f"🟢 <b>ACQUISTO (simulato) {_tok(p['name'])}</b> — {money(p['cost_usd'])}\n"
        f"🧠 Motivo: {why}\n"
        f"📈 Prezzo {price(p['entry_mid'])} $ → eseguito a {price(p['entry_fill'])} $ "
        f"({slip_src(c.get('buy_slip_src'))} {pct(c['buy_slip_pct'], 1, False)}"
        + (f", via {html.escape(c['buy_route'])}" if jup and c.get("buy_route") else "")
        + ")\n"
        + (
            f"💸 Commissioni: DEX incluse nel quote Jupiter + rete {money(c['buy_net_fee'])}\n"
            if jup
            else f"💸 Commissioni: DEX {money(c['buy_dex_fee'])} + rete {money(c['buy_net_fee'])}\n"
        )
        + f"🎯 Uscita ({TIPI.get(p['entry_type'], p['entry_type'])}): {rules_line(rules)}\n"
        f"{follow}{lat_line}"
        f"💼 Cassa {money(cash)} · posizioni aperte {n_open}\n"
        f"🔗 https://dexscreener.com/solana/{p.get('pool') or p['mint']}\n"
        f"<i>Simulazione: nessun denaro reale</i>"
    )


def msg_tp1(p, cash, rules, qty_sold, fill_price, pnl, cost_partial):
    """TP1 partial take profit notification."""
    entry_mid = p.get("entry_mid", 0)
    chg_pct = (fill_price / entry_mid - 1) * 100 if entry_mid > 0 else 0
    orig_qty = p.get("original_qty", p["qty"] + qty_sold)
    rem_qty = p.get("remaining_qty", p["qty"])
    token_decimals = p.get("token_decimals", (p.get("info") or {}).get("decimals"))
    tp1_pct = rules.get("tp1_pct", 0.25) * 100
    tp1_frac = rules.get("tp1_sell_fraction", 0.50) * 100

    return (
        f"🟡 <b>TP1 RAGGIUNTO (uscita parziale) {_tok(p['name'])}</b> — ✅ "
        f"P&L parziale {money(pnl, sign=True)} ({pct(pnl / cost_partial * 100 if cost_partial else 0, 1)})\n"
        f"🧠 Motivo: {MOTIVI.get('take_profit_1', 'take profit 1')}\n"
        f"📈 Prezzo entrata: {price(entry_mid)} $ → Prezzo TP1: {price(fill_price)} $ ({pct(chg_pct, 1)})\n"
        f"📊 Quantità venduta: {_token_qty(qty_sold, token_decimals)} ({tp1_frac:.0f}% della posizione originale {_token_qty(orig_qty, token_decimals)})\n"
        f"💰 Posizione residua: {_token_qty(rem_qty, token_decimals)} (costo residuo {money(p.get('cost_usd', 0))})\n"
        f"💸 Commissioni TP1: {money(p.get('tp1_fees', 0))} (slippage + rete + DEX)\n"
        f"💼 Cassa: {money(cash)} · posizioni aperte: {len(p.get('positions', [])) if 'positions' in p else '?'}\n"
        f"<i>Simulazione: nessun denaro reale</i>"
    )


def msg_breakeven(p, cash, rules, breakeven_price):
    """Breakeven stop activation notification."""
    entry_fill = p.get("entry_fill", 0)
    orig_qty = p.get("original_qty", p["qty"])
    rem_qty = p.get("remaining_qty", p["qty"])
    token_decimals = p.get("token_decimals", (p.get("info") or {}).get("decimals"))

    return (
        f"🛡 <b>BREAKEVEN ATTIVATO {_tok(p['name'])}</b>\n"
        f"📉 Stop protettivo spostato a: {price(breakeven_price)} $ (prezzo di carico effettivo)\n"
        f"📊 Posizione residua: {_token_qty(rem_qty, token_decimals)} (era {_token_qty(orig_qty, token_decimals)} prima di TP1)\n"
        f"💼 Cassa: {money(cash)}\n"
        f"ℹ️ Il trailing stop userà il livello più alto tra breakeven e trailing.\n"
        f"<i>Simulazione: nessun denaro reale</i>"
    )


def _completed_trade_rows(closed):
    """Exclude TP1 execution rows from logical trade counts; keep legacy rows."""
    return [x for x in closed if x.get("counts_as_trade", not (x.get("partial") or x.get("exit_reason") == "take_profit_1")) is not False]


def _logical_trade_pnl(trade):
    return float(trade.get("cumulative_pnl_usd", trade.get("pnl_usd", 0.0)) or 0.0)


def msg_tp2(t, cash, start, closed, cfg, equity=None, n_open=0):
    """TP2 final take profit notification, with cumulative logical-trade P&L."""
    total_pnl = _logical_trade_pnl(t)
    win = total_pnl > 0
    completed = _completed_trade_rows(closed)
    wins = sum(1 for x in completed if _logical_trade_pnl(x) > 0)
    reason = t["exit_reason"]
    why = MOTIVI.get(reason, reason)
    if reason == "take_profit_2":
        why += f" ({pct(100 * cfg.get('tp2_pct', cfg.get('take_profit_pct', 0.60)), 0)})"
    eq = cash if equity is None else equity
    tot = eq - start
    held = t["hold_sec"] / 60

    # TP1 is linked by logical trade id; never aggregate an earlier re-entry
    # of the same mint into this trade's result.
    tp1_pnl = float(t.get("tp1_realized_pnl_usd", 0.0) or 0.0)
    if "tp1_realized_pnl_usd" not in t:
        trade_id = t.get("trade_id")
        matching = [ct for ct in closed if ct.get("exit_reason") == "take_profit_1"
                    and ((trade_id and ct.get("trade_id") == trade_id)
                         or (not trade_id and ct.get("mint") == t.get("mint")
                             and ct.get("entry_ts") == t.get("entry_ts")))]
        tp1_pnl = sum(float(ct.get("pnl_usd", 0.0) or 0.0) for ct in matching)
        total_pnl = float(t.get("pnl_usd", 0.0)) + tp1_pnl

    if n_open and equity is not None and abs(equity - cash) > 1e-9:
        bal = (
            f"💼 Saldo {money(eq)} (liquidità {money(cash)} + {n_open} "
            f"{'posizione aperta' if n_open == 1 else 'posizioni aperte'} {money(eq - cash)} a mercato; "
            f"partenza {money(start)}, totale {money(tot, sign=True)} / {pct(100 * _div(tot, start), 1)})\n"
        )
    else:
        bal = (
            f"💼 Saldo {money(cash)} (partenza {money(start)}, totale {money(tot, sign=True)} / "
            f"{pct(100 * _div(tot, start), 1)})\n"
        )

    late = (
        f"\n⏱ Uscita in ritardo di {eur(t['late_sec'] / 60, 0)} min (bot rallentato)"
        if t.get("late_sec", 0) >= 60
        else ""
    )

    return (
        f"🟢 <b>TP2 RAGGIUNTO (chiusura finale) {_tok(t['token'])}</b> — {'✅' if win else '❌'} "
        f"P&L finale {money(total_pnl, sign=True)} ({pct(t.get('cumulative_pnl_pct', t['pnl_pct']), 0)})\n"
        f"🧠 Motivo: {why}\n"
        f"📉 Prezzo: entrata {price(t['entry_mid'])} $ → uscita {price(t['exit_mid'])} $ "
        f"({pct(t['price_move_pct'], 1)}) · tenuto {eur(held, 0)} min{late}\n"
        f"💸 Costi: slippage acquisto {pct(t.get('buy_slip_pct', 0), 1, False)} + vendita {pct(t.get('sell_slip_pct', 0), 1, False)}, "
        f"commissioni {money(t['dex_fees_usd'] + t['network_fees_usd'])}"
        + (f", vendite fallite {t['failed_exits']}" if t.get("failed_exits") else "")
        + f"\n📊 P&L TP1: {money(tp1_pnl, sign=True)} · P&L TP2: {money(t['pnl_usd'], sign=True)} · <b>TOTALE: {money(total_pnl, sign=True)}</b>\n"
        + bal
        + f"📊 Trade completati {len(completed)} · vinti {wins} · win rate {pct(100 * _div(wins, len(completed)), 0, False)}\n"
        f"<i>Simulazione: nessun denaro reale</i>"
    )


def msg_sell(t, cash, start, closed, cfg, equity=None, n_open=0):
    """cfg = exit rules of this trade (PaperBot.exit_rules)."""
    win = _logical_trade_pnl(t) > 0
    completed = _completed_trade_rows(closed)
    wins = sum(1 for x in completed if _logical_trade_pnl(x) > 0)
    reason = t["exit_reason"]
    why = MOTIVI.get(reason, reason)
    if reason == "stop_loss":
        why += f" ({pct(-100 * cfg['stop_loss_pct'], 0)})"
    elif reason in ("take_profit", "take_profit_2"):
        why += f" ({pct(100 * cfg['take_profit_pct'], 0)})"
    elif reason == "take_profit_1" and cfg.get("take_profit_1_pct"):
        why += f" (+{eur(100 * cfg['take_profit_1_pct'], 0)}%)"
    elif reason.startswith("time_exit"):
        why += f" ({eur(cfg['max_hold_minutes'], 0)} min)"
    elif reason == "top_traders_sold":
        why += f" ({t.get('wallets_sold', 0)} su {t.get('wallets_tracked', 0)} wallet seguiti hanno venduto/ridotto)"
    elif reason == "trailing_stop":
        why += f" ({pct(100 * cfg['trailing_stop_pct'], 0, False)} dal massimo)"
    eq = cash if equity is None else equity
    tot = eq - start
    held = t["hold_sec"] / 60
    if n_open and equity is not None and abs(equity - cash) > 1e-9:
        bal = (
            f"💼 Saldo {money(eq)} (liquidità {money(cash)} + {n_open} "
            f"{'posizione aperta' if n_open == 1 else 'posizioni aperte'} {money(eq - cash)} a mercato; "
            f"partenza {money(start)}, totale {money(tot, sign=True)} / {pct(100 * _div(tot, start), 1)})\n"
        )
    else:
        bal = (
            f"💼 Saldo {money(cash)} (partenza {money(start)}, totale {money(tot, sign=True)} / "
            f"{pct(100 * _div(tot, start), 1)})\n"
        )
    late = (
        f"\n⏱ Uscita in ritardo di {eur(t['late_sec'] / 60, 0)} min (bot rallentato)"
        if t.get("late_sec", 0) >= 60
        else ""
    )
    prov = t.get("price_provenance", "ESTIMATED")
    prov_label = "VERIFIED" if prov == "VERIFIED" else ("ESTIMATO" if prov == "ESTIMATED" else prov)
    liq = t.get("entry_liquidity", 0)
    liq_label = "VERIFIED" if liq > 0 else "UNKNOWN"
    dev_src = t.get("dev_pct_source", "unknown")
    top10_q = t.get("top10_pct_quality", "UNKNOWN")
    dq_parts = []
    if prov != "VERIFIED":
        dq_parts.append(f"qualità prezzo: {prov_label}")
    if liq <= 0:
        dq_parts.append(f"liquidità: {liq_label}")
    if dev_src == "unknown":
        dq_parts.append("dev: UNKNOWN")
    if top10_q == "UNKNOWN":
        dq_parts.append("top10: UNKNOWN")
    dq_line = f"\n⚠️ Dati: {' · '.join(dq_parts)}" if dq_parts else ""
    partial_tag = " [PARZIALE]" if t.get("partial") or reason == "take_profit_1" else ""
    real_buy_slip = t.get("realized_buy_slippage_pct", t.get("buy_slip_pct", 0))
    real_sell_slip = t.get("realized_sell_slippage_pct", t.get("sell_slip_pct", 0))
    return (
        f"🔴 <b>VENDITA{partial_tag} (simulata) {_tok(t['token'])}</b> — {'✅' if win else '❌'} "
        f"P&amp;L {money(_logical_trade_pnl(t), sign=True)} ({pct(t.get('cumulative_pnl_pct', t['pnl_pct']), 0)})\n"
        f"🧠 Motivo: {why}\n"
        f"📉 Prezzo: entrata {price(t['entry_mid'])} $ → uscita {price(t['exit_mid'])} $ "
        f"({pct(t['price_move_pct'], 1)}) · tenuto {eur(held, 0)} min{late}\n"
        f"💸 Costi: slippage acquisto {pct(real_buy_slip, 1, False)} ({_src(t.get('buy_slip_src'))}) "
        f"+ vendita {pct(real_sell_slip, 1, False)} ({_src(t.get('sell_slip_src'))}), "
        f"commissioni {money(t['dex_fees_usd'] + t['network_fees_usd'])}"
        + (f", vendite fallite {t['failed_exits']}" if t.get("failed_exits") else "")
        + dq_line
        + "\n"
        + bal
        + f"📊 Trade completati {len(completed)} · vinti {wins} · win rate {pct(100 * _div(wins, len(completed)), 0, False)}\n"
        f"<i>Simulazione: nessun denaro reale</i>"
    )


def msg_failed_sell(p, drop, fee):
    return (
        f"⚠️ <b>VENDITA FALLITA (simulata) {_tok(p['name'])}</b>\n"
        f"Il prezzo è crollato del {pct(100 * drop, 0, False)} in un controllo: la transazione simulata non è "
        f"passata (come succede davvero con la rete congestionata). Commissione di rete persa {money(fee)}. "
        f"Riprovo al prossimo controllo."
    )


DEFAULT_DISPLAY_NAME = "AEON by Rekt"


def display_name():
    v = os.getenv("BOT_DISPLAY_NAME")
    return DEFAULT_DISPLAY_NAME if v is None else v.strip()[:40]


def bot_header():
    b = display_name()
    return f"{html.escape(b)} (paper)" if b else "Paper bot"


def msg_start(
    cash,
    start,
    n_open,
    resumed,
    vybe_on,
    cfg,
    copy_rules=None,
    jup_on=True,
    copy_src="Vybe",
    antirug=False,
    antirug_enforce=(),
):
    vr = {
        k: cfg[k]
        for k in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct", "trailing_activate_pct", "max_hold_minutes")
    }
    ptp = cfg.get("partial_tp") or {}
    if ptp.get("enabled"):
        vr["take_profit_1_pct"] = ptp.get("tp1_pct")
    cd = cfg.get("reentry_cooldown_minutes") or 0
    max_slip = cfg.get("max_entry_slippage_pct")
    ap = cfg.get("anti_panic") or {}
    return (
        f"🚀 <b>{bot_header()} avviato</b>{' (ripreso dallo stato salvato)' if resumed else ''}\n"
        f"💼 Saldo {money(cash)} · partenza {money(start)} · posizioni aperte {n_open}\n"
        f"⚙️ Picco di volume: {rules_line(vr)}"
        + (f" · niente ri-acquisto dello stesso token per {eur(cd, 0)} min" if cd else "")
        + (f" · slippage max entrata {eur(max_slip, 1)}%" if max_slip else "")
        + "\n"
        f"👛 Copia top trader {'attiva (' + copy_src + ')' if vybe_on else 'disattivata (manca VYBE_API_KEY)'}"
        + (f": {rules_line(copy_rules)} · ricompra se i top trader ricomprano" if copy_rules else "")
        + "\n"
        f"💱 Prezzi di esecuzione: {'quote reali Jupiter (fallback: modello liquidità)' if jup_on else 'modello liquidità'}\n"
        f"🛡 Sopravvivenza: ogni trade {pct(100 * cfg['position_size_pct'], 0, False)} del capitale "
        f"(minimo {eur((cfg.get('survival') or {}).get('min_trade_eur', 3), 0)} €), "
        f"{'max ' + str(cfg['max_open_positions']) + ' posizioni' if cfg.get('max_open_positions') else 'nessun limite rigido di posizioni'}; "
        f"quando i soldi finiscono il bot MUORE\n"
        + (
            f"🧯 Anti-Panic crolli >{eur(100 * ap.get('crash_drop_pct', 0.70), 0)}%: attivo (verifica liquidità pool)\n"
            if ap.get("enabled")
            else ""
        )
        + f"🧠 Cervello: {'attivo (ogni 10 trade fino a 50, poi ogni 50)' if (cfg.get('brain') or {}).get('enabled', True) else 'spento'}\n"
        + (
            "🛡 AntiRug ACTIVE: blocca le entrate senza dati di sicurezza completi o con rischio rug (fail-closed)\n"
            if antirug == "active"
            else "👁 AntiRug SHADOW + top10 applicato: blocca SOLO le entrate con top 10 holder oltre il limite "
            "(dato verificato); tutto il resto è solo osservazione\n"
            if antirug and antirug_enforce
            else "👁 AntiRug SHADOW: valuta ogni entrata e registra cosa avrebbe bloccato, senza bloccare nulla\n"
            if antirug
            else ""
        )
        + "💬 Scrivi /aiuto per i comandi\n"
        "<i>Solo simulazione: nessun wallet, nessuna chiave, nessun ordine reale</i>"
    )


def msg_reset(note):
    if note.get("kind") == "ignored":
        when = time.strftime("%d/%m %H:%M", time.localtime(note.get("when") or 0))
        return (
            f"⚠️ <b>RESET_STATE={html.escape(str(note.get('value')))} è ancora impostato</b> ma era già stato applicato "
            f"il {when}: <b>ignorato</b>, ho ripreso lo stato salvato.\nToglilo dal file .env (RESET_STATE=0)."
        )
    arch = note.get("archived")
    return (
        f"🔄 <b>RESET: nuova simulazione dal saldo iniziale</b> (richiesto con {html.escape(str(note.get('value')))})\n"
        + (f"🗄 Lo stato precedente NON è cancellato: archiviato in data/{html.escape(arch)}\n" if arch else "")
        + "Il reset vale una sola volta: ai prossimi riavvii (anche watchdog/Docker) lo stato viene ripreso."
    )


def msg_stop(cash, start, n_open, reason):
    tot = cash - start
    return (
        f"⏹ <b>{bot_header()} fermato</b> ({html.escape(reason)})\n"
        f"💼 Saldo {money(cash)} ({money(tot, sign=True)}) · posizioni aperte {n_open} "
        f"(verranno riprese al riavvio)"
    )


def msg_watchdog(stalled_sec):
    return (
        f"🐕 <b>Watchdog</b>: il ciclo del bot era bloccato da {eur(stalled_sec / 60, 1)} min. "
        f"Lo riavvio; lo stato (saldo e posizioni) viene ripreso dal file salvato."
    )


def msg_daily(s, label):
    lines = [
        f"📊 <b>Resoconto {html.escape(label)}</b> (simulazione)",
        f"💼 Saldo {money(s['equity'])} su {money(s['start'])} → {money(s['equity'] - s['start'], sign=True)} "
        f"({pct(100 * (s['equity'] / s['start'] - 1), 1)})",
        f"🔁 Trade chiusi {s['n']} · vinti {s['wins']} · win rate "
        + (pct(s["win_rate"], 0, False) if s["win_rate"] is not None else "—"),
    ]
    if s.get("period_n") is not None:
        lines.append(f"🕘 Ultime 24 h: {s['period_n']} trade, P&amp;L {money(s['period_pnl'], sign=True)}")
    if s.get("best"):
        lines.append(f"🏆 Migliore {_tok(s['best']['token'])} {money(s['best']['pnl_usd'], sign=True)}")
    if s.get("worst"):
        lines.append(f"💀 Peggiore {_tok(s['worst']['token'])} {money(s['worst']['pnl_usd'], sign=True)}")
    lines.append(f"💸 Commissioni totali {money(s['fees'])} · slippage medio {pct(s['avg_slip'], 1, False)} per lato")
    if s.get("by_reason"):
        lines.append(
            "🚪 Uscite: " + ", ".join(f"{MOTIVI.get(k, k).split(' (')[0]} {v}" for k, v in s["by_reason"].items())
        )
    lines.append(f"📂 Posizioni aperte {s['open']}")
    return "\n".join(lines)


def msg_dead(r, start, sv):
    lines = [
        "💀 <b>MORTO</b> — il paper bot ha finito i soldi (simulazione)",
        f"Capitale finale {money(r['equity'])} su {money(start)} ({pct(100 * (_div(r['equity'], start) - 1), 1)}): "
        f"sotto il minimo per un trade ({money(sv['min_trade_usd'])} + commissioni).",
        f"Ha vissuto {sv.get('lifetime', '?')} · trade {r['n']} · vinti {r['wins']} · win rate "
        + (pct(r["win_rate"], 0, False) if r["win_rate"] is not None else "—"),
        f"Commissioni pagate {money(r['fees'])} · slippage medio {pct(r['avg_slip'], 1, False)} per lato",
    ]
    if r.get("best"):
        lines.append(
            f"🏆 Migliore {_tok(r['best']['token'])} {money(r['best']['pnl_usd'], sign=True)} · "
            f"💀 Peggiore {_tok(r['worst']['token'])} {money(r['worst']['pnl_usd'], sign=True)}"
        )
    lines.append("Il bot non farà più trade. Per una nuova vita: RESET_STATE=1 nel .env e riavvio (vedi README).")
    return "\n".join(lines)


def survival_line(sv):
    labels = {"ok": "✅ vivo e operativo", "paused": "⏸ in pausa (manuale)", "dead": "💀 MORTO"}
    s = labels.get(sv.get("mode"), sv.get("mode"))
    return (
        f"🛡 Sopravvivenza: {s}\n"
        f"   capitale {money(sv['equity_usd'])} · muore sotto {money(sv['death_line_usd'])} senza posizioni aperte\n"
        f"   prossimo trade {money(sv['next_size_usd'])} ({pct(sv['size_pct_eff'], 1, False)} del capitale, "
        f"minimo {money(sv['min_trade_usd'])}) · nessun limite di posizioni"
    )


def msg_brain(a, title="🧠 <b>Cervello</b> — analisi"):
    out = [f"{title} ({a['n']} trade)", html.escape(a["text"])]
    if a.get("changes"):
        out.append("\n<b>Parametri cambiati:</b>")
        out += [f"• {html.escape(c['text'])}" for c in a["changes"]]
    out.append("<i>Simulazione: cambia solo i filtri del paper bot.</i>")
    return "\n".join(out)


RISK_REASON = {
    "daily_loss": "perdita giornaliera massima",
    "drawdown": "drawdown massimo",
    "consecutive_losses": "perdite consecutive",
    "disk": "disco quasi pieno",
    "manual": "manuale",
}


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


def msg_risk(ev, mode="survival"):
    k = ev.get("kind")
    if k == "drawdown":
        return (
            f"📉 <b>Drawdown</b> −{eur(ev['dd_pct'], 1)}% dal massimo ({money(ev['peak'])} → {money(ev['equity'])})"
            + (" · modalità sopravvivenza: nessuno stop, solo avviso" if mode == "survival" else "")
        )
    if k == "pause":
        until = f" fino alle {_hm(ev['until'])}" if ev.get("until") else ""
        how = (
            "Per ripartire: /riprendi forza"
            if ev.get("manual")
            else "Ripresa automatica" + until + " (oppure /riprendi forza)"
        )
        return (
            f"🛡 <b>Risk Manager: nuovi acquisti in PAUSA</b> ({RISK_REASON.get(ev['reason'], ev['reason'])})\n"
            f"{html.escape(ev.get('text', ''))}\nLe posizioni aperte restano gestite. {how}."
        )
    if k == "resume":
        extra = (
            f" Ripresa controllata: size ×{eur(ev['factor'], 1)} per {ev['resume_trades']} trade."
            if ev.get("resume_trades") and ev.get("factor", 1) < 1
            else ""
        )
        return (
            f"▶️ <b>Risk Manager</b>: nuovi acquisti riattivati ({'automatico' if ev.get('auto') else 'comando'} · "
            f"{RISK_REASON.get(ev['reason'], ev['reason'])}).{extra}"
        )
    return f"🛡 Risk Manager: {html.escape(str(ev))[:300]}"


def msg_api_down(host, since, kind, n):
    mins = (time.time() - since) / 60 if since else 0
    return (
        f"⚠️ <b>API non disponibile</b>: {html.escape(host)} non risponde da {eur(mins, 0)} min "
        f"({html.escape(str(kind))}, {n} errori di fila). Il bot continua con le altre fonti e riprova da solo."
    )


def msg_api_up(host, down_s):
    return f"✅ <b>API di nuovo disponibile</b>: {html.escape(host)} (fuori servizio per {eur(down_s / 60, 0)} min)."


def msg_disk(ds):
    lvl = "CRITICO" if ds.get("level") == "critical" else "basso"
    return (
        f"💽 <b>Spazio disco {lvl}</b>: {eur(ds.get('free_mb', 0), 0)} MB liberi ({pct(ds.get('used_pct') or 0, 1, False)} usato)."
        + (
            " Nuovi acquisti in pausa finché non si libera spazio."
            if ds.get("level") == "critical"
            else " Libera spazio (docker system prune, vecchi backup)."
        )
    )


def msg_backup(r, every_min):
    return (
        f"💾 <b>Backup completato</b>: {r.get('files', 0)} file salvati in data/backups "
        f"({r.get('count', 0)} copie conservate, una ogni {eur(every_min, 0)} min). Prossimo avviso domani."
    )


def msg_backup_failed(err):
    return f"❌ <b>Backup NON riuscito</b>: {html.escape(str(err))[:200]}"


def msg_dashboard(ok, sec, kind=""):
    if ok:
        return f"✅ <b>Dashboard</b> di nuovo raggiungibile (era giù da {eur(sec / 60, 0)} min)."
    return (
        f"🖥 <b>Dashboard non raggiungibile</b> da {eur(sec / 60, 0)} min ({html.escape(str(kind))}). "
        f"Il bot continua a fare trading. Controlla: docker compose ps / logs dashboard."
    )


def msg_persistent_errors(n, last):
    return (
        f"🚨 <b>Errori persistenti</b>: {n} errori negli ultimi 10 min. Ultimo: <code>{html.escape(last)}</code>. "
        f"Il bot continua; se il ciclo si blocca il watchdog lo riavvia."
    )


def msg_recovered(info, cash, n_open, restarts):
    k = info.get("kind")
    if k == "corrupt_reset":
        return (
            "🚨 <b>Stato illeggibile</b>: state.json e tutti i backup erano corrotti, il bot è ripartito da capo. "
            "Il file danneggiato è stato conservato (.bad)."
        )
    if k == "restored_backup":
        what = f"recuperato da {html.escape(str(info.get('src')))}"
    elif k == "watchdog":
        what = "riavviato dal watchdog"
    else:
        what = "ripartito dopo un arresto non pulito (crash, kill o riavvio della VPS)"
    down = info.get("down_sec")
    return (
        f"♻️ <b>Ripristino</b>: il bot è {what}"
        + (f" dopo {eur(down / 60, 0)} min" if down else "")
        + ".\n"
        f"Stato ripreso: liquidità {money(cash)}, posizioni aperte {n_open} (uscite riapplicate subito). "
        f"Riavvii totali {restarts}."
    )


def msg_adaptive_poll_triggered(position_name, mint, reason, interval):
    return (
        f"⚡ <b>Monitoraggio Accelerato</b> ${_tok(position_name)}\n"
        f"Motivo: {reason}\n"
        f"Intervallo: {interval}s (base: 2.5s)\n"
        f"<i>Ritorna all'intervallo base dopo stabilità</i>"
    )


def msg_smart_recovery_started(position_name, mint, tick_drop):
    return (
        f"🛡 <b>SMART RECOVERY ATTIVATO</b> ${_tok(position_name)}\n"
        f"Crollo rilevato: -{100 * tick_drop:.0f}% in un intervallo\n"
        f"Verifica prezzo, liquidità e quotazione Jupiter in corso...\n"
        f"Uscite normali sospese durante la verifica."
    )


def msg_smart_recovery_result(position_name, mint, classification, details=""):
    icons = {
        "GENUINE_CRASH": "✅",
        "NON_LIQUIDATABLE": "❌",
        "DATA_ERROR": "⚠️",
        "UNRESOLVED": "🔄",
    }
    labels = {
        "GENUINE_CRASH": "Crash genuino confermato",
        "NON_LIQUIDATABLE": "Pool drenato / non liquidabile",
        "DATA_ERROR": "Anomalia dati rilevata",
        "UNRESOLVED": "Dati insufficienti per confermare",
    }
    icon = icons.get(classification, "🔍")
    label = labels.get(classification, classification)
    if classification == "GENUINE_CRASH":
        action = "Uscite normali ripristinate."
    elif classification == "NON_LIQUIDATABLE":
        action = "Posizione monitorata, uscita forzata a max-hold."
    elif classification == "UNRESOLVED":
        action = "Verifica in corso, uscite normali consentite."
    else:
        action = "Posizione monitorata, uscita forzata a max-hold."
    return (
        f"{icon} <b>SMART RECOVERY</b> ${_tok(position_name)} — {label}\n"
        f"{details}\n"
        f"{action}"
    )


def msg_non_liquidatable_position(position_name, mint, theoretical_value, executable_value):
    return (
        f"❌ <b>POSIZIONE NON LIQUIDABILE</b> ${_tok(position_name)}\n"
        f"Valore teorico (mark): {money(theoretical_value)}\n"
        f"Valore realizzabile (Jupiter): {money(executable_value)}\n"
        f"La posizione rimane aperta ma esclusa dall'equity.\n"
        f"Uscita forzata allo scadere del max-hold."
    )


def msg_adaptive_poll_status(accelerated_count, max_accelerated, base_interval, accel_interval):
    return (
        f"📡 <b>Adaptive Polling</b>\n"
        f"Base: {base_interval}s | Accelerato: {accel_interval}s\n"
        f"Posizioni accelerate: {accelerated_count}/{max_accelerated}"
    )
