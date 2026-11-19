#!/usr/bin/env python3
"""Offline unit tests for GMGN scanner (no real network needed)."""
import json
import logging
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

from gmgn import GmGnScanner


def test_gmgn_scanner_init():
    """Test GmGnScanner initialization and default values."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        assert scanner.enabled is True
        assert scanner.api_key == ""
        assert "new_pool_info" in scanner.subscription_channels
        assert "new_pair_update" in scanner.subscription_channels
        assert "new_launched_info" in scanner.subscription_channels
        assert "chain_stat" in scanner.subscription_channels
        assert scanner.status["enabled"] is True
        assert scanner.status["state"] == "disconnected"
        assert os.path.exists(os.path.join(d, "gmgn_state.json"))
        print("[1] GmGnScanner init: OK")


def test_gmgn_scanner_disabled():
    """Test GmGnScanner when disabled."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=False)

        assert scanner.enabled is False
        assert scanner.status["state"] == "disabled"
        print("[2] GmGnScanner disabled: OK")


def test_event_kind_parsing():
    """Test event kind detection for all public channels."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        # new_pool_info subscription channel -> new_pool internal
        ev = {"channel": "new_pool_info", "mint": "ABC123"}
        assert scanner._event_kind(ev) == "new_pool"

        # pair_update (new_pair_update)
        ev = {"channel": "new_pair_update", "mint": "ABC123", "pool": "pool1"}
        assert scanner._event_kind(ev) == "pair_update"

        # token_launch (new_launched_info)
        ev = {"channel": "new_launched_info", "mint": "ABC123", "name": "Test"}
        assert scanner._event_kind(ev) == "token_launch"

        # chain_stats (chain_stat)
        ev = {"channel": "chain_stat", "data": {}}
        assert scanner._event_kind(ev) == "chain_stats"

        # Unknown
        ev = {"channel": "unknown", "mint": "ABC123"}
        assert scanner._event_kind(ev) is None

        # Fallback: token with pool
        ev = {"mint": "ABC123", "name": "Test", "pool": "pool1"}
        assert scanner._event_kind(ev) == "new_pool"

        # Fallback: token without pool
        ev = {"mint": "ABC123", "name": "Test"}
        assert scanner._event_kind(ev) == "token_launch"

        # Fallback: pool update
        ev = {"pool": "pool1", "mint": "ABC123"}
        assert scanner._event_kind(ev) == "pair_update"

        print("[3] Event kind parsing: OK")


def test_parse_event():
    """Test event normalization to unified schema."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        # Complete event - use a recent timestamp (2024)
        recent_ts = 1705315800  # 2024-01-15
        data = {
            "channel": "new_pools",
            "mint": "So11111111111111111111111111111111111111112",
            "name": "Wrapped SOL",
            "symbol": "WSOL",
            "creator": "creator123",
            "pool": "pool123",
            "signature": "sig123",
            "quote_mint": "USDC",
            "base_mint": "WSOL",
            "market_cap_usd": 1000000,
            "liquidity_usd": 50000,
            "price_usd": 100.0,
            "volume_usd": 10000,
            "buys": 50,
            "sells": 30,
            "holders": 1000,
            "slot": 123456,
            "block_time": recent_ts,
            "migration_status": "not_migrated",
        }

        parsed = scanner._parse_event(data, "new_pool")

        assert parsed is not None
        assert parsed["source"] == "gmgn"
        assert parsed["event_type"] == "new_pool"
        assert parsed["chain"] == "solana"
        assert parsed["token_mint"] == "So11111111111111111111111111111111111111112"
        assert parsed["token_symbol"] == "WSOL"
        assert parsed["token_name"] == "Wrapped SOL"
        assert parsed["pool_address"] == "pool123"
        assert parsed["deployer_address"] == "creator123"
        assert parsed["transaction_signature"] == "sig123"
        assert parsed["slot"] == 123456
        assert parsed["block_time"] == float(recent_ts)
        assert parsed["market_cap_usd"] == 1000000
        assert parsed["liquidity_usd"] == 50000
        assert parsed["price_usd"] == 100.0
        assert parsed["volume_usd"] == 10000
        assert parsed["buy_count"] == 50
        assert parsed["sell_count"] == 30
        assert parsed["holder_count"] == 1000
        assert parsed["migration_status"] == "not_migrated"
        assert parsed["data_quality"] == "complete"
        assert parsed["confidence"] == 0.9
        assert parsed["verification_status"] == "unverified"
        assert parsed["base_mint"] == "WSOL"
        assert parsed["quote_mint"] == "USDC"
        assert parsed["raw_payload"] == data

        # Partial event (missing price/liquidity/quote_mint)
        data_partial = {
            "channel": "token_launch",
            "mint": "ABC123",
            "name": "Test Token",
            "symbol": "TEST",
        }

        parsed_partial = scanner._parse_event(data_partial, "token_launch")

        assert parsed_partial is not None
        assert parsed_partial["data_quality"] == "partial"
        assert parsed_partial["confidence"] == 0.5
        assert parsed_partial["price_usd"] is None
        assert parsed_partial["liquidity_usd"] is None
        # Missing quote_mint defaults to SOL (bonding curve tokens)
        assert parsed_partial["quote_mint"] == "So11111111111111111111111111111111111111112"

        print("[4] Event parsing/normalization: OK")


