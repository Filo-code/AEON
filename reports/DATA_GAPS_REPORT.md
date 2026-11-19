# AEON Data Gaps Report

## Critical Gaps

### 1. Liquidity is $0 for ALL trades
**Severity:** CRITICAL
**Status:** FIXED (2026-11-15)
**Root Cause:** `_pumpfun_to_pools()` and `_gmgn_to_pools()` populate `liquidity` but `discover()` doesn't merge it into `self.watch[m]`. The `spike()` filter checks `s["liquidity"] >= min_liquidity_usd` but the field is always 0.
**Fix:** Added proper liquidity merge logic in `discover()` - prefers DexScreener fresh liquidity, falls back to discovery source, marks as unknown if neither has data.

### 2. No distinction between 0% and UNKNOWN for dev_pct
**Severity:** HIGH
**Status:** FIXED
**Root Cause:** `RugCheck._parse()` returns 0.0 for dev_pct regardless of whether the creator was found, the balance is 0, or the API returned no data.
**Fix:** Added `dev_pct_source` field with values: `"verified"`, `"fallback_calculation"`, `"unknown"`.

### 3. No distinction between 0% and UNKNOWN for top10_pct
**Severity:** HIGH
**Status:** FIXED
**Root Cause:** `holder_filter()` treats missing holder data the same as verified 0% concentration.
**Fix:** Added `top10_pct_quality` field with values `"VERIFIED"` or `"UNKNOWN"`. Added config `reject_unknown_top10` (default: True) to reject entries with unknown holder data.

### 4. No volume spike baseline quality tracking
**Severity:** MEDIUM
**Status:** FIXED
**Root Cause:** `spike()` uses `min_baseline_5m_usd` as floor when `vol_h1` is unavailable, creating artificial spike ratios.
**Fix:** Added `baseline_quality` field to spike metadata: `"historical"` or `"minimum_floor"`.

### 5. No MFE/MAE tracking
**Severity:** MEDIUM
**Status:** FIXED
**Root Cause:** Maximum Favorable/Maximum Adverse Excursion not tracked per position.
**Fix:** Added `mfe_pct`, `mae_pct`, `mfe_ts`, `mae_ts` to position tracking, updated on each price tick.

### 6. No position lifecycle snapshots
**Severity:** MEDIUM
**Status:** FIXED
**Root Cause:** Only entry/exit snapshots recorded, no periodic observations.
**Fix:** Added `trade_snapshots.csv` with entry and exit snapshots per trade.

### 7. No execution event tracking
**Severity:** MEDIUM
**Status:** FIXED
**Root Cause:** Only final execution recorded, no quote attempts or route changes.
**Fix:** Added `execution_events.csv` with entry_quote, exit_quote, and failed_exit events.

### 8. No strategy signal tracking
**Severity:** LOW
**Status:** FIXED
**Root Cause:** Entry/exit decisions not recorded separately from trades.
**Fix:** Added `strategy_signals.csv` with entry/exit signal decisions and filter details.

### 9. No recovery event tracking
**Severity:** LOW
**Status:** FIXED
**Root Cause:** Recovery and Anti-Rug decisions not recorded.
**Fix:** Added `recovery_events.csv` with recovery and Anti-Rug events.

### 10. No trade_id for cross-file joins
**Severity:** MEDIUM
**Status:** FIXED
**Root Cause:** No unique identifier to join trades, snapshots, events, signals.
**Fix:** Added `trade_id` field to all CSV files: `{entry_ts}_{mint[:8]}`.

## Remaining Gaps

### 11. No Jupiter quote caching for exit
**Severity:** LOW
**Status:** UNRESOLVED
**Note:** Jupiter quotes are cached for buys but not for exits. Exits call `jup_sell()` fresh each time.

### 12. No Birdeye liquidity tracking
**Severity:** LOW
**Status:** UNRESOLVED
**Note:** Birdeye is optional and OFF by default. When enabled, liquidity from Birdeye is not separately tracked.

### 13. No pair age vs token age distinction
**Severity:** LOW
**Status:** UNRESOLVED
**Note:** `created_ts` could be token creation, pool creation, or pair creation depending on provider. Not distinguished.

### 14. No whale wallet identification
**Severity:** LOW
**Status:** UNRESOLVED
**Note:** Whale detection uses transaction size only, not wallet reputation. No "smart money" whale identification.

---

## Data Quality States

| State | Meaning | Example |
|---|---|---|
| `VERIFIED` | Executable quote from trusted provider | Jupiter quote price |
| `ESTIMATED` | Model-based or single-provider | Liquidity model slippage |
| `STALE` | Older than threshold | Price > 60s old |
| `UNKNOWN` | Provider unavailable, missing data | Holder data unavailable |
| `INVALID` | Failed validation | Negative liquidity |

---

*Last Updated: 2026-11-15*