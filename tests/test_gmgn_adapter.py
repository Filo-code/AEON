#!/usr/bin/env python3
"""Offline unit tests for GMGN Intelligence Adapter (no real network needed)."""
import json
import logging
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

from gmgn_adapter import GMGNIntelligenceAdapter, GMGNEvent, create_gmgn_adapter_from_env


def test_gmgn_adapter_init():
    """Test GMGNIntelligenceAdapter initialization."""
    with tempfile.TemporaryDirectory() as d:
        adapter = GMGNIntelligenceAdapter(
            data_dir=d,
            gmgn_api_key="",
            enabled=True,
            max_events=200,
        )

        assert adapter.enabled is True
        assert adapter.ws_enabled is True
        assert adapter.agent_enabled is False
        assert adapter.agent_client is None
        assert adapter.ws_scanner is not None
        assert os.path.exists(os.path.join(d, "gmgn_state.json"))
        print("[1] GMGNIntelligenceAdapter init (no API key): OK")


def test_gmgn_adapter_init_with_api_key():
    """Test GMGNIntelligenceAdapter initialization with API key."""
    with tempfile.TemporaryDirectory() as d:
        adapter = GMGNIntelligenceAdapter(
            data_dir=d,
            gmgn_api_key="test-key",
            enabled=True,
            max_events=200,
        )

        assert adapter.enabled is True
        assert adapter.agent_enabled is True
        assert adapter.agent_client is not None
        assert adapter.agent_client.config.api_key == "test-key"
        print("[2] GMGNIntelligenceAdapter init (with API key): OK")


def test_gmgn_event_normalization():
    """Test GMGNEvent normalization with extended signal types."""
    event = GMGNEvent(
        event_id="test_1",
        source="gmgn_agent",
        event_type="trending",
        chain="solana",
        token_mint="ABC123",
        token_symbol="TEST",
        token_name="Test Token",
        smart_money_buy=True,
        kol_buy=False,
        signal_type=1,
        # Extended signal types (all default to False)
        is_insider=False,
        is_bundler=False,
        is_rat_trader=False,
        is_sniper=False,
        is_dev_wallet=False,
        is_team_wallet=False,
        is_cex_wallet=False,
        is_mev_bot=False,
    )

    d = event.__dict__
    assert d["event_id"] == "test_1"
    assert d["source"] == "gmgn_agent"
    assert d["event_type"] == "trending"
    assert d["chain"] == "solana"
    assert d["token_mint"] == "ABC123"
    assert d["token_symbol"] == "TEST"
    assert d["smart_money_buy"] is True
    assert d["kol_buy"] is False
    assert d["signal_type"] == 1
    assert d["is_insider"] is False
    assert d["is_bundler"] is False
    assert d["is_rat_trader"] is False
    assert d["is_sniper"] is False
    assert d["is_dev_wallet"] is False
    assert d["is_team_wallet"] is False
    assert d["is_cex_wallet"] is False
    assert d["is_mev_bot"] is False
    print("[3] GMGNEvent normalization: OK")


def test_gmgn_extended_signal_types():
    """Test GMGNEvent with each extended signal type (manual flag setting)."""
    signal_types = [
        (11, "is_insider"),
        (12, "is_bundler"),
        (13, "is_rat_trader"),
        (17, "is_sniper"),
        (18, "is_dev_wallet"),
        (19, "is_team_wallet"),
        (20, "is_cex_wallet"),
        (21, "is_mev_bot"),
    ]
    for signal_type, expected_flag in signal_types:
        event = GMGNEvent(
            event_id=f"test_{signal_type}",
            source="gmgn_agent",
            event_type="signal",
            chain="solana",
            token_mint="ABC123",
            signal_type=signal_type,
            # Manually set the expected flag (normalization does this)
            **{expected_flag: True}
        )
        d = event.__dict__
        assert d["signal_type"] == signal_type
        assert d[expected_flag] is True, f"Expected {expected_flag} to be True for signal_type {signal_type}"
        # All other extended flags should be False
        for _, flag in signal_types:
            if flag != expected_flag:
                assert d[flag] is False, f"Expected {flag} to be False for signal_type {signal_type}"
    print("[3b] GMGNEvent extended signal types: OK")


def test_gmgn_adapter_get_status():
    """Test get_status method."""
    with tempfile.TemporaryDirectory() as d:
        adapter = GMGNIntelligenceAdapter(
            data_dir=d,
            gmgn_api_key="",
            enabled=True,
        )

        status = adapter.get_status()

        assert "enabled" in status
        assert "ws" in status
        assert "agent" in status
        assert "combined" in status
        assert status["ws"]["status"] == "disconnected"
        assert status["agent"]["enabled"] is False
        print("[4] GMGNIntelligenceAdapter get_status: OK")


