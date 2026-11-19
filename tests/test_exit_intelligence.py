import os
import time
import pytest
import yaml
from paper_bot import PaperBot


@pytest.fixture
def bot(tmp_path):
    cfg_path = "bot/config.yaml" if os.path.exists("bot/config.yaml") else "config.yaml"
    cfg = yaml.safe_load(open(cfg_path))
    cfg["telegram_enabled"] = False
    cfg["adaptive_holding"]["enabled"] = True
    # Isolamento: partial TP (Priority 2 in manage(), tp1 +25%) venderebbe metà posizione
    # PRIMA che holding adattivo / trailing possano valutare. I test di questo file verificano
    # quei motori in isolamento; il TP2 viene riabilitato dove serve (test_..._trailing).
    cfg["partial_tp"] = {"enabled": False}
    cfg["costs"]["simulate_failed_exits"] = False
    cfg["exit_slippage"] = {
        "max_exit_slippage_pct": 8.0,
        "critical_exit_slippage_pct": 25.0,
        "max_price_impact_pct": 10.0,
        "max_quote_age_s": 15,
        "max_size_to_liquidity_ratio": 0.15,
        "max_exit_retries": 2,
        "retry_backoff_seconds": 3.0,
        "non_liquidatable_recheck_seconds": 30.0,
        "partial_exit_enabled": True,
        "partial_exit_fractions": [0.50, 0.25],
        "min_partial_exit_usd": 1.0,
        "allow_model_fallback_on_api_error": False,
    }
    from types import SimpleNamespace
    b = PaperBot(cfg, str(tmp_path), reset=True)
    if b.jup is None:
        b.jup = SimpleNamespace(quote=lambda *a, **kw: None, cache={}, last_reason="")
    b.decimals_cache["MINT_TEST"] = 6
    return b


def _open_pos(bot, held_minutes=0, entry_price=1.0, curr_price=1.25, liq=50000.0):
    t_now = time.time()
    entry_ts = t_now - held_minutes * 60.0
    pos = {
        "mint": "MINT_TEST",
        "pool": "POOL_TEST",
        "name": "TEST",
        "dex": "raydium",
        "entry_type": "volume_spike",
        "entry_ts": entry_ts,
        "entry_mid": entry_price,
        "entry_fill": entry_price,
        "qty": 100.0,
        "original_qty": 100.0,
        "cost_usd": 100.0,
        "original_cost_usd": 100.0,
        "liquidity": liq,
        "entry_liquidity": liq,
        "entry_vol_5m": 10000.0,
        "peak": max(entry_price, curr_price),
        "peak_price": max(entry_price, curr_price),
        "last": curr_price,
        "last_price_ts": t_now,
        "adaptive_last_eval_price": entry_price,
        "adaptive_last_eval_ts": entry_ts,
        "info": {},
        "costs": {"buy_slip_pct": 1.0, "buy_dex_fee": 0.25, "buy_net_fee": 0.28, "buy_slip_src": "jupiter"},
        "failed_exits": 0,
        "adaptive_holding_enabled": True,
        "adaptive_holding_stage": 0,
        "adaptive_holding_extensions": 0,
        "adaptive_holding_next_check": entry_ts + 15 * 60.0,
    }
    bot.positions["MINT_TEST"] = pos
    bot.cash = 0.0
    return pos


def _mock_jup_quote(bot, out_usd, impact_pct=1.0, age_s=0.5, route="Raydium"):
    def fake_quote(in_mint, out_mint, amount_raw, deadline=None):
        scale = amount_raw / (100 * 10**6)
        return {
            "out": int(out_usd * scale * 10**6),
            "in": amount_raw,
            "impact_pct": impact_pct,
            "route": route,
            "endpoint": "v1/quote",
            "quote_ts": time.time() - age_s,
            "age_s": age_s,
            "latency_s": 0.05,
            "cached": False,
        }
    bot.jup.quote = fake_quote


