#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P41: Leverage sweep — SL=6.6, risk=3%, 5x/7x/10x/12x/15x.
Leverage simply scales position size (and thus PnL) linearly.
DD scales linearly too. But we must check for account blowup."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt_mod

bt_mod.MIN_PNL_CHECK_H = 72; bt_mod.MIN_EXPECTED_PNL_PCT = -0.5
bt_mod.START_BALANCE = 1000.0; bt_mod.BASE_RISK_PCT = 3.0
bt_mod.USE_P34_LONG_EXIT = True; bt_mod.SL_ATR_MULT_LONG = 6.6
bt_mod.TP1_ATR_MULT = 1.0; bt_mod.TP1_CLOSE_PCT = 0.30
bt_mod.TP2_ATR_MULT = 2.0; bt_mod.TP2_CLOSE_PCT = 0.30
bt_mod.TSL_ATR_LONG = 2.0

SYMBOLS_12 = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "LINKUSDT", "DOGEUSDT",
              "SUIUSDT", "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT", "ATOMUSDT"]
bt_mod.SYMBOLS = SYMBOLS_12

import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0
ep.LONG_THRESHOLD_BULL = 0.40; ep.LONG_THRESHOLD_BEAR = 0.45
ep.SHORT_THRESHOLD_BULL = 0.45; ep.SHORT_THRESHOLD_BEAR = 0.35

from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd
from collections import Counter

BALANCE = 1000.0
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

LEVERAGE_VALUES = [1, 5, 7, 10, 12, 15]

print("Fetching data...")
data_cache = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    print(f"  {pk}: BTC 4H={len(btc_df)} 1D={len(btc_1d)}")
    for s in SYMBOLS_12[1:]:
        df = fetch_binance(s, start_dt, end_dt); time.sleep(0.15); dfs[s] = df
    data_cache[pk] = (dfs, btc_df, btc_1d)

# Run baseline (1x) — all trades
print("\nRunning baseline backtest (1x leverage, 3% risk)...")
period_trades = {}
for pk, start_dt, end_dt in PERIODS:
    dfs, btc_df, btc_1d = data_cache[pk]
    all_trades = []
    for s in SYMBOLS_12:
        sym_df = dfs.get(s)
        if sym_df is None or sym_df.empty or len(sym_df) < 50: continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        all_trades.extend(res.trades)
    # Sort trades by entry time for equity curve
    all_trades.sort(key=lambda x: x.entry_time)
    period_trades[pk] = all_trades
    long_t = [x for x in all_trades if x.side == 'long']
    short_t = [x for x in all_trades if x.side == 'short']
    long_pnl = sum(x.pnl_usd for x in long_t); short_pnl = sum(x.pnl_usd for x in short_t)
    total_pnl = sum(x.pnl_usd for x in all_trades)
    # Calculate max drawdown for 1x
    peak = BALANCE; balance = BALANCE; max_dd_pct = 0.0
    for t in all_trades:
        balance += t.pnl_usd
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd_pct: max_dd_pct = dd
    print(f"  {pk:10s}  PnL=${total_pnl:+.2f}  DD={max_dd_pct:.1f}%  LONG=${long_pnl:+.2f}  SHORT=${short_pnl:+.2f}  Trades={len(all_trades)}")

# Now calculate leverage effects
# Key insight: PnL and DD scale LINEARLY with leverage
# But we must check for account blowup (equity < 0)
print(f"\n{'='*90}")
print(f"  LEVERAGE SWEEP (SL=6.6, base risk=3%, starting balance=${BALANCE:.0f})")
print(f"{'='*90}")
print(f"  {'LEV':>3s} | {'BULL_PnL':>9s} {'DD%':>6s} | {'BEAR_PnL':>9s} {'DD%':>6s} | {'CUR_PnL':>8s} {'DD%':>6s} | {'GRAND':>8s} {'MaxDD':>6s} | {'Note':>10s}")
print(f"  " + "-" * 85)

