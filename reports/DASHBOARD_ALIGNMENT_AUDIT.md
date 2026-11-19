# AEON — DASHBOARD ALIGNMENT & DATA INTEGRITY AUDIT
## Comprehensive Architectural Discovery, Data Flow Analysis & Audit Plan

**Auditor:** Senior Quant Developer & Trading Systems Architect  
**Date:** 2026-11-18  
**Repository Target:** `C:\Users\Ez__Rekt\Desktop\AOEN`  
**Phase:** DASHBOARD ALIGNMENT (Phase 3.5 — Dedicated Exclusively to Dashboard)  
**Status:** AUDIT COMPLETE — IMPLEMENTATION PLAN PENDING EXECUTION  

---

## 1. Executive Summary & Safety Constraints

This audit evaluates the architecture, data ingestion, metric calculation, and visualization layers of the AEON dashboard. The primary goal is ensuring **100% data integrity**: the dashboard must be a truthful mirror of the internal bot state, risk management, trade accounting, AntiRug security engine, and ValidatedBrain learning system.

### Absolute Safety & Non-Interference Invariants:
1. **Zero Trading Logic Alterations:** Signal detection, entry filters, order sizing, take-profit (TP1/TP2), stop-loss, trailing stops, and execution models in `bot/paper_bot.py` are strictly untouched.
2. **Zero Risk & Capital Alterations:** Risk limits, drawdown ceilings, capital allocation (€100.00 / \$108.00), and profile parameters in `bot/risk.py` remain immutable.
3. **Zero Brain Learning Modification:** Welch's t-test validation, observation intervals (10/50/100), evaluation hurdles ($t \ge 2.0$), and blacklisting logic in `bot/cervello.py` remain untouched.
4. **Simulation-Only Mode Guaranteed:** `simulation_only: true` is strictly preserved across all data structures. No live trading credentials or RPC execution hooks can be activated.
5. **No Data Fabrication:** Missing metrics will be represented as `N/A` or `—` rather than substituted with synthetic estimates.
6. **Backend Enhancements Are Read-Only:** All modifications to backend modules (`dashboard/app.py` and read-only telemetry accessors in `bot/paper_bot.py`) serve exclusively to extract, aggregate, and serialize authentic runtime state.

---

## 2. Existing Dashboard Architecture & Data Flow

### 2.1 Component Architecture Map
```
┌────────────────────────────────────────────────────────────────────────┐
│                          BOT RUNTIME ENGINE                            │
│  (bot/paper_bot.py, bot/cervello.py, bot/risk.py, bot/antirug.py)      │
└──────────────┬──────────────────────────┬──────────────────────────────┘
               │ Every 15s or             │ High frequency
               │ on entry/exit            │ pulse (every 2s)
               ▼                          ▼
      ┌─────────────────┐        ┌──────────────────┐
      │ data/state.json │        │data/heartbeat.json│
      └────────┬────────┘        └────────┬─────────┘
               │                          │
               └───────────┬──────────────┘
                           ▼
              ┌─────────────────────────┐
              │    DASHBOARD BACKEND    │◄─── data/pumpfun_state.json
              │   (dashboard/app.py)    │◄─── data/gmgn_state.json
              │  Flask + Basic Auth     │
              └────────────┬────────────┘
                           │ GET /api/state (JSON)
                           │ Polled every 2500ms
                           ▼
              ┌─────────────────────────┐
              │   DASHBOARD FRONTEND    │
              │(dashboard/static/js/    │
              │      dashboard.js)      │
              │  SPA + SVG Charts + UI  │
              └─────────────────────────┘
```

### 2.2 Data Ingestion & Storage Artifacts
1. **`DATA_DIR/state.json` (Authoritative Source of Truth):**
   - Written atomically by `PaperBot._save_state_locked(status)`.
   - Contains:
     - Balances: `starting_balance_usd`, `starting_balance_eur`, `cash`, `fx_usd_per_eur`, `fx_now`.
     - Positions: `positions` dict (keyed by SPL token mint).
     - Closed Executions: `closed` list (contains both partial and completed trade records).
     - Equity curve: `equity` list of triplets `[timestamp, equity_value, realized_value]`.
     - Risk state: `risk`, `survival` (death line, max drawdown, next size).
     - AI Brain: `brain` (summary of `ValidatedBrain`).
     - Security: `antirug` (summary of `AntiRugEngine`).
     - System Telemetry: `health`, `rpc`, `jupiter`, `stats`, `agents`, `events`.
