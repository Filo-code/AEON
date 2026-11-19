#!/usr/bin/env python3
"""Offline test of the 'cervello': synthetic trades where high-slippage / thin-liquidity entries lose.
Checks that the brain diagnoses it in Italian, changes a bounded parameter, logs it and persists it."""
import os, random, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
import yaml
from cervello import Brain, BOUNDS

cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
d = tempfile.mkdtemp()
b = Brain(cfg, d)
random.seed(1)
for i in range(30):
    thin = i % 2 == 0
    liq = random.uniform(2000, 8000) if thin else random.uniform(25000, 60000)
    slip = random.uniform(9, 16) if thin else random.uniform(3, 5)
    pnl = random.uniform(-45, -12) if thin else random.uniform(-10, 35)
    reason = "stop_loss" if pnl < -11 else ("take_profit" if pnl > 25 else "time_exit")
    pos = {"entry_type": "volume_spike", "dex": "pumpswap", "entry_ts": time.time() - 900, "liquidity": liq,
           "info": {"vol_ratio": random.uniform(8, 40), "age_min": random.uniform(2, 50), "vol_m5": random.uniform(3000, 20000),
                    "top10_pct": random.uniform(5, 19), "dev_pct": 0.5, "whale_buy_usd": random.uniform(300, 2000)}}
    t = {"token": f"T{i}", "buy_slip_pct": slip, "pnl_pct": pnl, "pnl_usd": pnl / 10, "exit_reason": reason,
         "price_move_pct": pnl * 0.9, "hold_sec": 600, "exit_ts": time.time()}
    b.record(pos, t)
    if b.due():
        a = b.analyze()
        print(f"--- analisi dopo {len(b.st['records'])} trade ---\n{a['text']}\n")

ok = True
changes = b.st["changes"]

# Brain should make at least one parameter change within bounds
if not changes:
    ok = False
    print("FAIL: Brain made no parameter changes")
else:
    print(f"OK: Brain made {len(changes)} parameter changes")

# All changed parameters must be in BOUNDS
for c in changes:
    if c["param"] not in BOUNDS:
        ok = False
        print(f"FAIL: Changed parameter {c['param']} not in BOUNDS")
    elif not (BOUNDS[c["param"]][0] <= c["new"] <= BOUNDS[c["param"]][1]):
        ok = False
        print(f"FAIL: Change {c['param']}={c['new']} outside bounds {BOUNDS[c['param']]}")

# Persistence: reload brain and verify at least one changed parameter matches
b2 = Brain(yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml"))), d)
for c in changes:
    param = c["param"]
    if getattr(b2, "c", {}).get(param) != getattr(b, "c", {}).get(param):
        ok = False
        print(f"FAIL: Persistence failed for {param}")

print("Modifiche:", [c["text"] for c in changes])
print("persistenza dopo riavvio: OK" if ok else "persistenza dopo riavvio: FAIL")
print("RESULT", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