for lev in LEVERAGE_VALUES:
    row = {}
    for pk in ["BULL", "BEAR/SIDE", "CURRENT"]:
        trades = period_trades[pk]
        scaled_pnl = [t.pnl_usd * lev for t in trades]
        total_pnl = sum(scaled_pnl)
        # Equity curve for max DD with leverage
        peak = BALANCE; balance = BALANCE; max_dd_pct = 0.0; blowup = False
        for pnl in scaled_pnl:
            balance += pnl
            if balance <= 0: blowup = True
            if balance > peak: peak = balance
            if peak > 0:
                dd = (peak - balance) / peak * 100
                if dd > max_dd_pct: max_dd_pct = dd
        long_pnl = sum(t.pnl_usd * lev for t in trades if t.side == 'long')
        short_pnl = sum(t.pnl_usd * lev for t in trades if t.side == 'short')
        row[pk] = {"pnl": total_pnl, "dd": max_dd_pct, "blowup": blowup,
                    "long_pnl": long_pnl, "short_pnl": short_pnl, "n": len(trades)}
    
    bp = row["BULL"]["pnl"]; bdd = row["BULL"]["dd"]
    bep = row["BEAR/SIDE"]["pnl"]; bedd = row["BEAR/SIDE"]["dd"]
    cp = row["CURRENT"]["pnl"]; cdd = row["CURRENT"]["dd"]
    grand = bp + bep + cp
    worst_dd = max(bdd, bedd, cdd)
    
    # Check if any period blew up
    blowup_note = ""
    for pk in ["BULL", "BEAR/SIDE", "CURRENT"]:
        if row[pk]["blowup"]:
            blowup_note += f" BLOWUP:{pk[:4]}"
    
    print(f"  {lev:>2}x | {bp:>+9.2f} {bdd:>5.1f}% | {bep:>+9.2f} {bedd:>5.1f}% | {cp:>+8.2f} {cdd:>5.1f}% | {grand:>+8.2f} {worst_dd:>5.1f}% | {blowup_note}")

# Also print per-period detail for each leverage
print(f"\n{'='*90}")
print(f"  DETAILED BREAKDOWN")
print(f"{'='*90}")
for lev in LEVERAGE_VALUES:
    print(f"\n  --- Leverage {lev}x (effective risk = {3*lev}%) ---")
    for pk in ["BULL", "BEAR/SIDE", "CURRENT"]:
        trades = period_trades[pk]
        long_t = [x for x in trades if x.side == 'long']
        short_t = [x for x in trades if x.side == 'short']
        long_pnl = sum(x.pnl_usd for x in long_t) * lev
        short_pnl = sum(x.pnl_usd for x in short_t) * lev
        total_pnl = (sum(x.pnl_usd for x in trades)) * lev
        long_wr = len([x for x in long_t if x.pnl_usd > 0])/len(long_t)*100 if long_t else 0
        short_wr = len([x for x in short_t if x.pnl_usd > 0])/len(short_t)*100 if short_t else 0
        
        peak = BALANCE; balance = BALANCE; max_dd_pct = 0.0
        for t in sorted(trades, key=lambda x: x.entry_time):
            balance += t.pnl_usd * lev
            if balance > peak: peak = balance
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd > max_dd_pct: max_dd_pct = dd
        
        print(f"  {pk:10s}  LONG: {len(long_t):>3} WR={long_wr:.1f}% PnL=${long_pnl:+.2f}  |  SHORT: {len(short_t):>3} WR={short_wr:.1f}% PnL=${short_pnl:+.2f}  |  TOTAL: ${total_pnl:+.2f}  DD={max_dd_pct:.1f}%")

bt_mod.MIN_PNL_CHECK_H = 48; bt_mod.START_BALANCE = 10000.0; bt_mod.BASE_RISK_PCT = 1.0
bt_mod.SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","LINKUSDT","DOGEUSDT","AVAXUSDT","ADAUSDT","SUIUSDT"]
bt_mod.USE_P34_LONG_EXIT = False; bt_mod.SL_ATR_MULT_LONG = 1.5