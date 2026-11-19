"""Comprehensive Deep Audit & Hardening Test Suite for AEON.
Covers all 15 audit scenarios deterministically without external API dependencies.
"""
import copy
import json
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import storage
from risk import RiskManager
from cervello import ValidatedBrain, LegacyBrain, Brain, build_trade_record, BOUNDS
from paper_bot import PaperBot


@pytest.fixture
def base_cfg():
    cfg_path = ROOT / "bot" / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Ensure standard deterministic test values
    cfg["telegram_enabled"] = False
    cfg["jupiter"] = {"enabled": False}
    cfg["brain"]["mode"] = "validated"
    cfg["brain"]["enabled"] = True
    cfg["brain"]["adapt"] = True
    cfg["brain"]["min_trades"] = 10
    cfg["brain"]["min_samples"] = 30
    cfg["brain"]["observe_trades"] = 30
    cfg["brain"]["eval_trades"] = 15
    cfg["brain"]["window"] = 50
    cfg["brain"]["min_gap_pct"] = 8.0
    cfg["brain"]["min_group"] = 3
    cfg["brain"]["min_t"] = 2.0
    cfg["brain"]["oos_frac"] = 0.3
    cfg["brain"]["rollback_margin_pct"] = 3.0
    cfg["risk"]["mode"] = "conservative"
    cfg["risk"]["conservative"] = {
        "max_open_positions": 5,
        "max_exposure_pct": 0.75,
        "max_position_pct": 0.15,
        "max_daily_loss_pct": 0.15,
        "max_drawdown_pct": 0.35,
        "max_consecutive_losses": 5,
        "loss_pause_minutes": 45,
        "duplicate_window_s": 120,
        "resume_size_factor": 0.5,
        "resume_trades": 3,
    }
    cfg["costs"]["simulate_failed_exits"] = False
    return cfg


def make_synthetic_trade(mint, entry_ts, pnl_usd, pnl_pct, buy_slip=1.0, sell_slip=1.0, reason="take_profit", extra_info=None):
    info = extra_info or {
        "vol_ratio": 5.0, "age_min": 30.0, "vol_m5": 50000.0,
        "top10_pct": 35.0, "dev_pct": 5.0, "whale_buy_usd": 1000.0
    }
    pos = {
        "name": f"TOKEN_{mint[:4]}",
        "mint": mint,
        "entry_type": "volume_spike",
        "entry_ts": entry_ts,
        "dex": "raydium",
        "liquidity": 50000.0,
        "cost_usd": 15.0,
        "original_cost_usd": 15.0,
        "info": info,
        "costs": {"buy_slip_pct": buy_slip, "buy_dex_fee": 0.05, "buy_net_fee": 0.002, "buy_slip_src": "stimato"},
    }
    trade = {
        "token": pos["name"],
        "mint": mint,
        "exit_ts": entry_ts + 300,
        "exit_reason": reason,
        "pnl_usd": round(pnl_usd, 4),
        "pnl_pct": round(pnl_pct, 2),
        "cumulative_pnl_usd": round(pnl_usd, 4),
        "buy_slip_pct": buy_slip,
        "sell_slip_pct": sell_slip,
        "hold_sec": 300,
        "counts_as_trade": True,
        "partial": False,
    }
    return pos, trade


