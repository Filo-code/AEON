#!/usr/bin/env python3
"""Offline test of survival mode: 100 EUR start, sizing as % of equity, and death when the money runs out."""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
os.environ.update({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "VYBE_API_KEY": ""})
import yaml, paper_bot  # noqa: E402

cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
d = tempfile.mkdtemp()
b = paper_bot.PaperBot(cfg, d, reset=True)
fx = b.meta["fx_start"]
ok = True
def check(c, label):
    global ok
    ok &= bool(c); print(("OK  " if c else "FAIL") + " " + label)

check(abs(b.cash / fx - 100) < 1e-6, f"start balance 100 EUR (= {b.cash:.2f} USD at EUR/USD {fx:.4f})")
check(abs(b.next_size() / fx - 15) < 0.01, f"trade size 15% of equity = {b.next_size() / fx:.2f} EUR")
b.cash = 10 * fx
check(abs(b.next_size() / fx - 3) < 0.01, f"at 10 EUR the size is raised to the 3 EUR minimum ({b.next_size() / fx:.2f})")
b.check_death()
check(not b.meta["dead"], "10 EUR: still alive")
b.cash = 3.2 * fx
b.check_death()
check(b.meta["dead"], "3.20 EUR < 3 EUR + round-trip fees: DEAD")
snap = {"mint": "X", "pool": "p", "name": "X", "dex": "raydium", "price": 1.0, "liquidity": 50000, "src": "dexscreener"}
b.buy(snap, "volume_spike", {})
check(not b.positions, "dead bot refuses new entries")
b.tg.flush(5); time.sleep(0.5)
out = open(os.path.join(d, "telegram_outbox.log")).read()
check("MORTO" in out, "Telegram 'DEAD MORTO' message produced")
b.save_state("dead")
b2 = paper_bot.PaperBot(cfg, d)
check(b2.meta["dead"] and b2.resumed, "death survives a restart")
print(out[out.index("MORTO") - 30:][:700])
sys.exit(0 if ok else 1)
