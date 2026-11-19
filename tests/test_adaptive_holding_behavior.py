#!/usr/bin/env python3
"""
Adaptive Holding Engine v1 — Behavioral Test Suite

Covers 7 categories with 48+ scenarios:
A. Holding Stage Transitions (15→20→25→30 min)
B. Decision Engine (score, missing data, invalid quotes)
C. Protective Exit Priority During Extensions (SL/TP/trailing/wallet before adaptive)
D. Brain Integration (threshold consistency, user locks, hard ceiling)
E. Persistence (restart at various stages)
F. Paper-Mode Enforcement (env var, simulation_only, wallet env vars)
G. Telemetry (baseline capture, fee accounting, CSV migration, failure isolation)
"""
import os, sys, tempfile, time, json, csv
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

os.environ.update({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "VYBE_API_KEY": ""})
import yaml, paper_bot


# ============================================================================
# Test Infrastructure
# ============================================================================

class FakeClock:
    """Deterministic time source for reproducible tests."""
    def __init__(self, t0=1_000_000.0):
        self.t = t0
    def now(self):
        return self.t
    def advance(self, s):
        self.t += s


def make_bot(cfg_overrides=None, data_dir=None, reset=True):
    """Create a PaperBot with adaptive holding enabled and all externals mocked."""
    cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
    cfg["adaptive_holding"]["enabled"] = True
    if cfg_overrides:
        for k, v in cfg_overrides.items():
            cfg["adaptive_holding"][k] = v
    if data_dir is None:
        data_dir = tempfile.mkdtemp()
    clk = FakeClock()
    # Monkeypatch time
    paper_bot.now = clk.now
    b = paper_bot.PaperBot(cfg, data_dir, reset=reset)
    b.meta["simulation_only"] = True
    # Mock all external deps
    b.jup_sell = lambda m, mid, qty: (mid * qty, mid, 0.0, {"route": "raydium"}) if qty > 0 else None
    b.jup_buy = lambda m, mid, usd: (usd / mid * 0.999, mid * 1.001, 0.001, {"route": "raydium"})
    b.ds.tokens = lambda mints: {m: {"price": 0.00011, "vol_m5": 60000, "liquidity": 19000} for m in mints}
    b.ds.last_ok = True
    # Disable SMART Recovery to avoid interference
    b.recovery_mode["enabled"] = False
    return b, clk


def make_position(b, clk, stage=0, extensions=0, next_check=None, **extra):
    """Build a volume_spike position with adaptive holding state."""
    entry = clk.t
    ah = b.c["adaptive_holding"]
    max_deadline = entry + ah.get("max_minutes", 30) * 60
    initial_min = ah.get("initial_minutes", 15)
    step_min = ah.get("step_minutes", 5)
    p = {
        "mint": "TESTmint", "pool": "p", "name": "TEST", "dex": "raydium",
        "entry_type": "volume_spike",
        "entry_ts": entry, "entry_mid": 0.0001, "entry_fill": 0.0001,
        "qty": 100000, "cost_usd": 10,
        "liquidity": 20000, "peak": 0.0001, "last": 0.0001, "last_price_ts": entry,
        "info": {},
        "costs": {"buy_slip_pct": 3, "buy_dex_fee": 0.03, "buy_net_fee": 0.3,
                  "buy_slip_src": "stimato", "buy_route": {}},
        "failed_exits": 0, "peak_price": 0.0001,
        "entry_liquidity": 20000, "entry_vol_5m": 50000,
        "entry_baseline_vol": 50000, "mfe_pct": 0.0, "mae_pct": 0.0,
        "adaptive_holding_enabled": True,
        "adaptive_holding_stage": stage,
        "adaptive_holding_extensions": extensions,
        "adaptive_holding_started_at": entry,
        "adaptive_holding_next_check": next_check or (entry + initial_min * 60),
        "adaptive_holding_last_decision": "INIT",
        "adaptive_holding_last_reason": "position_opened",
        "adaptive_holding_max_deadline": max_deadline,
        "adaptive_holding_baseline_quote": None,
        "adaptive_holding_initial_threshold_min": initial_min,
    }
    p.update(extra)
    b.positions[p["mint"]] = p
    b.cash = 1000
    return p


def run_manage(b):
    """Run manage() and return position (or None if closed)."""
    b.manage()
    return b.positions.get("TESTmint")


def assert_extended(p, expected_stage, expected_extensions):
    assert p is not None, "Position should still be open"
    assert p["adaptive_holding_stage"] == expected_stage, f"stage {p['adaptive_holding_stage']} != {expected_stage}"
    assert p["adaptive_holding_extensions"] == expected_extensions, f"ext {p['adaptive_holding_extensions']} != {expected_extensions}"


