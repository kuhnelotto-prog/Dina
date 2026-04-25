#!/usr/bin/env python3
"""P37 MaxDD diagnosis: break down by period, compare P35 baseline vs CVD=0."""
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

def fetch_binance_with_cvd(sym, start_dt, end_dt, interval="4h"):
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

def fetch_binance_no_cvd(sym, start_dt, end_dt, interval="4h"):
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

# Fetch both datasets
print("Fetching data WITH taker_buy_vol...")
data_cvd = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance_with_cvd("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance_with_cvd("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    for s in SYMBOLS_12[1:]: df = fetch_binance_with_cvd(s, start_dt, end_dt); time.sleep(0.15); dfs[s] = df
    data_cvd[pk] = (dfs, btc_df, btc_1d)
    print(f"  {pk}: OK")

print("\nFetching data WITHOUT taker_buy_vol...")
data_no_cvd = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance_no_cvd("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance_no_cvd("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    for s in SYMBOLS_12[1:]: df = fetch_binance_no_cvd(s, start_dt, end_dt); time.sleep(0.15); dfs[s] = df
    data_no_cvd[pk] = (dfs, btc_df, btc_1d)
    print(f"  {pk}: OK")

def run_variant(label, cvd_weight, data, use_cvd):
    importlib.reload(bt_mod)
    bt_mod.SYMBOLS = SYMBOLS_12
    bt_mod.CVD_WEIGHT_LONG = cvd_weight
    bt_mod.CVD_LOOKBACK = 20
    from backtester import Backtester as BT
    
    results = {}
    for pk, start_dt, end_dt in PERIODS:
        dfs, btc_df, btc_1d = data[pk]
        all_trades = []
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
        
        # Calculate MaxDD properly (cumulative PnL curve)
        cum = 0; peak = 0; max_dd = 0
        for t in sorted(all_trades, key=lambda x: x.exit_time if hasattr(x, 'exit_time') else 0):
            cum += t.pnl_usd
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd
        
        results[pk] = {
            'total_pnl': total_pnl,
            'long_pnl': long_pnl,
            'short_pnl': short_pnl,
            'long_n': len(long_trades),
            'short_n': len(short_trades),
            'max_dd': max_dd,
            'max_dd_pct': max_dd / BALANCE * 100,
        }
    
    # Also calculate combined MaxDD (as in original sweep)
    all_trades_combined = []
    for pk in ["BULL", "BEAR/SIDE", "CURRENT"]:
        dfs, btc_df, btc_1d = data[pk]
        for s in SYMBOLS_12:
            sym_df = dfs.get(s)
            if sym_df is None or len(sym_df) < 50: continue
            bt = BT(initial_balance=BALANCE)
            res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
            all_trades_combined.extend(res.trades)
    
    cum = 0; peak = 0; max_dd = 0
    for t in sorted(all_trades_combined, key=lambda x: x.exit_time if hasattr(x, 'exit_time') else 0):
        cum += t.pnl_usd
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd
    
    results['COMBINED'] = {'max_dd': max_dd, 'max_dd_pct': max_dd / BALANCE * 100}
    
    return results

# Run 4 variants
print("\n" + "="*80)
print("DIAGNOSIS: MaxDD breakdown by period")
print("="*80)

variants = [
    ("P35_NO_CVD_DATA", 0.0, data_no_cvd, False),   # No taker_buy_vol column at all
    ("P37_CVD=0_WITH_CVD_DATA", 0.0, data_cvd, True),  # Has taker_buy_vol but CVD disabled
    ("P37_CVD=0.3", 0.3, data_cvd, True),
]

for label, cvd_w, data, use_cvd in variants:
    print(f"\n--- {label} ---")
    res = run_variant(label, cvd_w, data, use_cvd)
    for pk in ["BULL", "BEAR/SIDE", "CURRENT"]:
        r = res[pk]
        print(f"  {pk:12s}: PnL=${r['total_pnl']:+.2f} (L=${r['long_pnl']:+.2f} S=${r['short_pnl']:+.2f}) MaxDD=${r['max_dd']:.2f} ({r['max_dd_pct']:.1f}%) L={r['long_n']} S={r['short_n']}")
    c = res['COMBINED']
    print(f"  {'COMBINED':12s}: MaxDD=${c['max_dd']:.2f} ({c['max_dd_pct']:.1f}%)")

print("\n" + "="*80)
print("ANALYSIS: If P35_NO_CVD_DATA shows ~21% DD but P37_CVD=0 shows 56%,")
print("the difference is likely due to the CVD data column presence affecting")
print("the backtest (or the combined DD calculation method).")
print("="*80)