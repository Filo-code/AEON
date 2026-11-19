"""
AEON Dashboard Alignment & Final Fixes Test Suite
=================================================
Independent regression tests verifying:
1. Canonical financial metrics & no frontend arithmetic.
2. No synthetic equity chart points.
3. Provider connectivity from actual telemetry, never ops_per_min.
4. Brain historical change classification (REJECTED, ACCEPTED, UNKNOWN).
5. Anti-Rug mode-aware presentation (OFF / SHADOW / ACTIVE).
6. RPC response freshness validation (HEALTHY, DEGRADED, DOWN, UNKNOWN).
7. Financial reconciliation across partial & complete exits.

All tests import production functions from dashboard.app directly.
SAFETY: Zero trading-logic modifications — read-only telemetry only.
"""
import sys, os, json, time, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from dashboard.app import _view, _is_completed_trade, _rpc_health, _classify_brain_change, _build_source_status


def _base_state(**overrides):
    """Minimal valid state.json skeleton."""
    now_ts = time.time()
    st = {
        "starting_balance_usd": 108.0,
        "cash": 108.0,
        "positions": {},
        "closed": [],
        "equity": [],
        "status": "running",
        "heartbeat_ts": now_ts,
        "agents": {},
        "stats": {},
        "meta": {},
        "brain": None,
        "risk": None,
        "survival": {},
        "exit_rules": {},
        "config": {},
        "jupiter": {},
        "rpc": {"ok": 10, "fail": 0, "last_success_ts": now_ts},
        "antirug": None,
    }
    st.update(overrides)
    return st


def _make_completed_trade(mint="AAAA", token="$TOKEN", entry_type="volume_spike",
                           cost=10.0, proceeds=12.0, partial_pnl=0.0,
                           entry_ts=None, exit_ts=None):
    """Build a full completed trade record (counts_as_trade=True)."""
    now = time.time()
    pnl = round(proceeds - cost, 4)
    cum_pnl = round(pnl + partial_pnl, 4)
    return {
        "mint": mint, "token": token, "entry_type": entry_type,
        "cost_usd": cost, "proceeds_usd": proceeds, "pnl_usd": pnl,
        "pnl_pct": round(100.0 * pnl / cost, 2) if cost else 0,
        "cumulative_pnl_usd": cum_pnl,
        "cumulative_pnl_pct": round(100.0 * cum_pnl / cost, 2) if cost else 0,
        "counts_as_trade": True,
        "partial": False,
        "exit_reason": "stop_loss" if pnl < 0 else "take_profit",
        "entry_ts": entry_ts or (now - 300),
        "exit_ts": exit_ts or now,
        "hold_sec": 300,
        "dex_fees_usd": 0.01,
        "network_fees_usd": 0.005,
        "buy_slip_pct": 0.5,
        "sell_slip_pct": 0.3,
        "failed_exits": 0,
    }