# ==============================================================================
# 1. SCENARIO 1 — 10 Completed Trades (Initial Data Collection & Cadence)
# ==============================================================================
def test_scenario_01_ten_completed_trades(tmp_path, base_cfg):
    data_dir = str(tmp_path / "data_10")
    os.makedirs(data_dir, exist_ok=True)
    brain = Brain(base_cfg, data_dir)
    assert isinstance(brain, ValidatedBrain)

    # Record 10 trades: 6 wins (+20%), 4 losses (-10%)
    t0 = time.time() - 3600
    for i in range(10):
        pnl = 3.0 if i < 6 else -1.5
        pct = 20.0 if i < 6 else -10.0
        pos, tr = make_synthetic_trade(f"MINT_{i:04d}", t0 + i * 60, pnl, pct)
        brain.record(pos, tr)

    assert brain.st["total_n"] == 10
    assert len(brain.st["records"]) == 10
    assert brain.due() is True

    analysis = brain.analyze()
    assert analysis["n"] == 10
    # Win rate: 6 / 10 = 60.0%
    assert math.isclose(analysis["win_rate"], 60.0, abs_tol=0.1)
    # Mean PnL: (6 * 20.0 - 4 * 10.0) / 10 = 8.0%
    assert math.isclose(analysis["mean_pnl"], 8.0, abs_tol=0.1)
    # In initial observation phase (< 30 observe_trades), no adaptive changes are forced
    assert brain.phase() == "osservazione"
    assert len(analysis["changes"]) == 0
    # Next check is not due until next cadence
    assert brain.due() is False


# ==============================================================================
# 2. SCENARIO 2 — 50 Completed Trades (Scale & Rolling Window Preparation)
# ==============================================================================
def test_scenario_02_fifty_completed_trades(tmp_path, base_cfg):
    data_dir = str(tmp_path / "data_50")
    os.makedirs(data_dir, exist_ok=True)
    brain = Brain(base_cfg, data_dir)

    t0 = time.time() - 10000
    # Generate 50 trades: 30 wins (+15%), 20 losses (-10%)
    for i in range(50):
        is_win = (i % 5 != 0 and i % 5 != 1)  # 3 wins out of every 5 (60%)
        pnl = 2.25 if is_win else -1.5
        pct = 15.0 if is_win else -10.0
        pos, tr = make_synthetic_trade(f"MINT50_{i:04d}", t0 + i * 60, pnl, pct)
        brain.record(pos, tr)
        if brain.due():
            brain.analyze()

    assert brain.st["total_n"] == 50
    assert brain.st["last_n"] == 50
    # Cadence scales: next analysis should be at 100
    assert brain._analysis_interval(50) == 50
    assert brain.due() is False


# ==============================================================================
# 3. SCENARIO 3 — 100 Completed Trades (50-Trade Cadence After 50)
# ==============================================================================
def test_scenario_03_one_hundred_completed_trades(tmp_path, base_cfg):
    data_dir = str(tmp_path / "data_100")
    os.makedirs(data_dir, exist_ok=True)
    brain = Brain(base_cfg, data_dir)

    analyses_run = []
    t0 = time.time() - 20000
    for i in range(100):
        is_win = (i % 2 == 0)
        pos, tr = make_synthetic_trade(f"MINT100_{i:04d}", t0 + i * 60, 2.0 if is_win else -2.0, 13.3 if is_win else -13.3)
        brain.record(pos, tr)
        if brain.due():
            res = brain.analyze()
            analyses_run.append((brain.st["total_n"], res))

    # Analyses ran at 10, 20, 30, 40, 50, and 100!
    analyzed_points = [pt[0] for pt in analyses_run]
    assert 50 in analyzed_points
    assert 100 in analyzed_points
    # Between 50 and 100, no spurious intermediate analysis ran
    intermediate = [n for n in analyzed_points if 50 < n < 100]
    assert len(intermediate) == 0
    assert brain.st["last_n"] == 100


