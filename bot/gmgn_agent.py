"""GMGN Official Agent API Client (free endpoints only).

Uses the GMGN OpenAPI with 'exist' authentication (X-APIKEY + timestamp + client_id).
Provides access to free-tier market intelligence endpoints:
- Trending swaps
- Token signals (smart money, KOL activity)
- Hot searches
- Token info, security, pool info
- Token top holders/traders
- Wallet holdings, activity, stats (if API key permits)

Reference: https://github.com/GMGNAI/gmgn-skills
"""
from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("paperbot.gmgn_agent")


@dataclass
class GMGNAgentConfig:
    """Configuration for GMGN Agent API."""
    api_key: str = ""
    host: str = "https://api.gmgn.ai"
    timeout: float = 10.0
    rate_limit_rps: float = 10.0  # Conservative default for free tier


class GMGNAgentClient:
    """GMGN Official Agent API Client for free endpoints only."""

    # Free endpoints (exist auth: X-APIKEY + timestamp + client_id)
    ENDPOINTS = {
        # Market data
        "trending_swaps": ("GET", "/v1/market/rank"),
        "token_signal": ("POST", "/v1/market/token_signal"),
        "hot_searches": ("POST", "/v1/market/hot_searches"),
        "search_market": ("GET", "/v1/market/search"),
        # Token info
        "token_info": ("GET", "/v1/token/info"),
        "token_security": ("GET", "/v1/token/security"),
        "token_pool_info": ("GET", "/v1/token/pool_info"),
        "token_kline": ("GET", "/v1/market/token_kline"),
        "token_top_holders": ("GET", "/v1/market/token_top_holders"),
        "token_top_traders": ("GET", "/v1/market/token_top_traders"),
        # Portfolio (may require higher tier)
        "wallet_holdings": ("GET", "/v1/user/wallet_holdings"),
        "wallet_activity": ("GET", "/v1/user/wallet_activity"),
        "wallet_stats": ("GET", "/v1/user/wallet_stats"),
        "wallet_profits": ("POST", "/v1/user/wallet_profits"),
        "wallet_token_balance": ("GET", "/v1/user/wallet_token_balance"),
        # Trenches
        "trenches": ("POST", "/v1/trenches"),
    }

    def __init__(self, config: GMGNAgentConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "solana-paper-bot/4.0 (gmgn-agent)"
        self._last_request = 0.0
        self._min_interval = 1.0 / max(config.rate_limit_rps, 0.1)
        self.stats = {
            "requests": 0,
            "success": 0,
            "errors": 0,
            "rate_limited": 0,
            "last_error": None,
            "last_success_ts": None,
        }

    def _wait_for_rate_limit(self):
        """Enforce rate limiting."""
        now = time.time()
        elapsed = now - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()

    def _build_headers(self, timestamp: Optional[int] = None) -> Dict[str, str]:
        """Build headers for 'exist' authentication."""
        ts = timestamp or int(time.time() * 1000)
        return {
            "X-APIKEY": self.config.api_key,
            "X-Timestamp": str(ts),
            "X-Client-ID": "solana-paper-bot",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        json_body: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Make authenticated request with rate limiting and error handling."""
        if not self.config.api_key:
            log.debug("GMGN Agent API key not configured")
            return None

        self._wait_for_rate_limit()
        url = f"{self.config.host}{path}"
        headers = self._build_headers()
        self.stats["requests"] += 1

        try:
            if method == "GET":
                resp = self.session.get(url, params=params, headers=headers, timeout=self.config.timeout)
            else:
                resp = self.session.post(url, params=params, json=json_body, headers=headers, timeout=self.config.timeout)

            if resp.status_code == 429:
                self.stats["rate_limited"] += 1
                self.stats["errors"] += 1
                self.stats["last_error"] = "rate_limited"
                log.warning("GMGN Agent API rate limited")
                return None

            if resp.status_code >= 500:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"http_{resp.status_code}"
                log.warning("GMGN Agent API server error: %s", resp.status_code)
                return None

            if resp.status_code == 403:
                self.stats["errors"] += 1
                self.stats["last_error"] = "forbidden"
                log.warning("GMGN Agent API forbidden - check API key permissions")
                return None

            if resp.status_code == 401:
                self.stats["errors"] += 1
                self.stats["last_error"] = "unauthorized"
                log.warning("GMGN Agent API unauthorized - invalid API key")
                return None

            if resp.status_code != 200:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"http_{resp.status_code}"
                log.warning("GMGN Agent API error %s: %s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            if data.get("code") != 0:
                self.stats["errors"] += 1
                err_code = data.get("code")
                err_msg = data.get("message") or data.get("error") or "unknown"
                self.stats["last_error"] = f"api_code_{err_code}"
                if err_code == 4001:  # PLAN_UPGRADE_REQUIRED or similar
                    log.warning("GMGN Agent API endpoint requires paid plan: %s", err_msg)
                else:
                    log.warning("GMGN Agent API error code %s: %s", err_code, err_msg)
                return None

            self.stats["success"] += 1
            self.stats["last_success_ts"] = time.time()
            return data.get("data")

        except requests.exceptions.Timeout:
            self.stats["errors"] += 1
            self.stats["last_error"] = "timeout"
            log.warning("GMGN Agent API timeout")
            return None
        except requests.exceptions.ConnectionError:
            self.stats["errors"] += 1
            self.stats["last_error"] = "connection_error"
            log.warning("GMGN Agent API connection error")
            return None
        except Exception as e:
            self.stats["errors"] += 1
            self.stats["last_error"] = f"exception_{type(e).__name__}"
            log.warning("GMGN Agent API exception: %s", e)
            return None

    # ===== Market Data Endpoints =====

    def get_trending_swaps(
        self,
        chain: str = "sol",
        interval: str = "1h",
        limit: int = 100,
        order_by: Optional[str] = None,
        direction: Optional[str] = None,
        filters: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None,
    ) -> Optional[List[Dict]]:
        """Get trending token swaps (trending/rank endpoint)."""
        params = {"chain": chain, "interval": interval}
        if limit:
            params["limit"] = min(limit, 100)
        if order_by:
            params["order_by"] = order_by
        if direction:
            params["direction"] = direction
        if filters:
            params["filters"] = filters
        if platforms:
            params["platforms"] = platforms

        data = self._request("GET", self.ENDPOINTS["trending_swaps"][1], params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "list" in data:
            return data["list"]
        return data

    def get_token_signal(
        self,
        chain: str = "sol",
        groups: Optional[List[Dict]] = None,
    ) -> Optional[List[Dict]]:
        """Get token signals (smart money, KOL buys, large buys, etc.)."""
        if not groups:
            groups = [{"signal_type": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 17, 18, 19, 20, 21]}]
        body = {"chain": chain, "groups": groups}
        return self._request("POST", self.ENDPOINTS["token_signal"][1], json_body=body)

    def get_hot_searches(
        self,
        chains: List[str] = None,
        interval: str = "24h",
        limit: int = 500,
        filters: Optional[List[str]] = None,
    ) -> Optional[List[Dict]]:
        """Get hot search rankings."""
        if chains is None:
            chains = ["sol", "bsc", "base", "eth", "arbitrum"]
        params_list = []
        for chain in chains:
            param = {"label": "hot-search", "chain": chain, "interval": interval}
            if limit:
                param["limit"] = min(limit, 500)
            if filters:
                param["filters"] = filters
            params_list.append(param)
        return self._request("POST", self.ENDPOINTS["hot_searches"][1], json_body={"params": params_list})

    def search_market(
        self,
        query: str,
        chain: Optional[str] = None,
        launchpad_platform: Optional[List[str]] = None,
    ) -> Optional[List[Dict]]:
        """Search for token or wallet by name/symbol/address."""
        params = {"q": query}
        if chain:
            params["chain"] = chain
        if launchpad_platform:
            params["launchpad_platform"] = launchpad_platform
        return self._request("GET", self.ENDPOINTS["search_market"][1], params=params)

    # ===== Token Endpoints =====

    def get_token_info(self, chain: str, address: str) -> Optional[Dict]:
        """Get token basic information."""
        return self._request("GET", self.ENDPOINTS["token_info"][1], params={"chain": chain, "address": address})

    def get_token_security(self, chain: str, address: str) -> Optional[Dict]:
        """Get token security analysis."""
        return self._request("GET", self.ENDPOINTS["token_security"][1], params={"chain": chain, "address": address})

    def get_token_pool_info(self, chain: str, address: str) -> Optional[Dict]:
        """Get token pool information."""
        return self._request("GET", self.ENDPOINTS["token_pool_info"][1], params={"chain": chain, "address": address})

    def get_token_kline(
        self,
        chain: str,
        address: str,
        resolution: str = "1h",
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> Optional[List[Dict]]:
        """Get token candlestick data."""
        params = {"chain": chain, "address": address, "resolution": resolution}
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts
        return self._request("GET", self.ENDPOINTS["token_kline"][1], params=params)

    def get_token_top_holders(
        self,
        chain: str,
        address: str,
        limit: int = 100,
    ) -> Optional[List[Dict]]:
        """Get top token holders."""
        params = {"chain": chain, "address": address}
        if limit:
            params["limit"] = limit
        return self._request("GET", self.ENDPOINTS["token_top_holders"][1], params=params)

    def get_token_top_traders(
        self,
        chain: str,
        address: str,
        limit: int = 100,
    ) -> Optional[List[Dict]]:
        """Get top token traders."""
        params = {"chain": chain, "address": address}
        if limit:
            params["limit"] = limit
        return self._request("GET", self.ENDPOINTS["token_top_traders"][1], params=params)

    # ===== Portfolio Endpoints (may require paid tier) =====

    def get_wallet_holdings(
        self,
        chain: str,
        wallet_address: str,
        limit: int = 100,
    ) -> Optional[List[Dict]]:
        """Get wallet token holdings."""
        params = {"chain": chain, "wallet_address": wallet_address}
        if limit:
            params["limit"] = limit
        return self._request("GET", self.ENDPOINTS["wallet_holdings"][1], params=params)

    def get_wallet_activity(
        self,
        chain: str,
        wallet_address: str,
        limit: int = 100,
    ) -> Optional[List[Dict]]:
        """Get wallet trading activity."""
        params = {"chain": chain, "wallet_address": wallet_address}
        if limit:
            params["limit"] = limit
        return self._request("GET", self.ENDPOINTS["wallet_activity"][1], params=params)

    def get_wallet_stats(
        self,
        chain: str,
        wallet_addresses: List[str],
        period: str = "7d",
    ) -> Optional[Dict]:
        """Get wallet statistics (PnL, win rate, etc.)."""
        params = {"chain": chain, "wallet_address": wallet_addresses, "period": period}
        return self._request("GET", self.ENDPOINTS["wallet_stats"][1], params=params)

    def get_wallet_profits(
        self,
        chain: str,
        wallet_addresses: List[str],
        period: str = "7d",
    ) -> Optional[Dict]:
        """Get wallet profit details."""
        body = {"chain": chain, "period": period, "wallet_addresses": wallet_addresses}
        return self._request("POST", self.ENDPOINTS["wallet_profits"][1], json_body=body)

    def get_wallet_token_balance(
        self,
        chain: str,
        wallet_address: str,
        token_address: str,
    ) -> Optional[Dict]:
        """Get specific token balance for wallet."""
        params = {"chain": chain, "wallet_address": wallet_address, "token_address": token_address}
        return self._request("GET", self.ENDPOINTS["wallet_token_balance"][1], params=params)

    # ===== Trenches =====

    def get_trenches(
        self,
        chain: str = "sol",
        types: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None,
        limit: int = 80,
        filters: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Get Trenches token data (new creation, near completion, completed)."""
        if types is None:
            types = ["new_creation", "near_completion", "completed"]
        body = {
            "version": "v2",
            "filters": ["offchain", "onchain"],
            "launchpad_platform_v2": True,
            "limit": min(limit, 80),
            **(filters or {}),
        }
        if platforms:
            body["launchpad_platform"] = platforms
        for t in types:
            body[t] = body.get(t, {})
        return self._request("POST", self.ENDPOINTS["trenches"][1], json_body=body)

    def get_status(self) -> Dict[str, Any]:
        """Return client status and statistics."""
        return {
            "enabled": bool(self.config.api_key),
            "host": self.config.host,
            "rate_limit_rps": self.config.rate_limit_rps,
            "stats": dict(self.stats),
        }

    def close(self):
        """Close the session."""
        try:
            self.session.close()
        except Exception:
            pass


def create_gmgn_agent_from_env() -> Optional[GMGNAgentClient]:
    """Create GMGN Agent client from environment variables."""
    api_key = os.getenv("GMGN_API_KEY", "").strip()
    if not api_key:
        log.info("GMGN Agent API key not configured - Agent API disabled")
        return None

    config = GMGNAgentConfig(
        api_key=api_key,
        host=os.getenv("GMGN_API_HOST", "https://api.gmgn.ai"),
        timeout=float(os.getenv("GMGN_API_TIMEOUT", "10")),
        rate_limit_rps=float(os.getenv("GMGN_API_RATE_LIMIT_RPS", "10")),
    )
    return GMGNAgentClient(config)