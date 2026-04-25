#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P34 test: Asymmetric LONG/SHORT — compare vs P24 baseline."""
import sys, os, time
sys.path.insert(0, '.')

import backtester as bt_mod

# P34 parameters
bt_mod.START_BALANCE = 10000.0
bt_mod.BASE_RISK_PCT = 1.0
bt_mod.SL_ATR_MULT_LONG = 3.5    # P34: wider SL for longs
bt_mod.SL_ATR_MULT_SHORT = 1.5  # standard SL for shorts
bt_mod.TSL_ATR_LONG_AFTER_TP1 = 2.0  # P34: softer trailing after TP1

SYMBOLS_10 = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","LINKUSDT","DOGEUSDT","AVAXUSDT","ADAUSDT","SUIUSDT"]
bt_mod.SYMBOLS = SYMBOLS_10

from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd

BALANCE = 10000.0
PERIODS = [
    ("BULL", datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
]

def fetch_binance(sym, start_dt, end_dt, interval="4h"):
    all_c = []; st = int(start_dt.timestamp()*1000); et = int(end_dt.timestamp()*1000); cs = st
    for _ in range(30):
        p = {"symbol": sym, "interval": interval, "startTime": cs, "endTime": et, "limit": 1500}
        try: r = requests.get("https://fapi.binance.com/fapi/v1/klines", params=p, timeout=30).json()
        except: break
        if not isinstance(r, list) or len(r) == 0: break
        for c in r: all_c.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        last_close = int(r[-1][6])
        if last_close >= et or len(r) < 1500: break
        cs = last_close + 1; time.sleep(0.1)
    if not all_c: return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True); return df

print("=" * 80)
print("P34 BACKTEST: Asymmetric LONG/SHORT")
print(f"SL_ATR_MULT_LONG=3.5, SL_ATR_MULT_SHORT=1.5, TSL_ATR_LONG_AFTER_TP1=2.0")
print(f"START=${BALANCE:,.0f}, SYMBOLS={SYMBOLS_10}")
print("=" * 80)

print("\nFetching data...")
data_cache = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    print(f"  {pk}: BTC 4H={len(btc_df)} 1D={len(btc_1d)}")
    for s in SYMBOLS_10[1:]:
        df = fetch_binance(s, start_dt, end_dt); time.sleep(0.15); dfs[s] = df
    data_cache[pk] = (dfs, btc_df, btc_1d)

grand_pnl = 0
grand_dd = 0
for pk, start_dt, end_dt in PERIODS:
    dfs, btc_df, btc_1d = data_cache[pk]
    all_trades = []
    for s in SYMBOLS_10:
        sym_df = dfs.get(s)
        if sym_df is None or len(sym_df) < 50:
            continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        all_trades.extend(res.trades)

    long_trades = [t for t in all_trades if t.side == "long"]
    short_trades = [t for t in all_trades if t.side == "short"]
    long_pnl = sum(t.pnl_usd for t in long_trades)
    short_pnl = sum(t.pnl_usd for t in short_trades)
    long_wr = sum(1 for t in long_trades if t.pnl_usd > 0) / len(long_trades) * 100 if long_trades else 0
    short_wr = sum(1 for t in short_trades if t.pnl_usd > 0) / len(short_trades) * 100 if short_trades else 0

    # Exit breakdown
    long_exits = {}
    for t in long_trades:
        reason = getattr(t, 'exit_reason', 'unknown')
        if reason not in long_exits:
            long_exits[reason] = {'count': 0, 'pnl': 0.0}
        long_exits[reason]['count'] += 1
        long_exits[reason]['pnl'] += t.pnl_usd

    short_exits = {}
    for t in short_trades:
        reason = getattr(t, 'exit_reason', 'unknown')
        if reason not in short_exits:
            short_exits[reason] = {'count': 0, 'pnl': 0.0}
        short_exits[reason]['count'] += 1
        short_exits[reason]['pnl'] += t.pnl_usd

    total_pnl = long_pnl + short_pnl
    # Approximate max drawdown from cumulative PnL
    cum = 0; peak = 0; max_dd = 0
    for t in sorted(all_trades, key=lambda x: x.exit_time if hasattr(x, 'exit_time') else 0):
        cum += t.pnl_usd
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd

    grand_pnl += total_pnl
    if max_dd > grand_dd: grand_dd = max_dd

    print(f"\n  {pk:12s} LONG: {len(long_trades):3d} WR={long_wr:.1f}% PnL=${long_pnl:+.2f}  |  SHORT: {len(short_trades):3d} WR={short_wr:.1f}% PnL=${short_pnl:+.2f}  |  TOTAL: ${total_pnl:+.2f}  DD={max_dd/BALANCE*100:.1f}%")

    for reason, data in sorted(long_exits.items(), key=lambda x: -x[1]['count']):
        print(f"    LONG exits: {reason:20s}: {data['count']:3d} ({data['count']/len(long_trades)*100:.1f}%) PnL=${data['pnl']:+.2f}")
    if short_trades:
        for reason, data in sorted(short_exits.items(), key=lambda x: -x[1]['count']):
            print(f"    SHORT exits: {reason:20s}: {data['count']:3d} ({data['count']/len(short_trades)*100:.1f}%) PnL=${data['pnl']:+.2f}")

print(f"\n{'='*80}")
print(f"  GRAND TOTAL (P34): ${grand_pnl:+.2f}  MaxDD={grand_dd:.2f}$")
print(f"{'='*80}")