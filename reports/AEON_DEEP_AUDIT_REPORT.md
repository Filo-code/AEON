# AEON — Deep Audit & Hardening Report

**Audit Type:** Senior Quant Developer, Python Architect, & Trading Systems Audit  
**Target Repository:** `c:\Users\Ez__Rekt\Desktop\AOEN`  
**Date:** 2026-11-17  
**Auditor Classification:** Autonomous Simulated Trading Systems Audit  
**Trading Status:** **SIMULATED / PAPER TRADING ONLY — NOT AUTHORIZED FOR LIVE CAPITAL**

---

## 1. Actual System Architecture

Reverse engineering of the runtime codebase reveals that AEON is an event-driven paper-trading bot operating primarily over a continuous polling loop with scheduled asynchronous telemetry tasks and defensive wrappers.

### 1.1 Entry Point & Lifecycle
- **Entry Point:** `bot/paper_bot.py` (`if __name__ == "__main__":` $\to$ `PaperBot.run()`).
- **Initialization:**
  1. Acquires OS-level single-instance lock (`storage.InstanceLock` via `msvcrt` on Windows / `fcntl` on Unix) preventing dual-process concurrency.
  2. Loads and merges YAML configuration (`config.yaml`), profile defaults (`profiles.py`), and risk parameters (`risk.py`).
  3. Reconstructs previous execution state (`storage.load_json_safe`), verifying integrity of `state.json` and falling back to `.bak` or timestamped backups if corrupted.
  4. Initializes subsystems: `RiskManager`, `Brain` (`ValidatedBrain`), `AntiRugEngine`, `DexScreener`, `Jupiter` quote engine, `RPCManager`, and `TelegramNotifier`.
  5. Spawns background threads for watchdog health monitoring, periodic dashboard screenshot generation, and disk guard monitoring.
- **Main Trading Loop:**
  - Evaluates `manage()` on existing open positions (stop loss, TP1 partial exit, breakeven stop, trailing stop, TP2 final exit, adaptive holding, and max hold duration).
  - Executes `discover()` every `poll_seconds` to detect new token candidates (`volume_spike` from GeckoTerminal/DexScreener, `copy_top_traders` via on-chain balances/Vybe/Birdeye).
  - Persists atomic state updates every `state_save_seconds` (default 15s) and writes heartbeats for systemd/watchdog.

### 1.2 Signal-Processing & Decision Pipeline
```mermaid
flowchart TD
    Discovery["Market Discovery (GeckoTerminal / DexScreener / Pump.fun / GMGN)"] --> FilterPre["Pre-Filter (Pair Age, Min Volume, Spike Multiplier)"]
    FilterPre --> AntiRug["AntiRug Engine (RugCheck Score, Mint/Freeze Auth, Dev/Top10 Concentration)"]
    AntiRug --> RiskGate["Risk Manager (check_entry: Duplicates, Max Positions, Exposure, Daily Loss)"]
    RiskGate --> Sizing["Risk Sizing (size: Requested % vs Max Position Ceiling vs Min Trade)"]
    Sizing --> Execution["Simulated Order Fill (Jupiter Quote / Liquidity-Slip Model)"]
    Execution --> PositionManager["Open Position Manager (manage: Hold, BE, TP1, Trailing, TP2)"]
    PositionManager --> Exits["Sell / Liquidation (Partial Exits, Full Closure, Fees Deducted)"]
    Exits --> RiskFeedback["Risk Manager on_trade_closed (MTM, Drawdown, Consecutive Losses)"]
    Exits --> BrainFeedback["Brain Telemetry & Learning (ValidatedBrain: Records, t-test, Rollback)"]
```

### 1.3 Purchase Authorization & Blocking Matrix
Before simulated capital is committed to an entry, the signal must pass all gates:
1. **System Health Gate:** Bot not dead, not paused, disk storage level not critical (`disk_status`).
2. **Re-entry Gate:** Cooldown period on previous token exit must have elapsed.
3. **Risk Manager Entry Gate (`check_entry`):**
   - Not in risk pause (`consecutive_losses`, `daily_loss`, `drawdown`, `disk`).
   - Token not already in open `positions`.
   - Token not currently `inflight` (prevents simultaneous buy race conditions).
   - Token not bought within `duplicate_window_s` (120s).
   - Current open position count $< \text{max\_open\_positions}$.
   - Total committed open cost basis $< \text{max\_exposure\_pct} \times \text{equity}$.
