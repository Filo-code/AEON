#!/usr/bin/env python3
"""Offline unit tests for RPC Manager (no real network needed)."""
import json
import logging
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

from rpc_manager import (
    ProviderConfig, ProviderRole, ProviderState,
    RPCProvider, RPCManager, create_rpc_manager_from_env
)


def test_provider_config():
    """Test ProviderConfig creation."""
    config = ProviderConfig(
        name="Test",
        role=ProviderRole.PRIMARY,
        http_url="https://example.com",
        ws_url="wss://example.com",
        api_key="test-key",
        enabled=True,
    )
    assert config.name == "Test"
    assert config.role == ProviderRole.PRIMARY
    assert config.http_url == "https://example.com"
    assert config.api_key == "test-key"
    assert config.enabled is True
    print("[1] ProviderConfig: OK")


def test_provider_state_enum():
    """Test ProviderState enum values."""
    assert ProviderState.CONNECTED.value == "connected"
    assert ProviderState.HEALTHY.value == "healthy"
    assert ProviderState.DEGRADED.value == "degraded"
    assert ProviderState.FAILING_OVER.value == "failing_over"
    assert ProviderState.DISCONNECTED.value == "disconnected"
    print("[2] ProviderState enum: OK")


def test_rpc_provider_init():
    """Test RPCProvider initialization."""
    config = ProviderConfig(
        name="TestProvider",
        role=ProviderRole.PRIMARY,
        http_url="https://example.com",
    )

    def mock_factory():
        return MagicMock()

    provider = RPCProvider(config, mock_factory)
    assert provider.id == "testprovider"
    assert provider.name == "TestProvider"
    assert provider.role == ProviderRole.PRIMARY
    assert provider.state == ProviderState.CONNECTED
    assert provider.config.enabled is True
    print("[3] RPCProvider init: OK")


def test_rpc_provider_stats():
    """Test RPCProvider statistics tracking."""
    config = ProviderConfig(
        name="TestProvider",
        role=ProviderRole.PRIMARY,
        http_url="https://example.com",
    )

    def mock_factory():
        return MagicMock()

    provider = RPCProvider(config, mock_factory)

    # Test initial stats
    stats = provider.get_stats()
    assert stats["name"] == "TestProvider"
    assert stats["role"] == "primary"
    assert stats["state"] == "connected"
    assert stats["consecutive_failures"] == 0
    assert stats["total_requests"] == 0

    # Record success
    provider._record_success()
    provider._record_latency(100.0)
    stats = provider.get_stats()
    assert stats["successful_requests"] == 1
    assert stats["total_requests"] == 1
    assert stats["consecutive_failures"] == 0
    assert stats["avg_latency_ms"] == 100.0

    # Record failure
    provider._record_failure("timeout")
    stats = provider.get_stats()
    assert stats["failed_requests"] == 1
    assert stats["consecutive_failures"] == 1
    assert stats["last_failure_kind"] == "timeout"

    print("[4] RPCProvider stats: OK")


def test_rpc_provider_circuit_breaker():
    """Test circuit breaker logic."""
    config = ProviderConfig(
        name="TestProvider",
        role=ProviderRole.PRIMARY,
        http_url="https://example.com",
        max_consecutive_failures=3,
        circuit_breaker_base_delay=1.0,
        circuit_breaker_max_delay=10.0,
    )

    def mock_factory():
        return MagicMock()

    provider = RPCProvider(config, mock_factory)

    # Initially circuit should be closed
    assert provider.is_circuit_open() is False

    # Record failures up to threshold
    for _ in range(2):
        provider._record_failure("timeout")
    assert provider.is_circuit_open() is False
    assert provider.stats.consecutive_failures == 2

    # One more failure should open circuit
    provider._record_failure("timeout")
    assert provider.stats.consecutive_failures == 3
    assert provider.is_circuit_open() is True
    assert provider.stats.circuit_open_until > time.time()

    print("[5] RPCProvider circuit breaker: OK")


def test_rpc_manager_init():
    """Test RPCManager initialization with multiple providers."""
    configs = [
        ProviderConfig(
            name="Primary",
            role=ProviderRole.PRIMARY,
            http_url="https://primary.com",
        ),
        ProviderConfig(
            name="Secondary",
            role=ProviderRole.SECONDARY,
            http_url="https://secondary.com",
        ),
        ProviderConfig(
            name="Fallback",
            role=ProviderRole.FALLBACK,
            http_url="https://fallback.com",
        ),
    ]

    def mock_factory():
        return MagicMock()

    manager = RPCManager(configs, mock_factory)

    # Should have all three providers
    assert len(manager._providers) == 3
    assert "primary" in manager._providers
    assert "secondary" in manager._providers
    assert "fallback" in manager._providers

    # Should select primary as active
    assert manager._active_provider is not None
    assert manager._active_provider.name == "Primary"
    assert manager.get_active_provider() == "Primary"

    print("[6] RPCManager init: OK")


