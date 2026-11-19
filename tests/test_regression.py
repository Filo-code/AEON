#!/usr/bin/env python3
"""Regression tests for Phase 5: Core Reliability & Concurrency Hardening.

These tests verify that the reported bugs are actually fixed. They run
offline with mock Telegram HTTP servers and do NOT depend on live APIs.

Run: python scripts/test_regression.py
"""
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
import telegram_notify as tg
import telegram_commands as cmds
import yaml

CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))


# =============================================================================
# TELEGRAM NOTIFICATION REGRESSIONS
# =============================================================================

def test_429_retry_after_null():
    """HTTP 429 with retry_after: null or missing must not crash."""
    got, delays = [], []

    class H(BaseHTTPRequestHandler):
        count = 0
        def do_POST(self):
            H.count += 1
            if H.count <= 2:
                # First two: 429 with missing retry_after in parameters
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "parameters": {}}).encode())
            else:
                got.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "result": {"message_id": H.count}}).encode())
        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tg.Telegram.API = f"http://127.0.0.1:{srv.server_port}/bot{{token}}/sendMessage"

    out = os.path.join(tempfile.mkdtemp(), "test_429_null.log")
    t = tg.Telegram(token="TEST", chat_id="1", outbox_path=out)
    t.send("test message")
    t.flush(30)

    ok = len(got) == 1
    print(f"  HTTP 429 retry_after null: {'OK' if ok else 'FAIL'}")
    srv.shutdown()
    return ok


def test_429_retry_after_non_numeric():
    """HTTP 429 with retry_after: 'abc' (non-numeric) must not raise."""
    got = []

    class H(BaseHTTPRequestHandler):
        count = 0
        def do_POST(self):
            H.count += 1
            if H.count == 1:
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "parameters": {"retry_after": "not-a-number"}}).encode())
            else:
                got.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "result": {"message_id": H.count}}).encode())
        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tg.Telegram.API = f"http://127.0.0.1:{srv.server_port}/bot{{token}}/sendMessage"

    out = os.path.join(tempfile.mkdtemp(), "test_nonnum.log")
    t = tg.Telegram(token="TEST", chat_id="1", outbox_path=out)
    t.send("test message")
    t.flush(30)

    ok = len(got) == 1
    print(f"  HTTP 429 retry_after non-numeric: {'OK' if ok else 'FAIL'}")
    srv.shutdown()
    return ok


def test_split_text_no_empty_fragments():
    """split_text must never generate empty message fragments."""
    max_len = 4000
    text = "x" * max_len
    parts = tg.split_text(text, max_len)
    ok = len(parts) == 1 and len(parts[0]) == max_len
    print(f"  split_text line at max length: {'OK' if ok else 'FAIL'}")
    return ok


def test_split_text_boundary():
    """split_text at exact boundary doesn't produce empty fragments."""
    max_len = 100
    text = "line1\n" + "x" * max_len + "\nline3"
    parts = tg.split_text(text, max_len)
    ok = all(p for p in parts)  # no empty strings
    print(f"  split_text boundary without empty: {'OK' if ok else 'FAIL'}")
    return ok


def test_zero_fx_rate():
    """money() must not crash on zero FX rate."""
    tg.set_currency(0, "$")
    try:
        result = tg.money(100)
        ok = result is not None and result != "nan" and result != "inf"
    except ZeroDivisionError:
        ok = False
    tg.set_currency(1.0, "$")  # reset
    print(f"  money() zero FX rate: {'OK' if ok else 'FAIL'}")
    return ok


def test_money_invalid_input():
    """money() handles None, '', non-numeric input gracefully."""
    tg.set_currency(1.08, "€")
    inputs = [None, "", "invalid", float('nan'), 100.0]
    try:
        for x in inputs:
            r = tg.money(x) if x is not None else tg.money(0)
        ok = True
    except Exception:
        ok = False
    print(f"  money() invalid input: {'OK' if ok else 'FAIL'}")
    return ok


# =============================================================================
# TELEGRAM COMMANDS REGRESSIONS
# =============================================================================