4. **Pre-Trade Guards (`_guards_entry`):**
   - Minimum pool liquidity threshold met (e.g., $\ge \$10,000$).
   - Price impact from quote does not exceed threshold.
   - Price quote verified or acceptable within mid-price tolerance.
5. **Anti-Rug Engine Gate:**
   - Token score within acceptable safety profile.
   - Mint authority and freeze authority disabled.
   - Creator holding and top 10 holder concentration within safety limits.
6. **Risk Sizing Gate (`size`):**
   - Position size strictly bounded by `max_position_pct`.
   - `min_trade` does not exceed risk ceiling.
   - Remaining cash is sufficient.

### 1.4 Trade Recording & Telemetry Flow
- Every partial and full execution records timestamps, mid-prices, fill prices, simulated slippage (Jupiter quote or liquidity model), DEX fees, and network fees.
- Open positions are stored in memory (`self.positions`) and committed to `state.json` via atomic file swap (`atomic_json`).
- On final position closure, `cumulative_pnl_usd` (aggregating all prior partial exits) is recorded, the position is popped from `self.positions`, appended to `self.closed`, and emitted to `trades.csv` and secondary telemetry logs.
- The completed trade record is handed to `brain.record()` with `counts_as_trade = True` and `partial = False`.

---

## 2. Identified Issues & Severity

| ID | Module | Issue Description | Severity | Impact |
|---|---|---|---|---|
| **ISSUE-01** | `risk.py` | Minimum position size override of maximum safety limit (`size = max(size, min_trade)`) | **CRITICAL** | Allowed position sizes to breach `max_position_pct` when equity dropped or `min_trade` was large. |
| **ISSUE-02** | `cervello.py` | `LegacyBrain` active by default in `config.yaml`, lacking statistical significance and OOS validation | **HIGH** | Bot modified parameters based on statistical noise without t-tests or out-of-sample confirmation. |
| **ISSUE-03** | `cervello.py` | No experiment history blacklist for previously failed / rolled back parameters | **HIGH** | Brain could repeatedly re-attempt the same parameter changes that had already degraded performance. |
| **ISSUE-04** | `cervello.py` | `due()` blocked at trade 10 because `min_samples` (30) took precedence over `min_trades` (10) | **MEDIUM** | Initial data collection and diagnostic analysis at 10 trades was prevented. |
| **ISSUE-05** | `cervello.py` | Interim evaluation of pending changes was forced to wait up to 50 trades before rollback | **MEDIUM** | Bad parameter changes continued to incur losses for dozens of trades before evaluation was triggered. |
| **ISSUE-06** | `paper_bot.py` | Incomplete aggregation of multiple partial exits into final cumulative PnL | **MEDIUM** | Only `tp1_realized_pnl` was added on final exit; additional partial exits were omitted from cumulative PnL. |
| **ISSUE-07** | `test_tp_core.py` | Hardcoded legacy float equality check in persistence test (`== 1.008725`) | **LOW** | Test failed despite state persistence working properly due to an outdated hardcoded float literal. |

---

## 3. Severity Breakdown

- **Critical:** 1 issue (Risk integrity breach).
- **High:** 2 issues (Brain noise adaptation and missing failed-experiment history).
- **Medium:** 3 issues (Learning cadence trigger, evaluation latency, and multi-partial exit PnL aggregation).
- **Low:** 1 issue (Test assertion rigidity).

---

## 4. Modified Files & Documentation of Changes

### 4.1 `bot/risk.py`
- **Location:** `RiskManager.size()` (lines 120–145).
- **Modification:** Added strict enforcement of the maximum position safety ceiling:
  ```python
  max_allowed = equity * self.L["max_position_pct"] if self.L.get("max_position_pct") is not None else float("inf")
  if self.s["resume_left"] > 0 and self.L["resume_size_factor"] < 1 and self.L.get("max_position_pct") is not None:
      max_allowed *= self.L["resume_size_factor"]

  # Minimum position size must never override maximum safety limit
  if min_trade > max_allowed:
      return 0.0, ", ".join(note + [f"minimo ({min_trade}) supera limite massimo ({max_allowed:.2f})"])

  size = max(size, min_trade)
  size = min(size, max_allowed)
  ```
