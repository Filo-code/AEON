#!/usr/bin/env python3
"""Offline test of the Telegram commands against a LOCAL mock Telegram API (no token, no network, no Vybe)."""
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
os.environ.update({"TELEGRAM_BOT_TOKEN": "TEST", "TELEGRAM_CHAT_ID": "777", "VYBE_API_KEY": "",
                   "DASHBOARD_URL": "https://bot.esempio.it"})
import yaml, paper_bot, telegram_notify, telegram_commands  # noqa: E402

SENT, REG, UPDATES = [], [], []


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        m = self.path.rsplit("/", 1)[-1]
        if m == "sendMessage":
            SENT.append(body["text"]); res = {"ok": True}
        elif m == "setMyCommands":
            REG.append(body); res = {"ok": True, "result": True}
        elif m == "getUpdates":
            ups = [u for u in UPDATES if u["update_id"] >= body.get("offset", 0)]
            if not ups:
                time.sleep(0.5)
            res = {"ok": True, "result": ups}
        else:
            res = {"ok": False}
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(res).encode())

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}/bot{{token}}"
telegram_notify.Telegram.API = base + "/sendMessage"
telegram_commands.Commands.API = base + "/{method}"

cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
d = tempfile.mkdtemp()
bot = paper_bot.PaperBot(cfg, d, reset=True)
bot.ds.tokens = lambda mints: {m: {"price": 0.00012} for m in mints}       # fake price feed, no network
now = time.time()
for name, mint in (("AGI", "AGImint111"), ("PEACE", "PEACEmint22")):
    bot.positions[mint] = {"mint": mint, "pool": "p", "name": name, "dex": "raydium", "entry_type": "volume_spike",
                           "entry_ts": now - 300, "entry_mid": 0.0001, "entry_fill": 0.000103, "qty": 100000, "cost_usd": 10,
                           "liquidity": 20000, "peak": 0.0001, "last": 0.00011, "last_price_ts": now, "info": {},
                           "costs": {"buy_slip_pct": 3, "buy_dex_fee": 0.03, "buy_net_fee": 0.3}, "failed_exits": 0}
    bot.cash -= 10
bot.closed.append({"token": "Su", "pnl_usd": 1.81, "pnl_pct": 15.2, "exit_reason": "take_profit", "exit_ts": now - 60,
                   "dex_fees_usd": 0.1, "network_fees_usd": 0.5, "mint": "Sumint"})

# This script tests Telegram command dispatch, not Jupiter execution.
# Use a deterministic simulated full close so the test remains offline.
def _fake_manual_sell(mint, price, reason, sell_qty=None, **kwargs):
    pos = bot.positions.pop(mint, None)
    if pos is None:
        return
    proceeds = float(pos.get("qty", 0)) * float(price)
    bot.cash = round(bot.cash + proceeds, 4)
    bot.closed.append({
        "mint": mint,
        "token": pos.get("name", mint),
        "pnl_usd": proceeds - float(pos.get("cost_usd", 0)),
        "exit_reason": reason,
        "exit_ts": time.time(),
    })

bot.sell = _fake_manual_sell

cmds = ["/start", "/aiuto", "/stato", "/saldo", "/posizioni", "/trade 3", "/oggi", "/pausa", "/config",
        "/imposta stop_loss 15", "/imposta size 80", "/imposta pippo 3", "/crediti", "/dashboard", "/cervello",
        "/chiudi agi", "/riprendi", "/chiuditutto", "/boh"]
uid = 1
UPDATES.append({"update_id": uid, "message": {"chat": {"id": 999}, "date": int(now), "text": "/chiuditutto"}}); uid += 1
UPDATES.append({"update_id": uid, "message": {"chat": {"id": 777}, "date": int(now) - 3600, "text": "/chiuditutto"}}); uid += 1
for c in cmds:
    UPDATES.append({"update_id": uid, "message": {"chat": {"id": 777}, "date": int(now), "text": c}}); uid += 1

bot.cmds.start()
# main loop stand-in: execute queued actions in order as they arrive
deadline = time.time() + 20
paused_seen = False
while time.time() < deadline and len(SENT) < len(cmds) + 3:
    bot.cmds.run_actions()
    paused_seen |= bool(bot.meta.get("paused"))
    time.sleep(0.2)
bot.cmds.run_actions(); bot.tg.flush(10); time.sleep(1)

ok = True
def check(cond, label):
    global ok
    ok &= bool(cond); print(("OK  " if cond else "FAIL") + " " + label)

check(REG and len(REG[0]["commands"]) == len(telegram_commands.COMMANDS), "setMyCommands registered %d commands" % len(telegram_commands.COMMANDS))
check(not any("999" in s for s in SENT) and len([s for s in SENT if "Chiudo" in s]) == 2, "foreign chat and stale command ignored")
# /pausa now queues an action - after run_actions() the pause is set
check(any("⏸ Pausa:" in msg for msg in SENT), "/pausa executed: new buys paused")
check(bot.c["stop_loss_pct"] == 0.15 and bot.meta["overrides"]["stop_loss_pct"] == 0.15, "/imposta stop_loss 15 -> 0.15 (persisted)")
check(bot.c["position_size_pct"] == cfg["position_size_pct"], "/imposta size 80 rejected (limit 50)")
check(not bot.positions and len(bot.closed) == 3 and all(t["exit_reason"] == "manual_close" for t in bot.closed[1:]), "/chiudi + /chiuditutto paper-closed both positions")
check(bot.meta.get("paused") is False, "/riprendi executed: pause cleared")
print("\n========== RISPOSTE ==========")
for s in SENT:
    print(s.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<code>", "").replace("</code>", "").replace("&amp;", "&"))
    print("-" * 40)
sys.exit(0 if ok else 1)
