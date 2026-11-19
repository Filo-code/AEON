import sys
sys.path.insert(0, '.')
from bot.paper_bot import PaperBot
import yaml, tempfile, time

with open('bot/config.yaml') as f:
    cfg = yaml.safe_load(f)

def test_breakeven_stop():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        bot = PaperBot(cfg, tmpdir, reset=True)
        now_ts = time.time()
        bot.cash = 100.0
        bot.positions['TESTMINT'] = {
            'mint': 'TESTMINT', 'pool': 'TESTPOOL', 'name': 'TEST', 'dex': 'raydium',
            'entry_type': 'volume_spike', 'entry_ts': now_ts, 'entry_mid': 1.0, 'entry_fill': 1.0,
            'qty': 50.0, 'cost_usd': 50.0, 'liquidity': 10000.0, 'peak': 1.25, 'last': 1.25,
            'last_price_ts': now_ts, 'info': {},
            'costs': {'buy_slip_pct': 1.0, 'buy_dex_fee': 0.5, 'buy_net_fee': 0.002, 'buy_slip_src': 'stimato'},
            'failed_exits': 0, 'tp1_hit': True, 'tp1_qty_sold': 50.0, 'tp1_price': 1.25,
            'tp1_realized_pnl': 6.89, 'tp1_fees': 1.0, 'tp1_sell_qty_requested': 50.0,
            'breakeven_activated': True, 'breakeven_price': 1.008725, 'original_qty': 100.0,
            'original_cost_usd': 100.0, 'remaining_qty': 50.0, 'tp2_done': False,
            'mfe_pct': 0.25, 'mae_pct': 0.0,
        }
        class MockDS:
            def __init__(self, price):
                self.last_ok = True; self.price = price
            def tokens(self, mints):
                return {'TESTMINT': {'price': self.price, 'liquidity': 10000.0, 'vol_m5': 5000, 'vol_h1': 10000, 'buys_m5': 10, 'sells_m5': 5, 'pool': 'TESTPOOL', 'name': 'TEST', 'dex': 'raydium', 'quote_mint': 'SOL', 'created_ts': 1000000, 'fdv': 1000000, 'src': 'dexscreener'}}
        mock_ds = MockDS(1.00)
        bot.ds = mock_ds
        bot.positions['TESTMINT']['last'] = 1.00
        bot.manage()
        p = bot.positions.get('TESTMINT')
        r = p is None and len(bot.closed) == 1 and bot.closed[-1]['exit_reason'] == 'breakeven_stop'
        print(f'Test 3: Breakeven stop: {"PASS" if r else "FAIL"}')
        return r

def test_trailing_stop_priority():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        bot = PaperBot(cfg, tmpdir, reset=True)
        now_ts = time.time()
        bot.cash = 100.0
        bot.positions['TESTMINT'] = {
            'mint': 'TESTMINT', 'pool': 'TESTPOOL', 'name': 'TEST', 'dex': 'raydium',
            'entry_type': 'volume_spike', 'entry_ts': now_ts, 'entry_mid': 1.0, 'entry_fill': 1.0,
            'qty': 50.0, 'cost_usd': 50.0, 'liquidity': 10000.0, 'peak': 2.0, 'last': 1.8,
            'last_price_ts': now_ts, 'info': {},
            'costs': {'buy_slip_pct': 1.0, 'buy_dex_fee': 0.5, 'buy_net_fee': 0.002, 'buy_slip_src': 'stimato'},
            'failed_exits': 0, 'tp1_hit': True, 'tp1_qty_sold': 50.0, 'tp1_price': 1.25,
            'tp1_realized_pnl': 6.89, 'tp1_fees': 1.0, 'tp1_sell_qty_requested': 50.0,
            'breakeven_activated': True, 'breakeven_price': 1.008725, 'original_qty': 100.0,
            'original_cost_usd': 100.0, 'remaining_qty': 50.0, 'tp2_done': False,
            'mfe_pct': 0.8, 'mae_pct': 0.0,
        }
        class MockDS:
            def __init__(self, price):
                self.last_ok = True; self.price = price
            def tokens(self, mints):
                return {'TESTMINT': {'price': self.price, 'liquidity': 10000.0, 'vol_m5': 5000, 'vol_h1': 10000, 'buys_m5': 10, 'sells_m5': 5, 'pool': 'TESTPOOL', 'name': 'TEST', 'dex': 'raydium', 'quote_mint': 'SOL', 'created_ts': 1000000, 'fdv': 1000000, 'src': 'dexscreener'}}
        mock_ds = MockDS(1.8)
        bot.ds = mock_ds
        bot.positions['TESTMINT']['last'] = 1.8
        bot.manage()
        p = bot.positions.get('TESTMINT')
        r = p is None and len(bot.closed) == 1 and bot.closed[-1]['exit_reason'] == 'trailing_stop'
        print(f'Test 4: Trailing stop priority: {"PASS" if r else "FAIL"}')
        return r

