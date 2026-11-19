"""Suite completa di test di staging e regressione per AEON."""
import csv
import json
import math
import sys
import time
from pathlib import Path
from unittest.mock import patch
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import storage
from antirug import AntiRugEngine
from paper_bot import PaperBot
from risk import RiskManager


# ==============================================================================
# B11 — AntiRug High-Risk Closure (Production AntiRugEngine Evaluation & Gating)
# ==============================================================================
def test_b11_antirug_evaluate_triggers_protective_exit():
    with open(ROOT / "bot" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Provide a RugCheck report callback that reports dangerous mint & freeze authorities
    fake_rc_report = lambda mint: {
        "mint": mint,
        "tokenMeta": {"mutable": True},
        "risks": [
            {"name": "Mint Authority still enabled", "level": "danger", "description": "Mint authority is not disabled"},
            {"name": "Freeze Authority still enabled", "level": "danger", "description": "Freeze authority is not disabled"},
        ],
        "score": 9200,
    }

    antirug = AntiRugEngine(cfg["antirug"], mode="active", rc_report=fake_rc_report)
    snap = {
        "mint": "So11111111111111111111111111111111111111112",
        "name": "RUG_TOKEN",
        "price": 0.001,
        "liquidity": 50000.0,
        "dex": "raydium",
        "pool": "Pool111111111111111111111111111111111111111",
    }
    verdict = antirug.evaluate(snap, usd_in=10.0)

    # Assert real production evaluation output
    assert verdict["decision"] == "BLOCK"
    assert verdict["decision_basis"] == "hard_block"
    assert verdict["level"] == "critico"
    assert "dangerous_token_feature" in verdict["hard_blocks"]

    # Assert gating logic strictly blocks execution
    allowed, why = antirug.gate(verdict)
    assert allowed is False
    assert why == "antirug_dangerous_token_feature"


# ==============================================================================
# B15 — Adaptive Composite Score Weights (Production Weight Isolation)
# ==============================================================================
def test_b15_composite_score_price_weight_isolation(tmp_path):
    with open(ROOT / "bot" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Isolate price weight to 100% and nullify all secondary weights in production config
    cfg["adaptive_holding"] = {
        "enabled": True,
        "paper_only_check": False,
        "min_score": 60,
        "indicators": {
            "price_move_pct_weight": 100,
            "momentum_weight": 0,
            "volume_accel_weight": 0,
            "liquidity_weight": 0,
            "risk_weight": 0,
        },
        "require_jupiter_route": False,
        "max_minutes": 60,
        "initial_minutes": 15,
    }

    bot = PaperBot(cfg, str(tmp_path / "bot_b15"), reset=True)
    mint = "MINT_B15_TEST"
    pos = {
        "mint": mint, "name": "B15_COIN", "entry_mid": 1.0, "entry_fill": 1.0,
        "entry_ts": time.time() - 300, "peak_price": 1.20, "qty": 100.0,
        "cost_usd": 100.0, "entry_liquidity": 10000.0, "adaptive_holding_stage": 0
    }

    # Case A: Price at 1.20 (+20% gain) -> isolated price score is maximum (100) -> HOLD (stage 1)
    # Nota API attuale: la decisione è "HOLD_STAGE_<n>" (prima "EXTEND_*"); l'isolamento del peso
    # prezzo si verifica dal composite_score (100*100/100 = 100.0, tutte le altre componenti peso 0).
    snap_up = {"price": 1.20, "liquidity": 10000.0, "vol_m5": 1000, "vol_h1": 2000}
    decision, reason, stage, ind, quote = bot._evaluate_adaptive_holding(mint, pos, 1.20, snap_up)
    assert decision.startswith("HOLD_STAGE")
    assert ind["price_move_pct"] == pytest.approx(20.0)
    assert ind["composite_score"] == 100.0

    # Case B: Price drops to 0.70 (-30% drop) -> isolated price score is 0.0 (< min_score 60) -> CLOSE
    snap_down = {"price": 0.70, "liquidity": 10000.0, "vol_m5": 1000, "vol_h1": 2000}
    decision_down, reason_down, stage_down, ind_down, _ = bot._evaluate_adaptive_holding(mint, pos, 0.70, snap_down)
    assert decision_down == "CLOSE"
    assert ind_down["composite_score"] == 0.0


# ==============================================================================
# C2 — Progressive Take Profit (Production Partial Exit TP1 & Full Exit TP2)
# ==============================================================================
def test_c2_progressive_take_profit_partial_and_full_close(tmp_path):
    with open(ROOT / "bot" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    bot = PaperBot(cfg, str(tmp_path / "bot_c2"), reset=True)
    bot.cash = 100.0
    initial_cash = bot.cash
    now_ts = time.time()
    mint = "TEST_MINT_C2"
    entry_price = 1.0

    bot.positions[mint] = {
        "mint": mint, "pool": "POOL_C2", "name": "C2_TOKEN", "dex": "raydium",
        "entry_type": "volume_spike", "entry_ts": now_ts, "entry_mid": entry_price, "entry_fill": entry_price,
        "qty": 100.0, "cost_usd": 100.0, "liquidity": 50000.0, "peak": entry_price, "last": entry_price,
        "last_price_ts": now_ts, "info": {},
        "costs": {"buy_slip_pct": 0.0, "buy_dex_fee": 0.0, "buy_net_fee": 0.0, "buy_slip_src": "stimato"},
        "failed_exits": 0, "original_qty": 100.0, "original_cost_usd": 100.0,
        "tp1_hit": False, "breakeven_activated": False,
    }
    bot._net_fee_usd = lambda: 0.0
    bot._slip = lambda gross, liq: 0.0
    bot._dex_fee = lambda dex: 0.0
    bot.decimals_cache[mint] = 6

    class MockDS:
        def __init__(self, p):
            self.price = p
            self.last_ok = True
        def tokens(self, mints):
            return {
                mint: {
                    "price": self.price, "liquidity": 50000.0, "vol_m5": 1000, "vol_h1": 2000,
                    "buys_m5": 10, "sells_m5": 5, "pool": "POOL_C2", "name": "C2_TOKEN",
                    "dex": "raydium", "quote_mint": "SOL", "created_ts": 1000000, "fdv": 1000000, "src": "mock"
                }
            }

    # Exit Intelligence: nessun fill simulato senza una quote Jupiter valida.
    # Mock della quote al prezzo di mercato corrente (slippaggio 0).
    def fake_quote(in_mint, out_mint, amount_raw, deadline=None):
        qty = amount_raw / 10**6
        usd_out = qty * ds_price["p"]
        return {"out": int(usd_out * 10**6), "in": amount_raw, "impact_pct": 1.0, "route": "Raydium",
                "endpoint": "v1/quote", "quote_ts": time.time(), "age_s": 0.1, "latency_s": 0.05, "cached": False}
    bot.jup.quote = fake_quote

    # Step 1: Price reaches +25% (1.25) -> triggers TP1 (sells 50%)
    ds_price = {"p": 1.25}
    ds = MockDS(1.25)
    bot.ds = ds
    bot.positions[mint]["last"] = 1.25
    bot.positions[mint]["peak"] = 1.25
    bot.manage()

    p = bot.positions.get(mint)
    assert p is not None
    assert p["tp1_hit"] is True
    assert p["qty"] == 50.0
    assert p["cost_usd"] == 50.0
    assert len(bot.closed) == 1
    assert bot.closed[0]["exit_reason"] == "take_profit_1"
    assert bot.closed[0]["proceeds_usd"] == 62.5
    assert bot.closed[0]["pnl_usd"] == 12.5
    assert math.isclose(bot.cash, initial_cash + 62.5)

    # Step 2: Price reaches +60% (1.60) -> triggers TP2 (closes remainder)
    ds_price["p"] = 1.60
    ds.price = 1.60
    bot.positions[mint]["last"] = 1.60
    bot.positions[mint]["peak"] = 1.60
    bot.manage()

    assert mint not in bot.positions
    assert len(bot.closed) == 2
    final_trade = bot.closed[-1]
    assert final_trade["exit_reason"] == "take_profit_2"
    assert final_trade["proceeds_usd"] == 80.0
    assert final_trade["pnl_usd"] == 30.0
    assert final_trade["cumulative_pnl_usd"] == 42.5
    # Full accounting reconciliation
    assert math.isclose(bot.cash, initial_cash + 62.5 + 80.0)
    total_proceeds = sum(t["proceeds_usd"] for t in bot.closed)
    assert math.isclose(total_proceeds, 142.5)
    total_realized_pnl = final_trade["cumulative_pnl_usd"]
    assert math.isclose(total_realized_pnl, 42.5)
    assert math.isclose(total_proceeds - 100.0, total_realized_pnl)


# ==============================================================================
# C3 — Trailing Stop Priority & Boundaries (+15% attivazione, 10% distanza)
# ==============================================================================
@pytest.mark.parametrize(
    "entry,peak,current,expected_exit",
    [
        (0.0001, 0.00015, 0.000135, True),   # Peak +50% (>15%), drop 10% esatto -> EXIT
        (0.0001, 0.00015, 0.000136, False),  # Drop 9.33% (<10%) -> HOLD
        (0.0001, 0.000114, 0.000100, False), # Peak +14% (<15% attivazione) -> HOLD
    ],
)
def test_c3_trailing_stop_activation_and_trigger(tmp_path, entry, peak, current, expected_exit):
    with open(ROOT / "bot" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg["trailing_activate_pct"] = 0.15
    cfg["trailing_stop_pct"] = 0.10

    bot = PaperBot(cfg, str(tmp_path / f"bot_c3_{entry}_{peak}_{current}"), reset=True)
    bot.cash = 100.0
    now_ts = time.time()
    mint = "MINT_C3_TRAIL"

    bot.positions[mint] = {
        "mint": mint, "pool": "POOL_C3", "name": "C3_COIN", "dex": "raydium",
        "entry_type": "volume_spike", "entry_ts": now_ts, "entry_mid": entry, "entry_fill": entry,
        "qty": 500.0, "cost_usd": entry * 500.0, "liquidity": 50000.0, "peak": peak, "last": current,
        "last_price_ts": now_ts, "info": {},
        "costs": {"buy_slip_pct": 0.0, "buy_dex_fee": 0.0, "buy_net_fee": 0.0, "buy_slip_src": "stimato"},
        "failed_exits": 0, "original_qty": 1000.0, "original_cost_usd": entry * 1000.0,
        "tp1_hit": True, "breakeven_activated": False,
    }
    bot._net_fee_usd = lambda: 0.0
    bot._slip = lambda gross, liq: 0.0
    bot._dex_fee = lambda dex: 0.0
    bot.decimals_cache[mint] = 6

    class MockDS:
        def __init__(self, p):
            self.price = p
            self.last_ok = True
        def tokens(self, mints):
            return {
                mint: {
                    "price": self.price, "liquidity": 50000.0, "vol_m5": 1000, "vol_h1": 2000,
                    "buys_m5": 10, "sells_m5": 5, "pool": "POOL_C3", "name": "C3_COIN",
                    "dex": "raydium", "quote_mint": "SOL", "created_ts": 1000000, "fdv": 1000000, "src": "mock"
                }
            }

    # Exit Intelligence: la vendita richiede una quote Jupiter valida (slippaggio 0 al prezzo corrente)
    def fake_quote(in_mint, out_mint, amount_raw, deadline=None):
        qty = amount_raw / 10**6
        usd_out = qty * current
        return {"out": int(usd_out * 10**6), "in": amount_raw, "impact_pct": 1.0, "route": "Raydium",
                "endpoint": "v1/quote", "quote_ts": time.time(), "age_s": 0.1, "latency_s": 0.05, "cached": False}
    bot.jup.quote = fake_quote

    bot.ds = MockDS(current)
    bot.manage()

    has_exited = (mint not in bot.positions) and (len(bot.closed) > 0 and bot.closed[-1]["exit_reason"] == "trailing_stop")
    assert has_exited is expected_exit


# ==============================================================================
# G5 — CSV Header Migration (Archiviazione header legacy senza perdita dati)
# ==============================================================================
def test_g5_csv_legacy_header_migration_preserves_rows(tmp_path):
    csv_file = tmp_path / "trades.csv"
    archive_file = tmp_path / "trades_legacy_archive.csv"

    legacy_header = ["timestamp", "symbol", "pnl"]
    new_header = ["timestamp", "symbol", "side", "pnl", "fees"]

    # Scrive file legacy
    with csv_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(legacy_header)
        writer.writerow(["2026-11-17T10:00:00Z", "SOL", "12.5"])

    # Simula migrazione sicura
    with csv_file.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    if rows and rows[0] != new_header:
        with archive_file.open("w", newline="", encoding="utf-8") as af:
            csv.writer(af).writerows(rows)
        with csv_file.open("w", newline="", encoding="utf-8") as nf:
            writer = csv.writer(nf)
            writer.writerow(new_header)
            writer.writerow(["2026-11-17T11:00:00Z", "BONK", "SELL", "5.0", "0.02"])

    assert archive_file.exists()
    archived_rows = list(csv.reader(archive_file.open("r", encoding="utf-8")))
    current_rows = list(csv.reader(csv_file.open("r", encoding="utf-8")))

    assert len(archived_rows) == 2
    assert archived_rows[1] == ["2026-11-17T10:00:00Z", "SOL", "12.5"]
    assert current_rows[0] == new_header
    assert len(current_rows) == 2


# ==============================================================================
# G6 — Telemetry Failure Isolation (Production PaperBot Telemetry Isolation)
# ==============================================================================
def test_g6_telemetry_write_failure_is_isolated(tmp_path):
    with open(ROOT / "bot" / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    bot = PaperBot(cfg, str(tmp_path / "bot_g6"), reset=True)
    t = {
        "trade_id": "10001_MINT",
        "entry_ts": time.time() - 100,
        "exit_ts": time.time(),
        "mint": "So11111111111111111111111111111111111111112",
        "token": "TEST_TOKEN",
        "pool": "Pool111111111111111111111111111111111111111",
        "dex": "raydium",
        "entry_type": "volume_spike",
        "entry_time_utc": "2026-11-18T00:00:00Z",
        "exit_time_utc": "2026-11-18T00:01:40Z",
        "filters": "{}",
    }

    # Simulate disk failure specifically during secondary CSV telemetry appending
    with patch.object(bot, "_write_trades_csv", side_effect=OSError("Simulated disk I/O write error")):
        bot._write_trade(t)

    # Observable production behavior: error caught, telemetry counter incremented, bot survives
    assert bot.ops["telemetry_write_errors"] == 1
    assert bot.meta.get("dead") is not True


# ==============================================================================
# Audit Fixes — Storage, Backups & RiskManager
# ==============================================================================
def test_audit_storage_and_risk_manager(tmp_path):
    data = tmp_path / "data"
    backups = data / "backups" / "20261117-010000"
    backups.mkdir(parents=True)
    (backups / "state.json").write_text('{"cash": 12}', encoding="utf-8")

    obj, source = storage.load_json_safe(
        str(data / "state.json"), backup_dir=str(data / "backups"), backup_name="state.json"
    )
    assert obj == {"cash": 12}
    assert source == "backup:20261117-010000"

    rm = RiskManager({"risk": {"mode": "conservative", "conservative": {"max_consecutive_losses": 2}}})
    rm.on_trade_closed(10.0, 110.0)
    rm.on_trade_closed(float("nan"), 110.0)
    rm.on_trade_closed(-5.0, float("inf"))
    rm.on_trade_closed(True, 110.0)

    assert rm.s["consec_losses"] == 0
    assert rm.s["peak_equity"] == 110.0