2. **`DATA_DIR/heartbeat.json`:**
   - Liveness heartbeat timestamp updated continuously by the bot main loop.
3. **`DATA_DIR/pumpfun_state.json` & `DATA_DIR/gmgn_state.json`:**
   - Stream state, recent events, and connection status for external discovery channels.

---

## 3. Metric-to-Source Mapping & Discrepancy Matrix

| Domain | Dashboard Metric | UI Element / Page | Current Calculation | Canonical Source of Truth | Identified Flaw / Discrepancy |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Financials** | Initial Capital | Overview KPI, Portfolio | `usd(v.start)` | `st["starting_balance_usd"]` / `st["starting_balance_eur"]` | Converts USD to EUR using `/ FX`. Accurate, but dual-currency display (EUR primary, USD secondary) should be explicit. |
| **Financials** | Cash Balance | Overview KPI, Portfolio | `usd(v.cash)` | `st["cash"]` | **Accurate** (synchronized to 4 decimals in Phase 3). |
| **Financials** | Invested Capital | Portfolio (`Esposizione`) | `usd(v.open_value)` | $\sum p[\text{"cost\_usd"}]$ | **CRITICAL BUG:** Conflates market value ($qty \times last$) with invested capital ($cost\_usd$). Invested capital must be cost basis. |
| **Financials** | Position Market Value | Portfolio, Donut | `open_val = sum(qty * last)` | $\sum p[\text{"qty"}] \times p[\text{"last"}]$ | Accurate for market value, but was missing a distinct label from cost basis. |
| **Financials** | Total Portfolio Equity | Overview KPI, Header, Chart | `st["cash"] + open_val` | `self.equity_value()` | Recomputed inline in `app.py`. Must be canonical backend field. |
| **Financials** | Realized P&L | Overview KPI, Portfolio | `sum(t["pnl_usd"] for t in closed)` | Sum of realized executions | Correct in dollar sum, but distorting trade counts due to partials. |
| **Financials** | Unrealized P&L | Overview KPI, Positions | `(v.pnl) - (v.realized)` | $\sum (qty \times last - cost\_usd)$ | **CRITICAL ARCHITECTURAL FLAW:** Computed in frontend via subtraction rather than backend position mark-to-market. |
| **Financials** | Total P&L | Overview KPI | `equity - start` | $\text{realized\_pnl} + \text{unrealized\_pnl}$ | Accurate, but should be canonical backend metric. |
| **Financials** | Return % | Overview KPI | Frontend inline formula | `((equity - start) / start) * 100` | Duplicated in frontend; backend must supply canonical `return_pct`. |
| **Financials** | Total Fees | Health Grid, Health Page | `(fees_dex) + (fees_net)` | $\sum t[\text{"dex\_fees\_usd"}] + t[\text{"network\_fees\_usd"}]$ | Computed in frontend; backend must supply canonical `fees_total`. |
| **Trading** | Total Trades Count | Performance KPI, Badge | `n = len(closed)` | `len([t for t in closed if t.get("counts_as_trade", True)])` | **CRITICAL BUG:** Counts partial legs as independent completed trades. Artificially inflates trade count. |
| **Trading** | Winning Trades | Performance KPI | `len([t for t in closed if t["pnl_usd"] > 0])` | Completed trades with `cumulative_pnl_usd > 0` | **CRITICAL BUG:** Partial TP1 legs counted as winning trades even if trade subsequently stopped out at a loss! |
| **Trading** | Losing Trades | Performance KPI | `n - len(wins)` | Completed trades with `cumulative_pnl_usd <= 0` | **CRITICAL BUG:** Distorted by partial trade inflation. |
| **Trading** | Win Rate % | Performance KPI, Badge | `100 * wins / n` | `100 * completed_wins / completed_trades` | **CRITICAL BUG:** Inaccurate due to partial exit inflation. |
| **Strategies** | Strategy Breakdown | Portfolio Page | `by_type[entry_type]` | Grouped by `entry_type` | Hardcoded names, partial legs counted as full trades, missing enabled/disabled status, win rate, avg hold time, open positions count. |
| **Brain** | AI Learning Status | Overview Intel, Strategies | `brn.diagnosis` & `brn.next_in` | `st["brain"]` (`ValidatedBrain.summary()`) | **INCOMPLETE:** Missing learning mode (`validated`), active parameter experiment details, accepted adjustments list, rejected adjustments list, and phase. |
| **AntiRug** | Security Mode & Scans | Intelligence Page | `ar.mode` & `r.decision` | `st["antirug"]` (`AntiRugEngine.summary()`) | **MISLEADING:** Scans marked `BLOCK` did not indicate whether the system was in SHADOW mode (where trades are NOT blocked) or ACTIVE mode. |
| **Health** | Solana RPC Status | Header Pill | `isAlive ? 'Connesso' : 'Offline'` | `st["rpc"]` & `st["health"]["hosts"]` | **MISLEADING:** Displayed "Solana RPC: Connesso" purely based on bot heartbeat age, completely ignoring real RPC errors. |

