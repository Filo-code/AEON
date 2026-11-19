#!/usr/bin/env python3
"""Offline tests for the v3 upgrades (no network for trading calls; everything mocked):
  - Jupiter quote fills (buy USDC->token, sell token->USDC) and fallback to the liquidity model
  - copy_top_traders exits: follow wallets, exit when the majority sold; wider stop / longer hold
  - re-entry cooldown ONLY for volume_spike (copy entries are never blocked)
  - sell() is idempotent (no duplicate SELL)
  - Italian Telegram texts ('i top trader hanno venduto', 'slippage reale Jupiter')
Option --live: one real Jupiter quote (USDC->SOL) to check the public endpoint answers."""
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
os.environ.update({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "VYBE_API_KEY": "", "LOG_STDOUT": "0"})
import yaml, paper_bot, telegram_notify as tg  # noqa: E402
from paper_bot import USDC  # noqa: E402

ok = True
def check(c, label):
    global ok
    ok &= bool(c); print(("OK  " if c else "FAIL") + " " + label)

cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
cfg["costs"]["simulate_failed_exits"] = False
# Disable Brain's entry slippage limit adaptation for this test
cfg["brain"] = cfg.get("brain") or {}
cfg["brain"]["entry_slippage_limit"] = False
# Disable trade guard that requires Jupiter route (test checks fallback to liquidity model)
cfg["trade_guards"] = cfg.get("trade_guards") or {}
cfg["trade_guards"]["require_jupiter_route"] = False
d = tempfile.mkdtemp()
b = paper_bot.PaperBot(cfg, d, reset=True)
b.brain.cfg_enabled = False
MINT, DEC = "TokenMint1111111111111111111111111111111111", 6
PRICE = {"v": 0.001}            # DexScreener mid (USD)
QUOTE = {"mode": "ok"}          # ok | fail
CALLS = []

class FakeJup:
    stats = {"ok": 0, "fail": 0, "no_route": 0}
    def quote(self, i, o, amt, deadline=None):
        CALLS.append((i, o, amt))
        if QUOTE["mode"] != "ok":
            self.stats["fail"] += 1; return None
        self.stats["ok"] += 1
        if i == USDC:      # buy: 4% worse than mid
            return {"out": int(amt / 1e6 / (PRICE["v"] * 1.04) * 10 ** DEC), "route": "Pump.fun Amm", "impact_pct": 1}
        return {"out": int(amt / 10 ** DEC * PRICE["v"] * 0.95 * 1e6), "route": "Pump.fun Amm", "impact_pct": 1}

BAL = {}
class FakeRPC:
    stats = {"ok": 0, "fail": 0}
    def balance(self, w, m): return BAL.get(w)
    def decimals(self, m): return DEC
    def balances_batch(self, wallets, mint):
        return {w: BAL.get(w) for w in wallets if w in BAL}

b.jup, b.rpc = FakeJup(), FakeRPC()
b.ds.tokens = lambda mints: {m: {"price": PRICE["v"], "liquidity": 20000} for m in mints}
snap = {"mint": MINT, "pool": "pool1", "name": "TST", "dex": "pumpswap", "price": PRICE["v"],
        "liquidity": 20000, "src": "dexscreener"}

# ---- 1. Jupiter buy / sell
b.buy(snap, "volume_spike", {"vol_ratio": 10})
p = b.positions[MINT]
check(p["costs"]["buy_slip_src"] == "jupiter" and abs(p["costs"]["buy_slip_pct"] - 4.0) < 0.05,
      f"buy filled from Jupiter quote: slippage {p['costs']['buy_slip_pct']}% (expected 4.0) via {p['costs']['buy_route']}")
check(p["costs"]["buy_dex_fee"] == 0, "DEX fee not double-counted (inside the Jupiter quote)")
b.sell(MINT, PRICE["v"], "take_profit")
t = b.closed[-1]
check(t["sell_slip_src"] == "jupiter" and abs(t["sell_slip_pct"] - 5.0) < 0.05,
      f"sell filled from Jupiter quote: slippage {t['sell_slip_pct']}% (expected 5.0)")
