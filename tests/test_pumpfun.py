#!/usr/bin/env python3
"""Offline unit tests for Pump.fun scanner (no real network needed)."""
import json
import logging
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

from pumpfun import PumpFunScanner


def test_pumpfun_scanner_init():
    """Test PumpFunScanner initialization and default values."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        assert scanner.enabled is True
        assert scanner.api_key == ""
        assert scanner.max_events == 200
        assert scanner.status["enabled"] is True
        assert scanner.status["state"] == "disconnected"
        assert os.path.exists(os.path.join(d, "pumpfun_state.json"))
        print("[1] PumpFunScanner init: OK")


def test_pumpfun_scanner_disabled():
    """Test PumpFunScanner when disabled."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=False)

        assert scanner.enabled is False
        assert scanner.status["state"] == "disabled"
        print("[2] PumpFunScanner disabled: OK")


def test_pumpfun_without_api_key():
    """Test PumpFunScanner without API key."""
    with tempfile.TemporaryDirectory() as d:
        import io
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("paperbot.pumpfun")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        scanner = PumpFunScanner(d, api_key="", enabled=True)

        log_output = log_stream.getvalue()
        assert "some features may be limited" in log_output.lower()

        logger.removeHandler(handler)
        print("[3] PumpFunScanner without API key: OK")


def test_event_kind_parsing():
    """Test event kind detection for new_token and migration."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        # new_token: bonding curve keys
        ev = {
            "mint": "ABC123",
            "name": "Test Token",
            "symbol": "TEST",
            "bondingCurveKey": "curve123",
            "signature": "sig123"
        }
        assert scanner._event_kind(ev) == "new_token"

        # new_token: initialBuy
        ev = {
            "mint": "ABC123",
            "name": "Test Token",
            "symbol": "TEST",
            "initialBuy": True
        }
        assert scanner._event_kind(ev) == "new_token"

        # new_token: name + symbol + mint
        ev = {
            "mint": "ABC123",
            "name": "Test Token",
            "symbol": "TEST"
        }
        assert scanner._event_kind(ev) == "new_token"

        # migration: pool address
        ev = {
            "mint": "ABC123",
            "pool": "pool123"
        }
        assert scanner._event_kind(ev) == "migration"

        # migration: raydiumPool
        ev = {
            "tokenMint": "ABC123",
            "raydiumPool": "pool123"
        }
        assert scanner._event_kind(ev) == "migration"

        # migration: txType
        ev = {
            "txType": "migration_complete",
            "mint": "ABC123"
        }
        assert scanner._event_kind(ev) == "migration"

        # Unknown
        ev = {"unknown": "data"}
        assert scanner._event_kind(ev) is None

        print("[4] Event kind parsing: OK")


def test_parse_event():
    """Test event normalization to unified schema."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        # Complete new_token event
        data = {
            "mint": "So11111111111111111111111111111111111111112",
            "name": "Wrapped SOL",
            "symbol": "WSOL",
            "traderPublicKey": "creator123",
            "pool": "pool123",
            "signature": "sig123",
            "quoteMint": "USDC",
            "baseMint": "WSOL",
            "marketCapUsd": 1000000,
            "liquidity_usd": 50000,
            "price_usd": 100.0,
            "volume_usd": 10000,
            "slot": 123456,
            "blockTime": 1705315800,
            "migrationStatus": "not_migrated",
        }

        parsed = scanner._parse_event(data, "new_token")

        assert parsed is not None
        assert parsed["source"] == "pumpfun"
        assert parsed["event_type"] == "new_token"
        assert parsed["chain"] == "solana"
        assert parsed["token_mint"] == "So11111111111111111111111111111111111111112"
        assert parsed["token_symbol"] == "WSOL"
        assert parsed["token_name"] == "Wrapped SOL"
        assert parsed["pool_address"] == "pool123"
        assert parsed["deployer_address"] == "creator123"
        assert parsed["transaction_signature"] == "sig123"
        assert parsed["slot"] == 123456
        assert parsed["block_time"] == 1705315800.0
        assert parsed["market_cap_usd"] == 1000000
        assert parsed["liquidity_usd"] == 50000
        assert parsed["price_usd"] == 100.0
        assert parsed["volume_usd"] == 10000
        assert parsed["data_quality"] == "complete"
        assert parsed["confidence"] == 0.9
        assert parsed["verification_status"] == "unverified"
        assert parsed["base_mint"] == "WSOL"
        assert parsed["quote_mint"] == "USDC"
        assert parsed["pool_type"] == "bonding_curve"

        # Partial event (missing price/liquidity/quote_mint)
        data_partial = {
            "mint": "ABC123",
            "name": "Test Token",
            "symbol": "TEST",
        }

        parsed_partial = scanner._parse_event(data_partial, "new_token")

        assert parsed_partial is not None
        assert parsed_partial["data_quality"] == "partial"
        assert parsed_partial["confidence"] == 0.5
        assert parsed_partial["price_usd"] is None
        assert parsed_partial["liquidity_usd"] is None
        assert parsed_partial["quote_mint"] == ""

        print("[5] Event parsing/normalization: OK")