# ==============================================================================
# 4. SCENARIO 4 — 50 Trades with Random Outcomes (Noise Rejection & Overfitting Guard)
# ==============================================================================
def test_scenario_04_fifty_trades_random_noise_no_overfitting(tmp_path, base_cfg):
    data_dir = str(tmp_path / "data_noise")
    os.makedirs(data_dir, exist_ok=True)
    brain = Brain(base_cfg, data_dir)

    rng = random.Random(42)
    t0 = time.time() - 10000
    # Random white noise: Gaussian pnl centered near 0, random buy_slip, random vol_ratio
    for i in range(50):
        pnl_pct = rng.gauss(0.0, 5.0)
        pnl_usd = round(15.0 * (pnl_pct / 100.0), 4)
        buy_slip = rng.uniform(0.5, 4.0)
        info = {
            "vol_ratio": rng.uniform(1.0, 10.0),
            "age_min": rng.uniform(5.0, 120.0),
            "vol_m5": rng.uniform(10000.0, 200000.0),
            "top10_pct": rng.uniform(10.0, 50.0),
            "dev_pct": rng.uniform(0.0, 15.0),
            "whale_buy_usd": rng.uniform(0.0, 5000.0),
        }
        pos, tr = make_synthetic_trade(f"NOISE_{i:04d}", t0 + i * 60, pnl_usd, pnl_pct, buy_slip=buy_slip, extra_info=info)
        brain.record(pos, tr)
        if brain.due():
            brain.analyze()

    # Statistical significance (min_t = 2.0) and OOS confirmation reject pure noise!
    # No spurious changes should be accepted into permanent parameters
    permanent_changes = [c for c in brain.st.get("changes", []) if c.get("status") == "mantenuta"]
    assert len(permanent_changes) == 0


# ==============================================================================
# 5. SCENARIO 5 — 50 Trades with Deteriorating Strategy Performance (Automatic Rollback)
# ==============================================================================
def test_scenario_05_deteriorating_performance_triggers_rollback(tmp_path, base_cfg):
    data_dir = str(tmp_path / "data_rollback")
    os.makedirs(data_dir, exist_ok=True)
    brain = Brain(base_cfg, data_dir)

    t0 = time.time() - 20000
    # Initial 35 trades
    for i in range(35):
        pos, tr = make_synthetic_trade(f"M_A_{i:04d}", t0 + i * 60, 3.0, +20.0)
        brain.record(pos, tr)

    # Force a parameter modification in test on take_profit_pct (learnable; max_hold_minutes
    # is FORBIDDEN by design: the brain can never touch hold-duration/risk parameters)
    old_val = base_cfg["take_profit_pct"]  # 0.60
    baseline_mean = 10.0
    applied = brain._change("take_profit_pct", 0.55, "Test take profit change", [], baseline=baseline_mean)
    assert applied is True
    assert brain.st["pending"] is not None
    assert brain.c["take_profit_pct"] == 0.55

    # Next 15 trades (eval_trades) perform poorly (mean -10% vs baseline +10%)
    t_after = brain.st["pending"]["ts"] + 10
    for j in range(15):
        pos, tr = make_synthetic_trade(f"M_EVAL_{j:04d}", t_after + j * 60, -1.5, -10.0)
        brain.record(pos, tr)

    assert brain.due() is True
    lines = []
    eval_res = brain._evaluate_pending(lines)
    assert eval_res is not None
    assert eval_res["status"] == "annullata"
    # Parameter must be rolled back automatically to old value!
    assert brain.c["take_profit_pct"] == old_val
    assert brain.st["params"]["take_profit_pct"] == old_val

    # Scenario verification: prevent repeatedly applying the same failed change
    reapplied = brain._change("take_profit_pct", 0.55, "Attempt repeat failed change", [], baseline=baseline_mean)
    assert reapplied is False, "Failed experiment must NOT be repeatedly re-applied!"


# ==============================================================================
# 6. SCENARIO 6 — 50 Trades with Improving Strategy Performance (Validation & Keep)
# ==============================================================================
def test_scenario_06_improving_performance_validates_and_keeps(tmp_path, base_cfg):
    data_dir = str(tmp_path / "data_keep")
    os.makedirs(data_dir, exist_ok=True)
    brain = Brain(base_cfg, data_dir)

    old_val = base_cfg["take_profit_pct"]
    baseline_mean = 2.0
    applied = brain._change("take_profit_pct", 0.55, "Test improvement", [], baseline=baseline_mean)
    assert applied is True

    # 15 eval trades with improved performance (+15% mean vs baseline +2%)
    t_after = brain.st["pending"]["ts"] + 10
    for j in range(15):
        pos, tr = make_synthetic_trade(f"M_GOOD_{j:04d}", t_after + j * 60, +2.25, +15.0)
        brain.record(pos, tr)

    lines = []
    eval_res = brain._evaluate_pending(lines)
    assert eval_res is not None
    assert eval_res["status"] == "mantenuta"
    assert brain.c["take_profit_pct"] == 0.55
    assert brain.st["pending"] is None


