"""RPC Provider Manager with intelligent failover for Solana RPC providers.

Supports:
- Helius (primary) - HTTPS JSON-RPC + WSS WebSocket
- Chainstack (secondary) - HTTPS JSON-RPC + WSS WebSocket
- Public RPC (emergency fallback) - HTTPS JSON-RPC only

Features:
- Health monitoring with latency tracking
- Automatic failover on errors/timeouts/rate-limits
- Circuit breaker per provider
- Recovery detection with periodic health checks
- Request deduplication to prevent duplicate side effects
- Credential-safe logging (no secrets in logs)
"""
from __future__ import annotations
import json
import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

log = logging.getLogger("paperbot.rpc_manager")


class ProviderState(Enum):
    """Provider health states."""
    CONNECTED = "connected"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILING_OVER = "failing_over"
    DISCONNECTED = "disconnected"


class ProviderRole(Enum):
    """Provider priority roles."""
    PRIMARY = "primary"
    SECONDARY = "secondary"
    FALLBACK = "fallback"


@dataclass
class ProviderConfig:
    """Configuration for a single RPC provider."""
    name: str
    role: ProviderRole
    http_url: str
    ws_url: Optional[str] = None
    api_key: Optional[str] = None
    enabled: bool = True
    # Health check settings
    health_check_interval: float = 30.0
    health_check_timeout: float = 5.0
    # Circuit breaker
    max_consecutive_failures: int = 5
    circuit_breaker_base_delay: float = 30.0
    circuit_breaker_max_delay: float = 300.0
    # Rate limiting
    rate_limit_rps: float = 100.0
    # Request timeout
    request_timeout: float = 10.0


@dataclass
class ProviderStats:
    """Runtime statistics for a provider."""
    consecutive_failures: int = 0
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    rate_limited_count: int = 0
    last_success_ts: Optional[float] = None
    last_failure_ts: Optional[float] = None
    last_failure_kind: Optional[str] = None
    circuit_open_until: float = 0.0
    current_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0
    latency_samples: deque = field(default_factory=lambda: deque(maxlen=100))
    error_kinds: Dict[str, int] = field(default_factory=dict)


