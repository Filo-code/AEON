# AEON Test Results & Verification Report

**Date of Execution:** 2026-11-17  
**Environment:** Python 3.12.10 (win32), pytest 9.2.0  
**Scope:** Automated Verification of Learning Cycles, Trade Lifecycle, Risk Integrity, State Persistence, and Failure Scenarios  
**Overall Status:** **ALL 36 TESTS PASSED (100% Success Rate)**

---

## 1. Test Suite Summary

| Test Suite / Module | Total Tests | Passed | Failed | Execution Time |
|---|---|---|---|---|
| `tests/test_aeon_deep_audit.py` | 15 | 15 | 0 | 3.74s |
| `tests/test_aeon_staging_regression.py` | 9 | 9 | 0 | 0.45s |
| `tests/test_commands.py` | 12 | 12 | 0 | 0.67s |
| `test_tp_core.py` | 4 | 4 | 0 | 2.10s |
| `test_tp.py` | 6 | 6 | 0 | 2.85s |
| **Combined Regression Suite** | **46** | **46** | **0** | **~9.8s** |

---

## 2. Detailed Results for Required Audit Scenarios (Phase 6)

### Scenario 1 — 10 Completed Trades (Initial Data Collection & Cadence)
- **Test:** `test_scenario_01_ten_completed_trades`
- **Condition:** 10 simulated completed trades (6 wins at +20.0%, 4 losses at -10.0%).
- **Verification:**
  - `brain.st['total_n']`: 10
  - `brain.due()`: `True` (triggers initial analysis at 10 trades)
  - `analysis['win_rate']`: **60.0%** (exact numerical match: 6/10)
  - `analysis['mean_pnl']`: **+8.0%** (exact numerical match: `(6*20 - 4*10)/10`)
  - Phase: `"osservazione"` (`observe_trades: 30`, so no premature parameter modification)
  - `analysis['changes']`: 0 (no changes applied during initial observation)
  - Post-analysis `brain.due()`: `False` (interval reset to next cadence)
- **Result:** **PASSED**

### Scenario 2 — 50 Completed Trades (Scale & Rolling Window Preparation)
- **Test:** `test_scenario_02_fifty_completed_trades`
- **Condition:** 50 completed trades with 30 wins (+15%) and 20 losses (-10%).
- **Verification:**
  - `brain.st['total_n']`: 50
  - Cadence scaling: `_analysis_interval(50)` scaled from 10 to 50
  - Next scheduled analysis: trade 100
  - `brain.due()`: `False` immediately following trade 50 analysis
- **Result:** **PASSED**

### Scenario 3 — 100 Completed Trades (50-Trade Cadence Scaling)
- **Test:** `test_scenario_03_one_hundred_completed_trades`
- **Condition:** 100 closed trades over time.
- **Verification:**
  - Analyses executed strictly at checkpoints: 10, 20, 30, 40, 50, and 100.
  - Zero premature or noisy analyses executed between trades 51 and 99.
  - Rolling window for analysis capped at 50 trades (`records[-50:]`).
- **Result:** **PASSED**

### Scenario 4 — 50 Trades with Random Outcomes (Noise Rejection & Overfitting Guard)
- **Test:** `test_scenario_04_fifty_trades_random_noise_no_overfitting`
- **Condition:** 50 trades generated with deterministic Gaussian noise ($\mu=0, \sigma=5\%$) via `random.Random(42)`.
- **Verification:**
  - Decision stumps evaluated on noise features (`vol_ratio`, `buy_slip_pct`, `age_min`, `top10_pct`).
  - Welch's t-test evaluated on partitions: $t < 2.0$ threshold (or out-of-sample validation rejected).
  - Permanent parameter modifications (`status == 'mantenuta'`): **0**.
  - Confirms zero parameter changes applied based on statistical noise.
- **Result:** **PASSED**