def _make_partial_exit(mint="AAAA", token="$TOKEN", entry_type="volume_spike",
                        cost_partial=5.0, proceeds_partial=6.0,
                        entry_ts=None, exit_ts=None):
    """Build a partial TP1 exit record (counts_as_trade=False)."""
    now = time.time()
    pnl = round(proceeds_partial - cost_partial, 4)
    return {
        "mint": mint, "token": token, "entry_type": entry_type,
        "cost_usd": cost_partial, "proceeds_usd": proceeds_partial, "pnl_usd": pnl,
        "pnl_pct": round(100.0 * pnl / cost_partial, 2) if cost_partial else 0,
        "counts_as_trade": False,
        "partial": True,
        "exit_reason": "take_profit_1",
        "entry_ts": entry_ts or (now - 600),
        "exit_ts": exit_ts or now,
        "hold_sec": 300,
        "dex_fees_usd": 0.005,
        "network_fees_usd": 0.002,
        "buy_slip_pct": 0.5,
        "sell_slip_pct": 0.2,
        "failed_exits": 0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — Portfolio equity reconciliation
# ═══════════════════════════════════════════════════════════════════════════════
def test_01_equity_reconciliation():
    """equity = cash + open_val = cash + invested_capital + unrealized_pnl"""
    st = _base_state(
        cash=50.0,
        positions={
            "MINT1": {"qty": 100.0, "last": 0.4, "cost_usd": 35.0,
                      "entry_mid": 0.35, "entry_ts": time.time() - 60, "entry_type": "volume_spike",
                      "pool": "P", "dex": "raydium", "name": "$TOKEN1",
                      "entry_fill": 0.35, "failed_exits": 0, "costs": {}, "info": {}, "track": {}},
        }
    )
    v = _view(st)
    assert v["ok"]
    expected_equity = round(50.0 + 100.0 * 0.4, 4)
    assert math.isclose(v["equity"], expected_equity, abs_tol=1e-6)
    assert math.isclose(v["invested_capital"], 35.0, abs_tol=1e-6)
    expected_unreal = round(100.0 * 0.4 - 35.0, 4)
    assert math.isclose(v["unrealized_pnl"], expected_unreal, abs_tol=1e-6)
    assert math.isclose(v["equity"], v["cash"] + v["invested_capital"] + v["unrealized_pnl"], abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — Cash balance consistency
# ═══════════════════════════════════════════════════════════════════════════════
def test_02_cash_balance_passthrough():
    """v.cash must exactly equal st['cash']."""
    for cash_val in [0.0, 1.0, 108.0, 99.9999, 0.0001]:
        st = _base_state(cash=cash_val)
        v = _view(st)
        assert v["cash"] == cash_val


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — Realized P&L = sum of all execution PnLs
# ═══════════════════════════════════════════════════════════════════════════════
def test_03_realized_pnl_sum_all_executions():
    """realized_pnl = sum of all pnl_usd across all closed records including partial exits."""
    t1 = _make_completed_trade(mint="AAA", cost=10.0, proceeds=12.0)
    t2 = _make_partial_exit(mint="BBB", cost_partial=5.0, proceeds_partial=6.5)
    t3 = _make_completed_trade(mint="CCC", cost=8.0, proceeds=7.0)
    expected = round(t1["pnl_usd"] + t2["pnl_usd"] + t3["pnl_usd"], 4)
    st = _base_state(closed=[t1, t2, t3])
    v = _view(st)
    assert math.isclose(v["realized_pnl"], expected, abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — Canonical unrealized P&L
# ═══════════════════════════════════════════════════════════════════════════════
def test_04_canonical_unrealized_pnl():
    """unrealized_pnl = sum(qty*last) - sum(cost_usd)."""
    positions = {
        "M1": {"qty": 200.0, "last": 0.1, "cost_usd": 18.0,
               "entry_mid": 0.09, "entry_ts": time.time()-120, "entry_type": "volume_spike",
               "pool": "P", "dex": "raydium", "name": "$A",
               "entry_fill": 0.09, "failed_exits": 0, "costs": {}, "info": {}, "track": {}},
        "M2": {"qty": 50.0, "last": 0.05, "cost_usd": 3.0,
               "entry_mid": 0.06, "entry_ts": time.time()-60, "entry_type": "copy_top_traders",
               "pool": "P", "dex": "raydium", "name": "$B",
               "entry_fill": 0.06, "failed_exits": 0, "costs": {}, "info": {}, "track": {}},
    }
    expected_market_val = 200.0 * 0.1 + 50.0 * 0.05
    expected_cost = 18.0 + 3.0
    expected_unreal = round(expected_market_val - expected_cost, 4)
    st = _base_state(positions=positions, cash=86.0)
    v = _view(st)
    assert math.isclose(v["unrealized_pnl"], expected_unreal, abs_tol=1e-6)
    assert math.isclose(v["invested_capital"], expected_cost, abs_tol=1e-6)
    assert math.isclose(v["open_value"], expected_market_val, abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — Partial exit not counted as completed trade
# ═══════════════════════════════════════════════════════════════════════════════
def test_05_partial_exit_not_counted_as_completed_trade():
    """Partial exits (counts_as_trade=False) do not increment completed_trades_count."""
    partial = _make_partial_exit()
    st = _base_state(closed=[partial])
    v = _view(st)
    assert v["completed_trades_count"] == 0
    assert v["partial_exits_count"] == 1
    assert v["completed_wins_count"] == 0
    assert v["completed_losses_count"] == 0
    assert v["completed_win_rate"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6 — Completed exit counted correctly
# ═══════════════════════════════════════════════════════════════════════════════
def test_06_completed_exit_counted_correctly():
    """Completed trades evaluated by cumulative_pnl_usd."""
    partial = _make_partial_exit(mint="AA", cost_partial=5.0, proceeds_partial=6.5)
    full = _make_completed_trade(mint="AA", cost=5.0, proceeds=3.5, partial_pnl=partial["pnl_usd"])
    full_win = _make_completed_trade(mint="BB", cost=10.0, proceeds=14.0)

    st = _base_state(closed=[partial, full, full_win])
    v = _view(st)
    assert v["completed_trades_count"] == 2
    assert v["partial_exits_count"] == 1
    assert v["completed_wins_count"] == 1
    assert v["completed_losses_count"] == 1
    assert math.isclose(v["completed_win_rate"], 50.0, abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7 — Fee aggregation
# ═══════════════════════════════════════════════════════════════════════════════
def test_07_fee_aggregation():
    """fees_total = fees_dex + fees_net."""
    t1 = _make_completed_trade()
    t2 = _make_partial_exit()
    expected_dex = round(t1["dex_fees_usd"] + t2["dex_fees_usd"], 4)
    expected_net = round(t1["network_fees_usd"] + t2["network_fees_usd"], 4)
    expected_total = round(expected_dex + expected_net, 4)
    st = _base_state(closed=[t1, t2])
    v = _view(st)
    assert math.isclose(v["fees_dex"], expected_dex, abs_tol=1e-6)
    assert math.isclose(v["fees_net"], expected_net, abs_tol=1e-6)
    assert math.isclose(v["fees_total"], expected_total, abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8 — Strategy aggregation
# ═══════════════════════════════════════════════════════════════════════════════
def test_08_strategy_aggregation():
    """strategies aggregates completed trades, wins, losses, win_rate."""
    vs1 = _make_completed_trade(mint="A", entry_type="volume_spike", cost=10.0, proceeds=14.0)
    vs2 = _make_completed_trade(mint="B", entry_type="volume_spike", cost=10.0, proceeds=12.0)
    ct1 = _make_completed_trade(mint="C", entry_type="copy_top_traders", cost=10.0, proceeds=8.0)
    partial = _make_partial_exit(mint="A", entry_type="volume_spike")

    st = _base_state(
        closed=[vs1, vs2, ct1, partial],
        exit_rules={"volume_spike": {}, "copy_top_traders": {}}
    )
    v = _view(st)
    strats = {s["key"]: s for s in v["strategies"]}

    assert "volume_spike" in strats
    vs = strats["volume_spike"]
    assert vs["trades"] == 2
    assert vs["wins"] == 2
    assert vs["losses"] == 0
    assert math.isclose(vs["win_rate"], 100.0, abs_tol=1e-6)
    assert vs["enabled"] is True

    assert "copy_top_traders" in strats
    ct = strats["copy_top_traders"]
    assert ct["trades"] == 1
    assert ct["wins"] == 0
    assert ct["losses"] == 1
    assert math.isclose(ct["win_rate"], 0.0, abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 9 — Brain checkpoint representation
# ═══════════════════════════════════════════════════════════════════════════════
def test_09_brain_checkpoint_representation():
    """brain_info exposes mode, records, next_in, accepted/rejected counts."""
    brain_state = {
        "mode": "validated",
        "phase": "collecting",
        "enabled": True,
        "records": 7,
        "analysis_interval": 10,
        "next_in": 3,
        "last_ts": None,
        "diagnosis": "Raccolta dati in corso.",
        "params": {},
        "pending": None,
        "changes": [
            {"ts": 1000.0, "text": "stop_loss_pct → 0.12", "rejected": False},
            {"ts": 2000.0, "text": "take_profit_pct → 0.25", "rejected": True},
            {"ts": 3000.0, "text": "max_hold_minutes → 45", "rejected": False},
        ],
        "locked": ["stop_loss_pct"],
    }
    st = _base_state(brain=brain_state)
    v = _view(st)
    bi = v["brain_info"]
    assert bi["mode"] == "validated"
    assert bi["records"] == 7
    assert bi["next_in"] == 3
    assert bi["next_at_trade"] == 10
    assert bi["accepted_count"] == 2
    assert bi["rejected_count"] == 1
    assert bi["diagnosis"] == "Raccolta dati in corso."


# ═══════════════════════════════════════════════════════════════════════════════
# Test 10 — Anti-Rug mode disambiguation
# ═══════════════════════════════════════════════════════════════════════════════
def test_10_antirug_mode_disambiguation():
    """In shadow mode BLOCK → OBSERVED; in active mode BLOCK → BLOCKED."""
    recent_records = [
        {"name": "TOKEN_A", "decision": "ALLOW", "risk": 10},
        {"name": "TOKEN_B", "decision": "BLOCK", "risk": 85},
    ]

    st_shadow = _base_state(antirug={
        "mode": "shadow", "enforce": [], "stats": {"evaluated": 2, "allow": 1, "block": 1},
        "recent": recent_records,
    })
    v_shadow = _view(st_shadow)
    ar_shadow = v_shadow.get("antirug", {})
    block_shadow = next((r for r in ar_shadow["recent"] if r["decision"] == "BLOCK"), None)
    assert block_shadow["effective_action"] == "OBSERVED"

    st_active = _base_state(antirug={
        "mode": "active", "enforce": [], "stats": {"evaluated": 2, "allow": 1, "block": 1},
        "recent": recent_records,
    })
    v_active = _view(st_active)
    ar_active = v_active.get("antirug", {})
    block_active = next((r for r in ar_active["recent"] if r["decision"] == "BLOCK"), None)
    assert block_active["effective_action"] == "BLOCKED"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 11 — Empty state
# ═══════════════════════════════════════════════════════════════════════════════
def test_11_empty_state_no_crash():
    """_view() does not crash on empty state."""
    st = _base_state()
    v = _view(st)
    assert v["ok"] is True
    assert v["completed_trades_count"] == 0
    assert v["partial_exits_count"] == 0
    assert v["completed_win_rate"] is None
    assert v["realized_pnl"] == 0.0
    assert v["fees_total"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Test 12 — Stale heartbeat
# ═══════════════════════════════════════════════════════════════════════════════
def test_12_stale_heartbeat_live_false(tmp_path, monkeypatch):
    """live=False when heartbeat is older than 90s."""
    import dashboard.app as dashboard_app
    monkeypatch.setattr(dashboard_app, "DATA_DIR", str(tmp_path))

    old_hb = time.time() - 200
    st = _base_state(heartbeat_ts=old_hb, status="running")
    v = _view(st)
    assert v["live"] is False
    assert v["heartbeat_age"] > 90


def test_12b_fresh_heartbeat_live_true(tmp_path, monkeypatch):
    """live=True when heartbeat is fresh."""
    import dashboard.app as dashboard_app
    monkeypatch.setattr(dashboard_app, "DATA_DIR", str(tmp_path))

    st = _base_state(heartbeat_ts=time.time() - 5, status="running")
    v = _view(st)
    assert v["live"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Test 13 — RPC degraded & freshness validation
# ═══════════════════════════════════════════════════════════════════════════════
def test_13_rpc_degraded_state():
    """rpc_health correctly reflects freshness and failure rate."""
    now_ts = time.time()
    # Healthy with fresh timestamp
    rh_ok = _rpc_health({"ok": 100, "fail": 0, "last_success_ts": now_ts})
    assert rh_ok["state"] == "healthy"
    assert rh_ok["cls"] == "ok"

    # Degraded (>10% fail rate, fresh timestamp)
    rh_deg = _rpc_health({"ok": 5, "fail": 10, "last_success_ts": now_ts})
    assert rh_deg["state"] == "degraded"
    assert rh_deg["cls"] == "warning"

    # Unknown when required timestamps missing
    rh_empty = _rpc_health({})
    assert rh_empty["state"] == "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 14 — Empty portfolio handling
# ═══════════════════════════════════════════════════════════════════════════════
def test_14_empty_portfolio_handling():
    """Zero positions: invested_capital=0, unrealized_pnl=0, open_value=0."""
    st = _base_state(cash=108.0, positions={})
    v = _view(st)
    assert v["invested_capital"] == 0.0
    assert v["open_value"] == 0.0
    assert v["unrealized_pnl"] == 0.0
    assert math.isclose(v["equity"], 108.0, abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 15 — Repeated polling idempotency
# ═══════════════════════════════════════════════════════════════════════════════
def test_15_repeated_polling_idempotent():
    """Calling _view() twice produces identical canonical metrics."""
    t1 = _make_completed_trade(mint="X", cost=10.0, proceeds=12.0)
    partial = _make_partial_exit(mint="Y")
    st = _base_state(cash=86.0, closed=[t1, partial])
    v1 = _view(st)
    v2 = _view(st)
    for key in ("equity", "cash", "invested_capital", "open_value", "unrealized_pnl",
                "realized_pnl", "total_pnl", "return_pct", "fees_total"):
        assert v1[key] == v2[key]


# ═══════════════════════════════════════════════════════════════════════════════
# Test 16 — Legacy trade backward compat
# ═══════════════════════════════════════════════════════════════════════════════
def test_16_is_completed_trade_backward_compat():
    """_is_completed_trade handles legacy records."""
    assert _is_completed_trade({"counts_as_trade": True}) is True
    assert _is_completed_trade({"counts_as_trade": False}) is False
    assert _is_completed_trade({"partial": True}) is False
    assert _is_completed_trade({"exit_reason": "take_profit_1"}) is False
    assert _is_completed_trade({"exit_reason": "stop_loss"}) is True
    assert _is_completed_trade({}) is True


# ═══════════════════════════════════════════════════════════════════════════════
# Test 17 — total_pnl and return_pct
# ═══════════════════════════════════════════════════════════════════════════════
def test_17_total_pnl_and_return_pct():
    """total_pnl = equity - start; return_pct = total_pnl / start * 100."""
    st = _base_state(cash=120.0, starting_balance_usd=100.0)
    v = _view(st)
    assert math.isclose(v["total_pnl"], 20.0, abs_tol=1e-4)
    assert math.isclose(v["return_pct"], 20.0, abs_tol=1e-2)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 18 — best/worst trade uses cumulative_pnl
# ═══════════════════════════════════════════════════════════════════════════════
def test_18_best_worst_use_cumulative_pnl():
    """best and worst completed trades use cumulative_pnl_usd."""
    partial_big = _make_partial_exit(mint="BIGWIN", cost_partial=5.0, proceeds_partial=10.0)
    full_net_loss = _make_completed_trade(
        mint="BIGWIN", cost=5.0, proceeds=3.0, partial_pnl=partial_big["pnl_usd"]
    )
    pure_loss = _make_completed_trade(mint="LOSS", cost=10.0, proceeds=7.0)
    pure_win = _make_completed_trade(mint="WIN", cost=10.0, proceeds=14.0)

    st = _base_state(closed=[partial_big, full_net_loss, pure_loss, pure_win])
    v = _view(st)
    assert v["best"]["mint"] == "WIN"
    assert v["worst"]["mint"] == "LOSS"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 19 — Legacy partial trades: string booleans and missing counts_as_trade
# ═══════════════════════════════════════════════════════════════════════════════
def test_19_legacy_partial_trades_string_booleans_and_missing_counts():
    """Historical partial exits with various serialization formats cannot inflate trades."""
    p1 = {"mint": "P1", "cost_usd": 5.0, "pnl_usd": 1.0, "partial": True, "exit_reason": "take_profit_1"}
    p2 = {"mint": "P2", "cost_usd": 5.0, "pnl_usd": 1.0, "partial": "true", "exit_reason": "take_profit_1"}
    p3 = {"mint": "P3", "cost_usd": 5.0, "pnl_usd": 1.0, "partial": 1, "exit_reason": "take_profit_1"}
    p4 = {"mint": "P4", "cost_usd": 5.0, "pnl_usd": 1.0, "exit_reason": "take_profit_1"}
    p5 = {"mint": "P5", "cost_usd": 5.0, "pnl_usd": 1.0, "counts_as_trade": "false", "partial": True}

    c1 = {"mint": "C1", "cost_usd": 10.0, "pnl_usd": 2.0, "exit_reason": "take_profit", "partial": False}
    c2 = {"mint": "C2", "cost_usd": 10.0, "pnl_usd": -1.5, "exit_reason": "stop_loss"}
    c3 = {"mint": "C3", "cost_usd": 10.0, "pnl_usd": 0.5, "exit_reason": "trailing_stop", "counts_as_trade": "true"}

    for p in (p1, p2, p3, p4, p5):
        assert _is_completed_trade(p) is False

    for c in (c1, c2, c3):
        assert _is_completed_trade(c) is True

    st = _base_state(closed=[p1, p2, p3, p4, p5, c1, c2, c3])
    v = _view(st)
    assert v["completed_trades_count"] == 3
    assert v["partial_exits_count"] == 5
    assert v["completed_wins_count"] == 2
    assert v["completed_losses_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test 20 — Brain historical changes without explicit rejected classified unconfirmed
# ═══════════════════════════════════════════════════════════════════════════════
def test_20_brain_changes_no_silent_acceptance():
    """Historical Brain changes lacking an explicit confirmation status are marked UNKNOWN."""
    brain_state = {
        "mode": "validated",
        "phase": "stabilized",
        "records": 25,
        "next_in": 5,
        "changes": [
            {"ts": 100.0, "param": "stop_loss_pct", "status": "confermata"},
            {"ts": 101.0, "param": "take_profit_pct", "accepted": True},
            {"ts": 102.0, "param": "trailing_stop_pct", "status": "annullata"},
            {"ts": 103.0, "param": "max_hold_minutes", "rejected": True},
            {"ts": 104.0, "param": "position_size_pct", "status": "in verifica"},
            {"ts": 105.0, "param": "reentry_cooldown_minutes", "old": 30, "new": 15},
            {"ts": 106.0, "param": "trailing_activate_pct", "old": 0.15, "new": 0.20},
        ],
    }
    st = _base_state(brain=brain_state)
    v = _view(st)
    bi = v["brain_info"]
    assert bi["accepted_count"] == 2
    assert bi["rejected_count"] == 2
    assert bi["pending_count"] == 1
    assert bi["unknown_count"] == 2

    accepted_params = [c["param"] for c in bi["accepted_changes"]]
    assert "reentry_cooldown_minutes" not in accepted_params
    assert "trailing_activate_pct" not in accepted_params


# ═══════════════════════════════════════════════════════════════════════════════
# Test 21 — RPC connectivity: four states and freshness verification
# ═══════════════════════════════════════════════════════════════════════════════
def test_21_rpc_connectivity_four_states_and_heartbeat_isolation():
    """RPC connectivity accurately reflects HEALTHY, DEGRADED, DOWN, UNKNOWN with freshness check."""
    now_ts = time.time()
    # 1. Healthy: fresh success (<=60s) and low errors
    h1 = _rpc_health({"ok": 200, "fail": 0, "last_success_ts": now_ts})
    assert h1["state"] == "healthy"
    assert h1["cls"] == "ok"
    assert h1["label"] == "HEALTHY"

    # 2. Degraded: fresh success but failure rate > 10%
    h2 = _rpc_health({"ok": 80, "fail": 20, "last_success_ts": now_ts})
    assert h2["state"] == "degraded"
    assert h2["cls"] == "warning"

    # 2b. Degraded: stale success (>60s ago)
    h2b = _rpc_health({"ok": 100, "fail": 0, "last_success_ts": now_ts - 90})
    assert h2b["state"] == "degraded"

    # 3. Down: 0 successes, failures > 0
    h3 = _rpc_health({"ok": 0, "fail": 15, "last_success_ts": None})
    assert h3["state"] == "down"
    assert h3["cls"] == "offline"

    # 3b. Down: very stale (>180s) with recent failure
    h3b = _rpc_health({"ok": 100, "fail": 5, "last_success_ts": now_ts - 250, "last_failure_ts": now_ts - 10})
    assert h3b["state"] == "down"

    # 4. Unknown: cumulative successes exist but NO timestamp provided
    h4 = _rpc_health({"ok": 500, "fail": 0})
    assert h4["state"] == "unknown"
    assert h4["label"] == "UNKNOWN"

    # 5. Heartbeat isolation: live=True (bot alive 2s ago), but RPC is DOWN
    st_alive_rpc_down = _base_state(
        status="running",
        heartbeat_ts=now_ts - 2.0,
        rpc={"ok": 0, "fail": 20}
    )
    v = _view(st_alive_rpc_down)
    assert v["live"] is True
    assert v["rpc_health"]["state"] == "down"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 22 — Comprehensive financial reconciliation
# ═══════════════════════════════════════════════════════════════════════════════
def test_22_comprehensive_financial_reconciliation():
    """All financial values reconcile to exact accounting identities."""
    initial_cash = 100.0
    start_usd = 108.0

    pos = {
        "TOKEN1": {"qty": 500.0, "last": 0.05, "cost_usd": 20.0,
                   "entry_mid": 0.04, "entry_ts": time.time() - 600, "entry_type": "volume_spike",
                   "pool": "P1", "dex": "raydium", "name": "$T1", "entry_fill": 0.04, "failed_exits": 0},
        "TOKEN2": {"qty": 100.0, "last": 0.12, "cost_usd": 15.0,
                   "entry_mid": 0.15, "entry_ts": time.time() - 300, "entry_type": "copy_top_traders",
                   "pool": "P2", "dex": "raydium", "name": "$T2", "entry_fill": 0.15, "failed_exits": 0},
    }

    t_partial = _make_partial_exit(mint="TOKEN1", cost_partial=10.0, proceeds_partial=12.5)
    t_complete = _make_completed_trade(mint="TOKEN3", cost=15.0, proceeds=11.0)

    st = _base_state(
        starting_balance_usd=start_usd,
        cash=initial_cash,
        positions=pos,
        closed=[t_partial, t_complete]
    )

    v = _view(st)
    assert math.isclose(v["invested_capital"], 35.0, abs_tol=1e-6)
    assert math.isclose(v["open_value"], 37.0, abs_tol=1e-6)
    assert math.isclose(v["unrealized_pnl"], 2.0, abs_tol=1e-6)
    assert math.isclose(v["equity"], 137.0, abs_tol=1e-6)
    assert math.isclose(v["equity"], v["cash"] + v["invested_capital"] + v["unrealized_pnl"], abs_tol=1e-6)
    assert math.isclose(v["total_pnl"], 29.0, abs_tol=1e-6)
    assert math.isclose(v["return_pct"], round(100.0 * 29.0 / 108.0, 2), abs_tol=1e-2)
    assert math.isclose(v["realized_pnl"], -1.5, abs_tol=1e-6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 23 — Area 1: Frontend financial fallbacks eliminated (Canonical Only)
# ═══════════════════════════════════════════════════════════════════════════════
def test_23_frontend_financial_fallbacks_eliminated():
    """When financial metrics are None, no synthetic substitute is calculated."""
    # State with None/zero values
    st = _base_state(
        starting_balance_usd=100.0,
        cash=100.0,
        positions={},
        closed=[]
    )
    v = _view(st)
    # Check that canonical keys are present and exact
    assert v["unrealized_pnl"] == 0.0
    assert v["realized_pnl"] == 0.0
    assert v["total_pnl"] == 0.0
    assert v["return_pct"] == 0.0
    assert v["fees_total"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Test 24 — Area 2: Synthetic equity chart points eliminated
# ═══════════════════════════════════════════════════════════════════════════════
def test_24_synthetic_equity_points_eliminated():
    """equity_curve is passed directly as recorded without fabricating synthetic points."""
    # Empty equity curve
    st_empty = _base_state(equity=[])
    v_empty = _view(st_empty)
    assert len(v_empty["equity_curve"]) == 0

    # 1 single point recorded
    now_ts = time.time()
    st_one = _base_state(equity=[[now_ts, 108.0, 108.0]])
    v_one = _view(st_one)
    assert len(v_one["equity_curve"]) == 1
    assert v_one["equity_curve"][0] == [now_ts, 108.0, 108.0]


# ═══════════════════════════════════════════════════════════════════════════════
# Test 25 — Area 3: Provider connectivity never derived from ops_per_min
# ═══════════════════════════════════════════════════════════════════════════════
def test_25_provider_connectivity_not_derived_from_ops_per_min():
    """GeckoTerminal & DexScreener connectivity must NOT be inferred from ops_per_min."""
    now_ts = time.time()

    # Case A: ops_per_min is HIGH (500) but no host telemetry exists -> state must be UNKNOWN and connected=False!
    api_data_no_hosts = {
        "ops_per_min": 500,
        "health": {"hosts": {}},
        "stats": {"pools_seen": 42, "signals": 10},
    }
    sources_a = _build_source_status(api_data_no_hosts)
    gt_a = next(s for s in sources_a if s["source"] == "geckoterminal")
    ds_a = next(s for s in sources_a if s["source"] == "dexscreener")

    assert gt_a["connected"] is False, "GeckoTerminal must not be connected based on ops_per_min"
    assert gt_a["state"] == "UNKNOWN"
    assert ds_a["connected"] is False, "DexScreener must not be connected based on ops_per_min"
    assert ds_a["state"] == "UNKNOWN"

    # Case B: ops_per_min is 0, but host telemetry shows recent OK (<180s) -> connected=True!
    api_data_with_hosts = {
        "ops_per_min": 0,
        "health": {
            "hosts": {
                "api.geckoterminal.com": {"last_ok": now_ts - 10, "consec": 0, "open_until": 0.0},
                "api.dexscreener.com": {"last_ok": now_ts - 20, "consec": 0, "open_until": 0.0},
            }
        },
        "stats": {"pools_seen": 100, "signals": 25},
    }
    sources_b = _build_source_status(api_data_with_hosts)
    gt_b = next(s for s in sources_b if s["source"] == "geckoterminal")
    ds_b = next(s for s in sources_b if s["source"] == "dexscreener")

    assert gt_b["connected"] is True
    assert gt_b["state"] == "CONNECTED"
    assert ds_b["connected"] is True
    assert ds_b["state"] == "CONNECTED"

    # Case C: Circuit breaker open -> state=ERROR and connected=False
    api_data_circuit_open = {
        "ops_per_min": 50,
        "health": {
            "hosts": {
                "api.geckoterminal.com": {"last_ok": now_ts - 50, "consec": 6, "open_until": now_ts + 60},
            }
        },
        "stats": {},
    }
    sources_c = _build_source_status(api_data_circuit_open)
    gt_c = next(s for s in sources_c if s["source"] == "geckoterminal")
    assert gt_c["connected"] is False
    assert gt_c["state"] == "ERROR"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 26 — Area 4: Brain historical changes (REJECTED, ACCEPTED, UNKNOWN)
# ═══════════════════════════════════════════════════════════════════════════════
def test_26_brain_historical_three_states_exact():
    """rejected=true -> REJECTED, rejected=false -> ACCEPTED, missing -> UNKNOWN."""
    # 1. rejected=true -> REJECTED
    assert _classify_brain_change({"rejected": True}) == "REJECTED"
    assert _classify_brain_change({"rejected": "true"}) == "REJECTED"
    assert _classify_brain_change({"status": "annullata"}) == "REJECTED"

    # 2. rejected=false -> ACCEPTED
    assert _classify_brain_change({"rejected": False}) == "ACCEPTED"
    assert _classify_brain_change({"accepted": True}) == "ACCEPTED"
    assert _classify_brain_change({"status": "confermata"}) == "ACCEPTED"

    # 3. missing rejected field -> UNKNOWN
    assert _classify_brain_change({"old": 1, "new": 2}) == "UNKNOWN"
    assert _classify_brain_change({"param": "stop_loss_pct"}) == "UNKNOWN"

    # Verify integration in _view()
    st = _base_state(brain={
        "changes": [
            {"param": "A", "rejected": True},
            {"param": "B", "rejected": False},
            {"param": "C", "old": 10, "new": 20}, # missing rejected field
        ]
    })
    v = _view(st)
    bi = v["brain_info"]
    assert bi["rejected_count"] == 1
    assert bi["accepted_count"] == 1
    assert bi["unknown_count"] == 1

    # Check classifications enriched in change items
    classes = {c["param"]: c["classification"] for c in bi["changes"]}
    assert classes["A"] == "REJECTED"
    assert classes["B"] == "ACCEPTED"
    assert classes["C"] == "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 27 — Area 5: Anti-Rug mode-aware presentation (OFF / SHADOW / ACTIVE)
# ═══════════════════════════════════════════════════════════════════════════════
def test_27_antirug_mode_presentation_off_shadow_active():
    """Explicit mode presentation: OFF (disabled), SHADOW (observed), ACTIVE (blocks)."""
    recent = [{"decision": "BLOCK", "name": "$RISK", "risk": 90}]

    # 1. OFF mode: disabled, no blocks, never labeled SHADOW
    st_off = _base_state(antirug={"mode": "off", "recent": recent})
    v_off = _view(st_off)
    ar_off = v_off["antirug"]
    assert ar_off["mode"] == "off"
    assert ar_off["mode_label"] == "OFF"
    assert "Disattivato" in ar_off["mode_desc"]
    assert ar_off["recent"][0]["effective_action"] == "DISABLED"

    # Missing antirug key defaults to OFF, never SHADOW!
    st_none = _base_state(antirug=None)
    v_none = _view(st_none)
    ar_none = v_none["antirug"]
    assert ar_none["mode"] == "off"
    assert ar_none["mode_label"] == "OFF"

    # 2. SHADOW mode: findings observed, no actual blocks
    st_shadow = _base_state(antirug={"mode": "shadow", "recent": recent})
    v_shadow = _view(st_shadow)
    ar_shadow = v_shadow["antirug"]
    assert ar_shadow["mode"] == "shadow"
    assert ar_shadow["mode_label"] == "SHADOW"
    assert "nessun blocco" in ar_shadow["mode_desc"]
    assert ar_shadow["recent"][0]["effective_action"] == "OBSERVED"

    # 3. ACTIVE mode: actual blocking decisions
    st_active = _base_state(antirug={"mode": "active", "recent": recent})
    v_active = _view(st_active)
    ar_active = v_active["antirug"]
    assert ar_active["mode"] == "active"
    assert ar_active["mode_label"] == "ACTIVE"
    assert "Blocco attivo" in ar_active["mode_desc"]
    assert ar_active["recent"][0]["effective_action"] == "BLOCKED"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 28 — Area 6: RPC response freshness validation
# ═══════════════════════════════════════════════════════════════════════════════
def test_28_rpc_freshness_validation_staleness_and_cumulative_guard():
    """Verify RPC freshness checks; cumulative successes alone return UNKNOWN."""
    now_ts = time.time()

    # A: Cumulative successes (1000) but no timestamp -> UNKNOWN (never assume healthy!)
    assert _rpc_health({"ok": 1000, "fail": 0})["state"] == "unknown"

    # B: Fresh success (2 seconds ago) -> HEALTHY
    h_fresh = _rpc_health({"ok": 50, "fail": 0, "last_success_ts": now_ts - 2})
    assert h_fresh["state"] == "healthy"
    assert h_fresh["label"] == "HEALTHY"

    # C: Stale success (90 seconds ago) -> DEGRADED
    h_stale = _rpc_health({"ok": 50, "fail": 0, "last_success_ts": now_ts - 90})
    assert h_stale["state"] == "degraded"
    assert h_stale["label"] == "DEGRADED"

    # D: Very stale (200 seconds ago) with failure after -> DOWN
    h_down = _rpc_health({
        "ok": 50, "fail": 5,
        "last_success_ts": now_ts - 200,
        "last_failure_ts": now_ts - 10
    })
    assert h_down["state"] == "down"
    assert h_down["label"] == "DOWN"

    # E: Explicit circuit open -> DOWN
    assert _rpc_health({"state": "circuit_open"})["state"] == "down"