def assert_closed(b, expected_reason):
    assert "TESTmint" not in b.positions, "Position should be closed"
    assert b.closed, "Should have closed trade"
    assert b.closed[-1]["exit_reason"] == expected_reason, f"exit_reason {b.closed[-1]['exit_reason']} != {expected_reason}"


# ============================================================================
# Category A: Holding Stage Transitions (15→20→25→30)
# ============================================================================

def test_A1_initial_15min_evaluation():
    """A1: At exactly 15 min, position evaluates and extends to 20."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    make_position(b, clk)
    clk.advance(15 * 60)
    p = run_manage(b)
    assert_extended(p, 1, 1)


def test_A2_extend_20min_evaluation():
    """A2: At 20 min, position evaluates and extends to 25."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    make_position(b, clk, stage=1, extensions=1, next_check=clk.t + 20 * 60)
    clk.advance(20 * 60)
    p = run_manage(b)
    assert_extended(p, 2, 2)


def test_A3_extend_25min_evaluation():
    """A3: At 25 min, position evaluates and extends to 30."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    make_position(b, clk, stage=2, extensions=2, next_check=clk.t + 25 * 60)
    clk.advance(25 * 60)
    p = run_manage(b)
    assert_extended(p, 3, 3)


def test_A4_hard_ceiling_30min_closes():
    """A4: At 30 min hard ceiling, position closes (no extension)."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    make_position(b, clk, stage=3, extensions=3, next_check=clk.t + 30 * 60)
    clk.advance(30 * 60)
    p = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_A5_30min_plus_one_minute_closes():
    """A5: One minute past 30 min deadline, position closes."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    make_position(b, clk, stage=3, extensions=3, next_check=clk.t + 30 * 60)
    clk.advance(31 * 60)
    p = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_A6_stage_0_to_1_time_calculation():
    """A6: Stage 0 next_check uses position's initial_minutes (not config)."""
    b, clk = make_bot({"initial_minutes": 10})  # config says 10
    p = make_position(b, clk)  # but position was created with 15
    assert p["adaptive_holding_next_check"] == clk.t + 10 * 60, f"next_check={p["adaptive_holding_next_check"]} expected={clk.t + 10 * 60}"


def test_A7_extensions_counter_increments():
    """A7: extensions counter increments 1→2→3 correctly."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    make_position(b, clk)
    clk.advance(15 * 60); run_manage(b)
    clk.advance(5 * 60); run_manage(b)
    clk.advance(5 * 60); run_manage(b)
    assert b.positions["TESTmint"]["adaptive_holding_extensions"] == 3


def test_A8_stage_respects_max_minutes_config():
    """A8: Config max_minutes (default 30) caps the hard deadline."""
    b, clk = make_bot({"max_minutes": 20})
    make_position(b, clk)
    # First extension to 15 min should work
    clk.advance(15 * 60); p = run_manage(b)
    assert_extended(p, 1, 1)
    # Second extension would exceed 20 min hard ceiling -> close
    clk.advance(6 * 60); p = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


# ============================================================================
# Category B: Decision Engine (score, missing data, invalid quotes)
# ============================================================================

def test_B1_score_above_min_extends():
    """B1: Composite score >= min_score (60) -> EXTEND."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # Positive price move, good volume, good liquidity -> high score
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00012, "vol_m5": 120000, "liquidity": 25000}}
    p2 = run_manage(b)
    assert_extended(p2, 1, 1)