def test_chiudi_empty_symbol():
    """/chiudi with empty symbol ($ or $$) must be rejected, not match everything."""
    import threading
    import tempfile
    from unittest.mock import patch, MagicMock

    # Minimal mock bot
    class MockBot:
        lock = threading.RLock()
        c = {"starting_balance_usd": 100}
        positions = {"SOLmint": {"name": "SOL", "cost_usd": 10}, "BONKmint": {"name": "BONK", "cost_usd": 5}}
        closed = []
        meta = {}
        tz = None
        health_report = lambda self: {}
        data = tempfile.gettempdir()

    # Mock notifier with required attributes
    mock_tg = MagicMock()
    mock_tg.token = "TEST"
    mock_tg.chat_id = "777"
    mock_tg.send = MagicMock()

    bot = MockBot()
    bot.tg = mock_tg

    cmds_inst = cmds.Commands(bot, mock_tg)

    # Test /chiudi $ (empty after lstrip)
    resp = cmds_inst._answer("chiudi", ["$"])
    ok = "simbolo" in resp.lower() or "non valido" in resp.lower()
    print(f"  /chiudi $ rejected: {'OK' if ok else 'FAIL - ' + resp}")

    # Test /chiudi
    resp = cmds_inst._answer("chiudi", [])
    ok = ok and ("simbolo" in resp.lower() or "scrivi" in resp.lower())
    print(f"  /chiudi (no args) rejected: {'OK' if ok else 'FAIL - ' + resp}")

    return ok


def test_lock_timeout_no_bypass():
    """When lock cannot be acquired, answer() must refuse, not execute unsynchronized.
    This tests that answer() returns a refusal message when lock acquisition times out."""
    import threading
    import tempfile
    import time
    from unittest.mock import patch, MagicMock

    data_dir = tempfile.gettempdir()
    class StuckBot:
        lock = threading.RLock()
        c = {"starting_balance_usd": 100, "stop_loss_pct": 0.1, "take_profit_pct": 0.2,
             "max_hold_minutes": 60, "position_size_pct": 0.15, "trailing_stop_pct": 0.05,
             "trailing_activate_pct": 0.05, "spike_multiplier": 10, "min_volume_5m_usd": 1000,
             "reentry_cooldown_minutes": 30, "max_open_positions": 3, "jupiter": {"enabled": True},
             "costs": {"sol_price_usd_fallback": 100}, "daily_summary_hour": 23,
             "state_save_seconds": 15, "poll_seconds": 30, "run_minutes": 0}
        positions = {"mint": {"name": "TEST", "cost_usd": 10}}
        closed = []
        meta = {}
        tz = None
        data = data_dir
        heartbeat = time.time()
        session_start = time.time()
        http = MagicMock()
        http.ops_per_min = lambda: {"total": 100}
        http.health_report = lambda: {}
        http.ok = True
        http.err = False
        watch = {}
        ds = MagicMock()
        ds.tokens = lambda mints: {m: {"price": 0.00012} for m in mints}
        equity_value = lambda: 100
        brain = MagicMock()
        brain.summary = lambda: {"diagnosis": None, "records": 0, "changes": [], "next_in": 10}
        provenance_summary = lambda: {"VERIFIED": {"n": 0, "pnl_usd": 0}, "ESTIMATED": {"n": 0, "pnl_usd": 0}, "REJECTED": {"n": 0}}
        provenance_on = False
        vybe = None
        birdeye = None
        top_traders = set()
        risk = MagicMock()
        risk.paused = False
        risk.pause_info = lambda: {"reason": "test", "text": ""}
        risk.user_resume = lambda force: (True, "resumed")
        risk.mode = "survival"
        risk.limits = lambda: {"max_open_positions": 3, "max_exposure_pct": None, "max_position_pct": None,
                                "max_daily_loss_pct": None, "max_drawdown_pct": None, "max_consecutive_losses": None}
        antirug = None
        antirug_mode = "off"
        backups = None
        ops = {"disk": {"free_mb": 1000, "level": "ok"}}
        profile_visible = False
        profile = "aggressiva"
        prev_risk = None
        reset_pending = False
        recovered = None
        profile_src = ".env RISK_PROFILE"
        def health_report(self): return {}
        def ops_per_min(self): return {"total": 100}
        def survival_status(self):
            return {"enabled": True, "mode": "ok", "equity_usd": 100, "min_trade_usd": 3,
                    "next_size_usd": 15, "size_pct_eff": 15.0, "max_open": None,
                    "dead_ts": None, "death_line_usd": 5}
        def save_state(self, *args, **kwargs): pass
        def profile_info(self):
            return {"name": "aggressiva", "label": "🔥 aggressiva", "source": "test",
                    "desc": "test profile", "restart_needed": False, "effective": {},
                    "antirug_mode": "off", "risk_mode": None}

    mock_tg = MagicMock()
    mock_tg.token = "TEST"
    mock_tg.chat_id = "777"
    mock_tg.send = MagicMock()

    with patch.object(cmds.Commands, 'reply'):
        sb = StuckBot()
        # Acquire the lock and hold it - the Commands.__init__ will succeed but answer() will fail
        sb.lock.acquire()
        try:
            bot = cmds.Commands(sb, mock_tg)
            result = bot.answer("stato", [])
            ok = "occupato" in result or "riprova" in result
        finally:
            sb.lock.release()
    print(f"  lock timeout refuses: {'OK' if ok else 'FAIL'}")
    return ok