@pytest.mark.parametrize("minutes", [15, 30, 60, 120, 180, 300])
def test_holding_positive_trend_across_horizons(bot, minutes):
    pos = _open_pos(bot, held_minutes=minutes, entry_price=1.0, curr_price=1.35, liq=60000.0)
    pos["adaptive_holding_next_check"] = time.time() - 1
    _mock_jup_quote(bot, out_usd=134.0, impact_pct=0.8)
    bot.ds.tokens = lambda _: {
        "MINT_TEST": {"price": 1.38, "liquidity": 62000.0, "vol_m5": 18000.0, "buys_m5": 40, "sells_m5": 12}
    }
    bot.manage()
    assert "MINT_TEST" in bot.positions
    assert bot.positions["MINT_TEST"]["adaptive_holding_next_check"] > time.time()
    # Punto 3: momentum deve riflettere il delta rispetto ad adaptive_last_eval_price (1.0 -> 1.38 = +38%)
    assert bot.positions["MINT_TEST"]["adaptive_last_eval_price"] == pytest.approx(1.38)


def test_holding_deteriorating_trend_accelerates_polling(bot):
    pos = _open_pos(bot, held_minutes=10, entry_price=1.0, curr_price=1.02)
    bot._update_adaptive_poll("MINT_TEST", 0.94, pos)
    assert "MINT_TEST" in bot.adaptive_poll["accelerated"]


def test_holding_significant_reversal_sells(bot):
    pos = _open_pos(bot, held_minutes=20, entry_price=1.0, curr_price=0.95, liq=20000.0)
    pos["adaptive_holding_next_check"] = time.time() - 1
    _mock_jup_quote(bot, out_usd=94.0, impact_pct=1.0)
    bot.ds.tokens = lambda _: {
        "MINT_TEST": {"price": 0.93, "liquidity": 11000.0, "vol_m5": 1500.0, "buys_m5": 3, "sells_m5": 25}
    }
    bot.manage()
    assert "MINT_TEST" not in bot.positions
    assert "adaptive_holding_" in bot.closed[-1]["exit_reason"]


def test_holding_stop_loss_take_profit_trailing(bot):
    _open_pos(bot, held_minutes=5, entry_price=1.0, curr_price=0.85)
    _mock_jup_quote(bot, out_usd=84.5)
    bot.ds.tokens = lambda _: {"MINT_TEST": {"price": 0.85, "liquidity": 50000.0}}
    bot.manage()
    assert bot.closed[-1]["exit_reason"] == "stop_loss"

    pos = _open_pos(bot, held_minutes=25, entry_price=1.0, curr_price=1.32)
    pos["peak"] = 1.50
    _mock_jup_quote(bot, out_usd=131.5)
    bot.ds.tokens = lambda _: {"MINT_TEST": {"price": 1.32, "liquidity": 50000.0}}
    bot.manage()
    assert bot.closed[-1]["exit_reason"] == "trailing_stop"

    pos = _open_pos(bot, held_minutes=40, entry_price=1.0, curr_price=1.65)
    pos["tp1_hit"] = True
    # TP2 richiede partial_tp attivo: riabilitato solo per questo sotto-caso
    bot.c["partial_tp"]["enabled"] = True
    _mock_jup_quote(bot, out_usd=164.0)
    bot.ds.tokens = lambda _: {"MINT_TEST": {"price": 1.65, "liquidity": 50000.0}}
    bot.manage()
    assert bot.closed[-1]["exit_reason"] == "take_profit_2"


def test_restart_triggers_immediate_eval_then_schedules_60m(bot, tmp_path):
    """Punto 5: posizione aperta a 45m al riavvio -> prima rivalutazione immediata, poi milestone 60m."""
    _open_pos(bot, held_minutes=45, entry_price=1.0, curr_price=1.40)
    bot.save_state("running")
    cfg_path = "bot/config.yaml" if os.path.exists("bot/config.yaml") else "config.yaml"
    cfg = yaml.safe_load(open(cfg_path))
    cfg["telegram_enabled"] = False
    cfg["adaptive_holding"]["enabled"] = True
    cfg["partial_tp"] = {"enabled": False}   # isolamento come nel fixture (vedi sopra)
    restarted = PaperBot(cfg, str(tmp_path), reset=False)
    assert "MINT_TEST" in restarted.positions
    assert restarted.positions["MINT_TEST"].get("adaptive_needs_immediate_eval") is True
    assert restarted.positions["MINT_TEST"]["adaptive_holding_next_check"] <= time.time() + 1.0

    _mock_jup_quote(restarted, out_usd=139.0)
    restarted.ds.tokens = lambda _: {
        "MINT_TEST": {"price": 1.42, "liquidity": 55000.0, "vol_m5": 15000.0, "buys_m5": 35, "sells_m5": 10}
    }
    restarted.manage()
    assert "MINT_TEST" in restarted.positions
    assert restarted.positions["MINT_TEST"].get("adaptive_needs_immediate_eval") is None
    # Dopo la rivalutazione immediata a 45m, la prossima milestone deve essere al minuto 60 (+15m da ora)
    remaining_min = (restarted.positions["MINT_TEST"]["adaptive_holding_next_check"] - time.time()) / 60.0
    assert 13.5 <= remaining_min <= 15.5