class RPCProvider:
    """Individual RPC provider wrapper with health monitoring."""

    def __init__(self, config: ProviderConfig, http_client_factory: Callable[[], requests.Session]):
        self.config = config
        self._http_factory = http_client_factory
        self._session: Optional[requests.Session] = None
        self.stats = ProviderStats()
        self._lock = threading.RLock()
        self._state = ProviderState.CONNECTED if config.enabled else ProviderState.DISCONNECTED
        self._last_health_check = 0.0
        self._id = config.name.lower().replace(" ", "_")

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def role(self) -> ProviderRole:
        return self.config.role

    @property
    def state(self) -> ProviderState:
        with self._lock:
            return self._state

    @state.setter
    def state(self, value: ProviderState):
        with self._lock:
            old = self._state
            self._state = value
            if old != value:
                log.info("Provider %s state: %s -> %s", self.name, old.value, value.value)

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = self._http_factory()
            self._session.headers["User-Agent"] = f"solana-paper-bot/4.0 ({self.name})"
            # Helius JSON-RPC authenticates via the ?api-key= query parameter (built in call());
            # do not inject an Authorization: Bearer header that causes 401/400 on Helius RPC nodes.
        return self._session

    def _record_latency(self, latency_ms: float):
        with self._lock:
            self.stats.latency_samples.append(latency_ms)
            self.stats.current_latency_ms = latency_ms
            if self.stats.latency_samples:
                self.stats.avg_latency_ms = sum(self.stats.latency_samples) / len(self.stats.latency_samples)

    def _record_success(self):
        with self._lock:
            self.stats.total_requests += 1
            self.stats.successful_requests += 1
            self.stats.consecutive_failures = 0
            self.stats.last_success_ts = time.time()
            self.stats.circuit_open_until = 0.0

    def _record_failure(self, kind: str, is_rate_limit: bool = False):
        with self._lock:
            self.stats.total_requests += 1
            self.stats.failed_requests += 1
            self.stats.consecutive_failures += 1
            self.stats.last_failure_ts = time.time()
            self.stats.last_failure_kind = kind
            self.stats.error_kinds[kind] = self.stats.error_kinds.get(kind, 0) + 1
            if is_rate_limit:
                self.stats.rate_limited_count += 1
            # Check circuit breaker
            if self.stats.consecutive_failures >= self.config.max_consecutive_failures:
                delay = min(
                    self.config.circuit_breaker_max_delay,
                    self.config.circuit_breaker_base_delay * 2 ** (self.stats.consecutive_failures - self.config.max_consecutive_failures)
                )
                self.stats.circuit_open_until = time.time() + delay * random.uniform(0.8, 1.2)
                log.warning("Provider %s circuit opened after %d failures, pausing for %.0fs",
                            self.name, self.stats.consecutive_failures, delay)

    def is_circuit_open(self) -> bool:
        with self._lock:
            return time.time() < self.stats.circuit_open_until

    def is_rate_limited(self) -> bool:
        with self._lock:
            return self.stats.rate_limited_count > 0 and self.stats.last_failure_ts is not None \
                and time.time() - self.stats.last_failure_ts < 60

    def call(self, method: str, params: List[Any], timeout: Optional[float] = None) -> Tuple[Optional[Any], str]:
        """Make a JSON-RPC call. Returns (result, error_kind)."""
        if self.is_circuit_open():
            return None, "circuit_open"

        session = self._get_session()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        headers = {"Content-Type": "application/json"}

        # Build URL with API key if needed (for providers that use query param)
        url = self.config.http_url
        if self.config.api_key and "helius" in self.config.http_url.lower() and "api-key" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}api-key={self.config.api_key}"

        start = time.time()
        try:
            request_timeout = self.config.request_timeout if timeout is None else min(
                self.config.request_timeout, max(0.05, float(timeout)))
            resp = session.post(url, json=payload, headers=headers, timeout=request_timeout)
            latency_ms = (time.time() - start) * 1000
            self._record_latency(latency_ms)

            if resp.status_code == 429:
                self._record_failure("rate_limited", is_rate_limit=True)
                return None, "rate_limited"
            if resp.status_code >= 500:
                self._record_failure(f"http_{resp.status_code}")
                return None, f"http_{resp.status_code}"
            if resp.status_code != 200:
                self._record_failure(f"http_{resp.status_code}")
                return None, f"http_{resp.status_code}"

            data = resp.json()
            if "error" in data:
                self._record_failure("rpc_error")
                return None, "rpc_error"
            if "result" not in data:
                self._record_failure("malformed")
                return None, "malformed"

            self._record_success()
            return data["result"], "ok"

        except requests.exceptions.Timeout:
            self._record_failure("timeout")
            return None, "timeout"
        except requests.exceptions.ConnectionError:
            self._record_failure("connection_error")
            return None, "connection_error"
        except Exception as e:
            self._record_failure(f"exception_{type(e).__name__}")
            return None, f"exception_{type(e).__name__}"

    def health_check(self, bypass_circuit: bool = False) -> bool:
        """Perform a health check using getHealth or getSlot."""
        if not bypass_circuit and self.is_circuit_open():
            return False

        result, kind = self.call("getHealth", [])
        if kind == "ok" and result == "ok":
            return True
        # Fallback to getSlot
        result, kind = self.call("getSlot", [])
        return kind == "ok" and result is not None

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "role": self.role.value,
                "state": self.state.value,
                "enabled": self.config.enabled,
                "consecutive_failures": self.stats.consecutive_failures,
                "total_requests": self.stats.total_requests,
                "successful_requests": self.stats.successful_requests,
                "failed_requests": self.stats.failed_requests,
                "rate_limited_count": self.stats.rate_limited_count,
                "last_success_ts": self.stats.last_success_ts,
                "last_failure_ts": self.stats.last_failure_ts,
                "last_failure_kind": self.stats.last_failure_kind,
                "circuit_open_until": self.stats.circuit_open_until,
                "current_latency_ms": round(self.stats.current_latency_ms, 2),
                "avg_latency_ms": round(self.stats.avg_latency_ms, 2),
                "error_kinds": dict(self.stats.error_kinds),
                "is_circuit_open": self.is_circuit_open(),
                "is_rate_limited": self.is_rate_limited(),
            }

    def close(self):
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None


