"""Wallet Intelligence 2.0 — Smart Money Scoring, Classification & Ranking.

This module implements a comprehensive wallet intelligence system for copy-trading decisions.
It builds on existing GMGN data sources to evaluate wallet performance, classify wallets,
rank smart money, and validate copy entries with liquidity-aware sizing.

All calculations are deterministic and use only completed trades.
No unrealized gains are counted as realized P&L.
"""

from __future__ import annotations
import json
import logging
import math
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("paperbot.wallet_intelligence")


# ============================================================
# Configuration
# ============================================================

@dataclass
class WalletIntelligenceConfig:
    """Configuration for wallet intelligence scoring and classification."""

    # Minimum sample sizes for reliable metrics
    min_trades_for_score: int = 10  # Minimum completed trades for scoring
    min_trades_for_elite: int = 25  # Minimum trades for ELITE classification
    min_trades_for_weekly: int = 5  # Minimum trades for WEEKLY_RISING

    # Time windows
    weekly_window_days: int = 7
    monthly_window_days: int = 30
    stale_data_hours: int = 72  # After this, data is considered stale

    # Scoring weights (must sum to 1.0)
    score_weights: Dict[str, float] = field(default_factory=lambda: {
        "realized_pnl": 0.25,       # Total realized P&L
        "profit_factor": 0.20,      # Gross profit / gross loss
        "expectancy": 0.15,         # Expected value per trade
        "win_rate": 0.15,           # Win rate
        "max_drawdown": 0.10,       # Max drawdown (inverted)
        "consistency": 0.10,        # Consistency over time
        "holding_efficiency": 0.05, # Profit per unit time held
    })

    # Classification thresholds
    elite_min_score: float = 0.70   # Minimum score for ELITE
    elite_min_profit_factor: float = 1.5
    elite_min_expectancy: float = 0.05  # 5% expected return per trade
    elite_max_drawdown: float = 0.30    # 30% max drawdown
    elite_max_consecutive_losses: int = 5

    weekly_rising_min_score: float = 0.55
    weekly_rising_min_trades: int = 5

    high_risk_max_score: float = 0.35
    high_risk_min_drawdown: float = 0.50  # 50%+ drawdown
    high_risk_max_win_rate: float = 0.40
    high_risk_max_consecutive_losses: int = 8

    # Copy entry validation thresholds
    min_wallet_score_for_copy: float = 0.40
    max_price_move_since_source_pct: float = 0.50  # 50% price increase since source buy
    max_source_age_minutes: int = 60  # Source buy older than this = late
    min_liquidity_usd_for_copy: float = 5000
    max_price_impact_pct: float = 10.0
    max_slippage_pct: float = 15.0

    # Multiple wallet confirmation
    min_independent_wallets: int = 2
    confirmation_window_minutes: int = 30

    # Liquidity-aware sizing
    max_size_to_liquidity_pct: float = 2.0
    elite_size_multiplier: float = 1.2
    weekly_rising_size_multiplier: float = 1.1
    unverified_size_multiplier: float = 0.5

    # Exit handling
    min_sell_pct_for_complete_exit: float = 0.80  # 80%+ sold = complete exit
    partial_exit_tracking: bool = True

    # Data quality
    min_data_freshness_hours: int = 24
    max_unknown_ratio: float = 0.30  # Max 30% unknown transactions


DEFAULT_CONFIG = WalletIntelligenceConfig()


# ============================================================
# Data Classes
# ============================================================

@dataclass
class WalletTrade:
    """A single completed trade from wallet history."""
    token_mint: str
    token_symbol: str
    buy_timestamp: float
    sell_timestamp: float
    buy_price_usd: float
    sell_price_usd: float
    amount_usd: float
    realized_pnl_usd: float
    realized_pnl_pct: float
    holding_time_seconds: float
    buy_signature: str = ""
    sell_signature: str = ""
    buy_price_impact_pct: Optional[float] = None
    sell_price_impact_pct: Optional[float] = None
    is_verified: bool = True  # False if transaction data incomplete

    def to_dict(self) -> Dict:
        return {
            "token_mint": self.token_mint,
            "token_symbol": self.token_symbol,
            "buy_timestamp": self.buy_timestamp,
            "sell_timestamp": self.sell_timestamp,
            "buy_price_usd": self.buy_price_usd,
            "sell_price_usd": self.sell_price_usd,
            "amount_usd": self.amount_usd,
            "realized_pnl_usd": self.realized_pnl_usd,
            "realized_pnl_pct": self.realized_pnl_pct,
            "holding_time_seconds": self.holding_time_seconds,
            "buy_signature": self.buy_signature,
            "sell_signature": self.sell_signature,
            "buy_price_impact_pct": self.buy_price_impact_pct,
            "sell_price_impact_pct": self.sell_price_impact_pct,
            "is_verified": self.is_verified,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "WalletTrade":
        return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})


