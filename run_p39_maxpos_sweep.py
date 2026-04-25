#!/usr/bin/env python3
"""P39: MAX_POSITIONS sweep. Fixed: CVD=0.3, SL=6.6, MIN_PNL_TIMEOUT_LONG=24h, sizing fix.
Variants: (4,3), (5,4), (6,5)"""
import sys, os, time, logging, importlib
sys.path.insert(0, '.')
logging.getLogger('backtester').setLevel(logging.WARNING)

import backtester as bt_mod
from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd

SYMBOLS_12 = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","LINKUSDT",
              "DOGEUSDT","AVAXUSDT","ADAUSDT","SUIUSDT","APEUSDT","ARBUSDT"]
BALANCE = 1000.0

PERIODS = [
    ("BULL",   datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
]

# (MAX_OPEN_POSITIONS, MAX_SHORT_OPEN)
VARIANTS = [(4, 3), (5, 4), (6, 5)]

def fetch_binance(sym, start_dt, end_dt, interval="4h"):
    all_c = []; st = int(start_dt.timestamp()*1000); et = int(end_dt.timestamp()*1000); cs = st
    for _ in range(30):
        p = {"symbol": sym, "interval": interval, "startTime": cs, "endTime": et, "limit": 1500}
        try: r = requests.get("https://fapi.binance.com/fapi/v1/klines", params=p, timeout=30).json()
        except: break
        if not isinstance(r, list) or len(r) == 0: break
        for c in r: all_c.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]), float(c[9])])
        last_close = int(r[-1][6])
        if last_close >= et or len(r) < 1500: break
        cs = last_close + 1; time.sleep(0.1)
    if not all_c: return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp","open","high","low","close","volume","taker_buy_vol"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True); return df

# Fetch data once
print("Fetching data with taker_buy_vol...")
all_data = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    print(f"  {pk}: BTC taker_buy_vol={'True' if 'taker_buy_vol' in btc_df.columns else 'False'}")
    for s in SYMBOLS_12[1:]:
        df = fetch_binance(s, start_dt, end_dt); time.sleep(0.15); dfs[s] = df
    all_data[pk] = (dfs, btc_df, btc_1d)

def run_variant(max_open, max_short):
    importlib.reload(bt_mod)
    bt_mod.SYMBOLS = SYMBOLS_12
    bt_mod.SL_ATR_MULT_LONG = 6.6
    bt_mod.CVD_WEIGHT_LONG = 0.3
    bt_mod.CVD_LOOKBACK = 20
    bt_mod.MIN_PNL_CHECK_H_LONG = 24
    bt_mod.MIN_PNL_LONG_ENABLED = True
    bt_mod.MIN_EXPECTED_PNL_PCT_LONG = -0.5
    bt_mod.MAX_OPEN_POSITIONS = max_open
    bt_mod.MAX_SHORT_OPEN = max_short
    from backtester import Backtester as BT
    
    all_trades = []
    for pk, start_dt, end_dt in PERIODS:
        dfs, btc_df, btc_1d = all_data[pk]
        for s in SYMBOLS_12:
            sym_df = dfs.get(s)
            if sym_df is None or len(sym_df) < 50: continue
            bt = BT(initial_balance=BALANCE)
            res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
            all_trades.extend(res.trades)
    
    long_trades = [t for t in all_trades if t.side == "long"]
    short_trades = [t for t in all_trades if t.side == "short"]
    long_pnl = sum(t.pnl_usd for t in long_trades)
    short_pnl = sum(t.pnl_usd for t in short_trades)
    total_pnl = long_pnl + short_pnl
    
    long_wr = sum(1 for t in long_trades if t.pnl_usd > 0) / len(long_trades) * 100 if long_trades else 0
    short_wr = sum(1 for t in short_trades if t.pnl_usd > 0) / len(short_trades) * 100 if short_trades else 0
    
    from collections import Counter
    long_reasons = Counter()
    for t in long_trades:
        reason = getattr(t, 'exit_reason', 'UNKNOWN')
        long_reasons[reason] += 1
    long_timeout_pct = (long_reasons.get('TIMEOUT', 0) + long_reasons.get('MIN_PNL_TIMEOUT', 0)) / len(long_trades) * 100 if long_trades else 0
    long_sl_pct = long_reasons.get('SL', 0) / len(long_trades) * 100 if long_trades else 0
    
    cum = 0; peak = 0; max_dd = 0
    for t in sorted(all_trades, key=lambda x: x.exit_time if hasattr(x, 'exit_time') else 0):
        cum += t.pnl_usd
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd
    max_dd_pct = max_dd / BALANCE * 100
    
    return {
        'long_n': len(long_trades), 'short_n': len(short_trades),
        'long_wr': long_wr, 'short_wr': short_wr,
        'long_pnl': long_pnl, 'short_pnl': short_pnl,
        'total_pnl': total_pnl, 'long_timeout_pct': long_timeout_pct,
        'long_sl_pct': long_sl_pct, 'max_dd_pct': max_dd_pct,
    }