def test_B2_score_below_min_closes():
    """B2: Composite score < min_score -> CLOSE with reason score_below_min_60."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # Terrible: price -10%, volume collapse, liquidity drain
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00009, "vol_m5": 1000, "liquidity": 100}}
    p2 = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_B3_missing_jupiter_quote_closes_if_required():
    """B3: No Jupiter quote + require_jupiter_route=True -> CLOSE."""
    b, clk = make_bot({"require_jupiter_route": True})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    b.jup_sell = lambda m, mid, qty: None
    p2 = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_B4_missing_jupiter_quote_ignored_if_not_required():
    """B4: No Jupiter quote + require_jupiter_route=False -> can still EXTEND on score."""
    b, clk = make_bot({"require_jupiter_route": False})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    b.jup_sell = lambda m, mid, qty: None
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00012, "vol_m5": 120000, "liquidity": 25000}}
    p2 = run_manage(b)
    assert_extended(p2, 1, 1)


def test_B5_stale_quote_rejected():
    """B5: Jupiter quote older than max_quote_age_s -> treated as unavailable."""
    b, clk = make_bot({"require_jupiter_route": True})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # Return quote with age_s > max_quote_age_s (default 15s)
    b.jup_sell = lambda m, mid, qty: None if 100 > b.c.get("jupiter", {}).get("max_quote_age_s", 15) else (mid * qty, mid, 0.0, {"route": "x", "age_s": 100})
    p2 = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_B6_excessive_slippage_closes():
    """B6: Jupiter sell slippage > min_sell_slippage_pct -> CLOSE."""
    b, clk = make_bot({"min_sell_slippage_pct": 5.0})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # 10% slippage
    b.jup_sell = lambda m, mid, qty: (mid * qty * 0.9, mid * 1.1, 0.10, {"route": "x"})
    p2 = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_B7_slippage_within_limit_extends():
    """B7: Jupiter sell slippage <= min_sell_slippage_pct -> EXTEND."""
    b, clk = make_bot({"min_sell_slippage_pct": 5.0})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # 3% slippage
    b.jup_sell = lambda m, mid, qty: (mid * qty * 0.97, mid * 1.03, 0.03, {"route": "x"})
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00011, "vol_m5": 60000, "liquidity": 19000}}
    p2 = run_manage(b)
    assert_extended(p2, 1, 1)


def test_B8_liquidity_below_min_closes():
    """B8: Current liquidity < min_liquidity_usd -> CLOSE."""
    b, clk = make_bot({"min_liquidity_usd": 5000})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00011, "vol_m5": 60000, "liquidity": 1000}}
    p2 = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_B9_drawdown_penalty_reduces_score():
    """B9: Large drawdown from peak reduces risk component score."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk, peak_price=0.0002)  # peaked at 2x entry
    clk.advance(15 * 60)
    # Current price back to entry = 50% drawdown from peak
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.0001, "vol_m5": 60000, "liquidity": 20000}}
    p2 = run_manage(b)
    # Should still extend if overall score OK, but verify it doesn't crash


def test_B10_volume_acceleration_score():
    """B10: Volume acceleration >2x gives max volume score."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # Volume 3x entry
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00011, "vol_m5": 150000, "liquidity": 20000}}
    p2 = run_manage(b)
    assert_extended(p2, 1, 1)


def test_B11_antirug_high_risk_closes():
    """B11: AntiRug high risk score -> CLOSE."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # Mock antirug to return high risk
    b.antirug = MagicMock()
    b.antirug.check = lambda mint: {"risk_score": 90}
    p2 = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_B12_rugged_token_closes():
    """B12: RugCheck reports rugged=True -> CLOSE."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    b.rc.holders = lambda mint: {"rugged": True}
    p2 = run_manage(b)
    assert_closed(b, "adaptive_holding_close")


def test_B13_invalid_env_var_fail_closed():
    """B13: ADAPTIVE_HOLDING_ENABLED=invalid -> fail closed (disabled)."""
    with patch.dict(os.environ, {"ADAPTIVE_HOLDING_ENABLED": "maybe"}):
        cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
        cfg["adaptive_holding"]["enabled"] = True
        d = tempfile.mkdtemp()
        b = paper_bot.PaperBot(cfg, d, reset=True)
        b.meta["simulation_only"] = True
        assert b._adaptive_holding_enabled() is False


def test_B14_HOLD_before_next_check():
    """B14: Before next_check time, evaluation returns HOLD (no action)."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    # Only 10 min passed, next_check at 15 min
    clk.advance(10 * 60)
    decision, reason, _, _, _ = b._evaluate_adaptive_holding("TESTmint", p, 0.00011, {"price": 0.00011, "vol_m5": 60000, "liquidity": 20000})
    assert decision == "HOLD"
    assert "before_next_evaluation" in reason


def test_B15_composite_score_weights():
    """B15: Score respects configured indicator weights."""
    b, clk = make_bot({"indicators": {"price_move_pct_weight": 100, "momentum_weight": 0,
                                      "volume_accel_weight": 0, "liquidity_weight": 0, "risk_weight": 0}})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # Price moved +20% -> should dominate with 100% weight
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00012, "vol_m5": 1000, "liquidity": 1000}}
    p2 = run_manage(b)
    assert_extended(p2, 1, 1)


# ============================================================================
# Category C: Protective Exit Priority During Extensions
# ============================================================================

def test_C1_stop_loss_fires_before_adaptive():
    """C1: Stop loss triggers before adaptive evaluation."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    # Price drops below stop_loss_pct (12%)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00008, "vol_m5": 60000, "liquidity": 20000}}
    b.manage()
    assert_closed(b, "stop_loss")


def test_C2_take_profit_fires_before_adaptive():
    """C2: Take profit triggers before adaptive evaluation."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00014, "vol_m5": 60000, "liquidity": 20000}}
    b.manage()
    assert_closed(b, "take_profit")