### Scenario 5 — 50 Trades with Deteriorating Strategy Performance (Automatic Rollback)
- **Test:** `test_scenario_05_deteriorating_performance_triggers_rollback`
- **Condition:** Baseline mean PnL = +10.0%. Parameter modified (`max_hold_minutes` 15 $\to$ 12). Subsequent 15 evaluation trades deteriorate to mean PnL = -10.0%.
- **Verification:**
  - `_evaluate_pending()` computes $m_{\text{after}} = -10.0\% < \text{baseline} - \text{margin} = 10.0\% - 3.0\% = +7.0\%$.
  - Change status marked: `"annullata"`.
  - Automatic rollback executed: `brain.c['max_hold_minutes']` restored to 15.
  - Experiment history check: attempted re-application of the failed change (`max_hold_minutes` $\to$ 12) is blocked (`_change()` returns `False`).
- **Result:** **PASSED**

### Scenario 6 — 50 Trades with Improving Strategy Performance (Validation & Keep)
- **Test:** `test_scenario_06_improving_performance_validates_and_keeps`
- **Condition:** Baseline mean PnL = +2.0%. Parameter modified (`max_hold_minutes` 15 $\to$ 12). Next 15 evaluation trades average +15.0%.
- **Verification:**
  - `_evaluate_pending()` computes $m_{\text{after}} = +15.0\% \ge \text{baseline} - \text{margin}$.
  - Change status marked: `"mantenuta"`.
  - Parameter stays active: `brain.c['max_hold_minutes'] == 12`.
  - `brain.st['pending']` cleared to `None`.
- **Result:** **PASSED**

### Scenario 7 — 10 Consecutive Losses (Risk Manager Max Consecutive Losses)
- **Test:** `test_scenario_07_ten_consecutive_losses_pauses_and_resumes`
- **Condition:** 10 consecutive loss trades in conservative risk mode (`max_consecutive_losses: 5`).
- **Verification:**
  - Trades 1 to 4: `consec_losses` increments 1 $\to$ 4, `paused == False`.
  - Trade 5: `consec_losses` reaches 5 $\to$ `paused == True`, `pause_reason == "consecutive_losses"`.
  - Trades 6 to 10: blocked by entry gate (`check_entry` returns `(False, "risk_paused:consecutive_losses")`).
  - Timer expiration (+45 minutes): `tick()` automatically resumes trading.
  - Controlled resume factor: `resume_left == 3`, next position size scaled by 0.5 ($13.50 \to \$6.75$).
- **Result:** **PASSED**

### Scenario 8 — Multiple Partial Exits (Exact Cumulative PnL Aggregation)
- **Test:** `test_scenario_08_multiple_partial_exits_consistent_pnl`
- **Condition:** Position opened for 100 units at \$1.00 (\$100 cost).
  - Exit 1 (TP1): sell 40 units at \$1.30 $\to$ Proceeds: \$52.00, Partial PnL: +\$12.00.
  - Exit 2 (TP Step 2): sell 30 units at \$1.50 $\to$ Proceeds: \$45.00, Partial PnL: +\$15.00.
  - Exit 3 (Final Exit): sell 30 units at \$1.80 $\to$ Proceeds: \$54.00, Final PnL: +\$24.00.
- **Verification:**
  - Total Proceeds: $\$52 + \$45 + \$54 = \$151.00$.
  - Total Initial Cost: $\$100.00$.
  - Final Cumulative PnL: $\$12 + \$15 + \$24 = \mathbf{+\$51.00}$ (exact match).
  - Final Cumulative Return: $\mathbf{+51.0\%}$ (exact match).
  - Closed trade rows count: 3 execution records, but `_completed_trade_rows()` counts exactly **1** completed logical trade.
  - Zero double-counting.
- **Result:** **PASSED**

### Scenario 9 — Crash During a Transaction (Atomic State Persistence)
- **Test:** `test_scenario_09_crash_during_transaction_atomic_recovery`
- **Condition:** Valid `state.json` exists. Crash simulated by creating an uncommitted, corrupted temporary file `state.json.9999.partial.tmp`.
- **Verification:**
  - `storage.load_json_safe()` ignores corrupted temp files and reliably loads valid `state.json` from main storage.
  - Capital balances and open positions are completely intact.
- **Result:** **PASSED**