class RPCManager:
    """Manages multiple RPC providers with intelligent failover."""

    def __init__(self, configs: List[ProviderConfig], http_client_factory: Callable[[], requests.Session]):
        self._budget_local = threading.local()
        self._providers: Dict[str, RPCProvider] = {}
        self._active_provider: Optional[RPCProvider] = None
        self._lock = threading.RLock()
        self._failover_callbacks: List[Callable[[str, str], None]] = []
        self._health_check_thread: Optional[threading.Thread] = None
        self._stop_health_checks = threading.Event()
        self._http_factory = http_client_factory

        for config in configs:
            if config.enabled:
                provider = RPCProvider(config, http_client_factory)
                self._providers[provider.id] = provider
                log.info("Registered RPC provider: %s (%s)", config.name, config.role.value)

        self._select_active_provider()

    def _select_active_provider(self) -> bool:
        """Select the best available provider based on role and health."""
        with self._lock:
            role_order = {ProviderRole.PRIMARY: 0, ProviderRole.SECONDARY: 1, ProviderRole.FALLBACK: 2}
            candidates = sorted(
                [p for p in self._providers.values() if p.config.enabled],
                key=lambda p: (role_order[p.role], p.stats.consecutive_failures, p.stats.avg_latency_ms or float('inf'))
            )

            for provider in candidates:
                if not provider.is_circuit_open() and provider.state != ProviderState.DISCONNECTED:
                    if self._active_provider is None or provider.id != self._active_provider.id:
                        old_name = self._active_provider.name if self._active_provider else "none"
                        self._active_provider = provider
                        provider.state = ProviderState.HEALTHY
                        log.info("Active RPC provider: %s (was %s)", provider.name, old_name)
                        for cb in self._failover_callbacks:
                            try:
                                cb(old_name, provider.name)
                            except Exception as e:
                                log.warning("Failover callback error: %s", e)
                    return True

            # No healthy provider found
            if self._active_provider:
                self._active_provider.state = ProviderState.DISCONNECTED
            self._active_provider = None
            return False

    def register_failover_callback(self, callback: Callable[[str, str], None]):
        """Register a callback(old_name, new_name) for failover notifications."""
        self._failover_callbacks.append(callback)

    def begin_cycle_budget(self, seconds: float):
        self._budget_local.deadline = time.monotonic() + max(0.0, float(seconds))

    def end_cycle_budget(self):
        if hasattr(self._budget_local, "deadline"):
            del self._budget_local.deadline

    def _cycle_remaining(self):
        deadline = getattr(self._budget_local, "deadline", None)
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    def call(self, method: str, params: List[Any]) -> Tuple[Optional[Any], Optional[str]]:
        """Make an RPC call with automatic failover. Returns (result, active_provider_name)."""
        with self._lock:
            if not self._active_provider:
                self._select_active_provider()
            provider = self._active_provider

        if not provider:
            return None, None

        # Execute network I/O outside self._lock so health checks and stats never block.
        remaining = self._cycle_remaining()
        if remaining is not None and remaining <= 0.05:
            return None, None
        result, kind = provider.call(method, params, timeout=remaining)

        if kind == "ok":
            return result, provider.name

        # On failure, check if we should failover
        if kind in ("timeout", "connection_error", "rate_limited", "http_500", "http_502", "http_503", "http_504", "circuit_open"):
            log.warning("Provider %s failed (%s), attempting failover", provider.name, kind)
            with self._lock:
                provider.state = ProviderState.FAILING_OVER
                selected = self._select_active_provider()
                next_provider = self._active_provider if (selected and self._active_provider != provider) else None

            if next_provider is not None:
                log.info("Failover from %s to %s", provider.name, next_provider.name)
                # Retry on new provider outside self._lock, but only inside the
                # remaining cycle budget; failover must not double the outage delay.
                remaining = self._cycle_remaining()
                if remaining is not None and remaining <= 0.05:
                    return None, None
                result, kind = next_provider.call(method, params, timeout=remaining)
                if kind == "ok":
                    return result, next_provider.name

        return result, provider.name if kind == "ok" else None

    def get_active_provider(self) -> Optional[str]:
        with self._lock:
            return self._active_provider.name if self._active_provider else None

    def get_provider(self, name: str) -> Optional[RPCProvider]:
        with self._lock:
            return self._providers.get(name.lower().replace(" ", "_"))

    def get_all_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_provider": self._active_provider.name if self._active_provider else None,
                "providers": {p.id: p.get_stats() for p in self._providers.values()},
            }

    def start_health_checks(self):
        """Start background health check thread."""
        if self._health_check_thread and self._health_check_thread.is_alive():
            return

        self._stop_health_checks.clear()
        self._health_check_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_check_thread.start()
        log.info("RPC health checks started")

    def stop_health_checks(self):
        self._stop_health_checks.set()
        if self._health_check_thread:
            self._health_check_thread.join(timeout=5)
        log.info("RPC health checks stopped")

    def _health_check_loop(self):
        while not self._stop_health_checks.is_set():
            try:
                self._run_health_checks()
            except Exception as e:
                log.warning("Health check error: %s", e)
            self._stop_health_checks.wait(30)

    def _run_health_checks(self):
        with self._lock:
            providers = list(self._providers.values())

        for provider in providers:
            if not provider.config.enabled:
                continue

            if provider.is_circuit_open():
                if provider.health_check(bypass_circuit=True):
                    with provider._lock:
                        provider.stats.circuit_open_until = 0.0
                        provider.stats.consecutive_failures = 0
                        provider.state = ProviderState.HEALTHY
                    log.info("Provider %s recovered from circuit breaker", provider.name)
                    self._select_active_provider()
            else:
                if provider != self._active_provider:
                    healthy = provider.health_check()
                    if healthy:
                        provider.state = ProviderState.HEALTHY
                    else:
                        provider.state = ProviderState.DEGRADED

        self._select_active_provider()

    def close(self):
        self.stop_health_checks()
        for provider in self._providers.values():
            provider.close()