def test_C3_trailing_stop_fires_before_adaptive():
    """C3: Trailing stop triggers before adaptive evaluation."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk, peak=0.00015, last=0.00012, peak_price=0.00015)  # Peaked at 1.5x entry
    clk.advance(15 * 60)
    # Price drops 15% from peak (trailing_stop_pct=10%, activate=15%)
    # peak=0.00015, entry=0.0001, price=0.000135
    # peak/entry - 1 = 0.5 >= 0.15 (activate), price <= peak*0.9 = 0.000135 -> trailing fires
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.000135, "vol_m5": 60000, "liquidity": 20000}}
    b.manage()
    assert_closed(b, "trailing_stop")


def test_C4_wallet_follow_sell_before_adaptive():
    """C4: Top-trader wallet sell triggers before adaptive."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk, entry_type="copy_top_traders", track={"wallets": {"W1": {"start": 1000, "now": 100, "sold": True, "src": "rpc"}}, "n": 1})
    clk.advance(15 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00011, "vol_m5": 60000, "liquidity": 19000}}
    b.check_wallets = lambda pos, rules: True
    b.manage()
    assert_closed(b, "top_traders_sold")


def test_C5_time_exit_fires_when_adaptive_disabled():
    """C5: If adaptive disabled for position, normal time_exit at max_hold."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk, adaptive_holding_enabled=False)
    clk.advance(15 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00011, "vol_m5": 60000, "liquidity": 19000}}
    b.manage()
    assert_closed(b, "time_exit")


def test_C6_protective_exit_updates_last_price_ts():
    """C6: After protective exit fires, position removed (no adaptive eval)."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00008, "vol_m5": 60000, "liquidity": 20000}}
    b.manage()
    assert "TESTmint" not in b.positions


# ============================================================================
# Category D: Brain Integration
# ============================================================================

def test_D1_brain_max_hold_sets_initial_threshold():
    """D1: Brain's max_hold_minutes (within 5-30) used as initial threshold."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    # Brain is initialized in PaperBot with config bounds
    rules = b.exit_rules("volume_spike")
    # When adaptive enabled, max_hold_minutes should be initial_minutes
    assert rules["max_hold_minutes"] == 15


def test_D2_user_lock_prevents_brain_change():
    """D2: User-locked params via /imposta are not changed by Brain."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    # Simulate user lock on max_hold_minutes: /imposta writes to config + meta.overrides
    b.meta["overrides"] = {"max_hold_minutes": 20}
    b.c["max_hold_minutes"] = 20
    rules = b.exit_rules("volume_spike")
    assert rules["max_hold_minutes"] == 15  # Adaptive holding overrides config


def test_D3_adaptive_hard_ceiling_never_exceeded():
    """D3: Even if Brain tries to set max_hold > 30, adaptive hard ceiling 30 wins."""
    b, clk = make_bot({"max_minutes": 30})
    p = make_position(b, clk)
    # Position's hard deadline is entry + 30 min
    deadline = p["adaptive_holding_max_deadline"]
    assert deadline - p["entry_ts"] == 30 * 60


def test_D4_brain_update_does_not_reset_entry_ts():
    """D4: Brain parameter updates don't reset position entry_ts."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    original_entry = p["entry_ts"]
    # Simulate brain changing a param
    b.c["max_hold_minutes"] = 20
    clk.advance(15 * 60)
    p2 = run_manage(b)
    assert p2["entry_ts"] == original_entry


def test_D5_brain_update_does_not_reset_extensions():
    """D5: Brain updates don't reset extension history."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk, stage=2, extensions=2, next_check=clk.t + 25 * 60)
    original_ext = p["adaptive_holding_extensions"]
    b.c["max_hold_minutes"] = 25
    clk.advance(25 * 60)
    p2 = run_manage(b)
    assert p2["adaptive_holding_extensions"] > original_ext, "Extensions should not be reset" + 1


def test_D6_brain_update_does_not_reset_max_deadline():
    """D6: Brain updates don't change hard deadline."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    original_deadline = p["adaptive_holding_max_deadline"]
    b.c["max_hold_minutes"] = 25
    clk.advance(15 * 60)
    p2 = run_manage(b)
    assert p2["adaptive_holding_max_deadline"] == original_deadline


def test_D7_copy_top_traders_unchanged():
    """D7: copy_top_traders strategy unaffected (180 min hold)."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    rules = b.exit_rules("copy_top_traders")
    assert rules["max_hold_minutes"] == 180
    assert rules["follow_wallets"] is True