def test_non_liquidatable_never_creates_fake_time_exit(bot):
    """Punto 4: una posizione non_liquidatable oltre max_hold NON viene chiusa con incasso fittizio."""
    pos = _open_pos(bot, held_minutes=40, entry_price=1.0, curr_price=0.90, liq=100.0)
    pos["non_liquidatable"] = True
    pos["_exit_next_retry_ts"] = time.time() - 1
    bot.jup.quote = lambda *a, **kw: None
    bot.jup.last_reason = "no_route"
    bot.ds.tokens = lambda _: {"MINT_TEST": {"price": 0.90, "liquidity": 100.0}}
    bot.manage()
    assert "MINT_TEST" in bot.positions
    assert len(bot.closed) == 0


def test_exit_slippage_acceptable_allows_sell(bot):
    _open_pos(bot, held_minutes=5, entry_price=1.0, curr_price=0.85)
    _mock_jup_quote(bot, out_usd=83.3, impact_pct=1.2)
    bot.try_sell("MINT_TEST", 0.85, "stop_loss", 0.05)
    assert "MINT_TEST" not in bot.positions
    assert bot.closed[-1]["sell_slip_src"] == "jupiter"


def test_exit_slippage_high_triggers_requote_backoff_then_partial(bot):
    """Punto 6 + vendita parziale: verifica backoff temporizzato e split parziale."""
    pos = _open_pos(bot, held_minutes=20, entry_price=1.0, curr_price=1.10, liq=2000.0)

    def high_slip_quote(in_mint, out_mint, amount_raw, deadline=None):
        out_usd = (100.0 * (amount_raw / (100 * 10**6)) * 1.10) * 0.55
        return {"out": int(out_usd * 10**6), "in": amount_raw, "impact_pct": 18.0, "route": "Raydium", "age_s": 0.1}

    bot.jup.quote = high_slip_quote
    bot.try_sell("MINT_TEST", 1.10, "adaptive_holding_close", 0.01)
    assert pos["_exit_retry_count"] == 1
    assert pos["_exit_next_retry_ts"] > time.time()

    # Chiamata prima dello scadere del backoff non deve incrementare il retry count
    bot.try_sell("MINT_TEST", 1.10, "adaptive_holding_close", 0.01)
    assert pos["_exit_retry_count"] == 1

    # Dopo backoff, quote parziale al 50% rientra nei limiti
    pos["_exit_next_retry_ts"] = time.time() - 1
    def tiered_quote(in_mint, out_mint, amount_raw, deadline=None):
        frac = amount_raw / (100 * 10**6)
        slip = 0.40 if frac > 0.6 else 0.04
        out_usd = (100.0 * frac * 1.10) * (1.0 - slip)
        return {"out": int(out_usd * 10**6), "in": amount_raw, "impact_pct": 3.0, "route": "Raydium", "age_s": 0.2}

    bot.jup.quote = tiered_quote
    bot.try_sell("MINT_TEST", 1.10, "adaptive_holding_close", 0.01)
    assert "MINT_TEST" in bot.positions
    assert bot.positions["MINT_TEST"]["qty"] == pytest.approx(50.0, rel=1e-6)
    assert len(bot.closed) == 1
    assert bot.closed[-1]["partial"] is True


