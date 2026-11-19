"""Regression test suite for AEON financial accounting precision and ledger reconciliation."""
import json
import math
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from paper_bot import PaperBot
import storage


@pytest.fixture
def base_cfg():
    with open(ROOT / "bot" / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_accounting_repeated_transactions_zero_drift(tmp_path, base_cfg):
    """Prove zero precision drift across 50 repeated buy/sell executions with slippage and fees."""
    bot_dir = str(tmp_path / "bot_repeated")
    bot = PaperBot(base_cfg, bot_dir, reset=True)
    initial_cash = 1000.0
    bot.cash = initial_cash

    quote_price = {"p": None}

    def fake_quote(in_mint, out_mint, amount_raw, deadline=None):
        decimals = bot.decimals_cache[in_mint]
        qty = amount_raw / (10 ** decimals)
        usd_out = qty * quote_price["p"]
        return {
            "out": int(round(usd_out * 10**6)),
            "in": amount_raw,
            "impact_pct": 1.0,
            "route": "Raydium",
            "endpoint": "v1/quote",
            "quote_ts": time.time(),
            "age_s": 0.1,
            "latency_s": 0.05,
            "cached": False,
        }

    bot.jup = SimpleNamespace(
        quote=fake_quote, cache={}, last_reason="",
        stats={}, unavailable={}
    )

    for i in range(50):
        mint = f"MINT_{i:04d}"
        size = 20.0  # $20.00 buy

        # Buy execution
        snap_buy = {
            "mint": mint, "pool": f"POOL_{i}", "name": f"COIN_{i}", "dex": "raydium",
            "price": 0.0010 + (i * 0.00001), "liquidity": 100000.0,
            "created_ts": time.time() - 1000,
        }
        # Simulate clean buy
        with bot.lock:
            bot.cash = round(bot.cash - size, 4)
            qty = size / snap_buy["price"]
            bot.positions[mint] = {
                "mint": mint, "pool": snap_buy["pool"], "name": snap_buy["name"], "dex": snap_buy["dex"],
                "entry_type": "volume_spike", "entry_ts": time.time(), "entry_mid": snap_buy["price"],
                "entry_fill": snap_buy["price"], "qty": qty, "cost_usd": size, "original_cost_usd": size,
                "liquidity": snap_buy["liquidity"], "peak": snap_buy["price"], "last": snap_buy["price"],
                "last_price_ts": time.time(), "info": {},
                "costs": {"buy_slip_pct": 0.1, "buy_dex_fee": 0.05, "buy_net_fee": 0.002, "buy_slip_src": "stimato"},
                "failed_exits": 0, "tp1_hit": False, "breakeven_activated": False,
            }

        # Sell execution with variable price move (+10% on even, -5% on odd)
        exit_multiplier = 1.10 if (i % 2 == 0) else 0.95
        exit_price = snap_buy["price"] * exit_multiplier
        bot.decimals_cache[mint] = 6
        quote_price["p"] = exit_price
        bot.sell(mint, exit_price, "take_profit" if (i % 2 == 0) else "stop_loss")

    assert len(bot.positions) == 0
    assert len(bot.closed) == 50

    # Invariant 1: For EVERY single execution row: proceeds_usd - cost_usd == pnl_usd
    for idx, t in enumerate(bot.closed):
        diff = round(t["proceeds_usd"] - t["cost_usd"] - t["pnl_usd"], 8)
        assert diff == 0.0, f"Trade {idx} ledger mismatch: proceeds {t['proceeds_usd']} - cost {t['cost_usd']} != pnl {t['pnl_usd']}"

    # Invariant 2: Total realized PnL equals sum(proceeds) - sum(costs)
    sum_proceeds = round(sum(t["proceeds_usd"] for t in bot.closed), 4)
    sum_cost = round(sum(t["cost_usd"] for t in bot.closed), 4)
    sum_pnl = round(sum(t["pnl_usd"] for t in bot.closed), 4)
    assert round(sum_proceeds - sum_cost, 4) == sum_pnl

    # Invariant 3: Portfolio cash matches Initial Cash + sum(pnl) to 0.000000 delta
    expected_cash = round(initial_cash + sum_pnl, 4)
    cash_diff = round(bot.cash - expected_cash, 8)
    assert cash_diff == 0.0, f"Cash drift detected: bot.cash={bot.cash}, expected={expected_cash}"


def test_accounting_multiple_partial_exits_reconciliation(tmp_path, base_cfg):
    """Prove cost basis conservation and cash reconciliation across 4 partial exits."""
    bot_dir = str(tmp_path / "bot_multi_partial")
    bot = PaperBot(base_cfg, bot_dir, reset=True)
    initial_cash = 500.0
    bot.cash = initial_cash

    entry_price = 1.0
    initial_qty = 100.0
    initial_cost = 100.0
    mint = "MINT_PARTIAL_4STEP"

    # Buy position
    with bot.lock:
        bot.cash = round(bot.cash - initial_cost, 4)
        bot.positions[mint] = {
            "mint": mint, "pool": "POOL_1", "name": "4STEP_COIN", "dex": "raydium",
            "entry_type": "volume_spike", "entry_ts": time.time() - 300,
            "entry_mid": entry_price, "entry_fill": entry_price,
            "qty": initial_qty, "cost_usd": initial_cost, "original_cost_usd": initial_cost,
            "liquidity": 100000.0, "peak": entry_price, "last": entry_price,
            "last_price_ts": time.time(), "info": {},
            "costs": {"buy_slip_pct": 0.0, "buy_dex_fee": 0.10, "buy_net_fee": 0.004, "buy_slip_src": "stimato"},
            "failed_exits": 0, "tp1_hit": False, "breakeven_activated": False,
        }

    # Disable fees for pure price progression verification
    bot._net_fee_usd = lambda: 0.0
    bot._slip = lambda gross, liq: 0.0
    bot._dex_fee = lambda dex: 0.0
    bot.decimals_cache[mint] = 6

    quote_price = {"p": None}

    def fake_quote(in_mint, out_mint, amount_raw, deadline=None):
        qty = amount_raw / 10**6
        usd_out = qty * quote_price["p"]
        return {
            "out": int(round(usd_out * 10**6)),
            "in": amount_raw,
            "impact_pct": 1.0,
            "route": "Raydium",
            "endpoint": "v1/quote",
            "quote_ts": time.time(),
            "age_s": 0.1,
            "latency_s": 0.05,
            "cached": False,
        }

    bot.jup = SimpleNamespace(
        quote=fake_quote, cache={}, last_reason="",
        stats={}, unavailable={}
    )

    # Step 1: Sell 25 units at $1.20 (+20%)
    quote_price["p"] = 1.20
    bot.sell(mint, 1.20, "partial_tp1", sell_qty=25.0)
    assert bot.closed[0]["partial"] is True
    assert bot.closed[0]["proceeds_usd"] == 30.0
    assert bot.closed[0]["cost_usd"] == 25.0
    assert bot.closed[0]["pnl_usd"] == 5.0

    # Step 2: Sell 25 units at $1.40 (+40%)
    quote_price["p"] = 1.40
    bot.sell(mint, 1.40, "partial_tp2", sell_qty=25.0)
    assert bot.closed[1]["proceeds_usd"] == 35.0
    assert bot.closed[1]["cost_usd"] == 25.0
    assert bot.closed[1]["pnl_usd"] == 10.0

    # Step 3: Sell 25 units at $1.60 (+60%)
    quote_price["p"] = 1.60
    bot.sell(mint, 1.60, "partial_tp3", sell_qty=25.0)
    assert bot.closed[2]["proceeds_usd"] == 40.0
    assert bot.closed[2]["cost_usd"] == 25.0
    assert bot.closed[2]["pnl_usd"] == 15.0

    # Step 4: Close remaining 25 units at $1.80 (+80%)
    quote_price["p"] = 1.80
    bot.sell(mint, 1.80, "final_close")
    assert mint not in bot.positions
    assert len(bot.closed) == 4
    assert bot.closed[3]["counts_as_trade"] is True
    assert bot.closed[3]["proceeds_usd"] == 45.0
    assert bot.closed[3]["cost_usd"] == 25.0
    assert bot.closed[3]["pnl_usd"] == 20.0

    # Cumulative metrics
    final_trade = bot.closed[3]
    assert final_trade["cumulative_pnl_usd"] == 50.0  # 5 + 10 + 15 + 20 = 50
    assert final_trade["cumulative_pnl_pct"] == 50.0  # $50 gain on $100 original cost = +50%

    # Conservation of cost basis
    total_cost_allocated = sum(t["cost_usd"] for t in bot.closed)
    assert math.isclose(total_cost_allocated, initial_cost, rel_tol=1e-12)

    # Cash reconciliation: Initial $500 - $100 buy + ($30 + $35 + $40 + $45) = $550.0
    assert math.isclose(bot.cash, 550.0, rel_tol=1e-12)
    assert math.isclose(bot.cash - initial_cash, final_trade["cumulative_pnl_usd"], rel_tol=1e-12)


def test_accounting_dust_and_fractional_precision(tmp_path, base_cfg):
    """Prove high-precision token quantities (e.g. 9 decimal places) coexist with 4-decimal cash."""
    bot_dir = str(tmp_path / "bot_dust")
    bot = PaperBot(base_cfg, bot_dir, reset=True)
    bot.cash = 100.0

    mint = "MINT_SPL_TOKEN"
    token_decimals = 9
    bot.decimals_cache[mint] = token_decimals

    # Small fractional quantity with sub-penny mid price
    qty = 1234567.891234567
    price = 0.0000123456
    cost = round(qty * price, 4)

    bot.positions[mint] = {
        "mint": mint, "pool": "POOL_D", "name": "SPL_COIN", "dex": "raydium",
        "entry_type": "volume_spike", "entry_ts": time.time(), "entry_mid": price,
        "entry_fill": price, "qty": qty, "cost_usd": cost, "original_cost_usd": cost,
        "liquidity": 100000.0, "peak": price, "last": price, "last_price_ts": time.time(),
        "info": {}, "costs": {"buy_slip_pct": 0.0, "buy_dex_fee": 0.0, "buy_net_fee": 0.0, "buy_slip_src": "stimato"},
        "failed_exits": 0,
    }

    bot._net_fee_usd = lambda: 0.0
    bot._slip = lambda gross, liq: 0.0
    bot._dex_fee = lambda dex: 0.0

    # Sell entire position
    exit_price = 0.0000150000

    quote_price = {"p": exit_price}

    def fake_quote(in_mint, out_mint, amount_raw, deadline=None):
        qty = amount_raw / 10**9
        usd_out = qty * quote_price["p"]
        return {
            "out": int(round(usd_out * 10**6)),
            "in": amount_raw,
            "impact_pct": 1.0,
            "route": "Raydium",
            "endpoint": "v1/quote",
            "quote_ts": time.time(),
            "age_s": 0.1,
            "latency_s": 0.05,
            "cached": False,
        }

    bot.jup = SimpleNamespace(
        quote=fake_quote, cache={}, last_reason="",
        stats={}, unavailable={}
    )

    bot.sell(mint, exit_price, "take_profit")

    assert mint not in bot.positions
    trade = bot.closed[0]
    assert isinstance(trade["proceeds_usd"], float)
    # Check that proceeds_usd and cost_usd have at most 4 decimal places
    assert trade["proceeds_usd"] == round(trade["proceeds_usd"], 4)
    assert trade["cost_usd"] == round(trade["cost_usd"], 4)
    assert trade["pnl_usd"] == round(trade["pnl_usd"], 4)
    assert round(trade["proceeds_usd"] - trade["cost_usd"], 4) == trade["pnl_usd"]


def test_accounting_state_json_roundtrip_precision(tmp_path, base_cfg):
    """Prove state.json save and safe restore preserves exact 4-decimal float cash."""
    bot_dir = str(tmp_path / "bot_persistence")
    bot = PaperBot(base_cfg, bot_dir, reset=True)
    bot.cash = 142.3750

    bot.save_state()
    state_file = os.path.join(bot_dir, "state.json")
    assert os.path.exists(state_file)

    loaded, src = storage.load_json_safe(state_file)
    assert loaded["cash"] == 142.3750

    bot2 = PaperBot(base_cfg, bot_dir, reset=False)
    assert bot2.cash == 142.3750