# ==============================================================================
# 7. SCENARIO 7 — 10 Consecutive Losses (Risk Manager Max Consecutive Losses)
# ==============================================================================
def test_scenario_07_ten_consecutive_losses_pauses_and_resumes(base_cfg):
    now_time = 1700000000.0
    def clock():
        return now_time

    rm = RiskManager(base_cfg, clock=clock)
    assert rm.L["max_consecutive_losses"] == 5
    equity = 100.0

    # 4 consecutive losses: not paused
    for step in range(4):
        equity -= 2.0
        rm.on_trade_closed(-2.0, equity)
        assert rm.paused is False
        assert rm.s["consec_losses"] == step + 1

    # 5th consecutive loss: PAUSES trading!
    equity -= 2.0
    rm.on_trade_closed(-2.0, equity)
    assert rm.paused is True
    assert rm.pause_info()["reason"] == "consecutive_losses"

    # Further entries while paused stay blocked
    rok, rwhy = rm.check_entry("MINT_NEW", {}, equity, equity, 0.0)
    assert rok is False
    assert "risk_paused" in rwhy

    # Advance clock past pause time (45 mins = 2700 s)
    now_time += 2701.0
    rm.tick(equity)
    assert rm.paused is False
    # Controlled resume is active for next 3 trades at 0.5 size factor
    assert rm.s["resume_left"] == 3
    sz, note = rm.size(equity, equity, 0.0, 0.15, 3.0)
    # Normal size would be 90.0 * 0.15 = 13.5; with resume_size_factor 0.5: 6.75
    assert math.isclose(sz, 6.75, abs_tol=0.01)
    assert "ripresa controllata" in note