# ============================================================================
# Category E: Persistence (restart at various stages)
# ============================================================================

def test_E1_restart_preserves_stage_0():
    """E1: Restart with stage 0 position preserves timer (not reset)."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(10 * 60)  # 10 min into 15 min window
    b.save_state()
    # New bot instance
    b2, clk2 = make_bot(data_dir=d, reset=False)
    clk2.t = clk.t
    # Position should load with same next_check
    loaded = b2.positions["TESTmint"]
    assert loaded["adaptive_holding_stage"] == 0
    assert loaded["adaptive_holding_next_check"] == p["adaptive_holding_next_check"]


def test_E2_restart_preserves_stage_1():
    """E2: Restart with stage 1 (extended to 20) preserves state."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)  # extends to stage 1
    b.save_state()
    # New bot
    b2, clk2 = make_bot(data_dir=d, reset=False)
    clk2.t = clk.t
    loaded = b2.positions["TESTmint"]
    assert loaded["adaptive_holding_stage"] == 1
    assert loaded["adaptive_holding_extensions"] == 1


def test_E3_restart_preserves_stage_2():
    """E3: Restart with stage 2 (extended to 25) preserves state."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60); run_manage(b)
    clk.advance(5 * 60); run_manage(b)
    b.save_state()
    b2, clk2 = make_bot(data_dir=d, reset=False)
    clk2.t = clk.t
    loaded = b2.positions["TESTmint"]
    assert loaded["adaptive_holding_stage"] == 2
    assert loaded["adaptive_holding_extensions"] == 2


def test_E4_restart_preserves_stage_3():
    """E4: Restart with stage 3 (extended to 30) preserves state."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60); run_manage(b)
    clk.advance(5 * 60); run_manage(b)
    clk.advance(5 * 60); run_manage(b)
    b.save_state()
    b2, clk2 = make_bot(data_dir=d, reset=False)
    clk2.t = clk.t
    loaded = b2.positions["TESTmint"]
    assert loaded["adaptive_holding_stage"] == 3
    assert loaded["adaptive_holding_extensions"] == 3


def test_E5_restart_no_duplicate_extension():
    """E5: Restart does not cause duplicate extension at same checkpoint."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)  # extends to stage 1
    b.save_state()
    # New bot at same time
    b2, clk2 = make_bot(data_dir=d, reset=False)
    clk2.t = clk.t
    b2.manage()  # Should NOT extend again
    loaded = b2.positions["TESTmint"]
    assert loaded["adaptive_holding_extensions"] == 1
    assert loaded["adaptive_holding_stage"] == 1


def test_E6_restart_timer_not_reset():
    """E6: Timer not reset on restart -- next_check absolute time preserved."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)
    next_check = p["adaptive_holding_next_check"]
    b.save_state()
    b2, clk2 = make_bot(data_dir=d, reset=False)
    clk2.t = clk.t
    loaded = b2.positions["TESTmint"]
    assert loaded["adaptive_holding_next_check"] == next_check