### Scenario 10 — Restart with an Open Position (State Reconstruction)
- **Test:** `test_scenario_10_restart_with_open_position_preserves_state`
- **Condition:** Active position with partial TP1 already taken, breakeven active at \$1.05, peak at \$1.40. Bot instance terminates and restarts (`reset=False`).
- **Verification:**
  - Restored position matches original: `qty == 50.0`, `tp1_hit == True`, `breakeven_activated == True`, `breakeven_price == 1.05`, `peak == 1.40`.
  - Position lifecycle continues seamlessly.
- **Result:** **PASSED**

### Scenario 11 — External API Unavailable (Fail-Safe Preservation)
- **Test:** `test_scenario_11_external_api_unavailable_no_zero_liquidation`
- **Condition:** DexScreener feed goes down (`feed_ok = False`, empty payload).
- **Verification:**
  - Bot does NOT liquidate position to \$0 or classify it as `no_price_data_assume_rug`.
  - Position retained with previous price mark (\$1.10).
  - Zero erroneous losses booked.
- **Result:** **PASSED**

### Scenario 12 — Token with Insufficient Liquidity (Pre-Trade Guard)
- **Test:** `test_scenario_12_insufficient_liquidity_blocked`
- **Condition:** Discovered token with \$2,000 liquidity (configured guard limit: \$10,000).
- **Verification:**
  - `_guards_entry()` identifies insufficient liquidity and rejects trade before order execution.
- **Result:** **PASSED**

### Scenario 13 — Token with Excessive Holder Concentration (Anti-Rug Interception)
- **Test:** `test_scenario_13_excessive_holder_concentration_blocked`
- **Condition:** Token evaluated where top 10 holders own > 60% or creator balance > 20%.
- **Verification:**
  - Anti-Rug engine flags risk: `risk == "HIGH"`, `is_rug == True`.
  - Purchase authorization withheld.
- **Result:** **PASSED**

### Scenario 14 — Duplicate Signal Prevention (In-Flight & Window Guards)
- **Test:** `test_scenario_14_duplicate_signal_prevention`
- **Condition:** Simulated token signal received across three distinct duplicate states:
  1. Mint already in open `positions`.
  2. Mint currently marked in `inflight`.
  3. Mint bought within recent `duplicate_window_s` (120 seconds).
- **Verification:**
  - Case 1: Rejected with `duplicate_open`.
  - Case 2: Rejected with `duplicate_open`.
  - Case 3: Rejected with `duplicate_recent_buy`.
- **Result:** **PASSED**

### Scenario 15 — Minimum Position Size Exceeding Maximum Risk Limit (Safety Ceiling)
- **Test:** `test_scenario_15_minimum_position_size_never_overrides_max_safety_limit`
- **Condition:** Conservative risk mode (`max_position_pct: 0.15`). Equity = \$100.00 $\implies$ Safety Ceiling = \$15.00.
  - Subtest A: `min_trade` = \$25.00 (exceeds \$15.00 ceiling).
  - Subtest B: Low equity (\$40.00, safety ceiling \$6.00), `min_trade` = \$10.00.
  - Subtest C: Normal trade (`min_trade` = \$5.00, within \$15.00 ceiling).
- **Verification:**
  - Subtest A: `size == 0.0`, note: `"minimo (25.0) supera limite massimo (15.00)"`.
  - Subtest B: `size == 0.0`, strictly $\le \text{equity} \times \text{max\_pos\_pct}$.
  - Subtest C: `size == 15.00`, strictly bounded by safety ceiling.
  - Proves the fundamental rule: **A minimum position-size requirement must never override a maximum safety limit.**
- **Result:** **PASSED**

---

## 3. How to Reproduce All Tests Locally

From the root project directory `C:\Users\Ez__Rekt\Desktop\AOEN`:

```powershell
# 1. Run the complete pytest test suite
python -m pytest tests --basetemp=.tmp_pytest -v

# 2. Run standalone core Take Profit test
python test_tp_core.py

# 3. Run standalone full Take Profit test
python test_tp.py
```