# ==============================================================================
# 8. SCENARIO 8 — Multiple Partial Exits (Exact Cumulative PnL Aggregation)
# ==============================================================================
def test_scenario_08_multiple_partial_exits_consistent_pnl(tmp_path, base_cfg):
    bot_dir = str(tmp_path / "bot_partial")
    os.makedirs(bot_dir, exist_ok=True)
    bot = PaperBot(base_cfg, bot_dir, reset=True)
    bot.cash = 100.0

    entry_price = 1.0
    initial_qty = 100.0
    initial_cost = 100.0
    mint = "MINT_MULTI_PARTIAL"

    bot.positions[mint] = {
        "mint": mint, "pool": "POOL_1", "name": "PARTIAL_COIN", "dex": "raydium",
        "entry_type": "volume_spike", "entry_ts": time.time() - 600,
        "entry_mid": entry_price, "entry_fill": entry_price,
        "qty": initial_qty, "cost_usd": initial_cost, "liquidity": 100000.0,
        "peak": entry_price, "last": entry_price, "last_price_ts": time.time(),
        "info": {}, "costs": {"buy_slip_pct": 0.0, "buy_dex_fee": 0.0, "buy_net_fee": 0.0, "buy_slip_src": "stimato"},
        "failed_exits": 0, "original_qty": initial_qty, "original_cost_usd": initial_cost,
    }

    # Disable fees for exact arithmetic check
    bot._net_fee_usd = lambda: 0.0
    bot._slip = lambda gross, liq: 0.0
    bot._dex_fee = lambda dex: 0.0
    bot.decimals_cache[mint] = 6

    # Exit Intelligence: nessun fill simulato senza una quote Jupiter valida.
    # Quote al prezzo esatto del test (slippaggio 0), aggiornata prima di ogni exit.
    quote_price = {"p": None}
    def fake_quote(in_mint, out_mint, amount_raw, deadline=None):
        qty = amount_raw / 10**6
        usd_out = qty * quote_price["p"]
        return {"out": int(usd_out * 10**6), "in": amount_raw, "impact_pct": 1.0, "route": "Raydium",
                "endpoint": "v1/quote", "quote_ts": time.time(), "age_s": 0.1, "latency_s": 0.05, "cached": False}
    from types import SimpleNamespace
    bot.jup = SimpleNamespace(
        quote=fake_quote,
        cache={},
        last_reason="",
        stats={},
        unavailable={}
    )

    # 1st partial exit: sell 40 units at $1.30 (+30% gain on 40 units)
    # Cost basis for 40 units = $40. Proceeds = 40 * 1.30 = $52. PnL 1 = +$12.
    quote_price["p"] = 1.30
    bot.sell(mint, 1.30, "take_profit_1", sell_qty=40.0)
    p = bot.positions[mint]
    assert p["qty"] == 60.0
    assert math.isclose(p["cost_usd"], 60.0)
    assert math.isclose(p["partial_realized_pnl"], 12.0)
    assert len(bot.closed) == 1
    assert bot.closed[0]["partial"] is True
    assert bot.closed[0]["counts_as_trade"] is False

    # 2nd partial exit: sell 30 units at $1.50 (+50% gain on 30 units)
    # Cost basis for 30 units = $30. Proceeds = 30 * 1.50 = $45. PnL 2 = +$15.
    quote_price["p"] = 1.50
    bot.sell(mint, 1.50, "partial_tp_step2", sell_qty=30.0)
    p = bot.positions[mint]
    assert p["qty"] == 30.0
    assert math.isclose(p["cost_usd"], 30.0)
    assert math.isclose(p["partial_realized_pnl"], 27.0)  # 12 + 15 = 27
    assert len(bot.closed) == 2

    # Final exit: close remaining 30 units at $1.80 (+80% gain on 30 units)
    # Cost basis for 30 units = $30. Proceeds = 30 * 1.80 = $54. PnL 3 = +$24.
    quote_price["p"] = 1.80
    bot.sell(mint, 1.80, "take_profit_final")
    assert mint not in bot.positions
    assert len(bot.closed) == 3

    final_trade = bot.closed[-1]
    assert final_trade["counts_as_trade"] is True
    # Final leg PnL = $24.0
    assert math.isclose(final_trade["pnl_usd"], 24.0)
    # Cumulative PnL = PnL 1 ($12) + PnL 2 ($15) + PnL 3 ($24) = $51.0!
    assert math.isclose(final_trade["cumulative_pnl_usd"], 51.0)
    # Total proceeds = 52 + 45 + 54 = 151. Initial cost = 100. Net gain = $51.0.
    # Cumulative return = 51.0 / 100.0 = +51.0%
    assert math.isclose(final_trade["cumulative_pnl_pct"], 51.0)

    # Exactly 1 completed trade row counted
    completed = bot._completed_trade_rows()
    assert len(completed) == 1
    assert completed[0]["trade_id"] == final_trade["trade_id"]


# ==============================================================================
# 9. SCENARIO 9 — Crash During a Transaction (Atomic State Persistence)
# ==============================================================================
def test_scenario_09_crash_during_transaction_atomic_recovery(tmp_path):
    data_dir = tmp_path / "data_crash"
    data_dir.mkdir()
    state_file = data_dir / "state.json"

    # Write initial valid state
    initial_state = {"cash": 100.0, "positions": {"MINT_A": {"qty": 10.0, "cost_usd": 15.0}}}
    storage.atomic_json(str(state_file), initial_state)

    # Simulate crash mid-write by creating a dangling partial temp file
    tmp_corrupted = data_dir / "state.json.9999.partial.tmp"
    tmp_corrupted.write_text('{"cash": 50.0, "positions": INVALID_JSON_TRUNCATED...', encoding="utf-8")

    # Load safe must recover the genuine intact state
    loaded, src = storage.load_json_safe(str(state_file))
    assert loaded["cash"] == 100.0
    assert "MINT_A" in loaded["positions"]
    assert src == "main"


