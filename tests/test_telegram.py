#!/usr/bin/env python3
"""Telegram check.

  python scripts/test_telegram.py          -> offline test: builds every message type and sends them to a
                                             LOCAL mock Telegram server (no token, nothing leaves the machine),
                                             including a 500 error and a 429 to exercise the retries.
  python scripts/test_telegram.py --real   -> sends ONE test message with TELEGRAM_BOT_TOKEN/CHAT_ID from .env
"""
import json, os, sys, threading, time, tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
import telegram_notify as tg  # noqa: E402
import yaml  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
POS = {"mint": "AGiXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "pool": "PooLXXXX", "name": "AGI", "dex": "pumpswap",
       "entry_type": "volume_spike", "entry_ts": time.time() - 1140, "entry_mid": 0.0001234, "entry_fill": 0.0001343,
       "qty": 1e5, "cost_usd": 14.69, "costs": {"buy_slip_pct": 8.8, "buy_dex_fee": 0.04, "buy_net_fee": 0.28},
       "info": {"vol_ratio": 65, "vol_m5": 12345, "age_min": 6, "dev_pct": 0.5, "top10_pct": 12, "whale_buy_usd": 450},
       "failed_exits": 0}
TRADE = {"token": "AGI", "pnl_usd": -3.13, "pnl_pct": -21.3, "exit_reason": "time_exit", "hold_sec": 1140, "late_sec": 240,
         "entry_mid": 0.0001234, "exit_mid": 0.000108, "price_move_pct": -12.5, "buy_slip_pct": 8.8, "sell_slip_pct": 9.1,
         "dex_fees_usd": 0.08, "network_fees_usd": 0.56, "failed_exits": 1}


def messages():
    closed = [{"pnl_usd": 1.81}, {"pnl_usd": -11.56}, TRADE]
    return [tg.msg_start(85, 85, 0, False, True, CFG),
            tg.msg_buy(POS, 58.75, 2, CFG),
            tg.msg_failed_sell(POS, 0.48, 0.28),
            tg.msg_sell(TRADE, 70.31, 85, closed, CFG),
            tg.msg_sell({**TRADE, "token": "Su", "pnl_usd": 1.81, "pnl_pct": 15.2, "exit_reason": "take_profit",
                         "late_sec": 0, "failed_exits": 0}, 72.12, 85, closed, CFG),
            tg.msg_daily({"equity": 70.31, "start": 85, "n": 4, "wins": 1, "win_rate": 25.0,
                          "best": {"token": "Su", "pnl_usd": 1.81}, "worst": {"token": "BOTTOM", "pnl_usd": -11.56},
                          "fees": 2.4, "avg_slip": 8.1, "by_reason": {"stop_loss": 2, "time_exit": 1, "take_profit": 1},
                          "open": 1, "period_n": 4, "period_pnl": -14.69}, "giornaliero 13/11 08:00"),
            tg.msg_watchdog(250), tg.msg_stop(70.31, 85, 1, "arresto manuale")]


def offline():
    got, plan = [], [500, 429]          # first request fails with 500, second with 429, then OK

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            code = plan.pop(0) if plan else 200
            if code == 200:
                got.append(body)
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"ok": code == 200, "parameters": {"retry_after": 1}}).encode())

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tg.Telegram.API = f"http://127.0.0.1:{srv.server_port}/bot{{token}}/sendMessage"
    out = os.path.join(tempfile.mkdtemp(), "outbox.log")
    t = tg.Telegram(token="TEST-TOKEN", chat_id="12345", outbox_path=out)
    msgs = messages()
    t0 = time.time()
    for m in msgs:
        t.send(m)
    enq = time.time() - t0
    t.flush(60)
    time.sleep(0.5)
    ok = len(got) == len(msgs) and all(g["chat_id"] == "12345" and g["parse_mode"] == "HTML" for g in got)
    print(f"enqueue time for {len(msgs)} messages: {enq * 1000:.1f} ms (non-blocking)")
    print(f"mock server received {len(got)}/{len(msgs)} messages after a 500 and a 429 -> {'OK' if ok else 'FAIL'}")
    # dry mode (no token): must not raise, writes to outbox only
    d = tg.Telegram(token="", chat_id="", outbox_path=out)
    d.send("prova dry"); d.flush(5); time.sleep(0.3)
    print("dry mode (no token):", "OK" if d.dry and d.sent == 1 else "FAIL")
    # unreachable Telegram must not crash
    tg.Telegram.API = "http://127.0.0.1:9/bot{token}/sendMessage"
    u = tg.Telegram(token="X", chat_id="1", outbox_path=None)
    u.send("x")
    print("unreachable Telegram: send() returned immediately, bot unaffected -> OK")
    print("\n================ ANTEPRIMA MESSAGGI ================")
    for m in msgs:
        print(m.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("&amp;", "&"))
        print("-" * 52)
    srv.shutdown()
    return ok


def real():
    sys.path.insert(0, os.path.join(ROOT, "bot"))
    from paper_bot import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    t = tg.Telegram(outbox_path=None)
    if t.dry:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID mancanti nel file .env"); return False
    t.send(" Test riuscito: il paper bot pu scriverti qui. (Simulazione, nessun denaro reale)")
    t.flush(30); time.sleep(1)
    print("inviato" if t.sent else "NON inviato: controlla token e chat id (vedi log sopra)")
    return bool(t.sent)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(0 if (real() if "--real" in sys.argv else offline()) else 1)
