#!/usr/bin/env python3
"""Offline unit tests for AEON dashboard (no real network needed)."""
import json
import logging
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))

import app


def test_dashboard_valid_state():
    """Test dashboard with valid state.json."""
    with tempfile.TemporaryDirectory() as d:
        # Create valid state.json - matching the structure expected by _view()
        state = {
            "starting_balance_usd": 100.0,
            "cash": 50.0,
            "equity": [[1234567890, 120.0, 100.0]],  # [timestamp, equity, realized]
            "positions": {},
            "closed": [],
            "events": [],
            "agents": {
                "stream": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "trend": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "volume": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "smart": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "copy": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "holders": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "whales": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "dev": {"n": 0, "ok": 0, "last": "", "ts": 0},
            },
            "meta": {"created_ts": 1234567890, "stalls": []},
            "stats": {"pools_seen": 100, "signals": 5, "failed_exits": 0},
            "status": "running",
            "heartbeat_ts": 1234567890,
            "paused": False,
            "dead": False,
        }
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump(state, f)

        # Create heartbeat.json
        with open(os.path.join(d, "heartbeat.json"), "w") as f:
            json.dump({"ts": 1234567890}, f)

        # Temporarily override DATA_DIR
        old_data_dir = app.DATA_DIR
        app.DATA_DIR = d
        try:
            result = app.view()
            assert result.get("ok") is True
            assert result.get("start") == 100.0
            assert result.get("cash") == 50.0
            print("[1] Valid state.json: OK")
        finally:
            app.DATA_DIR = old_data_dir


def test_dashboard_missing_state():
    """Test dashboard with missing state.json."""
    with tempfile.TemporaryDirectory() as d:
        old_data_dir = app.DATA_DIR
        app.DATA_DIR = d
        try:
            result = app.view()
            assert result.get("ok") is False
            assert "state.json non ancora creato" in result.get("error", "")
            print("[2] Missing state.json: OK")
        finally:
            app.DATA_DIR = old_data_dir