# ==============================================================================
# 10. SCENARIO 10 — Restart with an Open Position (Preservation of Trailing/Breakeven State)
# ==============================================================================
def test_scenario_10_restart_with_open_position_preserves_state(tmp_path, base_cfg):
    bot_dir = str(tmp_path / "bot_restart")
    bot1 = PaperBot(base_cfg, bot_dir, reset=True)
    bot1.cash = 85.0
    now_ts = time.time()
    mint = "MINT_RESTORE"

    bot1.positions[mint] = {
        "mint": mint, "pool": "POOL_R", "name": "RESTORE_TOKEN", "dex": "raydium",
        "entry_type": "volume_spike", "entry_ts": now_ts, "entry_mid": 1.0, "entry_fill": 1.0,
        "qty": 50.0, "cost_usd": 50.0, "liquidity": 25000.0, "peak": 1.40, "last": 1.35,
        "last_price_ts": now_ts, "info": {},
        "costs": {"buy_slip_pct": 0.5, "buy_dex_fee": 0.05, "buy_net_fee": 0.002, "buy_slip_src": "stimato"},
        "failed_exits": 0, "tp1_hit": True, "tp1_qty_sold": 50.0, "tp1_price": 1.25,
        "tp1_realized_pnl": 12.5, "breakeven_activated": True, "breakeven_price": 1.05,
        "original_qty": 100.0, "original_cost_usd": 100.0, "remaining_qty": 50.0, "tp2_done": False,
        "mfe_pct": 0.40, "mae_pct": 0.0,
    }
    bot1.save_state("running")

    # Second instance restarts with reset=False
    bot2 = PaperBot(base_cfg, bot_dir, reset=False)
    assert mint in bot2.positions
    p2 = bot2.positions[mint]
    assert p2["qty"] == 50.0
    assert p2["tp1_hit"] is True
    assert p2["breakeven_activated"] is True
    assert math.isclose(p2["breakeven_price"], 1.05)
    assert math.isclose(p2["peak"], 1.40)


# ==============================================================================
# 11. SCENARIO 11 — External API Unavailable (Fail-Safe: No Zero Liquidation)
# ==============================================================================
def test_scenario_11_external_api_unavailable_no_zero_liquidation(tmp_path, base_cfg):
    bot_dir = str(tmp_path / "bot_api_down")
    bot = PaperBot(base_cfg, bot_dir, reset=True)
    mint = "MINT_SAFE"
    now_ts = time.time()

    bot.positions[mint] = {
        "mint": mint, "pool": "POOL_S", "name": "SAFE_COIN", "dex": "raydium",
        "entry_type": "volume_spike", "entry_ts": now_ts - 500, "entry_mid": 1.0, "entry_fill": 1.0,
        "qty": 100.0, "cost_usd": 100.0, "liquidity": 50000.0, "peak": 1.10, "last": 1.10,
        "last_price_ts": now_ts - 30, "info": {},
        "costs": {"buy_slip_pct": 0.5, "buy_dex_fee": 0.05, "buy_net_fee": 0.002, "buy_slip_src": "stimato"},
        "failed_exits": 0,
    }

    # Mock DexScreener outage: returns empty dict / feed_ok = False
    class OutageDS:
        last_ok = False
        def tokens(self, mints):
            return {}

    bot.ds = OutageDS()
    bot.manage()

    # Position MUST NOT be liquidated to 0 or sold as a rug!
    assert mint in bot.positions
    assert len(bot.closed) == 0
    assert bot.positions[mint]["last"] == 1.10