---

## 4. Root Cause Analysis of Primary Discrepancies

### 4.1 The Partial Exit Trade Inflation Bug
In `bot/paper_bot.py`:
- When a position hits TP1, a partial exit record is appended to `self.closed` with `t["partial"] = True`, `t["counts_as_trade"] = False`, and `t["pnl_usd"] = pnl_leg1`.
- When the position fully exits, a record is appended with `t["counts_as_trade"] = True`, `t["pnl_usd"] = pnl_final`, and `t["cumulative_pnl_usd"] = total_pnl`.
- In `dashboard/app.py`:
  ```python
  closed = st.get("closed", [])
  wins = [t for t in closed if t["pnl_usd"] > 0]
  n = len(closed)
  ```
  The dashboard directly used `len(closed)` and `t["pnl_usd"] > 0` across all records. A single trade with 2 partial exits was counted as 3 independent trades, and profitable partials were counted as independent wins.
- **Correction:** Filter completed trades using `t.get("counts_as_trade", True)` for all trade count, win rate, win/loss, and strategy statistics. Sum `t["pnl_usd"]` across all executions for total realized cash gain, but evaluate win/loss outcome on the completed trade's `cumulative_pnl_usd`.

### 4.2 Frontend / Backend Formula Duplication & Unrealized P&L
In `dashboard.js`:
- Line 201: `const unreal = (v.pnl || 0) - (v.realized || 0);`
- Line 202: `const pnlPct = v.start > 0 ? ((v.equity - v.start) / v.start) * 100 : 0;`
- Line 547: `const totalFees = (v.fees_dex || 0) + (v.fees_net || 0);`
These calculations duplicated logic that belongs in the backend. When the backend does not provide canonical values, discrepancies arise between different dashboard tabs (e.g. Overview vs Portfolio vs Positions).
- **Correction:** Calculate all financial metrics in `dashboard/app.py` and deliver a single, canonical schema to the frontend.

### 4.3 Fake Solana RPC Liveness
In `dashboard.js`:
- Line 177: `const isAlive = Boolean(v.live && !v.dead);`
- Line 179: `if (rpcVal) rpcVal.textContent = v.dead ? 'Capitale Esaurito' : (isAlive ? 'Connesso (' + v.heartbeat_age + 's)' : 'Offline');`
The UI claimed "Solana RPC: Connesso" whenever the bot process was alive, even if the Solana RPC was throwing 500 errors or rate-limited.
- **Correction:** Consume `st["rpc"]` (which contains `errors`, `latency_ms`, `ok_rate`, `last_error`). The status pill will display "Connesso" (green) only when RPC error rate is low, "Degradato" (amber) when RPC has errors, and "Disconnesso" (red) when RPC is unreachable.

---

## 5. Files Requiring Modification

