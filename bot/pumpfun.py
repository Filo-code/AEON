"""Pump.fun discovery adapter (read-only, free channels only).

Uses PumpPortal WebSocket for free new-token and migration events only.
Never subscribes to token/account trades and never creates or signs transactions.
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

log = logging.getLogger("paperbot.pumpfun")


class PumpFunScanner:
    """Pump.fun WebSocket scanner for free public channels."""

    # PumpPortal WebSocket endpoint (verified from official docs)
    URL = "wss://pumpportal.fun/api/data?api-key={key}"

    def __init__(
        self,
        data_dir: str,
        api_key: str = "",
        enabled: bool = True,
        max_events: int = 200,
    ):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "pumpfun_state.json"
        self.api_key = (api_key or "").strip()
        self.enabled = bool(enabled)
        self.max_events = max_events
        self.events = deque(maxlen=max_events)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.ws = None
        self._subscribed = False
        self.status = {
            "enabled": self.enabled,
            "connected": False,
            "subscribed": False,
            "state": "disabled" if not self.enabled else "disconnected",
            "received": 0,
            "events_by_channel": {"new_token": 0, "migration": 0},
            "last_event_ts": None,
            "last_error": None,
            "updated_ts": time.time(),
            "events": [],
        }
        if enabled and not self.api_key:
            log.info("Pump.fun running without API key: some features may be limited")
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
            log.warning("Pump.fun state persist failed: %s", type(exc).__name__)

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
            if isinstance(value, (int, float)):
                if value > 1e12:
                    value = value / 1000.0
                if value > time.time() + 3600:
                    return None
                if value < 1577836800:
                    return None
                return float(value)
            if isinstance(value, str):
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
        """Determine the PumpPortal event type from the payload."""
        if not isinstance(data, dict):
            return None

        # PumpPortal messages have a "type" field (not txType)
        # Verified from live WebSocket: type=create for new tokens, type=migrate for migrations
        event_type = str(data.get("type") or data.get("txType") or "").lower()

        # Check for new token creation (type=create)
        if event_type == "create" or any(k in data for k in ("bondingCurveKey", "initialBuy", "tokenAmount", "uri")) and ("mint" in data or "signature" in data):
            return "new_token"
        if data.get("name") and data.get("symbol") and data.get("mint"):
            return "new_token"

        # Check for migration (type=migrate)
        if event_type == "migrate" or any(k in data for k in ("pool", "poolAddress", "raydiumPool")) and ("mint" in data or "tokenMint" in data):
            return "migration"
        if "migrat" in event_type or "complete" in event_type:
            return "migration"

        return None

    def _generate_event_id(self, kind: str, data: Dict[str, Any], received_ts: float) -> str:
        """
        Generate a stable event ID using available identifiers.
        Prefers: transaction_signature > pool_address > mint+received_ts
        """
        signature = str(data.get("signature") or data.get("txSignature") or "")[:100]
        pool = str(data.get("pool") or data.get("poolAddress") or data.get("raydiumPool") or "")[:64]
        mint = str(data.get("mint") or data.get("tokenMint") or "")[:64]

        if signature:
            return f"pumpfun_{kind}_{signature[:32]}"
        if pool:
            return f"pumpfun_{kind}_{pool[:32]}"
        if mint:
            return f"pumpfun_{kind}_{mint[:16]}_{int(received_ts * 1000)}"
        return f"pumpfun_{kind}_unknown_{int(received_ts * 1000)}"

    def _parse_event(self, data: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
        """Parse PumpPortal event into unified normalized format."""
        try:
            received_ts = time.time()
            mint = str(data.get("mint") or data.get("tokenMint") or "")[:64]
            name = str(data.get("name") or "")[:80]
            symbol = str(data.get("symbol") or "")[:24]
            creator = str(data.get("traderPublicKey") or data.get("creator") or data.get("user") or "")[:64]
            pool = str(data.get("pool") or data.get("poolAddress") or data.get("raydiumPool") or "")[:64]
            signature = str(data.get("signature") or data.get("txSignature") or "")[:100]

            # Quote/base mints
            quote_mint = str(data.get("quoteMint") or data.get("quote_mint") or "")[:64]
            base_mint = str(data.get("baseMint") or data.get("base_mint") or mint)[:64]

            # Market data (may be None for some channels)
            market_cap_sol = data.get("marketCapSol") or data.get("market_cap_sol")
            market_cap_usd = data.get("marketCapUsd") or data.get("market_cap_usd")
            liquidity = data.get("liquidity") or data.get("liquidity_usd")
            price = data.get("price") or data.get("price_usd")
            volume = data.get("volume") or data.get("volume_usd")

            # On-chain identifiers
            slot = data.get("slot")
            block_time = data.get("blockTime") or data.get("block_time") or data.get("timestamp")
            migration_status = data.get("migrationStatus") or data.get("migration_status")

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
                "source": "pumpfun",
                "event_type": kind,
                "chain": "solana",
                "token_mint": mint,
                "token_symbol": symbol,
                "token_name": name,
                "pool_address": pool,
                "deployer_address": creator,
                "wallet_address": creator if kind == "new_token" else None,
                "transaction_signature": signature,
                "slot": slot,
                "block_time": normalized_block_time,
                "received_at": received_ts,
                "market_cap_usd": market_cap_usd,
                "liquidity_usd": liquidity,
                "price_usd": price,
                "volume_usd": volume,
                "buy_count": None,
                "sell_count": None,
                "holder_count": None,
                "migration_status": migration_status,
                "raw_payload": data,
                "data_quality": data_quality,
                "confidence": confidence,
                "verification_status": verification_status,
                # Additional fields for adapter
                "base_mint": base_mint,
                "quote_mint": quote_mint,
                "pool_type": "bonding_curve",
            }
            return event
        except Exception as e:
            log.warning("Failed to parse Pump.fun event: %s", e)
            return None

    def _on_message(self, _ws, raw: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(raw)
        except Exception:
            log.debug("Pump.fun invalid JSON message")
            return

        # Handle subscription confirmation messages (PumpPortal format)
        if isinstance(data, dict) and "message" in data and "subscribed" in str(data.get("message", "")).lower():
            log.info("Pump.fun subscription confirmed: %s", data.get("message"))
            self._subscription_acknowledged = True
            self._subscribed = True
            self.status.update(subscribed=True, state="receiving_events", last_error=None)
            self._persist()
            return

        kind = self._event_kind(data)
        if not kind:
            log.debug("Pump.fun unknown event type: %s", list(data.keys())[:5] if isinstance(data, dict) else type(data))
            return

        # Only process channels we care about
        if kind not in ("new_token", "migration"):
            return

        ts = time.time()
        parsed = self._parse_event(data, kind)
        if not parsed:
            return

        with self.lock:
            self.events.appendleft(parsed)
            self.status["received"] += 1
            self.status["events_by_channel"][kind] = self.status["events_by_channel"].get(kind, 0) + 1
            self.status["last_event_ts"] = ts
            self.status["state"] = "receiving_events"
            self.status["last_error"] = None
        self._persist()
        log.debug("Pump.fun event received: %s", kind)

    def _on_open(self, ws) -> None:
        """WebSocket opened - subscribe to free channels."""
        self.ws = ws
        self.status.update(connected=True, state="subscription_sent", last_error=None)
        # Subscribe to free channels only
        ws.send(json.dumps({"method": "subscribeNewToken"}))
        ws.send(json.dumps({"method": "subscribeMigration"}))
        log.info("PumpPortal subscription sent: new-token + migration streams (read-only)")
        self._subscribed = False
        self._subscription_acknowledged = False
        self.status.update(subscribed=False)
        self._persist()

    def _on_error(self, _ws, error) -> None:
        self.status.update(connected=False, subscribed=False, state="error", last_error=str(error)[:180])
        self._subscribed = False
        self._subscription_acknowledged = False
        self._persist()
        log.warning("Pump.fun websocket error: %s", type(error).__name__)

    def _on_close(self, _ws, code, reason) -> None:
        state = "reconnecting" if not self.stop_event.is_set() else "stopped"
        self.status.update(connected=False, subscribed=False, state=state, last_error=(str(reason)[:180] if reason else None))
        self._subscribed = False
        self._subscription_acknowledged = False
        self._persist()
        log.info("Pump.fun websocket closed: %s %s", code, reason)

    def _run(self) -> None:
        """Main WebSocket loop with exponential backoff."""
        try:
            import websocket
        except Exception as exc:
            self.status.update(state="dependency_missing", last_error="Installa websocket-client")
            self._persist()
            log.warning("Pump.fun disabled: websocket-client missing (%s)", type(exc).__name__)
            return

        delay = 3
        max_delay = 60
        while not self.stop_event.is_set():
            try:
                self.status.update(state="connecting", connected=False, subscribed=False)
                self._persist()

                url = self.URL.format(key=self.api_key) if self.api_key else "wss://pumpportal.fun/api/data"
                app = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws = app
                app.run_forever(ping_interval=30, ping_timeout=10, reconnect=0)
                delay = 3
            except Exception as exc:
                self.status.update(state="error", connected=False, subscribed=False, last_error=str(exc)[:180])
                self._persist()
                log.warning("Pump.fun loop failed: %s", type(exc).__name__)
            if not self.stop_event.is_set():
                self.stop_event.wait(delay)
                delay = min(max_delay, delay * 2)
        self.status.update(state="stopped", connected=False, subscribed=False)
        self._persist()

    def start(self) -> None:
        """Start the Pump.fun scanner thread."""
        if not self.enabled or (self.thread and self.thread.is_alive()):
            return
        self.thread = threading.Thread(target=self._run, name="pumpfun-scanner", daemon=True)
        self.thread.start()
        log.info("Pump.fun scanner started (free channels: new_token, migration)")

    def stop(self) -> None:
        """Stop the Pump.fun scanner."""
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