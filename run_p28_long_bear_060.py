#!/usr/bin/env python3
"""P28: LONG_THRESHOLD_BEAR=0.60, rest=P24. Check LONG filtering in BEAR/SIDE."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester
backtester.MIN_PNL_CHECK_H = 72
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 1000.0
backtester.BASE_RISK_PCT = 2.0
backtester.LEVERAGE = 1
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
                      "LINKUSDT", "DOGEUSDT", "SUIUSDT",
                      "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT",
                      "ATOMUSDT"]

import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0
ep.LONG_THRESHOLD_BULL = 0.40   # same as P24
ep.LONG_THRESHOLD_BEAR = 0.60   # was 0.45, now stricter
ep.SHORT_THRESHOLD_BULL = 0.45  # same as P24
ep.SHORT_THRESHOLD_BEAR = 0.35  # same as P24

# CRITICAL: backtester uses 'from X import Y' which binds at import time.
# Must also update backtester module-level variables:
backtester.LONG_THRESHOLD_BEAR = 0.60
backtester.LONG_THRESHOLD_BULL = 0.40
backtester.SHORT_THRESHOLD_BULL = 0.45
backtester.SHORT_THRESHOLD_BEAR = 0.35
backtester.TSL_AFTER_TP2_ATR = 2.0   # P24 override (default is 1.5)
backtester.ADX_THRESHOLD = 20
backtester.SL_ATR_MULT = 3.0
backtester.TP1_ATR_MULT = 1.0
backtester.TP1_CLOSE_PCT = 0.30
backtester.TP2_ATR_MULT = 2.0
backtester.TP2_CLOSE_PCT = 0.30
backtester.TSL_FROM_ENTRY_ATR = 1.5
backtester.MAX_SIMULTANEOUS_TRADES = 4

from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd
from collections import Counter

SYMBOLS = backtester.SYMBOLS
BALANCE = 1000.0

PERIODS = [
    ("BULL (2023-11 -> 2024-04)", datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE (2024-05 -> 2024-10)", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT (2025-10 -> 2026-04)", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
]

def fetch_binance(sym, start_dt, end_dt, interval="4h"):
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
    if not all_c: return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

# Fetch data
data_cache = {}
print("Fetching data for all periods...")
for period_name, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    data_cache[f"BTCUSDT_{start_dt.date()}"] = (btc_df, btc_1d)
    print(f"  BTC 4H: {len(btc_df)}, 1D: {len(btc_1d)}")
    for s in SYMBOLS[1:]:
        df = fetch_binance(s, start_dt, end_dt)
        time.sleep(0.15)
        data_cache[f"{s}_{start_dt.date()}"] = df
        print(f"  {s}: {len(df)}")

print(f"\n{'='*100}")
print(f"  P28: LONG_THRESHOLD_BEAR=0.60 (was 0.45), LONG_THRESHOLD_BULL=0.40 (same)")
print(f"  LEV=1, $1000, 2% risk, per-symbol")
print(f"{'='*100}")

for period_name, start_dt, end_dt in PERIODS:
    btc_df, btc_1d = data_cache[f"BTCUSDT_{start_dt.date()}"]
    dfs = {"BTCUSDT": btc_df}
    for s in SYMBOLS[1:]:
        dfs[s] = data_cache[f"{s}_{start_dt.date()}"]

    all_trades = []
    for s in SYMBOLS:
        df = dfs.get(s)
        if df is None or df.empty or len(df) < 50:
            continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs={s: df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        all_trades.extend(res.trades)

    t = len(all_trades)
    if t == 0:
        print(f"\n  {period_name}: NO TRADES")
        continue

    long_t = [x for x in all_trades if getattr(x, 'side', '') == 'long']
    short_t = [x for x in all_trades if getattr(x, 'side', '') == 'short']
    long_pnl = sum(x.pnl_usd for x in long_t)
    short_pnl = sum(x.pnl_usd for x in short_t)
    long_wr = len([x for x in long_t if x.pnl_usd > 0])/len(long_t)*100 if long_t else 0
    short_wr = len([x for x in short_t if x.pnl_usd > 0])/len(short_t)*100 if short_t else 0
    total_pnl = sum(x.pnl_usd for x in all_trades)
    total_wr = len([x for x in all_trades if x.pnl_usd > 0])/t*100

    print(f"\n  {period_name}")
    print(f"  {'='*70}")
    print(f"  Trades: {t} | WR: {total_wr:.1f}% | PnL: ${total_pnl:+.2f} ({total_pnl/BALANCE*100:+.1f}%)")
    print(f"  LONG:  {len(long_t):>3} trades | WR={long_wr:.1f}% | PnL=${long_pnl:+.2f}")
    print(f"  SHORT: {len(short_t):>3} trades | WR={short_wr:.1f}% | PnL=${short_pnl:+.2f}")

    # P24 comparison
    p24_data = {
        "BULL": (104, 63.5, 112.61, 42, 71.4, 60.80),
        "BEAR": (73, 57.5, -152.41, 78, 66.7, 172.27),
        "CURRENT": (13, 69.2, -5.29, 208, 69.2, 823.57),
    }
    period_key = "BULL" if "BULL" in period_name else ("BEAR" if "BEAR" in period_name else "CURRENT")
    p24_long_n, p24_long_wr, p24_long_pnl, p24_short_n, p24_short_wr, p24_short_pnl = p24_data[period_key]
    print(f"  --- P24 comparison ---")
    print(f"  P24 LONG:  {p24_long_n:>3} trades | WR={p24_long_wr:.1f}% | PnL=${p24_long_pnl:+.2f}")
    print(f"  P24 SHORT: {p24_short_n:>3} trades | WR={p24_short_wr:.1f}% | PnL=${p24_short_pnl:+.2f}")
    long_diff = len(long_t) - p24_long_n
    print(f"  LONG trades blocked by higher threshold: {abs(long_diff)} {'(fewer)' if long_diff < 0 else '(more)'}")

# Restore defaults
backtester.MIN_PNL_CHECK_H = 48
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 10000.0
backtester.BASE_RISK_PCT = 1.0
backtester.LEVERAGE = 1
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]
ep.TSL_AFTER_TP2_ATR = 1.5
ep.LONG_THRESHOLD_BULL = 0.30
ep.LONG_THRESHOLD_BEAR = 0.40
ep.SHORT_THRESHOLD_BULL = 0.45
ep.SHORT_THRESHOLD_BEAR = 0.35