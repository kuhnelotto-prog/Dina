#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P39b: SL=5.0, 5.5, 6.0, 6.5, 7.0."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt_mod
bt_mod.MIN_PNL_CHECK_H = 72; bt_mod.MIN_EXPECTED_PNL_PCT = -0.5; bt_mod.START_BALANCE = 1000.0; bt_mod.BASE_RISK_PCT = 2.0
SYMBOLS_12 = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT", "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT", "ATOMUSDT"]
bt_mod.SYMBOLS = SYMBOLS_12
import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0; ep.LONG_THRESHOLD_BULL = 0.40; ep.LONG_THRESHOLD_BEAR = 0.45; ep.SHORT_THRESHOLD_BULL = 0.45; ep.SHORT_THRESHOLD_BEAR = 0.35
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

SWEEP_VALUES = [5.0, 5.5, 6.0, 6.5, 7.0]
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

def run_one(pk, dfs, btc_df, btc_1d, sl_mult):
    bt_mod.USE_P34_LONG_EXIT = True; bt_mod.SL_ATR_MULT_LONG = sl_mult
    bt_mod.TP1_ATR_MULT = 1.0; bt_mod.TP1_CLOSE_PCT = 0.30; bt_mod.TP2_ATR_MULT = 2.0; bt_mod.TP2_CLOSE_PCT = 0.30; bt_mod.TSL_ATR_LONG = 2.0
    all_trades = []
    for s in SYMBOLS_12:
        sym_df = dfs.get(s)
        if sym_df is None or sym_df.empty or len(sym_df) < 50: continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        all_trades.extend(res.trades)
    t = len(all_trades)
    if t == 0: return None
    long_t = [x for x in all_trades if getattr(x, 'side', '') == 'long']
    short_t = [x for x in all_trades if getattr(x, 'side', '') == 'short']
    long_pnl = sum(x.pnl_usd for x in long_t); short_pnl = sum(x.pnl_usd for x in short_t)
    long_wr = len([x for x in long_t if x.pnl_usd > 0])/len(long_t)*100 if long_t else 0
    short_wr = len([x for x in short_t if x.pnl_usd > 0])/len(short_t)*100 if short_t else 0
    total_pnl = sum(x.pnl_usd for x in all_trades)
    lr = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in long_t)
    sr = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in short_t)
    return {"total": t, "pnl": total_pnl, "long_n": len(long_t), "long_wr": long_wr, "long_pnl": long_pnl,
            "short_n": len(short_t), "short_wr": short_wr, "short_pnl": short_pnl,
            "long_sl_rate": lr.get('SL',0)/len(long_t)*100 if long_t else 0,
            "short_sl_rate": sr.get('SL',0)/len(short_t)*100 if short_t else 0}

results = {}
for sl_val in SWEEP_VALUES:
    print(f"\n{'='*90}\n  SL_ATR_MULT_LONG = {sl_val}\n{'='*90}")
    results[sl_val] = {}
    for pk, start_dt, end_dt in PERIODS:
        dfs, btc_df, btc_1d = data_cache[pk]
        r = run_one(pk, dfs, btc_df, btc_1d, sl_val)
        if r is None: print(f"  {pk}: NO TRADES"); continue
        results[sl_val][pk] = r
        print(f"  {pk:10s}  LONG: {r['long_n']:>3} WR={r['long_wr']:.1f}% PnL=${r['long_pnl']:+.2f} SL-hit={r['long_sl_rate']:.1f}%  |  SHORT: {r['short_n']:>3} WR={r['short_wr']:.1f}% PnL=${r['short_pnl']:+.2f}  |  TOTAL: {r['total']:>3} PnL=${r['pnl']:+.2f}")
    grand = sum(r['pnl'] for r in results[sl_val].values())
    print(f"  GRAND TOTAL: ${grand:+.2f}")

