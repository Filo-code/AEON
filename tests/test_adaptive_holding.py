#!/usr/bin/env python3
"""Adaptive Holding Engine v1 regression tests."""
import os, sys, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

def test_adaptive_holding_disabled_by_default():
    CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml"), encoding="utf-8"))
    ah = CFG.get("adaptive_holding", {})
    assert ah.get("enabled") == False
    print("  Adaptive holding disabled by default: OK")
    return True

def test_adaptive_holding_config_structure():
    CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml"), encoding="utf-8"))
    ah = CFG.get("adaptive_holding", {})
    required = ["enabled", "initial_minutes", "step_minutes", "max_minutes",
                "min_sell_slippage_pct", "min_liquidity_usd", "min_score",
                "require_jupiter_route", "paper_only_check", "baseline_quote_at_initial",
                "indicators"]
    for r in required:
        assert r in ah, f"Missing config field: {r}"
    assert ah["max_minutes"] == 30
    assert ah["initial_minutes"] == 15
    assert ah["step_minutes"] == 5
    print("  Adaptive holding config structure: OK")
    return True

def test_adaptive_holding_indicators_weights():
    CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml"), encoding="utf-8"))
    weights = CFG.get("adaptive_holding", {}).get("indicators", {})
    total = sum(weights.values())
    assert total == 100, f"Weights sum to {total}, not 100"
    expected = ["price_move_pct_weight", "momentum_weight", "volume_accel_weight",
                "liquidity_weight", "risk_weight"]
    for e in expected:
        assert e in weights, f"Missing weight: {e}"
    print("  Indicator weights sum to 100: OK")
    return True

def test_paper_mode_verification_logic():
    wallet_env_vars = [
        "SOLANA_PRIVATE_KEY", "PRIVATE_KEY", "WALLET_PRIVATE_KEY", "FEE_PAYER_PRIVATE_KEY",
        "SIGNING_KEY", "KEYPAIR_PATH", "KEYPAIR_FILE", "WALLET_KEYPAIR"
    ]
    for var in wallet_env_vars:
        assert not os.getenv(var), f"Wallet env var {var} should not be set"
    print("  Paper mode env vars clean: OK")
    return True

def test_adaptive_holding_state_fields():
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    fields = [
        "adaptive_holding_enabled",
        "adaptive_holding_stage",
        "adaptive_holding_extensions",
        "adaptive_holding_started_at",
        "adaptive_holding_next_check",
        "adaptive_holding_last_decision",
        "adaptive_holding_last_reason",
        "adaptive_holding_max_deadline",
        "adaptive_holding_baseline_quote",
    ]
    for field in fields:
        assert field in src, f"Missing adaptive holding field: {field}"
    print("  Adaptive holding state fields present in source: OK")
    return True

def test_adaptive_holding_decision_engine_exists():
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    assert "_evaluate_adaptive_holding" in src
    assert "_verify_paper_mode" in src
    print("  Decision engine methods present: OK")
    return True

def test_adaptive_holding_in_manage():
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    assert "_evaluate_adaptive_holding" in src
    assert "adaptive_holding_enabled" in src
    assert "adaptive_holding_next_check" in src
    assert "EXTEND_20" in src
    assert "EXTEND_25" in src
    assert "EXTEND_30" in src
    print("  Adaptive holding integrated in manage(): OK")
    return True

def test_protective_exits_priority():
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    manage_section = src[src.find("def manage(self):"):src.find("def report(self, period_hours")]
    sl_idx = manage_section.find("stop_loss")
    tp_idx = manage_section.find("take_profit")
    ts_idx = manage_section.find("trailing_stop")
    tt_idx = manage_section.find("top_traders_sold")
    ah_idx = manage_section.find("adaptive_holding_enabled")

    assert sl_idx != -1 and sl_idx < ah_idx
    assert tp_idx != -1 and tp_idx < ah_idx
    assert ts_idx != -1 and ts_idx < ah_idx
    assert tt_idx != -1 and tt_idx < ah_idx
    print("  Protective exits priority verified: OK")
    return True