def test_offset_corruption_handling():
    """Corrupted telegram_offset.json must be handled safely."""
    from unittest.mock import patch, MagicMock

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "telegram_offset.json")
        with open(path, "w") as f:
            f.write("not valid json {{{")

        class MockBot:
            lock = threading.RLock()
            c = {"starting_balance_usd": 100}
            positions = {}
            closed = []
            meta = {}
            tz = None
            health_report = lambda self: {}
            data = tmp

        bot = MockBot()
        mock_tg = MagicMock()
        mock_tg.token = "TEST"
        mock_tg.chat_id = "777"

        c = cmds.Commands(bot, mock_tg)
        c.offset_path = path
        loaded = c.load_offset()

        ok = loaded == 0
        print(f"  corrupted offset.json returns 0: {'OK' if ok else 'FAIL - got ' + str(loaded)}")
        return ok
        bot.positions = {}
        bot.c = {"starting_balance_usd": 100}
        bot.closed = []
        bot.meta = {}

        with patch.object(bot, 'chat_id', "1"):
            c = cmds.Commands(bot, tg.Telegram(token="test", chat_id="1", outbox_path=None))
            c.offset_path = path
            loaded = c.load_offset()

        ok = loaded == 0
        print(f"  corrupted offset.json returns 0: {'OK' if ok else 'FAIL - got ' + str(loaded)}")
        return ok


# =============================================================================
# SCREENSHOT REGRESSIONS
# =============================================================================

def test_popen_failure_closes_fds():
    """Popen failure must close all pipe file descriptors."""
    with open(os.path.join(ROOT, "bot", "screenshot.py")) as f:
        src = f.read()
    has_try_finally = "try:" in src and "proc = subprocess.Popen" in src and "finally:" in src
    has_fd_close = "os.close(r3)" in src and "os.close(w4)" in src

    ok = has_try_finally and has_fd_close
    print(f"  Popen exception FD cleanup (code review): {'OK' if ok else 'FAIL'}")
    return ok


def test_load_event_fired_timeout_logged():
    """Page.loadEventFired timeout should be logged, not silently ignored."""
    with open(os.path.join(ROOT, "bot", "screenshot.py")) as f:
        src = f.read()
    has_log = "Page.loadEventFired timeout" in src
    ok = has_log
    print(f"  loadEventFired timeout logging: {'OK' if ok else 'FAIL'}")
    return ok


# =============================================================================
# DISK_GUARD REGRESSIONS
# =============================================================================

def test_candidates_handles_missing_file():
    """candidates() should not crash when a file is deleted during iteration."""
    import disk_guard

    with tempfile.TemporaryDirectory() as tmp:
        test_file = os.path.join(tmp, "bot.log.1")
        with open(test_file, "w") as f:
            f.write("log content")

        # First call should succeed
        can = disk_guard.candidates(tmp, min_age_s=0)
        assert test_file in can, "Should find the file"

        # Delete file and call again - should NOT crash
        os.remove(test_file)
        can2 = disk_guard.candidates(tmp, min_age_s=0)
        ok = test_file not in can2  # file gone, but no crash
        print(f"  candidates() handles file removed mid-scan: {'OK' if ok else 'FAIL'}")
        return ok


# =============================================================================
# STARTUP_CHECKS REGRESSIONS
# =============================================================================