def test_tp2():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        bot = PaperBot(cfg, tmpdir, reset=True)
        now_ts = time.time()
        bot.cash = 100.0
        bot.positions['TESTMINT'] = {
            'mint': 'TESTMINT', 'pool': 'TESTPOOL', 'name': 'TEST', 'dex': 'raydium',
            'entry_type': 'volume_spike', 'entry_ts': now_ts, 'entry_mid': 1.0, 'entry_fill': 1.0,
            'qty': 100.0, 'cost_usd': 100.0, 'liquidity': 10000.0, 'peak': 1.0, 'last': 1.0,
            'last_price_ts': now_ts, 'info': {},
            'costs': {'buy_slip_pct': 1.0, 'buy_dex_fee': 0.5, 'buy_net_fee': 0.002, 'buy_slip_src': 'stimato'},
            'failed_exits': 0, 'tp1_hit': False, 'tp1_qty_sold': 0.0, 'tp1_price': 0.0,
            'tp1_realized_pnl': 0.0, 'tp1_fees': 0.0, 'tp1_sell_qty_requested': 0.0,
            'breakeven_activated': False, 'breakeven_price': 0.0, 'original_qty': 100.0,
            'original_cost_usd': 100.0, 'remaining_qty': 100.0, 'tp2_done': False,
            'mfe_pct': 0.0, 'mae_pct': 0.0,
        }
        class MockDS:
            def __init__(self, price):
                self.last_ok = True; self.price = price
            def tokens(self, mints):
                return {'TESTMINT': {'price': self.price, 'liquidity': 10000.0, 'vol_m5': 5000, 'vol_h1': 10000, 'buys_m5': 10, 'sells_m5': 5, 'pool': 'TESTPOOL', 'name': 'TEST', 'dex': 'raydium', 'quote_mint': 'SOL', 'created_ts': 1000000, 'fdv': 1000000, 'src': 'dexscreener'}}
        mock_ds = MockDS(1.0)
        bot.ds = mock_ds
        mock_ds.price = 1.25
        bot.positions['TESTMINT']['peak'] = 1.25
        bot.positions['TESTMINT']['last'] = 1.25
        bot.manage()
        mock_ds.price = 1.6
        bot.positions['TESTMINT']['peak'] = 1.6
        bot.positions['TESTMINT']['last'] = 1.6
        bot.manage()
        p = bot.positions.get('TESTMINT')
        r = p is None and len(bot.closed) == 2 and bot.closed[-1]['exit_reason'] == 'take_profit_2'
        print(f'Test 5: TP2 at +60%: {"PASS" if r else "FAIL"}')
        return r

def test_state_persistence():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        bot = PaperBot(cfg, tmpdir, reset=True)
        now_ts = time.time()
        bot.cash = 100.0
        bot.positions['TESTMINT'] = {
            'mint': 'TESTMINT', 'pool': 'TESTPOOL', 'name': 'TEST', 'dex': 'raydium',
            'entry_type': 'volume_spike', 'entry_ts': now_ts, 'entry_mid': 1.0, 'entry_fill': 1.0,
            'qty': 100.0, 'cost_usd': 100.0, 'liquidity': 10000.0, 'peak': 1.0, 'last': 1.0,
            'last_price_ts': now_ts, 'info': {},
            'costs': {'buy_slip_pct': 1.0, 'buy_dex_fee': 0.5, 'buy_net_fee': 0.002, 'buy_slip_src': 'stimato'},
            'failed_exits': 0, 'tp1_hit': False, 'tp1_qty_sold': 0.0, 'tp1_price': 0.0,
            'tp1_realized_pnl': 0.0, 'tp1_fees': 0.0, 'tp1_sell_qty_requested': 0.0,
            'breakeven_activated': False, 'breakeven_price': 0.0, 'original_qty': 100.0,
            'original_cost_usd': 100.0, 'remaining_qty': 100.0, 'tp2_done': False,
            'mfe_pct': 0.0, 'mae_pct': 0.0,
        }
        class MockDS:
            def __init__(self, price):
                self.last_ok = True; self.price = price
            def tokens(self, mints):
                return {'TESTMINT': {'price': self.price, 'liquidity': 10000.0, 'vol_m5': 5000, 'vol_h1': 10000, 'buys_m5': 10, 'sells_m5': 5, 'pool': 'TESTPOOL', 'name': 'TEST', 'dex': 'raydium', 'quote_mint': 'SOL', 'created_ts': 1000000, 'fdv': 1000000, 'src': 'dexscreener'}}
        mock_ds = MockDS(1.25)
        bot.ds = mock_ds
        bot.positions['TESTMINT']['peak'] = 1.25
        bot.positions['TESTMINT']['last'] = 1.25
        bot.manage()
        bot.save_state()
        bot2 = PaperBot(cfg, tmpdir, reset=False)
        p = bot2.positions['TESTMINT']
        p_orig = bot.positions['TESTMINT']
        r = bool(p.get('tp1_hit')) and bool(p.get('breakeven_activated')) and abs(p.get('breakeven_price', 0) - p_orig.get('breakeven_price', 0)) < 1e-9
        print(f'Test 6: State persistence: {"PASS" if r else "FAIL"}')
        return r

if __name__ == '__main__':
    all_pass = True
    all_pass &= test_breakeven_stop()
    all_pass &= test_trailing_stop_priority()
    all_pass &= test_tp2()
    all_pass &= test_state_persistence()
    print(f'\nAll core tests: {"PASS" if all_pass else "FAIL"}')