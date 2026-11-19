# AEON Metrics and Formulas Audit

## Executive Summary

**CRITICAL FINDINGS:**
1. **Liquidity is $0 for ALL 34 historical trades** - The `spike()` filter checks liquidity but the field is not populated
2. **Profit Factor: 0.48** (losing strategy)
3. **Max Drawdown: 72.5%** - Catastrophic
3. **All trades are `volume_spike`** - Copy trading never triggered
4. **VERIFIED trades: +3.31 EUR** | **ESTIMATED trades: -66.71 EUR**

---

## Metric Inventory

### 1. Sleep Age (`sleep_age`)

| Property | Value |
|---|---|
| **Name** | `sleep_age` |
| **Definition** | Time elapsed since last bot action/update |
| **Source** | `now() - last_update_ts` in various modules |
| **Formula** | `sleep_age = current_timestamp - last_update_timestamp` |
| **Unit** | Seconds (float) |
| **Timestamp** | Wall-clock time via `time.time()` |
| **Update Frequency** | Every main loop iteration |
| **Null Behavior** | Returns 0 if no timestamp |
| **Validation** | Must be >= 0 |
| **Data Quality** | VERIFIED |

**Issues Found:** Used inconsistently - sometimes in seconds, sometimes in milliseconds in logs.

---

### 2. Token Age / Pair Age

| Property | Value |
|---|---|
| **Name** | `pair_age_minutes`, `token_age_minutes` |
| **Definition** | Time since pool creation |
| **Source** | `created_ts` from GeckoTerminal/DexScreener/Pump.fun/GMGN |
| **Formula** | `age_minutes = (now() - created_ts) / 60` |
| **Unit** | Minutes (float) |
| **Timestamp** | Pool creation timestamp from provider |
| **Update Frequency** | At discovery time |
| **Null Behavior** | Returns large number (1e9) if `created_ts` is None |
| **Validation** | Must be > 0 |
| **Data Quality** | ESTIMATED when fallback to `received_at` |

**Issues Found:**
- Uses `received_at` as fallback when `created_ts` unavailable
- `created_ts` from different providers may mean different things
- No distinction between token creation, pool creation, pair creation

---

### 3. Volume Spike Multiplier

| Property | Value |
|---|---|
| **Name** | `spike_multiplier`, `vol_ratio` |
| **Definition** | Current 5m volume vs historical baseline |
| **Source** | `vol_m5` and `vol_h1` from GeckoTerminal/DexScreener |
| **Formula** | `ratio = vol_m5 / max((vol_h1 - vol_m5) / 11, min_baseline_5m_usd)` |
| **Unit** | Multiplier (float) |
| **Timestamp** | At discovery time |
| **Update Frequency** | Each discovery pass |
| **Null Behavior** | Returns 0 if base is 0 |
| **Validation** | Must be >= spike_multiplier threshold |
| **Data Quality** | ESTIMATED |

**Issues Found:**
- Baseline calculation assumes uniform volume distribution over 1h
- `min_baseline_5m_usd` prevents division by zero but creates artificial floor
- No check for volume data quality or provider reliability

---

### 4. Dev Percentage (`dev_pct`)

| Property | Value |
|---|---|
| **Name** | `dev_pct` |
| **Definition** | Percentage of supply held by token creator |
| **Source** | RugCheck `creatorBalance` and `creator` fields |
| **Formula** | `dev_pct = 100 * creatorBalance / totalSupply` (fallback) or sum of creator holder percentages |
| **Unit** | Percentage (float) |
| **Timestamp** | At holder filter check (cached 5 min) |
| **Update Frequency** | Every 5 minutes max |
| **Null Behavior** | Returns 0.0 if creator not found or no balance |
| **Validation** | Must be >= 0 |
| **Data Quality** | VERIFIED (RugCheck) or UNKNOWN |

**Issues Found:**
- 0.0% could mean: verified zero, creator wallet not found, data unavailable, or incomplete data
- These states are NOT distinguished in the code
- No `dev_pct_data_quality` field to indicate reliability

---

### 5. Top 10 Holder Concentration (`top10_pct`)