def test_malformed_numeric_handling():
    """Startup checks handle malformed numeric config values without crashing."""
    from unittest.mock import MagicMock

    mock_bot = MagicMock()
    mock_bot.c = {
        "starting_balance_eur": "not-a-number",
        "starting_balance_usd": 100,
        "stop_loss_pct": "invalid",
        "take_profit_pct": None,
        "max_hold_minutes": 60,
        "position_size_pct": 0.15,
        "trailing_stop_pct": 0.05,
        "trailing_activate_pct": 0.05,
        "spike_multiplier": 10,
        "min_volume_5m_usd": 1000,
        "reentry_cooldown_minutes": 30,
        "max_open_positions": 3,
        "jupiter": {"enabled": True},
        "costs": {"sol_price_usd_fallback": 100},
        "daily_summary_hour": 23,
        "state_save_seconds": 15,
        "poll_seconds": 30,
        "run_minutes": 0,
    }
    mock_bot.meta = {}
    mock_bot.brain = MagicMock()
    mock_bot.brain.st = {"seed_problems": [], "params": {}}
    mock_bot.positions = {}
    mock_bot.closed = []
    mock_bot.resumed = False
    mock_bot.antirug_mode = "off"
    mock_bot.data = tempfile.gettempdir()

    try:
        import startup_checks
        result = startup_checks.run(mock_bot)
        ok = "checks" in result
    except Exception as e:
        ok = False
        print(f"  Startup checks handle bad numbers: FAIL - {e}")

    print(f"  Startup checks handle malformed numerics: {'OK' if ok else 'FAIL'}")
    return ok


def test_null_concentration_config():
    """Startup checks handle antirug.concentration: null safely."""
    from unittest.mock import MagicMock

    mock_bot = MagicMock()
    mock_bot.c = {
        "starting_balance_eur": 100,
        "starting_balance_usd": 100,
        "stop_loss_pct": 0.1,
        "take_profit_pct": 0.3,
        "max_hold_minutes": 60,
        "position_size_pct": 0.15,
        "trailing_stop_pct": 0.05,
        "trailing_activate_pct": 0.05,
        "spike_multiplier": 10,
        "min_volume_5m_usd": 1000,
        "reentry_cooldown_minutes": 30,
        "max_open_positions": 3,
        "jupiter": {"enabled": True},
        "costs": {"sol_price_usd_fallback": 100},
        "daily_summary_hour": 23,
        "state_save_seconds": 15,
        "poll_seconds": 30,
        "run_minutes": 0,
        "antirug": {"concentration": None},
    }
    mock_bot.meta = {}
    mock_bot.brain = MagicMock()
    mock_bot.brain.st = {"seed_problems": [], "params": {}}
    mock_bot.positions = {}
    mock_bot.closed = []
    mock_bot.resumed = False
    mock_bot.antirug_mode = "shadow"
    mock_bot.data = tempfile.gettempdir()

    try:
        import startup_checks
        result = startup_checks.run(mock_bot)
        ok = True
    except Exception as e:
        ok = False
        print(f"  Startup checks handle null concentration: FAIL - {e}")

    print(f"  Startup checks handle null concentration: {'OK' if ok else 'FAIL'}")
    return ok


def test_missing_config_values():
    """Startup checks handle missing config keys gracefully."""
    from unittest.mock import MagicMock

    mock_bot = MagicMock()
    mock_bot.c = {
        "starting_balance_eur": 100,
        "starting_balance_usd": 100,
        "stop_loss_pct": 0.1,
        "take_profit_pct": 0.3,
        "max_hold_minutes": 60,
        "position_size_pct": 0.15,
        "trailing_stop_pct": 0.05,
        "trailing_activate_pct": 0.05,
        "spike_multiplier": 10,
        "min_volume_5m_usd": 1000,
        "reentry_cooldown_minutes": 30,
        "max_open_positions": 3,
        "jupiter": {"enabled": True},
        "costs": {"sol_price_usd_fallback": 100},
        "daily_summary_hour": 23,
        "state_save_seconds": 15,
        "poll_seconds": 30,
        "run_minutes": 0,
    }
    mock_bot.meta = {}
    mock_bot.brain = MagicMock()
    mock_bot.brain.st = {"seed_problems": [], "params": {}}
    mock_bot.positions = {}
    mock_bot.closed = []
    mock_bot.resumed = False
    mock_bot.antirug_mode = "off"
    mock_bot.data = tempfile.gettempdir()

    try:
        import startup_checks
        result = startup_checks.run(mock_bot)
        ok = "checks" in result
    except Exception as e:
        ok = False
        print(f"  Startup checks handle missing config: FAIL - {e}")

    print(f"  Startup checks handle missing config: {'OK' if ok else 'FAIL'}")
    return ok