@dataclass
class WalletMetrics:
    """Computed wallet performance metrics."""
    wallet_address: str
    total_trades: int = 0
    verified_trades: int = 0
    realized_pnl_usd: float = 0.0
    realized_pnl_pct: float = 0.0
    gross_profit_usd: float = 0.0
    gross_loss_usd: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_per_trade_usd: float = 0.0
    expectancy_per_trade_pct: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_losses: int = 0
    avg_holding_time_seconds: float = 0.0
    avg_holding_time_minutes: float = 0.0
    holding_efficiency: float = 0.0  # P&L per minute held
    tokens_traded: int = 0
    unique_tokens: int = 0
    low_liquidity_exposure_pct: float = 0.0
    best_trade_pnl_pct: float = 0.0
    best_trade_contribution_pct: float = 0.0

    # Time-windowed metrics
    weekly_trades: int = 0
    weekly_pnl_usd: float = 0.0
    weekly_win_rate: float = 0.0
    weekly_profit_factor: float = 0.0
    monthly_trades: int = 0
    monthly_pnl_usd: float = 0.0
    monthly_win_rate: float = 0.0
    monthly_profit_factor: float = 0.0

    # Data quality
    data_freshness_hours: float = 0.0
    unknown_ratio: float = 0.0
    last_trade_timestamp: float = 0.0

    # Computed score (0-1)
    score: float = 0.0
    classification: str = "UNVERIFIED"

    # Ranking
    rank_7d: Optional[int] = None
    rank_30d: Optional[int] = None
    rank_consistency: Optional[int] = None
    rank_risk_adjusted: Optional[int] = None

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    @classmethod
    def from_dict(cls, data: Dict) -> "WalletMetrics":
        return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})