def test_timestamp_normalization():
    """Test timestamp normalization for various formats."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        # Unix seconds
        recent_ts = 1705315800  # 2024-01-15
        assert scanner._normalize_timestamp(recent_ts) == float(recent_ts)

        # Unix milliseconds
        assert scanner._normalize_timestamp(recent_ts * 1000) == float(recent_ts)

        # ISO string
        ts = scanner._normalize_timestamp("2024-01-15T10:30:00Z")
        assert ts is not None
        assert abs(ts - 1705314600) < 1

        # ISO string with offset
        ts = scanner._normalize_timestamp("2024-01-15T10:30:00+00:00")
        assert ts is not None
        assert abs(ts - 1705314600) < 1

        # Future timestamp (should be rejected)
        future = time.time() + 7200
        assert scanner._normalize_timestamp(future) is None

        # Too old (before 2020)
        assert scanner._normalize_timestamp(1000000000) is None

        # Invalid
        assert scanner._normalize_timestamp("invalid") is None
        assert scanner._normalize_timestamp(None) is None

        print("[6] Timestamp normalization: OK")


def test_event_id_generation():
    """Test stable event ID generation."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)
        received_ts = 1234567890.0

        # With signature
        data = {"signature": "sig1234567890"}
        eid = scanner._generate_event_id("new_token", data, received_ts)
        assert eid.startswith("pumpfun_new_token_sig123456789")

        # With pool
        data = {"pool": "pool1234567890"}
        eid = scanner._generate_event_id("new_token", data, received_ts)
        assert eid.startswith("pumpfun_new_token_pool123456789")

        # With mint
        data = {"mint": "mint1234567890"}
        eid = scanner._generate_event_id("new_token", data, received_ts)
        assert "mint123456789" in eid

        # Unknown
        data = {}
        eid = scanner._generate_event_id("new_token", data, received_ts)
        assert eid.startswith("pumpfun_new_token_unknown_")

        print("[7] Event ID generation: OK")


def test_persistence():
    """Test atomic state persistence."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        # Add an event manually
        event = {
            "event_id": "test_1",
            "source": "pumpfun",
            "event_type": "new_token",
            "token_mint": "ABC123",
        }
        scanner.events.appendleft(event)
        scanner.status["received"] = 1
        scanner._persist()

        # Verify file exists and has correct content
        state_path = os.path.join(d, "pumpfun_state.json")
        assert os.path.exists(state_path)

        with open(state_path) as f:
            state = json.load(f)

        assert state["received"] == 1
        assert len(state["events"]) == 1
        assert state["events"][0]["event_id"] == "test_1"

        print("[8] State persistence: OK")


def test_snapshot():
    """Test snapshot method."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        # Add some events
        for i in range(5):
            scanner.events.appendleft({"event_id": f"test_{i}", "event_type": "new_token"})

        snap = scanner.snapshot()

        assert "enabled" in snap
        assert "connected" in snap
        assert "state" in snap
        assert "received" in snap
        assert "events" in snap
        assert len(snap["events"]) == 5

        print("[9] Snapshot: OK")