# ---- 2. sell idempotent
n = len(b.closed); b.sell(MINT, PRICE["v"], "take_profit")
check(len(b.closed) == n, "second sell() on the same token ignored (no duplicate SELL)")
# ---- 3. re-entry cooldown only for volume_spike
b.buy(snap, "volume_spike", {"vol_ratio": 10})
check(MINT not in b.positions, "volume_spike re-entry on a just-sold token blocked (cooldown 30 min)")
QUOTE["mode"] = "fail"
BAL.update({"W1": 100.0, "W2": 50.0, "W3": 10.0})
b.buy(snap, "copy_top_traders", {"top_traders_in": 3}, track=["W1", "W2", "W3", "W4"], hinfo={})
p = b.positions.get(MINT)
check(p is not None, "copy_top_traders entry NOT blocked by the cooldown (mirrors the top traders)")
check(p["costs"]["buy_slip_src"] == "stimato", "Jupiter quote failed -> fallback to the liquidity model ('stimato')")
check(p["track"]["n"] == 3, f"follows 3 wallets with a balance (W4 has none): n={p['track']['n']}")
# ---- 4. copy exits: wider stop, longer hold, majority sold
PRICE["v"] = 0.001 * 0.85          # -15%: would stop a volume trade (-12%), not a copy trade (-25%)
p["track"]["last_check"] = 0
BAL["W1"] = 20.0                   # W1 reduced 80% -> sold (1/3)
b.manage()
check(MINT in b.positions, "copy trade at -15% kept (copy stop loss -25%); 1/3 wallets sold -> no exit yet")
p["entry_ts"] -= 20 * 60
p["track"]["last_check"] = 0; b.manage()
check(MINT in b.positions, "copy trade still open after 20 min (copy max hold 180 min, volume max hold 15)")
BAL["W2"] = 0.0                    # 2/3 sold -> majority
QUOTE["mode"] = "ok"               # Jupiter disponibile per eseguire l'uscita
p["track"]["last_check"] = 0
b.manage()
t = b.closed[-1]
check(MINT not in b.positions and t["exit_reason"] == "top_traders_sold" and t["wallets_sold"] == 2,
      f"exit when the majority sold: reason={t['exit_reason']} ({t['wallets_sold']}/{t['wallets_tracked']})")
check(t["sell_slip_src"] == "jupiter", "copy exit executed with a valid Jupiter quote")
# copy re-entry right after the sell is allowed
PRICE["v"] = 0.001; QUOTE["mode"] = "ok"
b.buy({**snap, "price": PRICE["v"]}, "copy_top_traders", {"top_traders_in": 5}, track=["W3"], hinfo={})
check(MINT in b.positions, "copy re-entry allowed immediately after a sell")
check(b.positions[MINT]["track"]["n"] == 1, "1 followed wallet < min_tracked_wallets(2): only price/time exits")
# ---- 5. Telegram texts
b.tg.flush(5); time.sleep(0.5)
out = open(os.path.join(d, "telegram_outbox.log"), encoding="utf-8").read()
check("i top trader hanno venduto" in out, "Telegram exit reason 'i top trader hanno venduto'")
check("quote Jupiter" in out and "reale Jupiter" not in out,
      "Telegram identifies Jupiter slippage as a quote estimate, not a real fill")
check("slippage stimato" in out, "Telegram shows 'slippage stimato' on fallback")
check("Seguo 3 wallet" in out, "Telegram buy message says how many wallets are followed")
check("PAPER SELL" not in out, "no stray log lines in Telegram outbox")
b.save_state()
b2 = paper_bot.PaperBot(cfg, d)
check(MINT in b2.meta["last_exit"] and MINT in b2.positions and b2.positions[MINT].get("track"),
      "cooldown memory and wallet tracking survive a restart")
i = out.index("i top trader hanno venduto")
sample = out[out.rindex("VENDITA", 0, i) - 6:][:650]
print("\n--- sample sell message ---")
print(sample.encode("ascii", "replace").decode("ascii"))

if "--live" in sys.argv:
    h = paper_bot.Http()
    j = paper_bot.Jupiter(h, cfg["jupiter"])
    q = j.quote(USDC, paper_bot.SOL, 10_000_000)
    check(bool(q) and q["out"] > 0, f"LIVE Jupiter quote 10 USDC -> {q and q['out'] / 1e9:.4f} SOL via {q and q['route']} ({q and q['endpoint']})")
    r = paper_bot.SolanaRPC(h, cfg["solana_rpc_url"])
    check(r.decimals(USDC) == 6, "LIVE Solana RPC getTokenSupply(USDC).decimals == 6")
sys.exit(0 if ok else 1)