@dataclass
class CopySignal:
    """A potential copy trade signal."""
    source_wallet: str
    token_mint: str
    token_symbol: str
    source_buy_timestamp: float
    source_buy_price_usd: float
    source_amount_usd: float
    source_signature: str
    wallet_category: str
    wallet_score: float
    wallet_reliability: float
    token_age_minutes: float
    token_liquidity_usd: float
    price_change_since_source_pct: float
    time_since_source_minutes: float
    buy_price_impact_pct: Optional[float] = None
    sell_route_available: bool = False
    sell_price_impact_pct: Optional[float] = None
    is_late_entry: bool = False
    rejection_reasons: List[str] = field(default_factory=list)
    independent_wallet_count: int = 1
    is_coordinated: bool = False
    recommended_size_usd: float = 0.0
    recommended_size_pct_of_capital: float = 0.0
    # Signal intelligence (advisory only — does not change trading decisions)
    # Populated from GMGN signal events when available
    signal_is_insider: bool = False
    signal_is_bundler: bool = False
    signal_is_rat_trader: bool = False
    signal_is_sniper: bool = False
    signal_is_dev_wallet: bool = False
    signal_is_team_wallet: bool = False
    signal_is_cex_wallet: bool = False
    signal_is_mev_bot: bool = False
    signal_type: Optional[int] = None
    signal_strength: Optional[float] = None
    signal_source: str = ""  # "gmgn_agent", "gmgn_ws", etc.

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class SmartMoneyRanking:
    """Smart money ranking snapshot."""
    updated_timestamp: float
    rankings: Dict[str, List[Dict]]  # ranking_name -> list of wallet metrics
    total_tracked: int
    total_verified: int

    def to_dict(self) -> Dict:
        return {
            "updated_timestamp": self.updated_timestamp,
            "rankings": self.rankings,
            "total_tracked": self.total_tracked,
            "total_verified": self.total_verified,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "SmartMoneyRanking":
        return cls(
            updated_timestamp=data.get("updated_timestamp", 0),
            rankings=data.get("rankings", {}),
            total_tracked=data.get("total_tracked", 0),
            total_verified=data.get("total_verified", 0),
        )


# ============================================================
# Wallet Intelligence Engine
# ============================================================

class WalletIntelligence:
    """Wallet Intelligence 2.0 Engine."""

    def __init__(self, data_dir: str, config: Optional[WalletIntelligenceConfig] = None):
        self.data_dir = data_dir
        self.config = config or DEFAULT_CONFIG
        self.path = os.path.join(data_dir, "wallet_intelligence.json")
        self.lock = threading.RLock()

        # Wallet trade history and metrics (keyed by canonical Base58 wallet address)
        self.wallet_trades: Dict[str, List[WalletTrade]] = defaultdict(list)
        self.wallet_metrics: Dict[str, WalletMetrics] = {}

        # Smart money rankings
        self.rankings: Optional[SmartMoneyRanking] = None
        self.last_ranking_update: float = 0

        # Load persisted state
        self._load()

        # Initialize wallet metrics from loaded trades
        for wallet in list(self.wallet_trades.keys()):
            self._compute_wallet_metrics(wallet)

        self._update_rankings()

    def _resolve_wallet_key(self, wallet_address: str) -> str:
        """Preserve canonical Solana Base58 casing while matching existing keys case-insensitively."""
        w = (wallet_address or "").strip()
        if not w:
            return ""
        if w in self.wallet_metrics or w in self.wallet_trades:
            return w
        w_low = w.lower()
        for existing in self.wallet_metrics:
            if existing.lower() == w_low:
                return existing
        for existing in self.wallet_trades:
            if existing.lower() == w_low:
                return existing
        return w

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def _load(self) -> None:
        """Load wallet intelligence state from disk."""
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Load wallet trades
            for wallet, trades_data in data.get("wallet_trades", {}).items():
                self.wallet_trades[wallet] = [WalletTrade.from_dict(t) for t in trades_data]

            # Load rankings
            rankings_data = data.get("rankings")
            if rankings_data:
                self.rankings = SmartMoneyRanking(
                    updated_timestamp=rankings_data.get("updated_timestamp", 0),
                    rankings=rankings_data.get("rankings", {}),
                    total_tracked=rankings_data.get("total_tracked", 0),
                    total_verified=rankings_data.get("total_verified", 0),
                )
                self.last_ranking_update = rankings_data.get("updated_timestamp", 0)

            log.info("WalletIntelligence loaded: %d wallets, %d total trades",
                     len(self.wallet_trades), sum(len(t) for t in self.wallet_trades.values()))
        except Exception as e:
            log.warning("Failed to load wallet intelligence: %s", e)

    def _save(self) -> None:
        """Persist wallet intelligence state to disk."""
        try:
            with self.lock:
                data = {
                    "wallet_trades": {
                        wallet: [t.to_dict() for t in trades]
                        for wallet, trades in self.wallet_trades.items()
                    },
                    "rankings": self.rankings.to_dict() if self.rankings else None,
                    "last_updated": time.time(),
                }

                # Atomic write
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, separators=(",", ":"), default=str)
                os.replace(tmp, self.path)
        except Exception as e:
            log.error("Failed to save wallet intelligence: %s", e)

    # -----------------------------------------------------------------
    # Trade Ingestion
    # -----------------------------------------------------------------

    def ingest_wallet_trades(
        self,
        wallet_address: str,
        trades_data: List[Dict],
        source: str = "gmgn",
        save_immediately: bool = True,
    ) -> int:
        """
        Ingest wallet trade history from GMGN or other sources.
        Expects list of dicts with completed trade data.
        Returns number of new trades ingested.
        """
        wallet = self._resolve_wallet_key(wallet_address)
        if not wallet:
            return 0
        new_trades = 0

        with self.lock:
            existing_signatures = {
                (t.buy_signature, t.sell_signature)
                for t in self.wallet_trades[wallet]
            }

            for trade_data in trades_data:
                try:
                    trade = self._normalize_trade(trade_data, wallet)
                    if not trade:
                        continue

                    sig_pair = (trade.buy_signature, trade.sell_signature)
                    if sig_pair in existing_signatures:
                        continue

                    self.wallet_trades[wallet].append(trade)
                    existing_signatures.add(sig_pair)
                    new_trades += 1
                except Exception as e:
                    log.debug("Failed to normalize trade for %s: %s", wallet[:8], e)

            self.wallet_trades[wallet].sort(key=lambda t: t.sell_timestamp)
            self.wallet_trades[wallet] = self.wallet_trades[wallet][-250:]
            self._compute_wallet_metrics(wallet)
            if save_immediately:
                self._update_rankings()
                self._save()

        return new_trades

    def _normalize_trade(self, data: Dict, wallet: str) -> Optional[WalletTrade]:
        """Normalize trade data from various sources."""
        try:
            mint = str(data.get("token_mint") or data.get("token_address") or "").strip()
            if not mint:
                return None

            buy_ts = float(data.get("buy_timestamp") or data.get("buy_time") or 0)
            sell_ts = float(data.get("sell_timestamp") or data.get("sell_time") or 0)
            if not sell_ts or sell_ts <= buy_ts:
                return None

            buy_price = float(data.get("buy_price_usd") or data.get("buy_price") or 0)
            sell_price = float(data.get("sell_price_usd") or data.get("sell_price") or 0)
            amount = float(data.get("amount_usd") or data.get("amount") or 0)

            if buy_price <= 0 or sell_price <= 0 or amount <= 0:
                return None

            pnl_pct = (sell_price / buy_price - 1) * 100
            pnl_usd = amount * pnl_pct / 100

            return WalletTrade(
                token_mint=mint,
                token_symbol=str(data.get("token_symbol") or "")[:24],
                buy_timestamp=buy_ts,
                sell_timestamp=sell_ts,
                buy_price_usd=buy_price,
                sell_price_usd=sell_price,
                amount_usd=amount,
                realized_pnl_usd=pnl_usd,
                realized_pnl_pct=pnl_pct,
                holding_time_seconds=sell_ts - buy_ts,
                buy_signature=str(data.get("buy_signature") or data.get("buy_sig") or ""),
                sell_signature=str(data.get("sell_signature") or data.get("sell_sig") or ""),
                buy_price_impact_pct=data.get("buy_price_impact_pct"),
                sell_price_impact_pct=data.get("sell_price_impact_pct"),
                is_verified=bool(data.get("buy_signature") and data.get("sell_signature")),
            )
        except Exception as e:
            log.debug("Failed to normalize trade: %s", e)
            return None

    # -----------------------------------------------------------------
    # Metrics Computation
    # -----------------------------------------------------------------

    def _compute_wallet_metrics(self, wallet: str) -> Optional[WalletMetrics]:
        """Compute comprehensive metrics for a wallet from its trade history."""
        trades = self.wallet_trades.get(wallet, [])
        if not trades:
            return None

        now = time.time()
        config = self.config

        verified = [t for t in trades if t.is_verified]
        unverified = [t for t in trades if not t.is_verified]

        metric_trades = verified if len(verified) >= config.min_trades_for_score else trades

        if len(metric_trades) < 2:
            return None

        m = WalletMetrics(wallet_address=wallet)
        m.total_trades = len(trades)
        m.verified_trades = len(verified)
        m.unknown_ratio = len(unverified) / len(trades) if trades else 0.0

        m.realized_pnl_usd = sum(t.realized_pnl_usd for t in metric_trades)
        tot_amt = sum(t.amount_usd for t in metric_trades)
        m.realized_pnl_pct = sum(t.realized_pnl_pct * t.amount_usd for t in metric_trades) / tot_amt if tot_amt else 0.0
        m.gross_profit_usd = sum(t.realized_pnl_usd for t in metric_trades if t.realized_pnl_usd > 0)
        m.gross_loss_usd = abs(sum(t.realized_pnl_usd for t in metric_trades if t.realized_pnl_usd < 0))

        wins = [t for t in metric_trades if t.realized_pnl_usd > 0]
        losses = [t for t in metric_trades if t.realized_pnl_usd < 0]
        m.win_rate = len(wins) / len(metric_trades) if metric_trades else 0.0

        m.profit_factor = m.gross_profit_usd / m.gross_loss_usd if m.gross_loss_usd > 0 else (float('inf') if m.gross_profit_usd > 0 else 0.0)

        m.expectancy_per_trade_usd = m.realized_pnl_usd / len(metric_trades)
        avg_amount = tot_amt / len(metric_trades)
        m.expectancy_per_trade_pct = (m.expectancy_per_trade_usd / avg_amount * 100) if avg_amount > 0 else 0.0

        m.avg_win_usd = sum(t.realized_pnl_usd for t in wins) / len(wins) if wins else 0.0
        m.avg_loss_usd = sum(t.realized_pnl_usd for t in losses) / len(losses) if losses else 0.0
        m.avg_win_pct = sum(t.realized_pnl_pct for t in wins) / len(wins) if wins else 0.0
        m.avg_loss_pct = sum(t.realized_pnl_pct for t in losses) / len(losses) if losses else 0.0

        m.max_drawdown_pct = self._compute_max_drawdown(metric_trades)
        m.max_consecutive_losses = self._compute_max_consecutive_losses(metric_trades)

        m.avg_holding_time_seconds = sum(t.holding_time_seconds for t in metric_trades) / len(metric_trades)
        m.avg_holding_time_minutes = m.avg_holding_time_seconds / 60

        total_minutes = sum(t.holding_time_seconds for t in metric_trades) / 60
        m.holding_efficiency = m.realized_pnl_usd / total_minutes if total_minutes > 0 else 0.0

        tokens = set(t.token_mint for t in metric_trades)
        m.tokens_traded = len(metric_trades)
        m.unique_tokens = len(tokens)
        m.low_liquidity_exposure_pct = 0.0

        if metric_trades:
            best = max(metric_trades, key=lambda t: t.realized_pnl_pct)
            m.best_trade_pnl_pct = best.realized_pnl_pct
            total_pnl = sum(t.realized_pnl_usd for t in metric_trades)
            m.best_trade_contribution_pct = abs(best.realized_pnl_usd) / abs(total_pnl) * 100 if total_pnl != 0 else 0.0

        self._compute_windowed_metrics(m, metric_trades)

        if trades:
            m.last_trade_timestamp = max(t.sell_timestamp for t in trades)
            m.data_freshness_hours = (now - m.last_trade_timestamp) / 3600

        m.score = self._compute_wallet_score(m)
        m.classification = self._classify_wallet(m)

        self.wallet_metrics[wallet] = m
        return m

    def _compute_windowed_metrics(self, m: WalletMetrics, trades: List[WalletTrade]) -> None:
        """Compute 7-day and 30-day windowed metrics."""
        now = time.time()
        weekly_cutoff = now - self.config.weekly_window_days * 86400
        monthly_cutoff = now - self.config.monthly_window_days * 86400

        weekly = [t for t in trades if t.sell_timestamp >= weekly_cutoff]
        monthly = [t for t in trades if t.sell_timestamp >= monthly_cutoff]

        m.weekly_trades = len(weekly)
        m.monthly_trades = len(monthly)

        if weekly:
            weekly_wins = [t for t in weekly if t.realized_pnl_usd > 0]
            m.weekly_pnl_usd = sum(t.realized_pnl_usd for t in weekly)
            m.weekly_win_rate = len(weekly_wins) / len(weekly)
            weekly_gross_profit = sum(t.realized_pnl_usd for t in weekly if t.realized_pnl_usd > 0)
            weekly_gross_loss = abs(sum(t.realized_pnl_usd for t in weekly if t.realized_pnl_usd < 0))
            m.weekly_profit_factor = weekly_gross_profit / weekly_gross_loss if weekly_gross_loss > 0 else (float('inf') if weekly_gross_profit > 0 else 0.0)

        if monthly:
            monthly_wins = [t for t in monthly if t.realized_pnl_usd > 0]
            m.monthly_pnl_usd = sum(t.realized_pnl_usd for t in monthly)
            m.monthly_win_rate = len(monthly_wins) / len(monthly)
            monthly_gross_profit = sum(t.realized_pnl_usd for t in monthly if t.realized_pnl_usd > 0)
            monthly_gross_loss = abs(sum(t.realized_pnl_usd for t in monthly if t.realized_pnl_usd < 0))
            m.monthly_profit_factor = monthly_gross_profit / monthly_gross_loss if monthly_gross_loss > 0 else (float('inf') if monthly_gross_profit > 0 else 0.0)

    def _compute_max_drawdown(self, trades: List[WalletTrade]) -> float:
        """Compute max drawdown from equity curve."""
        if not trades:
            return 0.0

        equity = 0.0
        peak = 0.0
        max_dd = 0.0

        for t in sorted(trades, key=lambda x: x.sell_timestamp):
            equity += t.realized_pnl_usd
            peak = max(peak, equity)
            if peak > 0:
                dd = (peak - equity) / peak * 100
                max_dd = max(max_dd, dd)

        return max_dd

    def _compute_max_consecutive_losses(self, trades: List[WalletTrade]) -> int:
        """Compute max consecutive losing trades."""
        max_consec = 0
        current = 0

        for t in sorted(trades, key=lambda x: x.sell_timestamp):
            if t.realized_pnl_usd < 0:
                current += 1
                max_consec = max(max_consec, current)
            else:
                current = 0

        return max_consec

    # -----------------------------------------------------------------
    # Scoring & Classification
    # -----------------------------------------------------------------

    def _compute_wallet_score(self, m: WalletMetrics) -> float:
        """Compute 0-1 score based on weighted metrics."""
        config = self.config
        scores = {}

        scores["realized_pnl"] = min(1.0, max(0.0, m.realized_pnl_usd / 1000.0))
        scores["profit_factor"] = min(1.0, max(0.0, (m.profit_factor - 1.0) / 3.0)) if m.profit_factor != float('inf') else 1.0
        scores["expectancy"] = min(1.0, max(0.0, m.expectancy_per_trade_pct / 10.0))
        scores["win_rate"] = m.win_rate
        scores["max_drawdown"] = max(0.0, 1.0 - m.max_drawdown_pct / 50.0)
        scores["consistency"] = max(0.0, 1.0 - abs(m.expectancy_per_trade_pct) / 20.0) if m.expectancy_per_trade_pct else 0.5
        scores["holding_efficiency"] = min(1.0, max(0.0, m.holding_efficiency / 10.0))

        weights = config.score_weights
        total_score = sum(scores.get(k, 0) * weights.get(k, 0) for k in weights)

        return min(1.0, max(0.0, total_score))

    def _classify_wallet(self, m: WalletMetrics) -> str:
        """Classify wallet based on metrics and thresholds."""
        config = self.config

        if m.verified_trades < config.min_trades_for_score:
            return "UNVERIFIED"

        if m.unknown_ratio > config.max_unknown_ratio:
            return "UNVERIFIED"

        if m.data_freshness_hours > config.stale_data_hours:
            return "UNVERIFIED"

        if (m.verified_trades >= config.min_trades_for_elite and
                m.score >= config.elite_min_score and
                m.profit_factor >= config.elite_min_profit_factor and
                m.expectancy_per_trade_pct >= config.elite_min_expectancy * 100 and
                m.max_drawdown_pct <= config.elite_max_drawdown * 100 and
                m.max_consecutive_losses <= config.elite_max_consecutive_losses):
            return "ELITE"

        if (m.weekly_trades >= config.weekly_rising_min_trades and
                m.score >= config.weekly_rising_min_score and
                m.weekly_pnl_usd > 0 and
                m.weekly_profit_factor > 1.0):
            return "WEEKLY_RISING"

        if (m.score <= config.high_risk_max_score or
                m.max_drawdown_pct >= config.high_risk_min_drawdown * 100 or
                m.win_rate <= config.high_risk_max_win_rate or
                m.max_consecutive_losses >= config.high_risk_max_consecutive_losses):
            return "HIGH_RISK"

        return "UNVERIFIED"

    # -----------------------------------------------------------------
    # Rankings
    # -----------------------------------------------------------------

    def _update_rankings(self) -> None:
        """Update smart money rankings."""
        verified_wallets = {
            w: m for w, m in self.wallet_metrics.items()
            if m.classification in ("ELITE", "WEEKLY_RISING") and m.verified_trades >= self.config.min_trades_for_score
        }

        if not verified_wallets:
            self.rankings = SmartMoneyRanking(
                updated_timestamp=time.time(),
                rankings={},
                total_tracked=len(self.wallet_metrics),
                total_verified=len([m for m in self.wallet_metrics.values() if m.verified_trades > 0]),
            )
            return

        rankings = {}

        best_7d = sorted(verified_wallets.items(), key=lambda x: x[1].weekly_pnl_usd, reverse=True)[:20]
        rankings["best_7d"] = [{"wallet": w, **m.to_dict()} for w, m in best_7d]

        best_30d = sorted(verified_wallets.items(), key=lambda x: x[1].monthly_pnl_usd, reverse=True)[:20]
        rankings["best_30d"] = [{"wallet": w, **m.to_dict()} for w, m in best_30d]

        consistent = sorted(verified_wallets.items(),
                            key=lambda x: (x[1].profit_factor, -x[1].max_drawdown_pct),
                            reverse=True)[:20]
        rankings["most_consistent"] = [{"wallet": w, **m.to_dict()} for w, m in consistent]

        risk_adj = sorted(verified_wallets.items(),
                          key=lambda x: x[1].expectancy_per_trade_pct / max(1, x[1].max_drawdown_pct),
                          reverse=True)[:20]
        rankings["best_risk_adjusted"] = [{"wallet": w, **m.to_dict()} for w, m in risk_adj]

        early = sorted([(w, m) for w, m in verified_wallets.items()
                        if m.expectancy_per_trade_pct > 0 and m.avg_holding_time_minutes > 0],
                       key=lambda x: x[1].avg_holding_time_minutes)[:20]
        rankings["best_early_entry"] = [{"wallet": w, **m.to_dict()} for w, m in early]

        liq_aware = sorted(verified_wallets.items(),
                           key=lambda x: x[1].low_liquidity_exposure_pct)[:20]
        rankings["best_liquidity_aware"] = [{"wallet": w, **m.to_dict()} for w, m in liq_aware]

        self.rankings = SmartMoneyRanking(
            updated_timestamp=time.time(),
            rankings=rankings,
            total_tracked=len(self.wallet_metrics),
            total_verified=len([m for m in self.wallet_metrics.values() if m.verified_trades > 0]),
        )
        self.last_ranking_update = time.time()

    def get_rankings(self) -> Optional[SmartMoneyRanking]:
        return self.rankings

    # -----------------------------------------------------------------
    # Copy Signal Evaluation
    # -----------------------------------------------------------------

    def _get_gmgn_signal_info(self, token_mint: str, source_wallet: str) -> Optional[Dict]:
        return None

    def evaluate_copy_signal(
        self,
        source_wallet: str,
        token_mint: str,
        token_symbol: str,
        source_buy_timestamp: float,
        source_buy_price_usd: float,
        source_amount_usd: float,
        source_signature: str,
        token_age_minutes: float,
        token_liquidity_usd: float,
        current_price_usd: float,
        buy_price_impact_pct: Optional[float] = None,
        sell_route_available: bool = False,
        sell_price_impact_pct: Optional[float] = None,
        independent_wallet_count: int = 1,
        is_coordinated: bool = False,
        portfolio_capital_usd: float = 100.0,
        existing_exposure_usd: float = 0.0,
        gmgn_signal_type: Optional[int] = None,
        gmgn_signal_strength: Optional[float] = None,
        gmgn_signal_is_insider: bool = False,
        gmgn_signal_is_bundler: bool = False,
        gmgn_signal_is_rat_trader: bool = False,
        gmgn_signal_is_sniper: bool = False,
        gmgn_signal_is_dev_wallet: bool = False,
        gmgn_signal_is_team_wallet: bool = False,
        gmgn_signal_is_cex_wallet: bool = False,
        gmgn_signal_is_mev_bot: bool = False,
        gmgn_signal_source: str = "",
    ) -> CopySignal:
        """Evaluate a potential copy trade signal with full intelligence."""
        wallet = self._resolve_wallet_key(source_wallet)
        config = self.config

        signal = CopySignal(
            source_wallet=wallet,
            token_mint=token_mint,
            token_symbol=token_symbol,
            source_buy_timestamp=source_buy_timestamp,
            source_buy_price_usd=source_buy_price_usd,
            source_amount_usd=source_amount_usd,
            source_signature=source_signature,
            wallet_category="UNVERIFIED",
            wallet_score=0.0,
            wallet_reliability=0.0,
            token_age_minutes=token_age_minutes,
            token_liquidity_usd=token_liquidity_usd,
            price_change_since_source_pct=0.0,
            time_since_source_minutes=0.0,
            buy_price_impact_pct=buy_price_impact_pct,
            sell_route_available=sell_route_available,
            sell_price_impact_pct=sell_price_impact_pct,
            signal_is_insider=gmgn_signal_is_insider,
            signal_is_bundler=gmgn_signal_is_bundler,
            signal_is_rat_trader=gmgn_signal_is_rat_trader,
            signal_is_sniper=gmgn_signal_is_sniper,
            signal_is_dev_wallet=gmgn_signal_is_dev_wallet,
            signal_is_team_wallet=gmgn_signal_is_team_wallet,
            signal_is_cex_wallet=gmgn_signal_is_cex_wallet,
            signal_is_mev_bot=gmgn_signal_is_mev_bot,
            signal_type=gmgn_signal_type,
            signal_strength=gmgn_signal_strength,
            signal_source=gmgn_signal_source,
        )

        now = time.time()
        signal.time_since_source_minutes = (now - source_buy_timestamp) / 60
        if source_buy_price_usd > 0:
            signal.price_change_since_source_pct = (current_price_usd / source_buy_price_usd - 1) * 100

        metrics = self.wallet_metrics.get(wallet)
        if metrics:
            signal.wallet_category = metrics.classification
            signal.wallet_score = metrics.score
            signal.wallet_reliability = metrics.score * min(1.0, metrics.verified_trades / 50.0)

        reasons = []

        if signal.wallet_category == "HIGH_RISK":
            reasons.append("Source wallet classified as HIGH_RISK")
        elif signal.wallet_category == "UNVERIFIED":
            reasons.append("Source wallet UNVERIFIED (insufficient history)")

        if signal.wallet_score < config.min_wallet_score_for_copy:
            reasons.append(f"Wallet score {signal.wallet_score:.2f} below minimum {config.min_wallet_score_for_copy}")

        if signal.time_since_source_minutes > config.max_source_age_minutes:
            reasons.append(f"Source buy too old ({signal.time_since_source_minutes:.0f} min > {config.max_source_age_minutes} min)")
            signal.is_late_entry = True

        if signal.price_change_since_source_pct > config.max_price_move_since_source_pct * 100:
            reasons.append(f"Price moved {signal.price_change_since_source_pct:.1f}% since source buy (max {config.max_price_move_since_source_pct * 100}%)")
            signal.is_late_entry = True

        if signal.token_liquidity_usd < config.min_liquidity_usd_for_copy:
            reasons.append(f"Insufficient liquidity ${signal.token_liquidity_usd:,.0f} < ${config.min_liquidity_usd_for_copy:,.0f}")

        if buy_price_impact_pct and buy_price_impact_pct > config.max_price_impact_pct:
            reasons.append(f"Buy price impact {buy_price_impact_pct:.1f}% > max {config.max_price_impact_pct}%")

        if sell_price_impact_pct and sell_price_impact_pct > config.max_price_impact_pct:
            reasons.append(f"Sell price impact {sell_price_impact_pct:.1f}% > max {config.max_price_impact_pct}%")

        if buy_price_impact_pct and buy_price_impact_pct > config.max_slippage_pct:
            reasons.append(f"Expected slippage {buy_price_impact_pct:.1f}% > max {config.max_slippage_pct}%")

        if existing_exposure_usd > 0:
            reasons.append(f"Already have ${existing_exposure_usd:.2f} exposure to this token")

        signal.independent_wallet_count = independent_wallet_count
        signal.is_coordinated = is_coordinated

        if independent_wallet_count < config.min_independent_wallets:
            reasons.append(f"Only {independent_wallet_count} independent wallet(s), need {config.min_independent_wallets}")

        signal.rejection_reasons = reasons

        if not reasons:
            signal.recommended_size_usd = self._compute_copy_size(
                signal, portfolio_capital_usd, existing_exposure_usd
            )
            signal.recommended_size_pct_of_capital = (signal.recommended_size_usd / portfolio_capital_usd * 100) if portfolio_capital_usd > 0 else 0.0

        return signal

    def _compute_copy_size(
        self,
        signal: CopySignal,
        portfolio_capital: float,
        existing_exposure: float,
    ) -> float:
        """Compute liquidity-aware copy position size."""
        config = self.config
        available = portfolio_capital - existing_exposure
        if available <= 0:
            return 0.0

        base_pct = 0.10

        if signal.wallet_category == "ELITE":
            mult = config.elite_size_multiplier
        elif signal.wallet_category == "WEEKLY_RISING":
            mult = config.weekly_rising_size_multiplier
        else:
            mult = config.unverified_size_multiplier

        max_by_liquidity = signal.token_liquidity_usd * config.max_size_to_liquidity_pct / 100
        max_by_source = signal.source_amount_usd * 1.5

        size = available * base_pct * mult
        size = min(size, max_by_liquidity, max_by_source, available)

        return max(0.0, size)

    # -----------------------------------------------------------------
    # Copy Exit Evaluation
    # -----------------------------------------------------------------

    def evaluate_copy_exit(
        self,
        source_wallet: str,
        token_mint: str,
        source_sell_timestamp: float,
        source_sell_price_usd: float,
        source_sell_amount_usd: float,
        source_sell_signature: str,
        our_position_qty: float,
        our_entry_price_usd: float,
    ) -> Dict[str, Any]:
        """Evaluate whether to exit copy position based on source wallet activity."""
        return {
            "should_exit": True,
            "exit_pct": 1.0,
            "exit_reason": "source_wallet_sold",
            "source_exit_pct": 1.0,
            "is_complete_exit": True,
            "is_partial_exit": False,
            "exit_size_usd": our_position_qty * source_sell_price_usd,
        }

    # -----------------------------------------------------------------
    # Wallet History & Persistence
    # -----------------------------------------------------------------

    def add_wallet_trade(self, wallet: str, trade: WalletTrade) -> None:
        """Add a single trade to wallet history."""
        canonical = self._resolve_wallet_key(wallet)
        if not canonical:
            return
        with self.lock:
            self.wallet_trades[canonical].append(trade)
            self.wallet_trades[canonical].sort(key=lambda t: t.sell_timestamp)
            self._compute_wallet_metrics(canonical)
            self._update_rankings()
            self._save()

    def get_wallet_metrics(self, wallet: str) -> Optional[WalletMetrics]:
        canonical = self._resolve_wallet_key(wallet)
        return self.wallet_metrics.get(canonical)

    def get_wallet_trades(self, wallet: str) -> List[WalletTrade]:
        canonical = self._resolve_wallet_key(wallet)
        return self.wallet_trades.get(canonical, [])

    def get_all_wallet_metrics(self) -> Dict[str, WalletMetrics]:
        return dict(self.wallet_metrics)

    def get_top_wallets(self, limit: int = 20, category: Optional[str] = None) -> List[Tuple[str, WalletMetrics]]:
        """Get top wallets by score, optionally filtered by category."""
        wallets = list(self.wallet_metrics.items())
        if category:
            wallets = [(w, m) for w, m in wallets if m.classification == category]
        wallets.sort(key=lambda x: x[1].score, reverse=True)
        return wallets[:limit]

    def get_stats(self) -> Dict[str, Any]:
        """Get overall wallet intelligence statistics."""
        cats = defaultdict(int)
        for m in self.wallet_metrics.values():
            cats[m.classification] += 1

        return {
            "total_tracked": len(self.wallet_metrics),
            "total_trades": sum(len(t) for t in self.wallet_trades.values()),
            "categories": dict(cats),
            "rankings_updated": self.last_ranking_update,
            "rankings": self.rankings.to_dict() if self.rankings else None,
        }


# ============================================================
# Factory Function
# ============================================================

def create_wallet_intelligence(data_dir: str, config: Optional[WalletIntelligenceConfig] = None) -> WalletIntelligence:
    """Create WalletIntelligence from environment and data directory."""
    return WalletIntelligence(data_dir, config)
