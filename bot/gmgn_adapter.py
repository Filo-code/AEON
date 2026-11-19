"""GMGN Intelligence Adapter — Unified interface for GMGN Agent API + WebSocket.

Combines:
- GMGN Agent API (official, free endpoints): Smart Money, KOL, wallet stats, trending, signals
- GMGN WebSocket (open source, public channels): real-time new pools, token launches, pair updates

Normalizes all events to a common internal format.
"""
from __future__ import annotations
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gmgn import GmGnScanner
from gmgn_agent import GMGNAgentClient, create_gmgn_agent_from_env

log = logging.getLogger("paperbot.gmgn_adapter")


@dataclass
class GMGNEvent:
    """Normalized GMGN event."""
    event_id: str
    source: str  # "gmgn_agent" or "gmgn_ws"
    event_type: str  # "new_pool", "token_launch", "pair_update", "chain_stats", "smart_money", "kol_trade", "wallet_activity", "trending", "signal"
    chain: str
    token_mint: str = ""
    token_symbol: str = ""
    token_name: str = ""
    pool_address: str = ""
    wallet_address: str = ""
    transaction_signature: str = ""
    slot: Optional[int] = None
    block_time: Optional[float] = None
    received_at: float = field(default_factory=time.time)
    market_cap_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    price_usd: Optional[float] = None
    volume_usd: Optional[float] = None
    buy_count: Optional[int] = None
    sell_count: Optional[int] = None
    holder_count: Optional[int] = None
    migration_status: Optional[str] = None
    # Intelligence fields
    smart_money_buy: bool = False
    kol_buy: bool = False
    # Extended signal classification (advisory only — does not change trading decisions)
    is_insider: bool = False           # signal_type 11: deployer/team wallet buys
    is_bundler: bool = False           # signal_type 12: same-block multi-wallet coordinated buys
    is_rat_trader: bool = False        # signal_type 13: known malicious/manipulative wallets
    is_sniper: bool = False            # signal_type 17: early-block sniping wallets
    is_dev_wallet: bool = False        # signal_type 18: contract deployer wallet
    is_team_wallet: bool = False       # signal_type 19: project team allocation wallet
    is_cex_wallet: bool = False        # signal_type 20: centralized exchange deposit/withdrawal
    is_mev_bot: bool = False           # signal_type 21: MEV/searcher wallet activity
    signal_type: Optional[int] = None
    signal_strength: Optional[float] = None
    # Metadata
    raw_payload: Dict = field(default_factory=dict)
    data_quality: str = "partial"
    confidence: float = 0.5
    verification_status: str = "unverified"