def test_get_recent_events():
    """Test get_recent_events with limit."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        for i in range(10):
            scanner.events.appendleft({"event_id": f"test_{i}"})

        events = scanner.get_recent_events(limit=3)
        assert len(events) == 3
        assert events[0]["event_id"] == "test_9"  # Most recent first

        events = scanner.get_recent_events(limit=100)
        assert len(events) == 10

        print("[10] get_recent_events: OK")


def test_invalid_json_handling():
    """Test handling of invalid JSON messages."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        # Should not crash
        scanner._on_message(None, "not valid json")
        scanner._on_message(None, "{incomplete")

        # No events should be added
        assert len(scanner.events) == 0

        print("[11] Invalid JSON handling: OK")


def test_duplicate_events():
    """Test that duplicate events (same signature) get same event_id."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        data = {
            "mint": "ABC123",
            "name": "Test",
            "symbol": "TEST",
            "signature": "sig123",
            "quoteMint": "USDC",
        }

        # Send same event twice
        scanner._on_message(None, json.dumps(data))
        scanner._on_message(None, json.dumps(data))

        # Both events are stored but have the same event_id (signature-based deduplication)
        events = scanner.get_recent_events(limit=10)
        assert len(events) == 2

        # Same signature -> same event_id
        assert events[0]["event_id"] == events[1]["event_id"]
        assert events[0]["event_id"] == "pumpfun_new_token_sig123"

        # Test that different signatures get different IDs
        data2 = {
            "mint": "ABC123",
            "name": "Test",
            "symbol": "TEST",
            "signature": "sig456",
            "quoteMint": "USDC",
        }
        scanner._on_message(None, json.dumps(data2))
        events = scanner.get_recent_events(limit=10)
        assert len(events) == 3
        assert events[0]["event_id"] == "pumpfun_new_token_sig456"
        assert events[1]["event_id"] == "pumpfun_new_token_sig123"
        assert events[2]["event_id"] == "pumpfun_new_token_sig123"

        print("[12] Duplicate events handled: OK")


def test_migration_not_in_pools():
    """Test that migration events don't create new pool entries."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        data = {
            "mint": "ABC123",
            "pool": "pool123",
            "signature": "sig123",
            "quoteMint": "USDC",
        }

        scanner._on_message(None, json.dumps(data))

        events = scanner.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "migration"

        # When converting to pools, migration should be skipped
        pools = []
        for ev in events:
            if ev.get("event_type") == "new_token":
                pools.append(ev)

        assert len(pools) == 0
        print("[13] migration excluded from new pools: OK")


def test_malformed_payload():
    """Test handling of malformed payloads."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        # Missing required fields
        data = {"random": "data"}
        scanner._on_message(None, json.dumps(data))

        # Should not add event
        assert len(scanner.events) == 0

        print("[14] Malformed payload handling: OK")


def test_missing_quote_mint():
    """Test handling of events without quote mint."""
    with tempfile.TemporaryDirectory() as d:
        scanner = PumpFunScanner(d, api_key="", enabled=True)

        data = {
            "mint": "ABC123",
            "name": "Test",
            "symbol": "TEST",
            "signature": "sig123",
        }

        scanner._on_message(None, json.dumps(data))

        events = scanner.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["quote_mint"] == ""
        assert events[0]["data_quality"] == "partial"

        print("[15] Missing quote mint handling: OK")


def run_all_tests():
    """Run all tests."""
    tests = [
        test_pumpfun_scanner_init,
        test_pumpfun_scanner_disabled,
        test_pumpfun_without_api_key,
        test_event_kind_parsing,
        test_parse_event,
        test_timestamp_normalization,
        test_event_id_generation,
        test_persistence,
        test_snapshot,
        test_get_recent_events,
        test_invalid_json_handling,
        test_duplicate_events,
        test_migration_not_in_pools,
        test_malformed_payload,
        test_missing_quote_mint,
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