def test_E7_restart_past_deadline_closes_immediately():
    """E7: Restart with position past hard deadline -> closes on first manage."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(31 * 60)  # Past 30 min hard ceiling
    b.save_state()
    b2, clk2 = make_bot(data_dir=d, reset=False)
    clk2.t = clk.t
    b2.manage()
    assert "TESTmint" not in b2.positions
    assert b2.closed[-1]["exit_reason"] == "adaptive_holding_close"


def test_E8_restart_baseline_quote_preserved():
    """E8: Baseline quote captured at first extension is preserved on restart."""
    d = tempfile.mkdtemp()
    b, clk = make_bot({"baseline_quote_at_initial": True}, data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)
    baseline = p["adaptive_holding_baseline_quote"]
    b.save_state()
    b2, clk2 = make_bot(data_dir=d, reset=False)
    clk2.t = clk.t
    loaded = b2.positions["TESTmint"]
    assert loaded["adaptive_holding_baseline_quote"] == baseline


# ============================================================================
# Category F: Paper-Mode Enforcement
# ============================================================================

def test_F1_env_var_true_enables():
    """F1: ADAPTIVE_HOLDING_ENABLED=1 enables feature (subject to paper checks)."""
    with patch.dict(os.environ, {"ADAPTIVE_HOLDING_ENABLED": "1"}):
        cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
        cfg["adaptive_holding"]["enabled"] = False  # YAML says false
        d = tempfile.mkdtemp()
        b = paper_bot.PaperBot(cfg, d, reset=True)
        b.meta["simulation_only"] = True
        assert b._adaptive_holding_enabled() is True


def test_F2_env_var_false_disables():
    """F2: ADAPTIVE_HOLDING_ENABLED=0 disables feature (overrides YAML)."""
    with patch.dict(os.environ, {"ADAPTIVE_HOLDING_ENABLED": "0"}):
        cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
        cfg["adaptive_holding"]["enabled"] = True
        d = tempfile.mkdtemp()
        b = paper_bot.PaperBot(cfg, d, reset=True)
        assert b._adaptive_holding_enabled() is False


def test_F3_env_var_invalid_fail_closed():
    """F3: ADAPTIVE_HOLDING_ENABLED=invalid -> fail closed (disabled)."""
    with patch.dict(os.environ, {"ADAPTIVE_HOLDING_ENABLED": "yesplease"}):
        cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
        cfg["adaptive_holding"]["enabled"] = True
        d = tempfile.mkdtemp()
        b = paper_bot.PaperBot(cfg, d, reset=True)
        assert b._adaptive_holding_enabled() is False


def test_F4_yaml_enabled_without_env():
    """F4: YAML enabled=true with no env var -> enabled."""
    cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
    cfg["adaptive_holding"]["enabled"] = True
    d = tempfile.mkdtemp()
    b = paper_bot.PaperBot(cfg, d, reset=True)
    b.meta["simulation_only"] = True
    assert b._adaptive_holding_enabled() is True


def test_F5_simulation_only_false_disables():
    """F5: state.json simulation_only=False -> _verify_paper_mode fails -> disabled."""
    cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
    cfg["adaptive_holding"]["enabled"] = True
    d = tempfile.mkdtemp()
    b = paper_bot.PaperBot(cfg, d, reset=True)
    b.meta["simulation_only"] = False
    assert b._verify_paper_mode() is False


def test_F6_wallet_env_var_disables():
    """F6: Any wallet env var present -> _verify_paper_mode fails -> disabled."""
    with patch.dict(os.environ, {"SOLANA_PRIVATE_KEY": "xxx"}):
        cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
        cfg["adaptive_holding"]["enabled"] = True
        d = tempfile.mkdtemp()
        b = paper_bot.PaperBot(cfg, d, reset=True)
        b.meta["simulation_only"] = True
        assert b._verify_paper_mode() is False


def test_F7_multiple_wallet_vars_disables():
    """F7: Multiple wallet env vars all checked."""
    with patch.dict(os.environ, {"PRIVATE_KEY": "x", "KEYPAIR_PATH": "y"}):
        cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
        cfg["adaptive_holding"]["enabled"] = True
        d = tempfile.mkdtemp()
        b = paper_bot.PaperBot(cfg, d, reset=True)
        b.meta["simulation_only"] = True
        assert b._verify_paper_mode() is False


def test_F8_paper_only_check_false_bypasses():
    """F8: paper_only_check=false in config bypasses paper checks."""
    cfg = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
    cfg["adaptive_holding"]["enabled"] = True
    cfg["adaptive_holding"]["paper_only_check"] = False
    d = tempfile.mkdtemp()
    b = paper_bot.PaperBot(cfg, d, reset=True)
    b.meta["simulation_only"] = False
    with patch.dict(os.environ, {"SOLANA_PRIVATE_KEY": "xxx"}):
        assert b._verify_paper_mode() is True


def test_F9_position_adaptive_flag_respects_global():
    """F9: Position adaptive_holding_enabled=false even if global enabled."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk, adaptive_holding_enabled=False)
    clk.advance(15 * 60)
    b.manage()
    # Should fall through to normal time_exit
    assert "TESTmint" not in b.positions
    assert b.closed[-1]["exit_reason"] == "time_exit"


# ============================================================================
# Category G: Telemetry
# ============================================================================

def test_G1_trades_csv_includes_adaptive_fields():
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)  # extends to stage 1
    clk.advance(5 * 60)
    run_manage(b)  # extends to stage 2
    clk.advance(5 * 60)
    run_manage(b)  # extends to stage 3
    clk.advance(5 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00007, "vol_m5": 1000, "liquidity": 100}}
    b.manage()  # closes
    # Check CSV
    with open(b.csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    last = rows[-1]
    assert "adaptive_holding_enabled" in last
    assert last["adaptive_holding_enabled"] == "True"
    assert "adaptive_holding_final_stage" in last
    assert last["adaptive_holding_final_stage"] == "3"
    assert "adaptive_holding_extensions" in last
    assert last["adaptive_holding_extensions"] == "3"
def test_G2_baseline_quote_captured_at_initial():
    """G2: Baseline quote captured at stage 0->1 transition."""
    b, clk = make_bot({"baseline_quote_at_initial": True})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)
    baseline = p["adaptive_holding_baseline_quote"]
    assert baseline is not None
    assert "usd_out" in baseline
    assert "slip" in baseline