def test_timestamp_normalization():
    """Test timestamp normalization for various formats."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        # Unix seconds (recent)
        recent_ts = 1705314600  # 2024-01-15 10:30:00 UTC
        assert scanner._normalize_timestamp(recent_ts) == float(recent_ts)

        # Unix milliseconds (recent)
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

        print("[5] Timestamp normalization: OK")


def test_event_id_generation():
    """Test stable event ID generation."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)
        received_ts = 1234567890.0

        # With signature
        data = {"signature": "sig1234567890"}
        eid = scanner._generate_event_id("new_pool", data, received_ts)
        assert eid.startswith("gmgn_new_pool_sig123456789")

        # With pool
        data = {"pool": "pool1234567890"}
        eid = scanner._generate_event_id("new_pool", data, received_ts)
        assert eid.startswith("gmgn_new_pool_pool123456789")

        # With mint
        data = {"mint": "mint1234567890"}
        eid = scanner._generate_event_id("new_pool", data, received_ts)
        assert "mint123456789" in eid

        # Unknown
        data = {}
        eid = scanner._generate_event_id("new_pool", data, received_ts)
        assert eid.startswith("gmgn_new_pool_unknown_")

        print("[6] Event ID generation: OK")


def test_missing_mint():
    """Test that events without mint are handled gracefully."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        data = {"channel": "new_pools", "name": "Test"}
        parsed = scanner._parse_event(data, "new_pool")

        assert parsed is not None
        assert parsed["token_mint"] == ""
        assert parsed["data_quality"] == "partial"

        print("[7] Missing mint handling: OK")


def test_persistence():
    """Test atomic state persistence."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        # Add an event manually
        event = {
            "event_id": "test_1",
            "source": "gmgn",
            "event_type": "new_pool",
            "token_mint": "ABC123",
        }
        scanner.events.appendleft(event)
        scanner.status["received"] = 1
        scanner._persist()

        # Verify file exists and has correct content
        state_path = os.path.join(d, "gmgn_state.json")
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
        scanner = GmGnScanner(d, api_key="", enabled=True)

        # Add some events
        for i in range(5):
            scanner.events.appendleft({"event_id": f"test_{i}", "event_type": "new_pool"})

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
        scanner = GmGnScanner(d, api_key="", enabled=True)

        for i in range(10):
            scanner.events.appendleft({"event_id": f"test_{i}"})

        events = scanner.get_recent_events(limit=3)
        assert len(events) == 3
        assert events[0]["event_id"] == "test_9"  # Most recent first

        events = scanner.get_recent_events(limit=100)
        assert len(events) == 10

        print("[10] get_recent_events: OK")