def test_exit_stale_quote_no_route_timeout_no_fake_fill(bot):
    """Punto 1: route='' anche con usd_out > 0 DEVE bloccare la vendita."""
    pos = _open_pos(bot, held_minutes=20, entry_price=1.0, curr_price=1.10, liq=5000.0)

    # Quotazione scaduta
    _mock_jup_quote(bot, out_usd=108.0, age_s=25.0, route="Raydium")
    bot.try_sell("MINT_TEST", 1.10, "adaptive_holding_close", 0.01)
    assert "MINT_TEST" in bot.positions and len(bot.closed) == 0

    # Route vuota MA usd_out positivo -> DEVE essere bloccata!
    pos.pop("_exit_next_retry_ts", None)
    _mock_jup_quote(bot, out_usd=108.0, age_s=0.5, route="")
    bot.try_sell("MINT_TEST", 1.10, "adaptive_holding_close", 0.01)
    assert "MINT_TEST" in bot.positions and len(bot.closed) == 0

    # Timeout API
    pos.pop("_exit_next_retry_ts", None)
    bot.jup.quote = lambda *a, **kw: None
    bot.jup.last_reason = "timeout"
    bot.try_sell("MINT_TEST", 1.10, "adaptive_holding_close", 0.01)
    assert "MINT_TEST" in bot.positions and len(bot.closed) == 0

    # Liquidità insufficiente
    pos.pop("_exit_next_retry_ts", None)
    pos["liquidity"] = 200.0
    bot.jup.last_reason = "no_route"
    bot.try_sell("MINT_TEST", 1.10, "adaptive_holding_close", 0.01)
    assert "MINT_TEST" in bot.positions
    assert bot.positions["MINT_TEST"].get("non_liquidatable") is True
    assert len(bot.closed) == 0


def test_atomic_lock_blocks_concurrent_sell_and_pnl_coherence(bot):
    """Punto 2: lock atomico in try_sell() impedisce vendite concorrenti prima di Jupiter."""
    pos = _open_pos(bot, held_minutes=10, entry_price=1.0, curr_price=1.50)
    pos["exit_in_progress"] = True
    pos["exit_started_ts"] = time.time()
    _mock_jup_quote(bot, out_usd=142.0, impact_pct=1.5)
    bot.try_sell("MINT_TEST", 1.50, "take_profit_2", 0.0)
    assert "MINT_TEST" in bot.positions

    pos.pop("exit_in_progress")
    bot.try_sell("MINT_TEST", 1.50, "take_profit_2", 0.0)
    bot.try_sell("MINT_TEST", 1.50, "take_profit_2", 0.0)
    assert "MINT_TEST" not in bot.positions
    assert len(bot.closed) == 1
    assert bot.closed[0]["proceeds_usd"] < 142.0


def _open_copy_pos(bot, held_minutes=240, curr_price=1.10):
    pos = _open_pos(bot, held_minutes=held_minutes, entry_price=1.0, curr_price=curr_price)
    pos["entry_type"] = "copy_top_traders"
    pos["adaptive_holding_enabled"] = False
    pos["track"] = {
        "n": 3,
        "sold": 0,
        "last_check": time.time(),
        "wallets": {
            "wallet_a": {"start": 100.0, "now": 100.0, "sold": False},
            "wallet_b": {"start": 100.0, "now": 100.0, "sold": False},
            "wallet_c": {"start": 100.0, "now": 100.0, "sold": False},
        },
    }
    return pos


def test_copy_trade_has_no_time_exit_or_profit_trailing_exits(bot):
    pos = _open_copy_pos(bot, held_minutes=240, curr_price=1.10)
    bot.check_wallets = lambda p, rules: False
    bot.ds.tokens = lambda _: {"MINT_TEST": {"price": 1.10, "liquidity": 50000.0}}
    rules = bot.exit_rules(pos)
    assert rules["take_profit_pct"] == 0
    assert rules["trailing_stop_pct"] == 0
    bot.manage()
    assert "MINT_TEST" in bot.positions
    assert len(bot.closed) == 0


def test_copy_trade_keeps_emergency_stop_loss(bot):
    pos = _open_copy_pos(bot, held_minutes=240, curr_price=0.70)
    _mock_jup_quote(bot, out_usd=69.0)
    bot.ds.tokens = lambda _: {"MINT_TEST": {"price": 0.70, "liquidity": 50000.0}}
    bot.manage()
    assert "MINT_TEST" not in bot.positions
    assert bot.closed[-1]["exit_reason"] == "stop_loss"


def test_copy_trade_exits_when_majority_of_tracked_wallets_sell(bot):
    pos = _open_copy_pos(bot, held_minutes=10, curr_price=1.05)
    pos["track"]["last_check"] = 0
    bot.rpc.balances_batch = lambda wallets, mint: {
        "wallet_a": 0.0,
        "wallet_b": 0.0,
        "wallet_c": 100.0,
    }
    assert bot.check_wallets(pos, bot.exit_rules(pos)) is True
    assert pos["track"]["sold"] == 2