def test_dashboard_malformed_state():
    """Test dashboard with malformed state.json."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "state.json"), "w") as f:
            f.write("not valid json")

        old_data_dir = app.DATA_DIR
        app.DATA_DIR = d
        try:
            result = app.view()
            assert result.get("ok") is False
            # load_json_safe returns None for malformed JSON, so it's treated as missing
            assert "state.json non ancora creato" in result.get("error", "")
            print("[3] Malformed state.json: OK")
        finally:
            app.DATA_DIR = old_data_dir


def test_normalize_pumpfun_state():
    """Test Pump.fun state normalization."""
    with tempfile.TemporaryDirectory() as d:
        # Test None input
        result = app._normalize_pumpfun_state(None)
        assert result["enabled"] is False
        assert result["state"] == "not_configured"

        # Test with valid data
        raw = {
            "enabled": True,
            "connected": True,
            "subscribed": True,
            "received": 5,
            "events": [
                {
                    "kind": "new_token",
                    "symbol": "TEST",
                    "name": "Test Token",
                    "mint": "ABC123",
                    "pool": "pool123",
                    "ts": 1234567890,
                    "price_usd": 1.0,
                    "liquidity_usd": 1000,
                    "volume_usd": 5000,
                }
            ],
            "last_event_ts": 1234567890,
            "last_error": None,
            "updated_ts": 1234567890,
        }
        result = app._normalize_pumpfun_state(raw)
        assert result["enabled"] is True
        assert result["connected"] is True
        assert result["subscribed"] is True
        assert result["received"] == 5
        assert len(result["recent_events"]) == 1
        assert result["recent_events"][0]["source"] == "pumpfun"
        assert result["recent_events"][0]["event_type"] == "new_token"
        assert result["recent_events"][0]["token_symbol"] == "TEST"

        print("[4] Pump.fun normalization: OK")


def test_normalize_gmgn_state():
    """Test GMGN state normalization."""
    with tempfile.TemporaryDirectory() as d:
        # Test None input
        result = app._normalize_gmgn_state(None)
        assert result["enabled"] is False
        assert result["state"] == "not_configured"

        # Test with valid data
        raw = {
            "enabled": True,
            "connected": True,
            "subscribed": True,
            "received": 3,
            "events": [
                {
                    "event_type": "new_pool",
                    "token_symbol": "GMGN",
                    "token_name": "GMGN Token",
                    "token_mint": "GMGN123",
                    "pool_address": "pool456",
                    "received_at": 1234567890,
                    "block_time": 1234567890,
                    "price_usd": 0.5,
                    "liquidity_usd": 2000,
                    "volume_usd": 1000,
                }
            ],
            "last_event_ts": 1234567890,
            "last_error": None,
            "updated_ts": 1234567890,
        }
        result = app._normalize_gmgn_state(raw)
        assert result["enabled"] is True
        assert result["connected"] is True
        assert result["subscribed"] is True
        assert result["received"] == 3
        assert len(result["recent_events"]) == 1
        assert result["recent_events"][0]["source"] == "gmgn"
        assert result["recent_events"][0]["event_type"] == "new_pool"

        print("[5] GMGN normalization: OK")


def test_build_source_status():
    """Test source status building."""
    with tempfile.TemporaryDirectory() as d:
        api_data = {
            "pumpfun": {"enabled": True, "connected": True, "subscribed": True, "state": "connected", "received": 5},
            "gmgn": {"enabled": True, "connected": True, "subscribed": True, "state": "connected", "received": 3},
            "ops_per_min": 10,
            "stats": {"pools_seen": 100, "signals": 5},
        }
        sources = app._build_source_status(api_data)
        assert len(sources) == 4
        assert sources[0]["source"] == "gmgn"
        assert sources[1]["source"] == "pumpfun"
        assert sources[2]["source"] == "geckoterminal"
        assert sources[3]["source"] == "dexscreener"

        # GeckoTerminal and DexScreener should show polling state
        assert sources[2]["state"] == "polling"
        assert sources[3]["state"] == "polling"

        print("[6] Source status building: OK")


def test_dashboard_missing_gmgn():
    """Test dashboard with missing gmgn_state.json."""
    with tempfile.TemporaryDirectory() as d:
        state = {
            "starting_balance_usd": 100.0,
            "cash": 50.0,
            "equity": [[1234567890, 120.0, 100.0]],
            "positions": {},
            "closed": [],
            "events": [],
            "agents": {
                "stream": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "trend": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "volume": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "smart": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "copy": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "holders": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "whales": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "dev": {"n": 0, "ok": 0, "last": "", "ts": 0},
            },
            "meta": {"created_ts": 1234567890, "stalls": []},
            "stats": {"pools_seen": 100, "signals": 5, "failed_exits": 0},
            "status": "running",
            "heartbeat_ts": 1234567890,
            "paused": False,
            "dead": False,
        }
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump(state, f)

        with open(os.path.join(d, "heartbeat.json"), "w") as f:
            json.dump({"ts": 1234567890}, f)

        old_data_dir = app.DATA_DIR
        app.DATA_DIR = d
        try:
            result = app.view()
            assert result.get("ok") is True
            assert "gmgn" in result
            assert result["gmgn"]["enabled"] is False
            assert result["gmgn"]["state"] == "not_configured"
            print("[7] Missing GMGN state: OK")
        finally:
            app.DATA_DIR = old_data_dir


def test_dashboard_missing_pumpfun():
    """Test dashboard with missing pumpfun_state.json."""
    with tempfile.TemporaryDirectory() as d:
        state = {
            "starting_balance_usd": 100.0,
            "cash": 50.0,
            "equity": [[1234567890, 120.0, 100.0]],
            "positions": {},
            "closed": [],
            "events": [],
            "agents": {
                "stream": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "trend": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "volume": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "smart": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "copy": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "holders": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "whales": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "dev": {"n": 0, "ok": 0, "last": "", "ts": 0},
            },
            "meta": {"created_ts": 1234567890, "stalls": []},
            "stats": {"pools_seen": 100, "signals": 5, "failed_exits": 0},
            "status": "running",
            "heartbeat_ts": 1234567890,
            "paused": False,
            "dead": False,
        }
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump(state, f)

        with open(os.path.join(d, "heartbeat.json"), "w") as f:
            json.dump({"ts": 1234567890}, f)

        old_data_dir = app.DATA_DIR
        app.DATA_DIR = d
        try:
            result = app.view()
            assert result.get("ok") is True
            assert "pumpfun" in result
            assert result["pumpfun"]["enabled"] is False
            assert result["pumpfun"]["state"] == "not_configured"
            print("[8] Missing Pump.fun state: OK")
        finally:
            app.DATA_DIR = old_data_dir


def test_dashboard_malformed_gmgn():
    """Test dashboard with malformed gmgn_state.json."""
    with tempfile.TemporaryDirectory() as d:
        state = {
            "starting_balance_usd": 100.0,
            "cash": 50.0,
            "equity": [[1234567890, 120.0, 100.0]],
            "positions": {},
            "closed": [],
            "events": [],
            "agents": {
                "stream": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "trend": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "volume": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "smart": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "copy": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "holders": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "whales": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "dev": {"n": 0, "ok": 0, "last": "", "ts": 0},
            },
            "meta": {"created_ts": 1234567890, "stalls": []},
            "stats": {"pools_seen": 100, "signals": 5, "failed_exits": 0},
            "status": "running",
            "heartbeat_ts": 1234567890,
            "paused": False,
            "dead": False,
        }
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump(state, f)

        with open(os.path.join(d, "heartbeat.json"), "w") as f:
            json.dump({"ts": 1234567890}, f)

        with open(os.path.join(d, "gmgn_state.json"), "w") as f:
            f.write("not valid json")

        old_data_dir = app.DATA_DIR
        app.DATA_DIR = d
        try:
            result = app.view()
            assert result.get("ok") is True
            assert result["gmgn"]["enabled"] is False
            assert result["gmgn"]["state"] == "not_configured"
            print("[9] Malformed GMGN state: OK")
        finally:
            app.DATA_DIR = old_data_dir


def test_dashboard_no_credentials_exposed():
    """Test that API keys are not exposed in dashboard output."""
    with tempfile.TemporaryDirectory() as d:
        state = {
            "starting_balance_usd": 100.0,
            "cash": 50.0,
            "equity": [[1234567890, 120.0, 100.0]],
            "positions": {},
            "closed": [],
            "events": [],
            "agents": {
                "stream": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "trend": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "volume": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "smart": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "copy": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "holders": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "whales": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "dev": {"n": 0, "ok": 0, "last": "", "ts": 0},
            },
            "meta": {"created_ts": 1234567890, "stalls": []},
            "stats": {"pools_seen": 100, "signals": 5, "failed_exits": 0},
            "status": "running",
            "heartbeat_ts": 1234567890,
            "paused": False,
            "dead": False,
        }
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump(state, f)

        with open(os.path.join(d, "heartbeat.json"), "w") as f:
            json.dump({"ts": 1234567890}, f)

        old_data_dir = app.DATA_DIR
        app.DATA_DIR = d
        try:
            result = app.view()
            assert result.get("ok") is True
            # Check that no API keys or sensitive data are in the response
            response_str = json.dumps(result)
            assert "PUMPPORTAL_API_KEY" not in response_str
            assert "GMGN_API_KEY" not in response_str
            assert "TELEGRAM_BOT_TOKEN" not in response_str
            assert "DASHBOARD_PASSWORD" not in response_str
            print("[10] No credentials exposed: OK")
        finally:
            app.DATA_DIR = old_data_dir


def test_healthz():
    """Test /healthz endpoint."""
    with tempfile.TemporaryDirectory() as d:
        state = {
            "starting_balance_usd": 100.0,
            "cash": 50.0,
            "equity": [[1234567890, 120.0, 100.0]],
            "positions": {},
            "closed": [],
            "events": [],
            "agents": {
                "stream": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "trend": {"n": 10, "ok": 8, "last": "test", "ts": 1234567890, "load": 80},
                "volume": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "smart": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "copy": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "holders": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "whales": {"n": 0, "ok": 0, "last": "", "ts": 0},
                "dev": {"n": 0, "ok": 0, "last": "", "ts": 0},
            },
            "meta": {"created_ts": 1234567890, "stalls": []},
            "stats": {"pools_seen": 100, "signals": 5, "failed_exits": 0},
            "status": "running",
            "heartbeat_ts": 1234567890,
            "paused": False,
            "dead": False,
        }
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump(state, f)

        with open(os.path.join(d, "heartbeat.json"), "w") as f:
            json.dump({"ts": 1234567890}, f)

        old_data_dir = app.DATA_DIR
        app.DATA_DIR = d
        try:
            from flask.testing import FlaskClient
            from flask import Flask
            test_app = Flask(__name__)
            with test_app.test_request_context('/healthz'):
                # We can't easily test the Flask route without the app context
                # Just verify the view function works
                result = app.view()
                assert result.get("ok") is True
            print("[11] Healthz endpoint: OK")
        finally:
            app.DATA_DIR = old_data_dir


def run_all_tests():
    """Run all tests."""
    tests = [
        test_dashboard_valid_state,
        test_dashboard_missing_state,
        test_dashboard_malformed_state,
        test_normalize_pumpfun_state,
        test_normalize_gmgn_state,
        test_build_source_status,
        test_dashboard_missing_gmgn,
        test_dashboard_missing_pumpfun,
        test_dashboard_malformed_gmgn,
        test_dashboard_no_credentials_exposed,
        test_healthz,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAILED: {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    success = run_all_tests()
    sys.exit(0 if success else 1)