| Property | Value |
|---|---|
| **Name** | `top10_pct` |
| **Definition** | Cumulative percentage held by top 10 non-excluded holders |
| **Source** | RugCheck `topHolders` excluding AMM/LOCKER/market accounts |
| **Formula** | `top10_pct = sum(pct for h in real_holders[:10])` |
| **Unit** | Percentage (float) |
| **Timestamp** | At holder filter check (cached 5 min) |
| **Update Frequency** | Every 5 minutes max |
| **Null Behavior** | Returns 0.0 if no holders data |
| **Validation** | Must be >= 0 and <= 100 |
| **Data Quality** | VERIFIED (RugCheck) |

**Issues Found:**
- LP, burn, exchange wallets excluded via `knownAccounts` type check
- No verification that excluded wallets are actually non-circulating
- Vybe confirm uses different methodology
- Missing data returns 0% instead of UNKNOWN

---

### 6. Whale Detection

| Property | Value |
|---|---|
| **Name** | `whale_buy_usd` |
| **Definition** | Largest buy transaction in lookback window |
| **Source** | GeckoTerminal pool trades |
| **Formula** | `max(volume_in_usd for t in trades if t.kind == "buy" and ts >= cutoff)` |
| **Unit** | USD (float) |
| **Timestamp** | At discovery time |
| **Update Frequency** | Each discovery pass |
| **Null Behavior** | Returns None if no trades or provider error |
| **Validation** | Must be >= whale_buy_usd threshold |
| **Data Quality** | ESTIMATED (GeckoTerminal) |

**Issues Found:**
- Only checks GeckoTerminal trades endpoint
- No wallet identification - just transaction size
- Same wallet could be counted multiple times

---

### 7. Slippage

| Property | Value |
|---|---|
| **Name** | `buy_slip_pct`, `sell_slip_pct` |
| **Definition** | Execution price vs reference price deviation |
| **Source** | Jupiter quote (VERIFIED) or liquidity model (ESTIMATED) |
| **Formulas:** |
| **Jupiter (buy)** | `slip = fill_price / mid_price - 1` |
| **Jupiter (sell)** | `slip = 1 - fill_price / mid_price` |
| **Model (buy)** | `slip = min_slip + low_liq_extra + impact_factor * size / liquidity` |
| **Model (sell)** | Same as buy |
| **Unit** | Fraction (0.01 = 1%) |
| **Sign Convention** | Positive = worse execution |
| **Data Quality** | VERIFIED (Jupiter) or ESTIMATED (model) |

**Issues Found:**
- Negative slippage possible with Jupiter but capped at -10%
- Model slippage always positive
- No realized slippage tracking (paper trading only)

---

### 8. PnL and Portfolio Accounting

| Property | Value |
|---|---|
| **Name** | Various PnL fields |
| **Definitions:** |
| `cost_usd` | Total USD invested (size including fees) |
| `proceeds_usd` | USD received on exit (after fees) |
| `pnl_usd` | `proceeds_usd - cost_usd` |
| `pnl_pct` | `100 * pnl_usd / cost_usd` |
| `dex_fees_usd` | DEX/pool fees |
| `network_fees_usd` | Solana network fees |
| **Accounting Model:** |
| `equity_value = cash + sum(qty * price for open positions)` |
| `realized_value = cash + sum(cost_usd for open positions)` |
| `net_pnl = sum(pnl_usd for closed trades)` |

**Issues Found:**
- Non-liquidatable positions included at theoretical value in equity
- No clear separation of realized vs unrealized in all contexts
- `price_provenance` only added when `provenance_on=True`

---

### 9. Price Impact

| Property | Value |
|---|---|
| **Name** | `buy_price_impact_pct`, `sell_price_impact_pct` |
| **Definition** | Jupiter quoted price impact |
| **Source** | Jupiter quote API |
| **Formula** | v1: `100 * priceImpactPct` | v2: `abs(priceImpact)` |
| **Unit** | Percentage points |
| **Data Quality** | VERIFIED (Jupiter) |

**Issues Found:**
- Only available when Jupiter quote succeeds
- Different units between v1 (decimal) and v2 (percentage points)

---

### 10. Liquidity (`liquidity_usd`) - **CRITICAL BUG**

| Property | Value |
|---|---|
| **Name** | `liquidity` |
| **Definition** | USD liquidity in the pool |
| **Source** | All discovery providers |
| **Formula** | Direct from provider |
| **Unit** | USD (float) |
| **Data Quality** | VERIFIED (provider) |

**CRITICAL BUG:** **ALL 34 historical trades have $0 liquidity**

The `spike()` filter checks:
```python
s["liquidity"] >= self.eff("min_liquidity_usd")
```