# COMPLETE TABLE
print(f"\n{'='*90}")
print(f"  COMPLETE SL_ATR_MULT_LONG SWEEP TABLE")
print(f"{'='*90}")
print(f"  {'SL':>3s} | {'BULL_PnL':>9s} {'SL%':>5s} | {'BEAR_PnL':>9s} {'SL%':>5s} | {'CUR_PnL':>8s} {'SL%':>5s} | {'GRAND':>8s}")
print(f"  " + "-" * 70)
all_data = [
    (1.5, -652.86, 39.6, -219.96, 40.6, 432.80, 36.7),
    (2.0, -516.29, 37.2, 160.09, 39.4, 335.35, 40.0),
    (2.5, -429.49, 29.1, 103.52, 33.0, 352.34, 31.1),
    (3.0, -427.70, 24.0, 82.96, 25.2, 342.16, 26.0),
    (3.2, -443.42, 22.4, 189.28, 22.0, 400.36, 20.8),
    (3.3, -388.21, 19.8, 160.32, 22.0, 369.86, 20.8),
    (3.4, -327.89, 18.8, 151.83, 21.6, 347.99, 19.4),
    (3.5, -299.53, 17.5, 175.76, 21.1, 340.53, 19.4),
    (3.6, -295.12, 17.2, 210.91, 18.8, 354.84, 16.7),
    (3.7, -301.71, 16.2, 204.17, 18.3, 340.23, 15.3),
    (3.8, -281.85, 15.6, 185.28, 17.4, 334.88, 15.3),
    (3.9, -227.65, 13.9, 221.68, 16.6, 329.76, 15.3),
    (4.0, -224.27, 13.2, 219.19, 15.7, 322.10, 13.9),
    (4.1, -202.38, 12.3, 242.58, 13.4, 317.73, 13.9),
    (4.2, -138.20, 11.0, 265.65, 12.0, 313.57, 13.9),
    (4.3, -131.54, 10.3, 252.02, 10.6, 309.60, 13.9),
    (4.4, -130.25, 10.0, 232.55, 9.7, 306.59, 13.9),
    (4.5, -137.94, 10.0, 227.88, 9.7, 294.90, 12.5),
    (4.6, -136.69, 9.6, 225.78, 9.2, 298.17, 9.7),
    (4.7, -109.36, 9.0, 251.30, 8.8, 314.53, 8.3),
]
for sl, bp, bsl, bep, besl, cp, csl in all_data:
    grand = bp + bep + cp
    marker = " <---" if sl == 4.7 else ""
    print(f"  {sl:>3.1f} | {bp:>+9.2f} {bsl:>4.1f}% | {bep:>+9.2f} {besl:>4.1f}% | {cp:>+8.2f} {csl:>4.1f}% | {grand:>+8.2f}{marker}")
for sl_val in SWEEP_VALUES:
    r = results.get(sl_val, {})
    b = r.get('BULL', {}); be = r.get('BEAR/SIDE', {}); c = r.get('CURRENT', {})
    bp = b.get('pnl', 0); bsl = b.get('long_sl_rate', 0)
    bep = be.get('pnl', 0); besl = be.get('long_sl_rate', 0)
    cp = c.get('pnl', 0); csl = c.get('long_sl_rate', 0)
    grand = bp + bep + cp
    print(f"  {sl_val:>3.1f} | {bp:>+9.2f} {bsl:>4.1f}% | {bep:>+9.2f} {besl:>4.1f}% | {cp:>+8.2f} {csl:>4.1f}% | {grand:>+8.2f}")

bt_mod.MIN_PNL_CHECK_H = 48; bt_mod.START_BALANCE = 10000.0; bt_mod.BASE_RISK_PCT = 1.0
bt_mod.SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","LINKUSDT","DOGEUSDT","AVAXUSDT","ADAUSDT","SUIUSDT"]
bt_mod.USE_P34_LONG_EXIT = False; bt_mod.SL_ATR_MULT_LONG = 1.5