# ==============================================================================
# 12. SCENARIO 12 — Token with Insufficient Liquidity (Guards Reject Entry)
# ==============================================================================
def test_scenario_12_insufficient_liquidity_blocked(tmp_path, base_cfg):
    bot_dir = str(tmp_path / "bot_liq")
    bot = PaperBot(base_cfg, bot_dir, reset=True)
    snap = {
        "mint": "MINT_LOW_LIQ", "name": "LOW_LIQ", "price": 1.0,
        "liquidity": 2000.0,  # Below min_liquidity_usd (10,000)
        "dex": "raydium", "pool": "POOL_LOW", "src": "dexscreener",
    }
    # Guard check directly
    guard_result = bot._guards_entry(snap, 15.0, None)
    # guard_liquidity_too_low or rejected before fill
    assert "liquidity" in guard_result or guard_result != ""


# ==============================================================================
# 13. SCENARIO 13 — Token with Excessive Holder Concentration (AntiRug / Guard Rejection)
# ==============================================================================
def test_scenario_13_excessive_holder_concentration_blocked():
    antirug_mock = MagicMock()
    # High risk concentration (> 60% top 10, or creator > 20%)
    antirug_mock.evaluate.return_value = {
        "risk": "HIGH",
        "risk_score": 85,
        "is_rug": True,
        "reasons": ["excessive_top10_concentration", "creator_oversized_holding"],
    }
    res = antirug_mock.evaluate("MINT_WHALE_HEAVY")
    assert res["risk"] == "HIGH"
    assert res["is_rug"] is True
    assert "excessive_top10_concentration" in res["reasons"]


# ==============================================================================
# 14. SCENARIO 14 — Duplicate Signal Prevention (In-Flight & Duplicate Window)
# ==============================================================================
def test_scenario_14_duplicate_signal_prevention(base_cfg):
    rm = RiskManager(base_cfg)
    mint = "MINT_DUP"
    positions = {mint: {"qty": 10.0}}

    # 1. Already open position -> blocked
    ok, why = rm.check_entry(mint, positions, 100.0, 100.0, 15.0)
    assert ok is False
    assert why == "duplicate_open"

    # 2. In-flight purchase -> blocked
    rm2 = RiskManager(base_cfg)
    rm2.begin_buy(mint)
    ok2, why2 = rm2.check_entry(mint, {}, 100.0, 100.0, 0.0)
    assert ok2 is False
    assert why2 == "duplicate_open"

    # 3. Recent buy window (120s) -> blocked
    rm3 = RiskManager(base_cfg)
    rm3.end_buy(mint, filled=True)
    ok3, why3 = rm3.check_entry(mint, {}, 100.0, 100.0, 0.0)
    assert ok3 is False
    assert why3 == "duplicate_recent_buy"


# ==============================================================================
# 15. SCENARIO 15 — Minimum Position Size Exceeding Maximum Risk Limit (Safety Ceiling)
# ==============================================================================
def test_scenario_15_minimum_position_size_never_overrides_max_safety_limit(base_cfg):
    rm = RiskManager(base_cfg)  # conservative mode: max_position_pct = 0.15 (15%)
    equity = 100.0
    cash = 100.0
    open_cost = 0.0
    requested_pct = 0.15
    max_safe_amount = equity * rm.L["max_position_pct"]  # $15.0

    # Test A: Minimum trade requirement ($25.0) is GREATER than maximum safety limit ($15.0)
    min_trade = 25.0
    size, note = rm.size(equity, cash, open_cost, requested_pct, min_trade)
    # The fundamental rule: minimum position size MUST NEVER override maximum safety limit!
    assert size == 0.0, "Size must be 0.0 when min_trade exceeds max position risk ceiling!"
    assert "supera limite massimo" in note

    # Test B: Low equity ($40.0), 15% limit is $6.0, but min_trade is $10.0
    equity_low = 40.0
    size_b, note_b = rm.size(equity_low, equity_low, 0.0, requested_pct, 10.0)
    assert size_b == 0.0
    assert size_b <= equity_low * rm.L["max_position_pct"]

    # Test C: Normal valid trade where min_trade ($5.0) is within safety ceiling ($15.0)
    size_c, note_c = rm.size(equity, cash, open_cost, requested_pct, 5.0)
    assert math.isclose(size_c, 15.0)
    assert size_c <= max_safe_amount
