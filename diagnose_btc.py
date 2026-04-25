import json
from datetime import datetime

f = open('backtest_honest_results.json', 'r', encoding='utf-8')
d = json.load(f)
f.close()

trades = d.get('trades', [])
btc = [t for t in trades if t.get('symbol') == 'BTCUSDT']
print(f'=== BTC: {len(btc)} trades ===\n')

for i, t in enumerate(btc):
    s = t['side']
    e = t['entry_price']
    x = t['exit_price']
    p = t['pnl_usd']
    pp = t['pnl_pct']
    r = t['exit_reason']
    step = t['trailing_step']
    rem = t['remaining_pct']
    dur = '?'
    try:
        e1 = datetime.fromisoformat(t['entry_time'][:19])
        e2 = datetime.fromisoformat(t['exit_time'][:19])
        dur = f'{(e2-e1).total_seconds()/3600:.0f}h'
    except:
        pass
    w = 'W' if p > 0 else 'L'
    print(f'{i+1:2d}. {s:5s} entry={e:,.0f} exit={x:,.0f} {w} {p:+.0f}$({pp:+.1f}%) {r:20s} step={step} rem={rem*100:.0f}% {dur}')

# Summary by exit reason
for reason in ['SL', 'TP_2ATR', 'TSL', 'MIN_PNL_TIMEOUT', 'TIMEOUT', 'END_OF_BACKTEST']:
    r_trades = [t for t in btc if t['exit_reason'] == reason]
    if r_trades:
        pnl = sum(t['pnl_usd'] for t in r_trades)
        print(f'\n{reason}: {len(r_trades)} trades, PnL={pnl:+.0f}$')

# LONG vs SHORT
lo = [t for t in btc if t['side'] == 'long']
sh = [t for t in btc if t['side'] == 'short']
lwr = sum(1 for t in lo if t['pnl_usd'] > 0) / len(lo) * 100 if lo else 0
swr = sum(1 for t in sh if t['pnl_usd'] > 0) / len(sh) * 100 if sh else 0
print(f'\nLONG:  {len(lo)} trades, WR={lwr:.0f}%, PnL={sum(t["pnl_usd"] for t in lo):+.0f}$')
print(f'SHORT: {len(sh)} trades, WR={swr:.0f}%, PnL={sum(t["pnl_usd"] for t in sh):+.0f}$')

# Duration stats
durs = []
for t in btc:
    try:
        e1 = datetime.fromisoformat(t['entry_time'][:19])
        e2 = datetime.fromisoformat(t['exit_time'][:19])
        durs.append((e2-e1).total_seconds() / 3600)
    except:
        pass
if durs:
    print(f'\nDuration: avg={sum(durs)/len(durs):.0f}h, min={min(durs):.0f}h, max={max(durs):.0f}h')
    sl_durs = [durs[i] for i, t in enumerate(btc) if t['exit_reason'] == 'SL']
    tp_durs = [durs[i] for i, t in enumerate(btc) if t['exit_reason'] == 'TP_2ATR']
    if sl_durs:
        print(f'SL avg duration: {sum(sl_durs)/len(sl_durs):.0f}h')
    if tp_durs:
        print(f'TP_2ATR avg duration: {sum(tp_durs)/len(tp_durs):.0f}h')

# Entry price ranges
print('\n--- Entry price ranges ---')
ranges = [(60000, 75000), (75000, 90000), (90000, 105000), (105000, 120000)]
for lo_p, hi_p in ranges:
    in_range = [t for t in btc if lo_p <= t['entry_price'] < hi_p]
    if in_range:
        wr = sum(1 for t in in_range if t['pnl_usd'] > 0) / len(in_range) * 100
        pnl = sum(t['pnl_usd'] for t in in_range)
        print(f'  ${lo_p/1000:.0f}k-${hi_p/1000:.0f}k: {len(in_range)} trades, WR={wr:.0f}%, PnL={pnl:+.0f}$')