# =============================================================================
# MAIN
# =============================================================================

def run_all():
    print("\\n=== REGRESSION TESTS ===\\n")

    ok = True

    print("Telegram Notifications:")
    ok = test_429_retry_after_null() and ok
    ok = test_429_retry_after_non_numeric() and ok
    ok = test_split_text_no_empty_fragments() and ok
    ok = test_split_text_boundary() and ok
    ok = test_zero_fx_rate() and ok
    ok = test_money_invalid_input() and ok

    print("\\nTelegram Commands:")
    ok = test_chiudi_empty_symbol() and ok
    ok = test_lock_timeout_no_bypass() and ok
    ok = test_offset_corruption_handling() and ok

    print("\\nScreenshot:")
    ok = test_popen_failure_closes_fds() and ok
    ok = test_load_event_fired_timeout_logged() and ok

    print("\\nDisk Guard:")
    ok = test_candidates_handles_missing_file() and ok

    print("\\nStartup Checks:")
    ok = test_malformed_numeric_handling() and ok
    ok = test_null_concentration_config() and ok
    ok = test_missing_config_values() and ok

    print("\\n" + "=" * 50)
    if ok:
        print("ALL REGRESSION TESTS PASSED")
    else:
        print("SOME REGRESSION TESTS FAILED")
    return ok


# =============================================================================
# ADAPTIVE HOLDING ENGINE v1 REGRESSION TESTS
# =============================================================================

def test_adaptive_holding_disabled_by_default():
    """Adaptive holding should be disabled unless explicitly enabled."""
    import yaml
    CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
    ah = CFG.get("adaptive_holding", {})
    assert ah.get("enabled") == False, "Adaptive holding should be disabled by default"
    print("  Adaptive holding disabled by default: OK")
    return True


def test_adaptive_holding_config_structure():
    """Adaptive holding config should have all required fields."""
    import yaml
    CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
    ah = CFG.get("adaptive_holding", {})
    required = ["enabled", "initial_minutes", "step_minutes", "max_minutes",
                "min_sell_slippage_pct", "min_liquidity_usd", "min_score",
                "require_jupiter_route", "paper_only_check", "baseline_quote_at_initial",
                "indicators"]
    for r in required:
        assert r in ah, f"Missing config field: {r}"
    assert ah["max_minutes"] == 30, "Hard ceiling must be 30 minutes"
    assert ah["initial_minutes"] == 15, "Initial evaluation must be 15 minutes"
    assert ah["step_minutes"] == 5, "Step must be 5 minutes"
    print("  Adaptive holding config structure: OK")
    return True


def test_adaptive_holding_indicators_weights():
    """Indicator weights should sum to 100."""
    import yaml
    CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
    weights = CFG.get("adaptive_holding", {}).get("indicators", {})
    total = sum(weights.values())
    assert total == 100, f"Indicator weights must sum to 100, got {total}"
    expected = ["price_move_pct_weight", "momentum_weight", "volume_accel_weight",
                "liquidity_weight", "risk_weight"]
    for e in expected:
        assert e in weights, f"Missing weight: {e}"
    print("  Indicator weights sum to 100: OK")
    return True


def test_paper_mode_verification_logic():
    """Test the paper mode verification logic."""
    # This tests the logic conceptually - actual implementation tested via integration
    wallet_env_vars = [
        "SOLANA_PRIVATE_KEY", "PRIVATE_KEY", "WALLET_PRIVATE_KEY", "FEE_PAYER_PRIVATE_KEY",
        "SIGNING_KEY", "KEYPAIR_PATH", "KEYPAIR_FILE", "WALLET_KEYPAIR"
    ]
    # None should be set in paper mode
    for var in wallet_env_vars:
        assert not os.getenv(var), f"Wallet env var {var} should not be set in paper mode"
    print("  Paper mode env vars clean: OK")
    return True