def test_gmgn_adapter_get_all_events():
    """Test get_all_events with mocked data."""
    with tempfile.TemporaryDirectory() as d:
        adapter = GMGNIntelligenceAdapter(
            data_dir=d,
            gmgn_api_key="",
            enabled=True,
        )

        # Add a mock agent event
        mock_event = GMGNEvent(
            event_id="test_agent_1",
            source="gmgn_agent",
            event_type="trending",
            chain="solana",
            token_mint="ABC123",
            token_symbol="TEST",
            received_at=time.time(),
        )
        adapter.events.append(mock_event)

        # Mock WS events
        adapter.ws_scanner.events.append({
            "event_id": "test_ws_1",
            "event_type": "new_pool",
            "chain": "solana",
            "token_mint": "XYZ789",
            "token_symbol": "WS",
            "received_at": time.time(),
        })

        events = adapter.get_all_events(limit=10)
        assert len(events) == 2
        # Should be sorted by received_at descending
        assert events[0]["event_id"] in ("test_agent_1", "test_ws_1")
        print("[5] GMGNIntelligenceAdapter get_all_events: OK")


def test_create_gmgn_adapter_from_env():
    """Test factory function with environment variables."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["GMGN_API_KEY"] = "test-key"
        os.environ["GMGN_ENABLED"] = "1"

        adapter = create_gmgn_adapter_from_env(d)

        assert adapter is not None
        assert adapter.enabled is True
        assert adapter.agent_enabled is True

        os.environ.pop("GMGN_API_KEY", None)
        os.environ.pop("GMGN_ENABLED", None)
        print("[6] create_gmgn_adapter_from_env: OK")


def test_create_gmgn_adapter_from_env_disabled():
    """Test factory function with GMGN disabled."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["GMGN_ENABLED"] = "0"

        adapter = create_gmgn_adapter_from_env(d)

        assert adapter is None

        os.environ.pop("GMGN_ENABLED", None)
        print("[7] create_gmgn_adapter_from_env (disabled): OK")


def test_gmgn_adapter_no_api_key():
    """Test adapter creation without API key."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["GMGN_ENABLED"] = "1"
        os.environ.pop("GMGN_API_KEY", None)

        adapter = create_gmgn_adapter_from_env(d)

        assert adapter is not None
        assert adapter.enabled is True
        assert adapter.agent_enabled is False
        assert adapter.ws_scanner is not None

        os.environ.pop("GMGN_ENABLED", None)
        print("[8] GMGNIntelligenceAdapter without API key: OK")


def test_gmgn_signal_normalization_extended():
    """Test _normalize_agent_signals correctly classifies extended signal types."""
    with tempfile.TemporaryDirectory() as d:
        adapter = GMGNIntelligenceAdapter(
            data_dir=d,
            gmgn_api_key="test-key",
            enabled=True,
            max_events=200,
        )

        # Test each extended signal type
        signal_test_cases = [
            (11, "is_insider"),
            (12, "is_bundler"),
            (13, "is_rat_trader"),
            (17, "is_sniper"),
            (18, "is_dev_wallet"),
            (19, "is_team_wallet"),
            (20, "is_cex_wallet"),
            (21, "is_mev_bot"),
        ]

        for signal_type, expected_flag in signal_test_cases:
            # Clear events
            adapter.events.clear()

            # Mock signal payload
            mock_data = [{
                "tokens": [{
                    "token_address": "ABC123",
                    "symbol": "TEST",
                    "name": "Test Token",
                    "wallet_address": "WALLET123",
                    "signature": "SIG123",
                    "signal_type": signal_type,
                    "strength": 1000,
                    "amount_usd": 5000,
                }]
            }]

            adapter._normalize_agent_signals(mock_data)

            assert len(adapter.events) == 1
            event = adapter.events[0]
            assert event.signal_type == signal_type
            assert getattr(event, expected_flag) is True, f"Expected {expected_flag} to be True for signal_type {signal_type}"
            # Verify other extended flags are False
            for _, flag in signal_test_cases:
                if flag != expected_flag:
                    assert getattr(event, flag) is False, f"Expected {flag} to be False for signal_type {signal_type}"

            # Verify original smart_money_buy and kol_buy are False for these types
            assert event.smart_money_buy is False
            assert event.kol_buy is False

        # Test smart money types (1-5)
        for smart_type in [1, 2, 3, 4, 5]:
            adapter.events.clear()
            mock_data = [{
                "tokens": [{
                    "token_address": "ABC123",
                    "signal_type": smart_type,
                }]
            }]
            adapter._normalize_agent_signals(mock_data)
            assert adapter.events[0].smart_money_buy is True
            assert adapter.events[0].kol_buy is False

        # Test KOL types (6-10)
        for kol_type in [6, 7, 8, 9, 10]:
            adapter.events.clear()
            mock_data = [{
                "tokens": [{
                    "token_address": "ABC123",
                    "signal_type": kol_type,
                }]
            }]
            adapter._normalize_agent_signals(mock_data)
            assert adapter.events[0].smart_money_buy is False
            assert adapter.events[0].kol_buy is True

        print("[9] GMGN signal normalization extended types: OK")


def run_all_tests():
    """Run all tests."""
    tests = [
        test_gmgn_adapter_init,
        test_gmgn_adapter_init_with_api_key,
        test_gmgn_event_normalization,
        test_gmgn_extended_signal_types,
        test_gmgn_adapter_get_status,
        test_gmgn_adapter_get_all_events,
        test_create_gmgn_adapter_from_env,
        test_create_gmgn_adapter_from_env_disabled,
        test_gmgn_adapter_no_api_key,
        test_gmgn_signal_normalization_extended,
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