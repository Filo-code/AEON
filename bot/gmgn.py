"""GMGN discovery adapter (read-only, free channels only).

Uses the GmGnAPI open-source client to subscribe to public GMGN WebSocket channels:
- new_pools
- pair_update
- token_launch
- chain_stats

Never subscribes to authenticated channels (token_social, wallet_trades, limit_orders).
No wallet creation, no private keys, no transactions.
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("paperbot.gmgn")

# WSOL mint - used as default quote for bonding curve tokens
SOL = "So11111111111111111111111111111111111111112"


class GmGnScanner:
    """GMGN WebSocket scanner for free public channels."""

    # Public GMGN WebSocket endpoint (verified from GmGnAPI source)
    # Reference: https://github.com/ChipaDevTeam/GmGnAPI
    # The actual endpoint is wss://gmgn.ai/ws with query params for device/session
    # API key enables authenticated channels (wallet_trade_data, token_social_info, limit_order_info)
    URL = "wss://gmgn.ai/ws"

    def __init__(
        self,
        data_dir: str,
        api_key: str = "",
        enabled: bool = True,
        max_events: int = 200,
        public_channels: Optional[List[str]] = None,
    ):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "gmgn_state.json"
        self.api_key = (api_key or "").strip()
        self.enabled = bool(enabled)
        # Subscription channel names (what we send to the server) - verified from GmGnAPI
        self.subscription_channels = public_channels or [
            "new_pool_info",
            "new_pair_update",
            "new_launched_info",
            "chain_stat",
        ]
        # Internal normalized event types (what we emit)
        self.event_type_map = {
            "new_pools": "new_pool",
            "pair_update": "pair_update",
            "token_launch": "token_launch",
            "chain_stats": "chain_stats",
        }
        self.events = deque(maxlen=max_events)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.ws = None
        self._subscribed = False
        self._subscription_acknowledged = False
        self._subscription_ack_ts = None
        self._stable_session = False
        self.status = {
            "enabled": self.enabled,
            "connected": False,
            "subscribed": False,
            "state": "disabled" if not self.enabled else "disconnected",
            "received": 0,
            "events_by_channel": {v: 0 for v in self.event_type_map.values()},
            "last_event_ts": None,
            "last_error": None,
            "updated_ts": time.time(),
            "events": [],
        }
        if enabled and not self.api_key:
            log.info("GMGN running without API key: public channels only")
        self._persist()

    def _persist(self) -> None:
        """Atomically persist state to disk."""
        with self.lock:
            payload = dict(self.status)
            payload["events"] = list(self.events)
            payload["updated_ts"] = time.time()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception as exc:
            log.warning("GMGN state persist failed: %s", type(exc).__name__)

    @staticmethod
    def _normalize_timestamp(value: Any) -> Optional[float]:
        """
        Normalize timestamp to Unix seconds (float).
        Accepts:
        - Unix seconds (int/float)
        - Unix milliseconds (int/float > 1e12)
        - ISO 8601 strings
        Returns None for invalid or future timestamps.
        """
        if value is None:
            return None
        try:
            # Already a number
            if isinstance(value, (int, float)):
                # Milliseconds if > year 2001 in ms (roughly 1e12)
                if value > 1e12:
                    value = value / 1000.0
                # Sanity check: not in the future (allow 1 hour clock skew)
                if value > time.time() + 3600:
                    return None
                # Not before 2020
                if value < 1577836800:
                    return None
                return float(value)
            # String - try ISO format
            if isinstance(value, str):
                # Try ISO format first
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                ts = dt.timestamp()
                if ts > time.time() + 3600 or ts < 1577836800:
                    return None
                return ts
        except Exception:
            pass
        return None

    @staticmethod
    def _event_kind(data: Dict[str, Any]) -> Optional[str]:
        """
        Determine the GMGN internal event type from the payload.
        Maps subscription channel names to internal event types.
        """
        if not isinstance(data, dict):
            return None

        # GMGN messages have a "channel" field with actual channel names
        channel = data.get("channel") or ""
        channel = str(channel).lower()

        # Map actual GMGN subscription channels to internal event types
        if channel == "new_pool_info":
            return "new_pool"
        if channel == "new_pair_update":
            return "pair_update"
        if channel == "new_launched_info":
            return "token_launch"
        if channel == "chain_stat":
            return "chain_stats"

        # Fallback: try to infer from payload structure
        if "mint" in data and ("name" in data or "symbol" in data):
            # Looks like a new token/pool
            if data.get("pool") or data.get("pool_address"):
                return "new_pool"
            return "token_launch"
        if "pool" in data and ("mint" in data or "token_mint" in data):
            return "pair_update"

        return None

    def _generate_event_id(self, kind: str, data: Dict[str, Any], received_ts: float) -> str:
        """
        Generate a stable event ID using available identifiers.
        Prefers: transaction_signature > pool_address > mint+received_ts
        """
        signature = str(data.get("signature") or data.get("tx_signature") or "")[:100]
        pool = str(data.get("pool") or data.get("pool_address") or data.get("bonding_curve") or "")[:64]
        mint = str(data.get("mint") or data.get("token_mint") or data.get("base_mint") or "")[:64]

        if signature:
            return f"gmgn_{kind}_{signature[:32]}"
        if pool:
            return f"gmgn_{kind}_{pool[:32]}"
        if mint:
            return f"gmgn_{kind}_{mint[:16]}_{int(received_ts * 1000)}"
        return f"gmgn_{kind}_unknown_{int(received_ts * 1000)}"

    def _parse_event(self, data: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
        """Parse GMGN event into unified normalized format."""
        try:
            received_ts = time.time()
            mint = str(data.get("mint") or data.get("token_mint") or data.get("base_mint") or "")[:64]
            name = str(data.get("name") or data.get("token_name") or "")[:80]
            symbol = str(data.get("symbol") or data.get("token_symbol") or "")[:24]
            creator = str(data.get("creator") or data.get("deployer") or data.get("user") or "")[:64]
            pool = str(data.get("pool") or data.get("pool_address") or data.get("bonding_curve") or "")[:64]
            signature = str(data.get("signature") or data.get("tx_signature") or "")[:100]

            # Quote/base mints
            quote_mint = str(data.get("quote_mint") or data.get("quote") or "")[:64]
            # Bonding curve launches may omit quote_mint - default to SOL/WSOL
            if not quote_mint:
                quote_mint = SOL
            base_mint = str(data.get("base_mint") or mint)[:64]

            # Market data (may be None for some channels)
            market_cap_sol = data.get("market_cap_sol") or data.get("marketCapSol")
            market_cap_usd = data.get("market_cap_usd") or data.get("marketCapUsd")
            liquidity = data.get("liquidity") or data.get("liquidity_usd")
            price = data.get("price") or data.get("price_usd")
            volume = data.get("volume") or data.get("volume_usd")
            buys = data.get("buys") or data.get("buy_count")
            sells = data.get("sells") or data.get("sell_count")
            holders = data.get("holders") or data.get("holder_count")

            # On-chain identifiers
            slot = data.get("slot")
            block_time = data.get("block_time") or data.get("block_timestamp")
            migration_status = data.get("migration_status") or data.get("migrated")

            # Normalize timestamp
            normalized_block_time = self._normalize_timestamp(block_time)

            # Data quality assessment
            has_mint = bool(mint)
            has_price = price is not None
            has_liquidity = liquidity is not None
            has_quote_mint = bool(quote_mint)
            data_quality = "complete" if (has_mint and has_price and has_liquidity and has_quote_mint) else "partial"
            confidence = 0.9 if data_quality == "complete" else 0.5
            verification_status = "unverified"

            event = {
                "event_id": self._generate_event_id(kind, data, received_ts),
                "source": "gmgn",
                "event_type": kind,
                "chain": "solana",
                "token_mint": mint,
                "token_symbol": symbol,
                "token_name": name,
                "pool_address": pool,
                "deployer_address": creator,
                "wallet_address": creator if kind == "token_launch" else None,
                "transaction_signature": signature,
                "slot": slot,
                "block_time": normalized_block_time,
                "received_at": received_ts,
                "market_cap_usd": market_cap_usd,
                "liquidity_usd": liquidity,
                "price_usd": price,
                "volume_usd": volume,
                "buy_count": buys,
                "sell_count": sells,
                "holder_count": holders,
                "migration_status": migration_status,
                "raw_payload": data,
                "data_quality": data_quality,
                "confidence": confidence,
                "verification_status": verification_status,
                # Additional fields for adapter
                "base_mint": base_mint,
                "quote_mint": quote_mint,
                "pool_type": "amm" if "pool" in str(data).lower() else "bonding_curve",
            }
            return event
        except Exception as e:
            log.warning("Failed to parse GMGN event: %s", e)
            return None

    def _mark_session_stable(self) -> None:
        ack_ts = self._subscription_ack_ts
        if ack_ts is not None and time.monotonic() - ack_ts >= 60.0:
            self._stable_session = True
        self._subscription_ack_ts = None

    def _on_message(self, _ws, raw: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(raw)
        except Exception:
            log.debug("GMGN invalid JSON message")
            return

        # Handle subscription confirmation messages (GmGnAPI format)
        if isinstance(data, dict) and data.get("action") == "subscribe" and "result" in data:
            log.info("GMGN subscription confirmed: %s", data.get("result"))
            self._subscription_acknowledged = True
            self._subscription_ack_ts = time.monotonic()
            self._subscribed = True
            self.status.update(subscribed=True, state="receiving_events", last_error=None)
            self._persist()
            return

        # Handle subscription error messages
        if isinstance(data, dict) and data.get("action") == "subscribe" and "error" in data:
            log.warning("GMGN subscription error: %s", data.get("error"))
            self._subscription_acknowledged = False
            self._subscription_ack_ts = None
            self._subscribed = False
            self.status.update(subscribed=False, state="error", last_error=str(data.get("error"))[:180])
            self._persist()
            return

        kind = self._event_kind(data)
        if not kind:
            # Unknown channel or malformed - log and skip
            log.debug("GMGN unknown event type: %s", list(data.keys())[:5] if isinstance(data, dict) else type(data))
            return

        # Only process public channels we subscribed to
        if kind not in self.event_type_map.values():
            return

        ts = time.time()
        parsed = self._parse_event(data, kind)
        if not parsed:
            return

        # Skip chain_stats for watch list (handled separately)
        if kind == "chain_stats":
            log.debug("GMGN chain_stats received")
            with self.lock:
                self.events.appendleft(parsed)
                self.status["received"] += 1
                self.status["events_by_channel"][kind] = self.status["events_by_channel"].get(kind, 0) + 1
                self.status["last_event_ts"] = ts
                self.status["last_error"] = None
            self._persist()
            return

        with self.lock:
            self.events.appendleft(parsed)
            self.status["received"] += 1
            self.status["events_by_channel"][kind] = self.status["events_by_channel"].get(kind, 0) + 1
            self.status["last_event_ts"] = ts
            self.status["state"] = "receiving_events"
            self.status["last_error"] = None
        self._persist()
        log.info("GMGN event: %s %s (%s)", kind, parsed.get("token_symbol", "?"), parsed.get("token_mint", "?")[:8])

    def _on_open(self, ws) -> None:
        """WebSocket opened - subscribe to public channels."""
        self.ws = ws
        self._subscription_ack_ts = None
        self._stable_session = False
        self.status.update(connected=True, state="connecting", last_error=None)
        # Subscribe to each public channel using GmGnAPI format
        for channel in self.subscription_channels:
            sub_msg = json.dumps({
                "action": "subscribe",
                "channel": channel,
                "f": "w",
                "data": [{"chain": "sol"}]
            })
            try:
                ws.send(sub_msg)
                log.info("GMGN subscription sent for: %s", channel)
            except Exception as e:
                log.warning("GMGN subscribe failed for %s: %s", channel, e)
        self._subscribed = False
        self._subscription_acknowledged = False
        self.status.update(subscribed=False)
        self._persist()

    def _on_error(self, _ws, error) -> None:
        self.status.update(connected=False, subscribed=False, state="error", last_error=str(error)[:180])
        self._mark_session_stable()
        self._subscribed = False
        self._subscription_acknowledged = False
        self._persist()
        log.warning("GMGN websocket error: %s", type(error).__name__)

    def _on_close(self, _ws, code, reason) -> None:
        state = "reconnecting" if not self.stop_event.is_set() else "stopped"
        self.status.update(connected=False, subscribed=False, state=state, last_error=(str(reason)[:180] if reason else None))
        self._mark_session_stable()
        self._subscribed = False
        self._subscription_acknowledged = False
        self._persist()
        log.info("GMGN websocket closed: %s %s", code, reason)

    def _run(self) -> None:
        """Main WebSocket loop with exponential backoff."""
        try:
            import websocket
        except Exception as exc:
            self.status.update(state="dependency_missing", last_error="Installa websocket-client")
            self._persist()
            log.warning("GMGN disabled: websocket-client missing (%s)", type(exc).__name__)
            return

        # Exponential backoff: 3, 6, 12, 24, 48, 60 (cap)
        delay = 3
        max_delay = 60
        while not self.stop_event.is_set():
            try:
                self.status.update(state="connecting", connected=False, subscribed=False)
                self._persist()

                # Build URL with query parameters matching GmGnAPI client
                import uuid
                import urllib.parse
                device_id = str(uuid.uuid4())
                fp_did = uuid.uuid4().hex
                user_uuid = uuid.uuid4().hex[:16]
                app_version = "20260202-10623-98faccb"
                params = {
                    "device_id": device_id,
                    "fp_did": fp_did,
                    "client_id": f"gmgn_web_{app_version}",
                    "from_app": "gmgn",
                    "app_ver": app_version,
                    "tz_name": "Europe/Rome",
                    "tz_offset": "3600",
                    "app_lang": "it-IT",
                    "os": "web",
                    "worker": "0",
                    "uuid": user_uuid,
                    "reconnect": "0",
                }
                if self.api_key:
                    params["access_token"] = self.api_key
                query_string = urllib.parse.urlencode(params)
                url = f"{self.URL}?{query_string}"

                app = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws = app
                app.run_forever(ping_interval=30, ping_timeout=10, reconnect=0)
                self._mark_session_stable()
            except Exception as exc:
                self.status.update(state="error", connected=False, subscribed=False, last_error=str(exc)[:180])
                self._persist()
                log.warning("GMGN loop failed: %s", type(exc).__name__)
            if self._stable_session:
                delay = 3
                self._stable_session = False
                log.info("GMGN stable session: backoff reset")
            if not self.stop_event.is_set():
                self.stop_event.wait(delay)
                delay = min(max_delay, delay * 2)
        self.status.update(state="stopped", connected=False, subscribed=False)
        self._persist()

    def start(self) -> None:
        """Start the GMGN scanner thread."""
        if not self.enabled or (self.thread and self.thread.is_alive()):
            return
        self.thread = threading.Thread(target=self._run, name="gmgn-scanner", daemon=True)
        self.thread.start()
        log.info("GMGN scanner started (public channels: %s)", self.subscription_channels)

    def stop(self) -> None:
        """Stop the GMGN scanner."""
        self.stop_event.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

    def snapshot(self) -> Dict[str, Any]:
        """Return current status and recent events."""
        with self.lock:
            out = dict(self.status)
            out["events"] = list(self.events)[:20]
            return out

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent events for integration with discovery."""
        with self.lock:
            return list(self.events)[:limit]