def test_brain_adaptive_precedence():
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    er_section = src[src.find("def exit_rules(self, p_or_type):"):src.find("def reentry_block")]
    assert "Adaptive Holding Engine v1 precedence" in er_section
    assert "initial evaluation threshold" in er_section
    assert "hard ceiling" in er_section
    assert "must NOT reset" in er_section
    print("  Brain/adaptive precedence documented: OK")
    return True

def test_telemetry_csv_fields():
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    fields = [
        "adaptive_holding_enabled",
        "adaptive_holding_final_stage",
        "adaptive_holding_extensions",
        "adaptive_holding_final_decision",
        "adaptive_holding_max_deadline",
        "adaptive_baseline_quote_proceeds_usd",
        "adaptive_baseline_quote_slippage_pct",
        "adaptive_baseline_pnl_usd",
        "adaptive_baseline_pnl_pct",
    ]
    for field in fields:
        assert field in src, f"Missing telemetry field: {field}"
    print("  Telemetry CSV adaptive fields present: OK")
    return True

def test_sell_populates_adaptive_fields():
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    sell_section = src[src.find('t = {"entry_time_utc": iso(p["entry_ts"])'):src.find("if getattr(self, \"provenance_on\", False):")]
    assert "adaptive_holding_enabled" in sell_section
    assert "adaptive_holding_final_stage" in sell_section
    assert "adaptive_holding_extensions" in sell_section
    assert "adaptive_holding_final_decision" in sell_section
    assert "adaptive_holding_max_deadline" in sell_section
    assert "adaptive_baseline_quote" in sell_section
    print("  sell() populates adaptive fields: OK")
    return True

def test_dashboard_exposes_adaptive_fields():
    with open(os.path.join(ROOT, "dashboard", "app.py"), encoding="utf-8") as f:
        src = f.read()

    pos_section = src[src.find('"positions": [{**p, "held_min"'):src.find('"closed": [{**t, "exit_clock"')]
    fields = [
        "adaptive_holding_enabled",
        "adaptive_holding_stage",
        "adaptive_holding_extensions",
        "adaptive_holding_next_check",
        "adaptive_holding_last_decision",
        "adaptive_holding_last_reason",
        "adaptive_holding_max_deadline",
        "adaptive_holding_baseline_quote",
    ]
    for field in fields:
        assert field in pos_section, f"Dashboard missing field: {field}"
    print("  Dashboard exposes adaptive fields: OK")
    return True

def test_copy_top_traders_unchanged():
    CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml"), encoding="utf-8"))
    copy_exits = CFG.get("copy_exits", {})
    assert copy_exits.get("max_hold_minutes") == 180
    assert copy_exits.get("stop_loss_pct") == 0.25
    assert copy_exits.get("take_profit_pct") == 1.0
    assert copy_exits.get("trailing_stop_pct") == 0.20
    assert copy_exits.get("follow_wallets") == True
    print("  copy_top_traders exits unchanged: OK")
    return True

def run_all():
    tests = [
        test_adaptive_holding_disabled_by_default,
        test_adaptive_holding_config_structure,
        test_adaptive_holding_indicators_weights,
        test_paper_mode_verification_logic,
        test_adaptive_holding_state_fields,
        test_adaptive_holding_decision_engine_exists,
        test_adaptive_holding_in_manage,
        test_protective_exits_priority,
        test_brain_adaptive_precedence,
        test_telemetry_csv_fields,
        test_sell_populates_adaptive_fields,
        test_dashboard_exposes_adaptive_fields,
        test_copy_top_traders_unchanged,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
                print(f"  {test.__name__}: FAIL")
        except Exception as e:
            failed += 1
            print(f"  {test.__name__}: ERROR - {e}")
    print(f"\nAdaptive Holding Tests: {passed} passed, {failed} failed")
    return failed == 0

if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)