class GMGNIntelligenceAdapter:
    """Unified GMGN intelligence adapter combining Agent API and WebSocket."""

    def __init__(
        self,
        data_dir: str,
        gmgn_api_key: str = "",
        enabled: bool = True,
        max_events: int = 500,
    ):
        self.data_dir = data_dir
        self.enabled = enabled
        self.max_events = max_events
        self.lock = threading.RLock()
        self.events: List[GMGNEvent] = []
        self.last_events_ts = 0.0

        # Initialize WebSocket scanner (public channels only)
        self.ws_scanner = GmGnScanner(
            data_dir=data_dir,
            api_key="",  # WebSocket public channels don't need API key
            enabled=enabled,
            max_events=max_events,
            public_channels=[
                "new_pool_info",
                "new_pair_update",
                "new_launched_info",
                "chain_stat",
            ],
        )

        # Initialize Agent API client (requires API key)
        self.agent_client = None
        if gmgn_api_key:
            from gmgn_agent import GMGNAgentClient, GMGNAgentConfig
            config = GMGNAgentConfig(
                api_key=gmgn_api_key,
                host=os.getenv("GMGN_API_HOST", "https://api.gmgn.ai"),
                timeout=float(os.getenv("GMGN_API_TIMEOUT", "10")),
                rate_limit_rps=float(os.getenv("GMGN_API_RATE_LIMIT_RPS", "10")),
            )
            self.agent_client = GMGNAgentClient(config)
            log.info("GMGN Adapter: Agent API enabled")
        else:
            log.info("GMGN Adapter: Agent API disabled (no API key)")

        # Internal state
        self.ws_enabled = enabled
        self.agent_enabled = self.agent_client is not None
        self._last_poll = 0.0
        self._poll_interval = 300  # 5 minutes for Agent API polling
        self._agent_poll_thread: Optional[threading.Thread] = None
        self._stop_poll = threading.Event()

        # Statistics
        self.stats = {
            "ws_events": 0,
            "agent_events": 0,
            "total_events": 0,
            "last_ws_event_ts": None,
            "last_agent_event_ts": None,
            "ws_status": "disabled",
            "agent_status": "disabled" if not self.agent_client else "enabled",
        }

    def start(self) -> None:
        """Start both WebSocket scanner and Agent API polling."""
        if not self.enabled:
            return

        # Start WebSocket scanner
        self.ws_scanner.start()
        self.stats["ws_status"] = "connecting"

        # Start Agent API polling thread
        if self.agent_client:
            self._stop_poll.clear()
            self._agent_poll_thread = threading.Thread(target=self._agent_poll_loop, daemon=True)
            self._agent_poll_thread.start()
            self.stats["agent_status"] = "polling"

        log.info("GMGN Adapter started (WS: enabled, Agent: %s)", "enabled" if self.agent_client else "disabled")

    def stop(self) -> None:
        """Stop all components."""
        self.ws_scanner.stop()
        self._stop_poll.set()
        if self._agent_poll_thread and self._agent_poll_thread.is_alive():
            self._agent_poll_thread.join(timeout=5)
        if self.agent_client:
            self.agent_client.close()
        self.stats["ws_status"] = "stopped"
        self.stats["agent_status"] = "stopped"
        log.info("GMGN Adapter stopped")

    def _agent_poll_loop(self) -> None:
        """Background loop to poll Agent API endpoints."""
        while not self._stop_poll.is_set():
            try:
                self._poll_agent_api()
            except Exception as e:
                log.warning("GMGN Agent poll error: %s", e)
            self._stop_poll.wait(300)  # Poll every 5 minutes

    def _poll_agent_api(self) -> None:
        """Poll Agent API for intelligence data."""
        if not self.agent_client:
            return

        try:
            # Get trending swaps (free endpoint)
            trending = self.agent_client.get_trending_swaps(chain="sol", interval="1h", limit=50)
            if trending:
                self._normalize_agent_trending(trending)

            # Get token signals (smart money, KOL)
            signals = self.agent_client.get_token_signal(chain="sol")
            if signals:
                self._normalize_agent_signals(signals)

            # Get hot searches
            hot = self.agent_client.get_hot_searches(chains=["sol"], interval="1h", limit=100)
            if hot:
                self._normalize_agent_hot_searches(hot)

            # Get Trenches (new token creations)
            trenches = self.agent_client.get_trenches(chain="sol", limit=50)
            if trenches:
                self._normalize_agent_trenches(trenches)

        except Exception as e:
            log.warning("Agent API poll error: %s", e)

    def _normalize_agent_trending(self, data: List[Dict]) -> None:
        """Normalize trending swaps to internal events."""
        for item in data[:20]:  # Limit to top 20
            try:
                mint = str(item.get("address") or item.get("token_address") or "")[:64]
                if not mint:
                    continue

                event = GMGNEvent(
                    event_id=f"gmgn_agent_trending_{mint[:16]}_{int(time.time() * 1000)}",
                    source="gmgn_agent",
                    event_type="trending",
                    chain="solana",
                    token_mint=mint,
                    token_symbol=str(item.get("symbol") or "")[:24],
                    token_name=str(item.get("name") or "")[:80],
                    pool_address=str(item.get("pool_address") or "")[:64],
                    transaction_signature="",
                    received_at=time.time(),
                    market_cap_usd=item.get("market_cap_usd") or item.get("market_cap"),
                    liquidity_usd=item.get("liquidity_usd") or item.get("liquidity"),
                    price_usd=item.get("price_usd") or item.get("price"),
                    volume_usd=item.get("volume_usd") or item.get("volume_24h"),
                    raw_payload=item,
                    data_quality="partial",
                    confidence=0.7,
                    verification_status="unverified",
                )
                self._add_event(event)
            except Exception as e:
                log.debug("Failed to normalize trending item: %s", e)

    def _normalize_agent_signals(self, data: List[Dict]) -> None:
        """Normalize token signals (smart money, KOL, and extended types) to internal events."""
        for group in data:
            if not isinstance(group, dict):
                continue
            tokens = group.get("tokens") or group.get("list") or []
            for item in tokens[:30]:  # Limit per group
                try:
                    mint = str(item.get("token_address") or item.get("address") or "")[:64]
                    if not mint:
                        continue

                    signal_type = item.get("signal_type")
                    is_smart_money = signal_type in [1, 2, 3, 4, 5]  # Smart money signal types
                    is_kol = signal_type in [6, 7, 8, 9, 10]  # KOL signal types
                    # Extended signal types (advisory only — does not change trading decisions)
                    is_insider = signal_type == 11
                    is_bundler = signal_type == 12
                    is_rat_trader = signal_type == 13
                    is_sniper = signal_type == 17
                    is_dev_wallet = signal_type == 18
                    is_team_wallet = signal_type == 19
                    is_cex_wallet = signal_type == 20
                    is_mev_bot = signal_type == 21

                    event = GMGNEvent(
                        event_id=f"gmgn_agent_signal_{mint[:16]}_{int(time.time() * 1000)}",
                        source="gmgn_agent",
                        event_type="signal",
                        chain="solana",
                        token_mint=str(item.get("token_address") or item.get("address") or "")[:64],
                        token_symbol=str(item.get("symbol") or "")[:24],
                        token_name=str(item.get("name") or "")[:80],
                        wallet_address=str(item.get("wallet_address") or item.get("trader") or "")[:64],
                        transaction_signature=str(item.get("signature") or "")[:100],
                        received_at=time.time(),
                        market_cap_usd=item.get("market_cap_usd"),
                        price_usd=item.get("price_usd"),
                        volume_usd=item.get("volume_usd"),
                        buy_count=item.get("buy_count"),
                        raw_payload=item,
                        data_quality="partial",
                        confidence=0.8,
                        verification_status="unverified",
                        smart_money_buy=is_smart_money,
                        kol_buy=is_kol,
                        is_insider=is_insider,
                        is_bundler=is_bundler,
                        is_rat_trader=is_rat_trader,
                        is_sniper=is_sniper,
                        is_dev_wallet=is_dev_wallet,
                        is_team_wallet=is_team_wallet,
                        is_cex_wallet=is_cex_wallet,
                        is_mev_bot=is_mev_bot,
                        signal_type=signal_type,
                        signal_strength=float(item.get("strength") or item.get("amount_usd") or 0),
                    )
                    self._add_event(event)
                except Exception as e:
                    log.debug("Failed to normalize signal item: %s", e)

    def _normalize_agent_hot_searches(self, data: List[Dict]) -> None:
        """Normalize hot searches to internal events."""
        for chain_data in data:
            if not isinstance(chain_data, dict):
                continue
            tokens = chain_data.get("tokens") or chain_data.get("list") or []
            for item in tokens[:20]:
                try:
                    mint = str(item.get("address") or item.get("token_address") or "")[:64]
                    if not mint:
                        continue

                    event = GMGNEvent(
                        event_id=f"gmgn_agent_hot_{mint[:16]}_{int(time.time() * 1000)}",
                        source="gmgn_agent",
                        event_type="hot_search",
                        chain="solana",
                        token_mint=mint,
                        token_symbol=str(item.get("symbol") or "")[:24],
                        token_name=str(item.get("name") or "")[:80],
                        received_at=time.time(),
                        market_cap_usd=item.get("market_cap_usd"),
                        price_usd=item.get("price_usd"),
                        volume_usd=item.get("volume_usd"),
                        raw_payload=item,
                        data_quality="partial",
                        confidence=0.6,
                        verification_status="unverified",
                    )
                    self._add_event(event)
                except Exception as e:
                    log.debug("Failed to normalize hot search item: %s", e)

    def _normalize_agent_trenches(self, data: Dict) -> None:
        """Normalize Trenches data to internal events."""
        for trench_type in ["new_creation", "near_completion", "completed"]:
            tokens = data.get(trench_type) or data.get(trench_type.replace("_", "-")) or []
            if not isinstance(tokens, list):
                continue
            for item in tokens[:20]:
                try:
                    mint = str(item.get("address") or item.get("token_address") or "")[:64]
                    if not mint:
                        continue

                    event = GMGNEvent(
                        event_id=f"gmgn_agent_trench_{mint[:16]}_{int(time.time() * 1000)}",
                        source="gmgn_agent",
                        event_type="trench",
                        chain="solana",
                        token_mint=mint,
                        token_symbol=str(item.get("symbol") or "")[:24],
                        token_name=str(item.get("name") or "")[:80],
                        pool_address=str(item.get("pool_address") or "")[:64],
                        received_at=time.time(),
                        market_cap_usd=item.get("market_cap_usd") or item.get("market_cap"),
                        liquidity_usd=item.get("liquidity_usd") or item.get("liquidity"),
                        price_usd=item.get("price_usd") or item.get("price"),
                        volume_usd=item.get("volume_usd") or item.get("volume"),
                        raw_payload={"trench_type": trench_type, **item},
                        data_quality="partial",
                        confidence=0.7,
                        verification_status="unverified",
                    )
                    self._add_event(event)
                except Exception as e:
                    log.debug("Failed to normalize trench item: %s", e)

    def _add_event(self, event: GMGNEvent) -> None:
        """Add normalized event to the internal buffer."""
        with self.lock:
            self.events.insert(0, event)
            # Keep only max_events
            if len(self.events) > self.max_events:
                self.events = self.events[:self.max_events]
            self.stats["total_events"] += 1
            self.stats["last_agent_event_ts"] = time.time()

    def get_ws_events(self, limit: int = 50) -> List[Dict]:
        """Get recent WebSocket events (normalized)."""
        ws_events = self.ws_scanner.get_recent_events(limit)
        normalized = []
        for ev in ws_events:
            # Convert to GMGNEvent format
            event = GMGNEvent(
                event_id=ev.get("event_id", f"gmgn_ws_{int(time.time()*1000)}"),
                source="gmgn_ws",
                event_type=ev.get("event_type", "unknown"),
                chain=ev.get("chain", "solana"),
                token_mint=ev.get("token_mint", ""),
                token_symbol=ev.get("token_symbol", ""),
                token_name=ev.get("token_name", ""),
                pool_address=ev.get("pool_address", ""),
                wallet_address=ev.get("wallet_address", ""),
                transaction_signature=ev.get("transaction_signature", ""),
                slot=ev.get("slot"),
                block_time=ev.get("block_time"),
                received_at=ev.get("received_at", time.time()),
                market_cap_usd=ev.get("market_cap_usd"),
                liquidity_usd=ev.get("liquidity_usd"),
                price_usd=ev.get("price_usd"),
                volume_usd=ev.get("volume_usd"),
                buy_count=ev.get("buy_count"),
                sell_count=ev.get("sell_count"),
                holder_count=ev.get("holder_count"),
                migration_status=ev.get("migration_status"),
                raw_payload=ev.get("raw_payload", {}),
                data_quality=ev.get("data_quality", "partial"),
                confidence=ev.get("confidence", 0.5),
                verification_status=ev.get("verification_status", "unverified"),
            )
            normalized.append(event.__dict__)
        return normalized

    def get_agent_events(self, limit: int = 50) -> List[Dict]:
        """Get recent Agent API events."""
        with self.lock:
            return [e.__dict__ for e in self.events[:limit]]

    def get_all_events(self, limit: int = 50) -> List[Dict]:
        """Get all events (WS + Agent) sorted by received time."""
        ws_events = self.get_ws_events(limit)
        agent_events = self.get_agent_events(limit)
        all_events = ws_events + agent_events
        all_events.sort(key=lambda x: x.get("received_at", 0), reverse=True)
        return all_events[:limit]

    def get_status(self) -> Dict[str, Any]:
        """Get combined status of both sources."""
        ws_status = self.ws_scanner.snapshot()
        agent_status = self.agent_client.get_status() if self.agent_client else {"enabled": False}

        return {
            "enabled": self.enabled,
            "ws": {
                "status": ws_status.get("state", "disabled"),
                "connected": ws_status.get("connected", False),
                "subscribed": ws_status.get("subscribed", False),
                "received": ws_status.get("received", 0),
                "events_by_channel": ws_status.get("events_by_channel", {}),
                "last_event_ts": ws_status.get("last_event_ts"),
                "last_error": ws_status.get("last_error"),
            },
            "agent": {
                "enabled": self.agent_client is not None,
                "status": self.stats.get("agent_status", "disabled"),
                "stats": agent_status.get("stats", {}),
            },
            "combined": {
                "total_events": self.stats.get("total_events", 0),
                "ws_events": self.stats.get("ws_events", 0),
                "agent_events": self.stats.get("agent_events", 0),
                "last_ws_event_ts": self.stats.get("last_ws_event_ts"),
                "last_agent_event_ts": self.stats.get("last_agent_event_ts"),
            },
        }

    def get_recent_events(self, limit: int = 50) -> List[Dict]:
        """Get all recent events for integration with discovery."""
        return self.get_all_events(limit)


def create_gmgn_adapter_from_env(data_dir: str) -> Optional["GMGNIntelligenceAdapter"]:
    """Create GMGN Adapter from environment variables."""
    api_key = os.getenv("GMGN_API_KEY", "").strip()
    enabled = os.getenv("GMGN_ENABLED", "1").lower() not in ("0", "false", "no", "off")

    if not enabled:
        log.info("GMGN disabled via GMGN_ENABLED")
        return None

    adapter = GMGNIntelligenceAdapter(
        data_dir=data_dir,
        gmgn_api_key=api_key,
        enabled=enabled,
        max_events=200,
    )
    return adapter