- **Rationale:** Guarantees that a minimum position size requirement can never bypass the maximum safety percentage limit, enforcing the fundamental risk invariant.

### 4.2 `bot/cervello.py`
- **Location:** `ValidatedBrain.due()`, `_change()`, `analyze()`, and `Brain()` factory.
- **Modifications:**
  1. Updated `due()` to allow initial analysis at $\min(\text{min\_trades}, \text{min\_samples}) = 10$, and to trigger immediately when a pending change has completed its evaluation trades (`eval_trades` = 15).
  2. In `_change()`, added verification against `self.st["changes"]` to reject any parameter modification that was previously marked `"annullata"` (failed experiment).
  3. In `analyze()`, decoupled pending change evaluation from regular interval advancement, preventing parameter churn during interim evaluations.
  4. In `Brain()` factory, switched default mode from `"legacy"` to `"validated"`.
- **Rationale:** Ensures that the Brain only adapts on statistically significant patterns ($t \ge 2.0$), validates on out-of-sample data, evaluates changes after 15 trades with automatic rollback on deterioration, and never repeats failed experiments.

### 4.3 `bot/config.yaml`
- **Location:** `brain.mode`
- **Modification:** Set `mode: validated` (was `mode: legacy`).
- **Rationale:** Activates the full validated learning framework configured in `config.yaml` (`eval_trades`, `min_t`, `oos_frac`, `rollback_margin_pct`).

### 4.4 `bot/paper_bot.py`
- **Location:** `PaperBot.sell()` (lines 1550–1560 and 1680–1700).
- **Modifications:**
  1. In partial sell, accumulated all realized partial PnL:
     `p["partial_realized_pnl"] = round(float(p.get("partial_realized_pnl", 0.0)) + pnl, 4)`.
  2. On final full sell, aggregated total cumulative PnL:
     `partial_pnl = float(p.get("partial_realized_pnl", p.get("tp1_realized_pnl", 0.0)))`
     `total_cum_pnl = round(pnl + partial_pnl, 4)`.
- **Rationale:** Ensures mathematically consistent cumulative PnL tracking for positions with arbitrary multiple partial exits.

### 4.5 `test_tp_core.py` & `test_tp.py`
- **Modifications:** Replaced outdated hardcoded constant `1.008725` with exact parity comparison against the in-memory bot's computed `breakeven_price`, and added `ignore_cleanup_errors=True` on `tempfile.TemporaryDirectory` for Windows file handle cleanup.
- **Rationale:** Prevents spurious platform-specific test failures without altering trading logic.

---

## 5. Automated Tests Executed & Numerical Results

The test suite executed includes 15 audit scenarios in `tests/test_aeon_deep_audit.py`, 9 regression tests in `test_aeon_staging_regression.py`, 12 telegram command tests in `test_commands.py`, and the standalone test scripts (`test_tp_core.py` and `test_tp.py`).

### Scenario Numerical Verification Matrix
| Scenario | Target Metric | Expected Value | Observed Value | Status |
|---|---|---|---|---|
| **1. 10 Trades** | Win Rate % | 60.0% | 60.0% | **PASS** |
| | Mean PnL % | +8.0% | +8.0% | **PASS** |
| | Parameter Changes | 0 (Observation) | 0 | **PASS** |
| **2. 50 Trades** | Scale Interval | 50 trades | 50 trades | **PASS** |
| **3. 100 Trades** | Execution Checkpoints | 10, 20, 30, 40, 50, 100 | Exact match | **PASS** |
| **4. Noise Rejection** | Permanent Changes on Noise | 0 changes | 0 changes ($t < 2.0$) | **PASS** |
| **5. Rollback on Deterioration** | Rollback Action | Revert to old value | Old value restored | **PASS** |
| | Re-apply Failed Experiment | Blocked (`False`) | `False` | **PASS** |
| **6. Validate on Improvement** | Retention Action | Keep parameter (`mantenuta`) | `mantenuta` | **PASS** |
| **7. 10 Consecutive Losses** | Risk Pause Trigger | At loss 5 (`consecutive_losses`) | Exact at 5 | **PASS** |
| | Resume Size Factor | 0.50 ($13.50 \to \$6.75$) | \$6.75 | **PASS** |
| **8. Multiple Partial Exits** | Cumulative Realized PnL | \$12 + \$15 + \$24 = \$51.00 | \$51.00 | **PASS** |
| | Completed Trade Count | Exactly 1 logical trade | 1 trade | **PASS** |
| **9. Crash Recovery** | State Loading from Corrupted Temp | Recover from intact `state.json` | 100% intact | **PASS** |
| **10. Restart Open Position** | Restored Breakeven Price | \$1.05 preserved | \$1.05 | **PASS** |
| **11. API Down** | Zero Liquidation Assumption | Retain mark, no rug assumption | Position retained | **PASS** |
| **12. Insufficient Liquidity** | Pre-trade Liquidity Guard | Reject entry ($2k < $10k) | Rejected | **PASS** |
| **13. Holder Concentration** | Anti-Rug Concentration Check | Flag HIGH risk ($>60\%$) | Flagged HIGH | **PASS** |
| **14. Duplicate Signals** | Duplicate Signal Rejection | Reject inflight & window | Rejected | **PASS** |
| **15. Min Position vs Safety** | Size when Min > Safety Ceiling | Strict \$0.00 (reject) | \$0.00 | **PASS** |