def test_rpc_manager_failover_callback():
    """Test failover callback registration and invocation."""
    configs = [
        ProviderConfig(name="Primary", role=ProviderRole.PRIMARY, http_url="https://primary.com"),
        ProviderConfig(name="Secondary", role=ProviderRole.SECONDARY, http_url="https://secondary.com"),
    ]

    def mock_factory():
        return MagicMock()

    manager = RPCManager(configs, mock_factory)

    callback_results = []

    def callback(old_name, new_name):
        callback_results.append((old_name, new_name))

    manager.register_failover_callback(callback)

    # Force secondary to be selected by marking primary as failing
    primary = manager.get_provider("Primary")
    primary.stats.consecutive_failures = 10
    primary.stats.circuit_open_until = time.time() + 3600

    # Trigger failover
    manager._select_active_provider()

    assert callback_results == [("Primary", "Secondary")]
    print("[7] RPCManager failover callback: OK")


def test_rpc_manager_get_all_stats():
    """Test getting stats for all providers."""
    configs = [
        ProviderConfig(name="Primary", role=ProviderRole.PRIMARY, http_url="https://primary.com"),
        ProviderConfig(name="Secondary", role=ProviderRole.SECONDARY, http_url="https://secondary.com"),
    ]

    def mock_factory():
        return MagicMock()

    manager = RPCManager(configs, mock_factory)

    stats = manager.get_all_stats()
    assert "active_provider" in stats
    assert "providers" in stats
    assert stats["active_provider"] == "Primary"
    assert len(stats["providers"]) == 2
    assert "primary" in stats["providers"]
    assert "secondary" in stats["providers"]

    print("[8] RPCManager get_all_stats: OK")


def test_create_rpc_manager_from_env():
    """Test creating RPCManager from environment variables."""
    # Set up environment
    os.environ["CHAINSTACK_RPC_URL"] = "https://chainstack.com"
    os.environ["CHAINSTACK_WS_URL"] = "wss://chainstack.com"
    os.environ["SOLANA_PUBLIC_RPC_ENABLED"] = "false"
    # No Helius key - should use defaults

    def mock_factory():
        return MagicMock()

    manager = create_rpc_manager_from_env(mock_factory)

    # Should have Chainstack and Public RPC
    assert len(manager._providers) == 2
    assert "chainstack" in manager._providers
    assert "public_rpc_(default)" in manager._providers

    # Clean up
    os.environ.pop("CHAINSTACK_RPC_URL", None)
    os.environ.pop("CHAINSTACK_WS_URL", None)
    os.environ.pop("SOLANA_PUBLIC_RPC_ENABLED", None)

    print("[9] create_rpc_manager_from_env: OK")


def test_rpc_provider_record_latency():
    """Test latency recording and averaging."""
    config = ProviderConfig(
        name="TestProvider",
        role=ProviderRole.PRIMARY,
        http_url="https://example.com",
    )

    def mock_factory():
        return MagicMock()

    provider = RPCProvider(config, mock_factory)

    # Record multiple latencies
    for latency in [100.0, 200.0, 300.0]:
        provider._record_latency(latency)

    stats = provider.get_stats()
    assert stats["current_latency_ms"] == 300.0
    assert stats["avg_latency_ms"] == 200.0  # (100+200+300)/3

    print("[10] RPCProvider latency recording: OK")


def test_rpc_provider_rate_limited():
    """Test rate limit detection."""
    config = ProviderConfig(
        name="TestProvider",
        role=ProviderRole.PRIMARY,
        http_url="https://example.com",
    )

    def mock_factory():
        return MagicMock()

    provider = RPCProvider(config, mock_factory)

    # Initially not rate limited
    assert provider.is_rate_limited() is False

    # Record a rate limit failure
    provider._record_failure("rate_limited", is_rate_limit=True)
    assert provider.is_rate_limited() is True
    assert provider.stats.rate_limited_count == 1

    # After 60 seconds, should no longer be rate limited
    provider.stats.last_failure_ts = time.time() - 61
    assert provider.is_rate_limited() is False

    print("[11] RPCProvider rate limited detection: OK")


def test_rpc_manager_close():
    """Test proper cleanup on close."""
    configs = [
        ProviderConfig(name="Primary", role=ProviderRole.PRIMARY, http_url="https://primary.com"),
    ]

    def mock_factory():
        session = MagicMock()
        return session

    manager = RPCManager(configs, mock_factory)
    manager.close()

    # Health checks should be stopped
    assert manager._stop_health_checks.is_set()

    print("[12] RPCManager close: OK")


def run_all_tests():
    """Run all tests."""
    tests = [
        test_provider_config,
        test_provider_state_enum,
        test_rpc_provider_init,
        test_rpc_provider_stats,
        test_rpc_provider_circuit_breaker,
        test_rpc_manager_init,
        test_rpc_manager_failover_callback,
        test_rpc_manager_get_all_stats,
        test_create_rpc_manager_from_env,
        test_rpc_provider_record_latency,
        test_rpc_provider_rate_limited,
        test_rpc_manager_close,
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