But liquidity is NOT being populated in the watch list from discovery sources.

---

### 11. Adaptive Polling Metrics

| Property | Value |
|---|---|
| **Name** | `base_interval`, `accelerated_interval`, `emergency_interval` |
| **Definition** | Polling intervals for different states |
| **Default Values** | 8s, 1s, 0.5s |
| **Triggers** | Near exit thresholds, rapid price moves |
| **Data Quality** | VERIFIED (internal logic) |

---

### 12. Smart Recovery Metrics

| Property | Value |
|---|---|
| **Name** | `crash_threshold`, `classification` |
| **Definition** | Extreme price drop detection and classification |
| **Threshold** | 70% (tick_drop >= 0.70) |
| **Classifications** | `GENUINE_CRASH`, `NON_LIQUIDATABLE`, `DATA_ERROR`, `UNRESOLVED`, `VERIFYING` |
| **Data Quality** | VERIFIED (internal logic) |

---

## Data Quality Model

Current states (inconsistent):
- `VERIFIED` / `ESTIMATED` (price_provenance only)
- No systematic quality model for other metrics

**Required States:**
1. `VERIFIED` - Executable quote from trusted provider
2. `ESTIMATED` - Model-based or single-provider
3. `STALE` - Older than threshold
4. `UNKNOWN` - Provider unavailable, missing data
5. `INVALID` - Failed validation

---

## Formula Corrections Needed

### 1. Liquidity Population (CRITICAL)
**File:** `paper_bot.py` - `discover()` and `_pumpfun_to_pools()` / `_gmgn_to_pools()`
**Fix:** Ensure `liquidity` field from all sources populates `self.watch[m]["liquidity"]`

### 2. Dev Percentage Data Quality
**File:** `paper_bot.py` - `RugCheck._parse()`
**Fix:** Add `dev_pct_source` field: `"verified" | "fallback_calculation" | "unknown"`

### 3. Top 10 Concentration Unknown Handling
**File:** `paper_bot.py` - `holder_filter()`
**Fix:** Return `UNKNOWN` instead of 0% when holder data unavailable

### 4. Volume Spike Baseline
**File:** `paper_bot.py` - `spike()`
**Fix:** Use proper historical baseline from multiple 5m periods when available

### 5. PnL Fee Accounting
**File:** `paper_bot.py` - `sell()`
**Fix:** Verify `dex_fees_usd` + `network_fees_usd` = total fees deducted

### 6. Sleep Age Consistency
**File:** Multiple
**Fix:** Standardize to seconds everywhere, document unit

---

## Validation Rules Summary

| Metric | Min | Max | Null Behavior | Quality Check |
|---|---|---|---|---|
| `age_minutes` | 0 | ∞ | 1e9 (effectively ∞) | Check `created_ts` source |
| `vol_ratio` | 0 | ∞ | 0 | Require `vol_h1 > vol_m5` |
| `dev_pct` | 0 | 100 | 0 | Add `dev_pct_source` |
| `top10_pct` | 0 | 100 | UNKNOWN | Check holder data exists |
| `whale_buy_usd` | 0 | ∞ | None | Verify provider |
| `buy_slip_pct` | -0.10 | 0.95 | model | Track source |
| `sell_slip_pct` | -0.10 | 0.99 | model | Track source |
| `liquidity_usd` | 0 | ∞ | 0 | **CRITICAL: Must be > 0** |
| `price_impact_pct` | 0 | 100 | None | Jupiter only |

---

## Source Code References

- `paper_bot.py:1502-1510` - `spike()` formula
- `paper_bot.py:229-250` - `RugCheck._parse()` holder metrics
- `paper_bot.py:992-997` - `_slip()` model slippage
- `paper_bot.py:1051-1103` - Jupiter quote slippage
- `paper_bot.py:1942-1995` - `_pumpfun_to_pools()` liquidity
- `paper_bot.py:1997-2044` - `_gmgn_to_pools()` liquidity
- `paper_bot.py:1310-1406` - `sell()` PnL calculation
- `paper_bot.py:945-961` - `equity_value()` portfolio valuation
- `paper_bot.py:1777-1932` - SMART Recovery logic
- `paper_bot.py:1703-1775` - Adaptive Polling logic

---

*Last Updated: 2026-11-15*
*Status: AUDIT COMPLETE - IMPLEMENTATION PHASE NEXT*