---

## 6. Remaining Issues & Technical Debt

1. **Synthetic Slippage Model Divergence from On-Chain Realities:**
   - In simulated mode when Jupiter quotes are unavailable or under high network latency, the liquidity-based model ($(\text{gross}/\text{liquidity}) \times \text{factor}$) provides an idealized continuous price impact. On real decentralized AMMs (Raydium, Pump.fun bonding curves), low-liquidity tokens experience discrete pool curve shifts, front-running, and MEV sandwiching.
2. **Hardcoded Fallbacks for API Rate Limits:**
   - Multiple providers (DexScreener, GeckoTerminal, Birdeye, Vybe) have discrete rate limit behaviors. While exponential backoff and cycle budgets exist, extended multi-provider rate-limiting can delay mark-to-market updates for trailing stops.
3. **Single Process Memory Footprint:**
   - As `self.closed` grows over thousands of trades, in-memory aggregation of full filter metadata dictionaries increases memory usage. While `trades.csv` and backups handle persistence, memory pruning for closed positions beyond 2,000 items should be monitored during multi-week runs.

---

## 7. Residual Risks

1. **Market Microstructure Risk:**
   - Simulated paper fills assume that any order matching the quoted mid or Jupiter quote can execute at quoted prices. In live decentralized memecoin environments, slippage tolerance spikes can result in severe adverse fills during pool migration or dev dumping.
2. **Provider Dependency & API Desynchronization:**
   - If DexScreener reports stale prices while an AMM pool is being drained, the bot's trailing stops could trigger late if Jupiter quotes are disabled or rate-limited.
3. **Overfitting to Backtest / Simulation Data:**
   - Even with Welch's t-test and out-of-sample confirmation, memecoin market regimes change rapidly. Parameter adjustments that proved optimal over a 50-trade bull trend may lag when market volatility or volume contracts.

---

## 8. Overall System Maturity Assessment

### Maturity Rating: **B+ (Robust Simulation & Validated Learning Framework)**

- **Architecture Consistency:** **EXCELLENT.** The separation of concerns between `RiskManager`, `ValidatedBrain`, and `PaperBot` position management is clean and robust.
- **Risk Integrity:** **VERIFIED & HARDENED.** The fundamental safety invariant that minimum position sizes cannot override maximum risk ceilings is mathematically enforced.
- **Brain Intelligence:** **VERIFIED & HARDENED.** The learning module now operates strictly under statistical significance thresholds ($t \ge 2.0$), out-of-sample confirmation, rolling evaluation windows, automatic rollback on performance degradation, and historical failure blacklisting.
- **Lifecycle & Accounting:** **EXCELLENT.** Buy, Hold, Partial TP1, Breakeven Stop, Trailing Stop, TP2, and Full Sell lifecycles maintain consistent cumulative PnL without duplicate trade counting.
- **Crash Resilience:** **EXCELLENT.** Atomic write-fsync-replace mechanics and multi-tiered backup restoration protect against state loss.

### Final Audit Determination
AEON has been successfully hardened and verified as an autonomous, self-contained **simulated paper-trading system**. It is technically consistent, resilient to failures, and capable of progressive learning without compromising risk management limits. **It is NOT authorized for live trading with real funds.**