# First: baseline with current settings (3,3) + CVD=0.3
print("\nP39 SWEEP: MAX_POSITIONS (fixed: CVD=0.3, SL=6.6, TIMEOUT=24h)")
print("="*120)

# Run baseline first
print(f"Baseline: MAX_OPEN=3, MAX_SHORT=3 (current)")
r_base = run_variant(3, 3)
print(f"  => LONG:{r_base['long_n']} WR={r_base['long_wr']:.1f}% PnL=${r_base['long_pnl']:+.2f} SHORT:{r_base['short_n']} PnL=${r_base['short_pnl']:+.2f} Total=${r_base['total_pnl']:+.2f} MaxDD={r_base['max_dd_pct']:.1f}%")
sys.stdout.flush()

results = [(3, 3, r_base)]
for max_open, max_short in VARIANTS:
    print(f"Variant: MAX_OPEN={max_open}, MAX_SHORT={max_short}")
    r = run_variant(max_open, max_short)
    results.append((max_open, max_short, r))
    print(f"  => LONG:{r['long_n']} WR={r['long_wr']:.1f}% PnL=${r['long_pnl']:+.2f} SHORT:{r['short_n']} PnL=${r['short_pnl']:+.2f} Total=${r['total_pnl']:+.2f} MaxDD={r['max_dd_pct']:.1f}%")
    sys.stdout.flush()

# Summary
print("\n" + "="*120)
print("P39 SWEEP: MAX_POSITIONS (fixed: CVD=0.3, SL=6.6, TIMEOUT=24h, sizing fix)")
print("="*120)
hdr = f"{'Variant':20s} {'LONG#':>5s} {'SHORT#':>6s} {'L_WR%':>6s} {'L_PnL$':>10s} {'S_PnL$':>10s} {'Total$':>10s} {'MaxDD%':>6s}"
print(hdr)
print("-"*120)
for max_open, max_short, r in results:
    label = f"MAX={max_open}/S={max_short}"
    print(f"{label:20s} {r['long_n']:5d} {r['short_n']:6d} {r['long_wr']:6.1f} {r['long_pnl']:+10.2f} {r['short_pnl']:+10.2f} {r['total_pnl']:+10.2f} {r['max_dd_pct']:6.1f}%")
print("="*120)

# Save
with open('p39_maxpos_results.txt', 'w') as f:
    f.write("P39 SWEEP: MAX_POSITIONS (fixed: CVD=0.3, SL=6.6, TIMEOUT=24h, sizing fix)\n")
    f.write("="*120 + "\n")
    f.write(hdr + "\n")
    f.write("-"*120 + "\n")
    for max_open, max_short, r in results:
        label = f"MAX={max_open}/S={max_short}"
        f.write(f"{label:20s} {r['long_n']:5d} {r['short_n']:6d} {r['long_wr']:6.1f} {r['long_pnl']:+10.2f} {r['short_pnl']:+10.2f} {r['total_pnl']:+10.2f} {r['max_dd_pct']:6.1f}%\n")
    f.write("="*120 + "\n")

print("\nResults written to p39_maxpos_results.txt")