1. **`dashboard/app.py` (Backend API & Canonical Metric Provider):**
   - Refactor `_view(st)` to generate canonical financial metrics:
     - `invested_capital`: exact cost basis of open positions ($\sum p[\text{"cost\_usd"}]$).
     - `open_value`: current market value of open positions ($\sum p[\text{"qty"}] \times p[\text{"last"}]$).
     - `unrealized_pnl`: exact unrealized P&L ($\sum \text{open\_val} - \text{invested\_capital}$).
     - `unrealized_pnl_pct`: percentage return on open positions.
     - `realized_pnl`: sum of all realized execution P&Ls.
     - `total_pnl`: canonical net P&L from inception.
     - `return_pct`: percentage return on starting capital.
     - `fees_total`, `fees_dex`, `fees_net`.
     - `completed_trades`: count of distinct completed trades (`counts_as_trade == True`).
     - `completed_wins`: completed trades with `cumulative_pnl_usd > 0`.
     - `completed_losses`: completed trades with `cumulative_pnl_usd <= 0`.
     - `win_rate`: accurate win rate based strictly on completed trades.
     - `partial_exits_count`: distinct count of partial executions (`partial: True`).
   - Refactor Strategy Performance aggregation:
     - Compute per-strategy metrics: `enabled`, `trades_completed`, `wins`, `losses`, `win_rate`, `realized_pnl`, `avg_pnl`, `avg_hold_sec`, `open_positions_count`.
     - Read strategy enabled status directly from `st.get("config")` / `st.get("exit_rules")`.
   - Refactor Brain Intelligence aggregation:
     - Expose `mode` (`"validated"`), `phase`, `analysis_interval`, `next_in`, `next_at_trade`, `last_analysis_ts`, `records_analyzed`, `pending_trial`, `changes_accepted`, `changes_rejected`, `diagnosis`.
   - Refactor Anti-Rug Security aggregation:
     - Clearly tag each recent scan with `effective_action`: `"BLOCKED"` (in active mode) vs `"OBSERVED"` (in shadow mode).
   - Refactor System Health aggregation:
     - Evaluate real RPC status from `st.get("rpc")`, host health from `st.get("health", {}).get("hosts")`, storage persistence from `st.get("health", {}).get("persistence_degraded")`.
2. **`dashboard/static/js/dashboard.js` (Frontend Presentation Layer):**
   - Bind all KPI cards to the canonical backend metrics (eliminating frontend `pnl - realized` subtraction).
   - Update `renderKPIs()`:
     - Display canonical `unrealized_pnl`, `realized_pnl`, `cash`, `equity`, `return_pct`.
   - Update `renderTradingTable()` and `renderSecondaryPage('positions')` / `renderSecondaryPage('history')`:
     - Explicitly distinguish partial exits (`PARTIAL` badge with fraction, e.g. `TP1 (50%)`) from complete exits.
     - Display remaining token quantity and allocated cost basis.
   - Update `renderSecondaryPage('portfolio')`:
     - Display both Invested Capital (cost basis) and Current Market Value.
     - Render full strategy performance table with real backend metrics.
   - Update `renderSecondaryPage('strategies')`:
     - Display active Brain learning mode (`ValidatedBrain`), current window, pending experiment, accepted adjustments, and blacklisted adjustments.
   - Update `renderIntelligence()`:
     - Clearly label Anti-Rug mode and distinguish shadow observations from active blocks.
   - Update `renderHeaderStatus()` & `renderHealth()`:
     - Display real Solana RPC health (healthy / degraded / down) and host latencies.
3. **`dashboard/templates/index.html`:**
   - Add/align semantic data attributes, tooltips, and ensure responsive layout accommodates enhanced metrics without visual clutter.
4. **`tests/test_aeon_dashboard_alignment.py` (New Automated Test Suite):**
   - 15 comprehensive tests covering all required verification points (portfolio reconciliation, cash consistency, partial exit representation, fee aggregation, strategy metrics, brain checkpoint fidelity, antirug mode disambiguation, missing/stale data resilience, and refresh deduplication).

---

## 6. Implementation Plan & Staged Execution

```
┌────────────────────────────────────────────────────────────────────────┐
│ STAGE 1: Backend Canonical Metric Pipeline (dashboard/app.py)          │
│ - Implement canonical financial metrics & reconciliation formulas      │
│ - Implement completed-trade filtering for Win Rate & PnL               │
│ - Implement strategy performance aggregation engine                    │
│ - Implement Brain, Anti-Rug, and System Health data enrichers          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STAGE 2: Frontend Data Binding & Alignment (dashboard.js, index.html)  │
│ - Remove duplicate / inline client-side math                           │
│ - Bind Overview KPIs to canonical backend fields                       │
│ - Upgrade Open Positions & History tables with partial exit indicators │
│ - Enhance Strategy, Brain, Anti-Rug, and Health panels                 │
│ - Refactor Solana RPC status pill to reflect real RPC connectivity     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STAGE 3: Test Suite Implementation & Verification                      │
│ - Author tests/test_aeon_dashboard_alignment.py (15 tests)             │
│ - Run full PyTest regression suite & standalone TP suites              │
│ - Verify zero regressions across existing 50 tests                     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STAGE 4: Final Documentation & Deliverables                            │
│ - Generate reports/DASHBOARD_ALIGNMENT_REPORT.md                       │
│ - Mirror deliverables to versione_finale                               │
└────────────────────────────────────────────────────────────────────────┘
```

The audit phase is complete. Proceeding to implementation.
