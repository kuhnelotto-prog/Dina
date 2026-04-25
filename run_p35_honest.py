#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P35: Первый по-настоящему честный прогон.

Что изменилось по сравнению с P34 final:
  1. Funding direction: шорты ПОЛУЧАЮТ funding (sign=-1), а не платят
  2. P34 параметры уже в backtester.py (SL_ATR_MULT_LONG=6.6 и т.д.)
  3. Никаких оверрайдов порогов — используем дефолты backtester.py
  4. Никаких оверрайдов BASE_RISK_PCT — используем 1.0%
"""

import sys, os, time
sys.path.insert(0, '.')

import backtester as bt_mod
from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd

SYMBOLS_12 = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","LINKUSDT",
              "DOGEUSDT","AVAXUSDT","ADAUSDT","SUIUSDT","APEUSDT","ARBUSDT"]
bt_mod.SYMBOLS = SYMBOLS_12

BALANCE = 1000.0

PERIODS = [
    ("BULL",   datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
]

def fetch_binance(sym, start_dt, end_dt, interval="4h"):
    """Fetch historical klines from Binance Futures API."""
    all_c = []
    st = int(start_dt.timestamp() * 1000)
    et = int(end_dt.timestamp() * 1000)
    cs = st
    for _ in range(30):
        p = {"symbol": sym, "interval": interval, "startTime": cs, "endTime": et, "limit": 1500}
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/klines", params=p, timeout=30).json()
        except Exception:
            break
        if not isinstance(r, list) or len(r) == 0:
            break
        for c in r:
            all_c.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        last_close = int(r[-1][6])
        if last_close >= et or len(r) < 1500:
            break
        cs = last_close + 1
        time.sleep(0.1)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df


# ── Print configuration ──
print("=" * 80)
print("P35 HONEST RUN: первый честный прогон с исправленным funding")
print("=" * 80)
print(f"  SL_ATR_MULT_LONG  = {bt_mod.SL_ATR_MULT_LONG}")
print(f"  SL_ATR_MULT_SHORT  = {bt_mod.SL_ATR_MULT_SHORT}")
print(f"  TSL_ATR_LONG_STEP0 = {bt_mod.TSL_ATR_LONG_STEP0}")
print(f"  TSL_ATR_LONG_AFTER_TP1 = {bt_mod.TSL_ATR_LONG_AFTER_TP1}")
print(f"  FUNDING_RATE       = {bt_mod.FUNDING_RATE}")
print(f"  FUNDING_INTERVAL_H = {bt_mod.FUNDING_INTERVAL_H}")
print(f"  BASE_RISK_PCT      = {bt_mod.BASE_RISK_PCT}")
print(f"  SLIPPAGE_PCT       = {bt_mod.SLIPPAGE_PCT}")
print(f"  THRESHOLDS: LONG_BULL=0.30, LONG_BEAR=0.40, SHORT_BULL=0.45, SHORT_BEAR=0.35")
print(f"  BALANCE = ${BALANCE:,.0f}  SYMBOLS = {len(SYMBOLS_12)}")
print("=" * 80)

# ── Fetch data ──
print("\nFetching data from Binance...")
data_cache = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    print(f"  {pk}: BTC 4H={len(btc_df)} 1D={len(btc_1d)}", end="")
    for s in SYMBOLS_12[1:]:
        df = fetch_binance(s, start_dt, end_dt)
        time.sleep(0.15)
        dfs[s] = df
        print(f" {s[:3]}={len(df)}", end="")
    print()
    data_cache[pk] = (dfs, btc_df, btc_1d)

# ── Run backtests ──
print("\n" + "=" * 80)
print("RESULTS")
print("=" * 80)

grand_pnl = 0
grand_dd = 0
grand_long_pnl = 0
grand_short_pnl = 0
grand_long_trades = 0
grand_short_trades = 0
grand_long_wins = 0
grand_short_wins = 0

for pk, start_dt, end_dt in PERIODS:
    dfs, btc_df, btc_1d = data_cache[pk]
    all_trades = []

    for s in SYMBOLS_12:
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

    # SL hit rate
    long_sl_count = sum(1 for t in long_trades if getattr(t, 'exit_reason', '') == 'SL')
    long_sl_pct = long_sl_count / len(long_trades) * 100 if long_trades else 0
    short_sl_count = sum(1 for t in short_trades if getattr(t, 'exit_reason', '') == 'SL')
    short_sl_pct = short_sl_count / len(short_trades) * 100 if short_trades else 0

    # Funding impact
    long_funding = sum(t.total_funding for t in long_trades)
    short_funding = sum(t.total_funding for t in short_trades)

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

    # Drawdown
    cum = 0; peak = 0; max_dd = 0
    for t in sorted(all_trades, key=lambda x: x.exit_time if hasattr(x, 'exit_time') else 0):
        cum += t.pnl_usd
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd

    grand_pnl += total_pnl
    if max_dd > grand_dd: grand_dd = max_dd
    grand_long_pnl += long_pnl
    grand_short_pnl += short_pnl
    grand_long_trades += len(long_trades)
    grand_short_trades += len(short_trades)
    grand_long_wins += sum(1 for t in long_trades if t.pnl_usd > 0)
    grand_short_wins += sum(1 for t in short_trades if t.pnl_usd > 0)

    print(f"\n  {pk:12s} LONG: {len(long_trades):3d} WR={long_wr:.1f}% PnL=${long_pnl:+.2f} SL={long_sl_pct:.1f}% fund=${long_funding:+.2f}  |  SHORT: {len(short_trades):3d} WR={short_wr:.1f}% PnL=${short_pnl:+.2f} SL={short_sl_pct:.1f}% fund=${short_funding:+.2f}  |  TOTAL: ${total_pnl:+.2f}  DD={max_dd/BALANCE*100:.1f}%")

    for reason, data in sorted(long_exits.items(), key=lambda x: -x[1]['count']):
        print(f"    LONG  exits: {reason:20s}: {data['count']:3d} ({data['count']/len(long_trades)*100:.1f}%) PnL=${data['pnl']:+.2f}")
    if short_trades:
        for reason, data in sorted(short_exits.items(), key=lambda x: -x[1]['count']):
            print(f"    SHORT exits: {reason:20s}: {data['count']:3d} ({data['count']/len(short_trades)*100:.1f}%) PnL=${data['pnl']:+.2f}")

# ── Grand totals ──
grand_wr_long = grand_long_wins / grand_long_trades * 100 if grand_long_trades else 0
grand_wr_short = grand_short_wins / grand_short_trades * 100 if grand_short_trades else 0
grand_total_trades = grand_long_trades + grand_short_trades

print(f"\n{'='*80}")
print(f"  GRAND TOTAL (P35 HONEST): ${grand_pnl:+.2f}  MaxDD=${grand_dd:.2f} ({grand_dd/BALANCE*100:.1f}%)")
print(f"  LONG:  {grand_long_trades:3d} trades  WR={grand_wr_long:.1f}%  PnL=${grand_long_pnl:+.2f}")
print(f"  SHORT: {grand_short_trades:3d} trades  WR={grand_wr_short:.1f}%  PnL=${grand_short_pnl:+.2f}")
print(f"  Total: {grand_total_trades:3d} trades  Return={grand_pnl/BALANCE*100:+.1f}%")
print(f"{'='*80}")