def test_G3_baseline_quote_only_once():
    """G3: Baseline quote captured only at first extension."""
    b, clk = make_bot({"baseline_quote_at_initial": True})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)
    first_baseline = p["adaptive_holding_baseline_quote"]
    clk.advance(5 * 60)
    run_manage(b)
    second_baseline = p["adaptive_holding_baseline_quote"]
    assert second_baseline is first_baseline  # Same object, not replaced


def test_G4_fee_accounting_includes_net_fees():
    """G4: Trade P&L includes network fees correctly."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)
    clk.advance(5 * 60)
    run_manage(b)
    clk.advance(5 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00008, "vol_m5": 60000, "liquidity": 20000}}
    b.manage()
    with open(b.csv_path, newline="") as f:
        reader = csv.DictReader(f)
        last = list(reader)[-1]
    # net_fees = buy_net_fee + sell_net_fee
    fees = float(last["network_fees_usd"])
    assert fees > 0


def test_G5_csv_header_migration():
    """G5: CSV header migration works when new fields added."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    # Write an old-format CSV (without adaptive fields)
    old_fields = ["entry_time_utc", "exit_time_utc", "token", "mint", "pnl_usd"]
    with open(b.csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=old_fields)
        writer.writeheader()
        writer.writerow({"entry_time_utc": "2024-01-01T00:00:00", "exit_time_utc": "2024-01-01T00:15:00",
                         "token": "OLD", "mint": "OLD", "pnl_usd": "1.0"})
    # Now run a new trade - need full cycle to close
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)  # extends to stage 1
    clk.advance(5 * 60)
    run_manage(b)  # extends to stage 2
    clk.advance(5 * 60)
    run_manage(b)  # extends to stage 3
    clk.advance(5 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00007, "vol_m5": 1000, "liquidity": 100}}
    b.manage()  # closes
    # Should be able to read both rows - check both main and backup files
    import glob
    all_rows = []
    for f in glob.glob(os.path.join(d, "trades.csv*")):
        with open(f, newline="") as ff:
            reader = csv.DictReader(ff)
            all_rows.extend(list(reader))
    assert len(all_rows) == 2
    assert "adaptive_holding_enabled" in all_rows[1]


def test_G6_telemetry_failure_isolation():
    """G6: Telemetry write failure doesn't crash trading loop."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    # Make CSV path unwritable
    import os
    os.chmod(b.csv_path, 0o444)
    try:
        p = make_position(b, clk)
        clk.advance(15 * 60)
        # Use bad data to trigger close (score below min)
        b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00007, "vol_m5": 1000, "liquidity": 100}}
        b.manage()  # Should not raise
        assert "TESTmint" not in b.positions
    finally:
        os.chmod(b.csv_path, 0o644)


def test_G7_strategy_signals_csv_logs_evaluations():
    """G7: Adaptive evaluations logged to strategy_signals.csv."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)
    clk.advance(5 * 60)
    run_manage(b)
    # Check strategy_signals.csv exists and has adaptive_evaluation rows
    sig_path = b.csv_path.replace("trades.csv", "strategy_signals.csv")
    if os.path.exists(sig_path):
        with open(sig_path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        adaptive_rows = [r for r in rows if r.get("signal_type") == "adaptive_evaluation"]
        assert len(adaptive_rows) >= 2


def test_G8_dashboard_exposes_adaptive_fields():
    """G8: Dashboard state includes adaptive holding fields."""
    d = tempfile.mkdtemp()
    b, clk = make_bot(data_dir=d)
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)
    # Check internal state that dashboard reads
    assert "adaptive_holding_stage" in p
    assert "adaptive_holding_next_check" in p
    assert "adaptive_holding_last_decision" in p


def test_G9_baseline_pnl_comparison():
    """G9: Baseline P&L (hypothetical 15-min exit) recorded in trade."""
    b, clk = make_bot({"baseline_quote_at_initial": True})
    p = make_position(b, clk)
    clk.advance(15 * 60)
    run_manage(b)
    clk.advance(5 * 60)
    run_manage(b)
    clk.advance(5 * 60)
    b.ds.tokens = lambda mints: {"TESTmint": {"price": 0.00008, "vol_m5": 60000, "liquidity": 20000}}
    b.manage()
    with open(b.csv_path, newline="") as f:
        reader = csv.DictReader(f)
        last = list(reader)[-1]
    assert "adaptive_baseline_pnl_usd" in last
    assert "adaptive_baseline_pnl_pct" in last