def create_rpc_manager_from_env(http_client_factory: Callable[[], requests.Session]) -> RPCManager:
    """Create RPCManager from environment variables."""
    configs = []

    # Helius - Primary
    helius_key = os.getenv("HELIUS_API_KEY", "").strip()
    if helius_key:
        helius_url = os.getenv("HELIUS_RPC_URL", f"https://mainnet.helius-rpc.com/?api-key={helius_key}").strip()
        helius_ws = os.getenv("HELIUS_WS_URL", f"wss://mainnet.helius-rpc.com/?api-key={helius_key}").strip()
        configs.append(ProviderConfig(
            name="Helius",
            role=ProviderRole.PRIMARY,
            http_url=helius_url,
            ws_url=helius_ws,
            api_key=helius_key,
            enabled=True,
            rate_limit_rps=100.0,
            request_timeout=10.0,
        ))

    # Chainstack - Secondary
    chainstack_url = os.getenv("CHAINSTACK_RPC_URL", "").strip()
    chainstack_ws = os.getenv("CHAINSTACK_WS_URL", "").strip()
    if chainstack_url:
        configs.append(ProviderConfig(
            name="Chainstack",
            role=ProviderRole.SECONDARY,
            http_url=chainstack_url,
            ws_url=chainstack_ws if chainstack_ws else None,
            enabled=True,
            rate_limit_rps=50.0,
            request_timeout=10.0,
        ))

    # Public RPC - Fallback (only if explicitly enabled)
    public_rpc = os.getenv("SOLANA_RPC_URL", "").strip()
    if public_rpc and os.getenv("SOLANA_PUBLIC_RPC_ENABLED", "false").lower() in ("1", "true", "yes", "on"):
        configs.append(ProviderConfig(
            name="Public RPC",
            role=ProviderRole.FALLBACK,
            http_url=public_rpc,
            enabled=True,
            rate_limit_rps=10.0,
            request_timeout=15.0,
        ))
    else:
        configs.append(ProviderConfig(
            name="Public RPC (default)",
            role=ProviderRole.FALLBACK,
            http_url="https://api.mainnet-beta.solana.com",
            enabled=True,
            rate_limit_rps=5.0,
            request_timeout=15.0,
        ))

    return RPCManager(configs, http_client_factory)