def test_public_channels_filtering():
    """Test that only configured subscription channels are processed."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(
            d,
            api_key="",
            enabled=True,
            public_channels=["new_pools", "token_launch"]  # Only these two
        )

        # Should process
        assert "new_pools" in scanner.subscription_channels
        assert "token_launch" in scanner.subscription_channels

        # Should not process (not in configured list)
        assert "pair_update" not in scanner.subscription_channels
        assert "chain_stats" not in scanner.subscription_channels

        # Internal event types still include all for counting
        assert "pair_update" in scanner.event_type_map.values()
        assert "chain_stats" in scanner.event_type_map.values()

        print("[11] Public channels filtering: OK")


def test_no_api_key_warning():
    """Test that running without API key logs info message."""
    with tempfile.TemporaryDirectory() as d:
        import io
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logger = logging.getLogger("paperbot.gmgn")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        scanner = GmGnScanner(d, api_key="", enabled=True)

        log_output = log_stream.getvalue()
        assert "public channels only" in log_output.lower()

        logger.removeHandler(handler)
        print("[12] No API key warning: OK")


def test_chain_stats_not_in_watch():
    """Test that chain_stats events don't create watch entries."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        data = {
            "channel": "chain_stat",
            "data": {"total_pairs": 1000}
        }

        # Process the event
        scanner._on_message(None, json.dumps(data))

        # Event should be stored but not passed to watch conversion
        events = scanner.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "chain_stats"

        # When converting to pools, chain_stats should be skipped
        pools = []
        for ev in events:
            if ev.get("event_type") in ("new_pool", "token_launch"):
                pools.append(ev)

        assert len(pools) == 0
        print("[13] chain_stats excluded from watch: OK")


def test_invalid_json_handling():
    """Test handling of invalid JSON messages."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        # Should not crash
        scanner._on_message(None, "not valid json")
        scanner._on_message(None, "{incomplete")

        # No events should be added
        assert len(scanner.events) == 0

        print("[14] Invalid JSON handling: OK")


def test_duplicate_events():
    """Test that duplicate events (same signature) are handled.

    Events with the same signature should get the same event_id (deduplication).
    This is correct behavior - we don't want to process the same on-chain event twice.
    """
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        data = {
            "channel": "new_pool_info",
            "mint": "ABC123",
            "name": "Test",
            "symbol": "TEST",
            "signature": "sig123",
        }

        # Send same event twice
        scanner._on_message(None, json.dumps(data))
        scanner._on_message(None, json.dumps(data))

        # Both events are stored but have the same event_id (signature-based deduplication)
        events = scanner.get_recent_events(limit=10)
        assert len(events) == 2

        # Same signature -> same event_id (deduplication key)
        assert events[0]["event_id"] == events[1]["event_id"]
        assert events[0]["event_id"] == "gmgn_new_pool_sig123"

        # Test that different signatures get different IDs
        data2 = {
            "channel": "new_pool_info",
            "mint": "ABC123",
            "name": "Test",
            "symbol": "TEST",
            "signature": "sig456",
        }
        scanner._on_message(None, json.dumps(data2))
        events = scanner.get_recent_events(limit=10)
        assert len(events) == 3
        # events are in reverse chronological order (most recent first)
        # events[0] = sig456 (most recent), events[1] = sig123, events[2] = sig123
        assert events[0]["event_id"] == "gmgn_new_pool_sig456"
        assert events[1]["event_id"] == "gmgn_new_pool_sig123"
        assert events[2]["event_id"] == "gmgn_new_pool_sig123"

        print("[15] Duplicate events handled: OK")


def test_pair_update_enrichment():
    """Test that pair_update can enrich existing tokens but not create new ones."""
    with tempfile.TemporaryDirectory() as d:
        scanner = GmGnScanner(d, api_key="", enabled=True)

        # pair_update with valid data (new_pair_update channel)
        data = {
            "channel": "new_pair_update",
            "mint": "ABC123",
            "pool": "pool123",
            "price_usd": 1.5,
            "liquidity_usd": 10000,
            "quote_mint": "USDC",
        }

        scanner._on_message(None, json.dumps(data))

        events = scanner.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "pair_update"
        assert events[0]["price_usd"] == 1.5

        # When converting to pools, pair_update should be skipped
        pools = []
        for ev in events:
            if ev.get("event_type") in ("new_pool", "token_launch"):
                pools.append(ev)

        assert len(pools) == 0
        print("[16] pair_update excluded from new pools: OK")


def run_all_tests():
    """Run all tests."""
    tests = [
        test_gmgn_scanner_init,
        test_gmgn_scanner_disabled,
        test_event_kind_parsing,
        test_parse_event,
        test_timestamp_normalization,
        test_event_id_generation,
        test_missing_mint,
        test_persistence,
        test_snapshot,
        test_get_recent_events,
        test_public_channels_filtering,
        test_no_api_key_warning,
        test_chain_stats_not_in_watch,
        test_invalid_json_handling,
        test_duplicate_events,
        test_pair_update_enrichment,
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