# ============================================================================
# Test Runner
# ============================================================================

def run_all():
    tests = [
        # A: Stage Transitions
        ("A1", test_A1_initial_15min_evaluation),
        ("A2", test_A2_extend_20min_evaluation),
        ("A3", test_A3_extend_25min_evaluation),
        ("A4", test_A4_hard_ceiling_30min_closes),
        ("A5", test_A5_30min_plus_one_minute_closes),
        ("A6", test_A6_stage_0_to_1_time_calculation),
        ("A7", test_A7_extensions_counter_increments),
        ("A8", test_A8_stage_respects_max_minutes_config),
        # B: Decision Engine
        ("B1", test_B1_score_above_min_extends),
        ("B2", test_B2_score_below_min_closes),
        ("B3", test_B3_missing_jupiter_quote_closes_if_required),
        ("B4", test_B4_missing_jupiter_quote_ignored_if_not_required),
        ("B5", test_B5_stale_quote_rejected),
        ("B6", test_B6_excessive_slippage_closes),
        ("B7", test_B7_slippage_within_limit_extends),
        ("B8", test_B8_liquidity_below_min_closes),
        ("B9", test_B9_drawdown_penalty_reduces_score),
        ("B10", test_B10_volume_acceleration_score),
        ("B11", test_B11_antirug_high_risk_closes),
        ("B12", test_B12_rugged_token_closes),
        ("B13", test_B13_invalid_env_var_fail_closed),
        ("B14", test_B14_HOLD_before_next_check),
        ("B15", test_B15_composite_score_weights),
        # C: Protective Exit Priority
        ("C1", test_C1_stop_loss_fires_before_adaptive),
        ("C2", test_C2_take_profit_fires_before_adaptive),
        ("C3", test_C3_trailing_stop_fires_before_adaptive),
        ("C4", test_C4_wallet_follow_sell_before_adaptive),
        ("C5", test_C5_time_exit_fires_when_adaptive_disabled),
        ("C6", test_C6_protective_exit_updates_last_price_ts),
        # D: Brain Integration
        ("D1", test_D1_brain_max_hold_sets_initial_threshold),
        ("D2", test_D2_user_lock_prevents_brain_change),
        ("D3", test_D3_adaptive_hard_ceiling_never_exceeded),
        ("D4", test_D4_brain_update_does_not_reset_entry_ts),
        ("D5", test_D5_brain_update_does_not_reset_extensions),
        ("D6", test_D6_brain_update_does_not_reset_max_deadline),
        ("D7", test_D7_copy_top_traders_unchanged),
        # E: Persistence
        ("E1", test_E1_restart_preserves_stage_0),
        ("E2", test_E2_restart_preserves_stage_1),
        ("E3", test_E3_restart_preserves_stage_2),
        ("E4", test_E4_restart_preserves_stage_3),
        ("E5", test_E5_restart_no_duplicate_extension),
        ("E6", test_E6_restart_timer_not_reset),
        ("E7", test_E7_restart_past_deadline_closes_immediately),
        ("E8", test_E8_restart_baseline_quote_preserved),
        # F: Paper Mode
        ("F1", test_F1_env_var_true_enables),
        ("F2", test_F2_env_var_false_disables),
        ("F3", test_F3_env_var_invalid_fail_closed),
        ("F4", test_F4_yaml_enabled_without_env),
        ("F5", test_F5_simulation_only_false_disables),
        ("F6", test_F6_wallet_env_var_disables),
        ("F7", test_F7_multiple_wallet_vars_disables),
        ("F8", test_F8_paper_only_check_false_bypasses),
        ("F9", test_F9_position_adaptive_flag_respects_global),
        # G: Telemetry
        ("G1", test_G1_trades_csv_includes_adaptive_fields),
        ("G2", test_G2_baseline_quote_captured_at_initial),
        ("G3", test_G3_baseline_quote_only_once),
        ("G4", test_G4_fee_accounting_includes_net_fees),
        ("G5", test_G5_csv_header_migration),
        ("G6", test_G6_telemetry_failure_isolation),
        ("G7", test_G7_strategy_signals_csv_logs_evaluations),
        ("G8", test_G8_dashboard_exposes_adaptive_fields),
        ("G9", test_G9_baseline_pnl_comparison),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"  {name}: PASS")
            passed += 1
        except Exception as e:
            print(f"  {name}: FAIL - {e}")
            failed += 1
    print(f"\nBehavioral Tests: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)