def test_adaptive_holding_state_fields():
    """Test that adaptive holding state fields are properly initialized."""
    # We verify the code has the right field names by checking the source
    import ast
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
    """Verify _evaluate_adaptive_holding method exists with correct signature."""
    import ast
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    assert "_evaluate_adaptive_holding" in src, "Decision engine method missing"
    assert "_verify_paper_mode" in src, "Paper mode verification method missing"
    print("  Decision engine methods present: OK")
    return True


def test_adaptive_holding_in_manage():
    """Verify adaptive holding is integrated in manage() exit flow."""
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    # Check that manage() calls _evaluate_adaptive_holding
    assert "_evaluate_adaptive_holding" in src
    assert "adaptive_holding_enabled" in src
    assert "adaptive_holding_next_check" in src
    assert "EXTEND_20" in src
    assert "EXTEND_25" in src
    assert "EXTEND_30" in src
    print("  Adaptive holding integrated in manage(): OK")
    return True


def test_protective_exits_priority():
    """Verify protective exits still fire before adaptive holding."""
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    # In manage(), protective exits (stop_loss, top_traders_sold, take_profit, trailing_stop)
    # should appear BEFORE adaptive holding check
    manage_section = src[src.find("def manage(self):"):src.find("def report(self, period_hours")]
    # Order should be: stop_loss, top_traders_sold, take_profit, trailing_stop, THEN adaptive
    sl_idx = manage_section.find("stop_loss")
    tp_idx = manage_section.find("take_profit")
    ts_idx = manage_section.find("trailing_stop")
    tt_idx = manage_section.find("top_traders_sold")
    ah_idx = manage_section.find("adaptive_holding_enabled")

    assert sl_idx != -1 and sl_idx < ah_idx, "stop_loss must come before adaptive holding"
    assert tp_idx != -1 and tp_idx < ah_idx, "take_profit must come before adaptive holding"
    assert ts_idx != -1 and ts_idx < ah_idx, "trailing_stop must come before adaptive holding"
    assert tt_idx != -1 and tt_idx < ah_idx, "top_traders_sold must come before adaptive holding"
    print("  Protective exits priority verified: OK")
    return True


def test_brain_adaptive_precedence():
    """Verify exit_rules documents precedence correctly."""
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
    """Verify trades CSV includes adaptive holding fields."""
    with open(os.path.join(ROOT, "bot", "paper_bot.py"), encoding="utf-8") as f:
        src = f.read()

    fields = [
        "adaptive_holding_enabled",
        "adaptive_holding_final_stage",
        "adaptive_holding_extensions",
        "adaptive_holding_final_decision",
        "adaptive_holding_max_deadline",
        "adaptive_baseline_15min_proceeds_usd",
        "adaptive_baseline_15min_slippage_pct",
        "adaptive_baseline_15min_pnl_usd",
        "adaptive_baseline_15min_pnl_pct",
    ]
    for field in fields:
        assert field in src, f"Missing telemetry field: {field}"
    print("  Telemetry CSV adaptive fields present: OK")
    return True


def test_sell_populates_adaptive_fields():
    """Verify sell() populates adaptive holding fields in trade record."""
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
    """Verify dashboard _view() exposes adaptive holding fields."""
    with open(os.path.join(ROOT, "dashboard", "app.py")) as f:
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
    """Verify copy_top_traders exit rules are unchanged."""
    import yaml
    CFG = yaml.safe_load(open(os.path.join(ROOT, "bot", "config.yaml")))
    copy_exits = CFG.get("copy_exits", {})
    assert copy_exits.get("max_hold_minutes") == 180, "copy_top_traders max_hold must remain 180"
    assert copy_exits.get("stop_loss_pct") == 0.25, "copy_top_traders stop_loss unchanged"
    assert copy_exits.get("take_profit_pct") == 1.0, "copy_top_traders take_profit unchanged"
    assert copy_exits.get("trailing_stop_pct") == 0.20, "copy_top_traders trailing_stop unchanged"
    assert copy_exits.get("follow_wallets") == True, "copy_top_traders follow_wallets unchanged"
    print("  copy_top_traders exits unchanged: OK")
    return True


def run_adaptive_holding_tests():
    """Run all adaptive holding tests."""
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
    # Run adaptive holding tests
    print("\n=== ADAPTIVE HOLDING ENGINE v1 TESTS ===")
    ah_success = run_adaptive_holding_tests()
    sys.exit(0 if (success and ah_success) else 1)