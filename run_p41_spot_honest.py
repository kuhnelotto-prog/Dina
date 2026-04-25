#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P41: HONEST SPOT comparison — same P24 params, just no shorts & no funding."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'P41_SPOT_FINAL'))
import backtester as bt_mod

# ── Use EXACT P24 baseline params (same as the profitable futures test) ──
bt_mod.START_BALANCE = 10000.0
bt_mod.BASE_RISK_PCT = 1.0
bt_mod.USE_P34_LONG_EXIT = False       # P24 baseline (TRAILING_STAGES)
bt_mod.SL_ATR_MULT_LONG = 1.5          # P24 baseline (same as SHORT)
bt_mod.MIN_PNL_CHECK_H = 48
bt_mod.MIN_EXPECTED_PNL_PCT = -0.5

SYMBOLS_10 = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","LINKUSDT","DOGEUSDT","AVAXUSDT","ADAUSDT","SUIUSDT"]
bt_mod.SYMBOLS = SYMBOLS_10

from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd
from collections import Counter

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
print("HONEST SPOT COMPARISON: P24 params, LONG only, no funding, no shorts")
print("Params: SL_ATR=1.5, BASE_RISK=1%, USE_P34=False, START=$10000")
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
        if sym_df is None or sym_df.empty or len(sym_df) < 50: continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        all_trades.extend(res.trades)
    
    long_t = [x for x in all_trades if x.side == 'long']
    short_t = [x for x in all_trades if x.side == 'short']
    long_pnl = sum(x.pnl_usd for x in long_t)
    short_pnl = sum(x.pnl_usd for x in short_t)
    total_pnl = sum(x.pnl_usd for x in all_trades)
    
    all_trades.sort(key=lambda x: x.entry_time)
    peak = BALANCE; balance = BALANCE; max_dd = 0
    for t in all_trades:
        balance += t.pnl_usd
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
    
    long_wr = len([x for x in long_t if x.pnl_usd > 0])/len(long_t)*100 if long_t else 0
    long_sl = Counter(getattr(x, 'exit_reason', '') for x in long_t)
    
    print(f"\n  {pk:10s}  LONG: {len(long_t):>3} WR={long_wr:.1f}% PnL=${long_pnl:+.2f}  |  SHORT: {len(short_t):>3} PnL=${short_pnl:+.2f}  |  TOTAL: ${total_pnl:+.2f}  DD={max_dd:.1f}%")
    if long_t:
        for reason, count in long_sl.most_common():
            reason_pnl = sum(x.pnl_usd for x in long_t if getattr(x, 'exit_reason', '') == reason)
            print(f"    LONG exits: {reason:15s}: {count:3d} ({count/len(long_t)*100:.1f}%) PnL=${reason_pnl:+.2f}")
    
    grand_pnl += total_pnl
    grand_dd = max(grand_dd, max_dd)

print(f"\n{'='*80}")
print(f"  GRAND TOTAL (SPOT LONG-only, P24 params): ${grand_pnl:+.2f}  MaxDD={grand_dd:.1f}%")
print(f"{'='*80}")

# Reset to defaults
bt_mod.MIN_PNL_CHECK_H = 48; bt_mod.START_BALANCE = 10000.0; bt_mod.BASE_RISK_PCT = 1.0
bt_mod.SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","LINKUSDT","DOGEUSDT","AVAXUSDT","ADAUSDT","SUIUSDT"]
bt_mod.USE_P34_LONG_EXIT = False; bt_mod.SL_ATR_MULT_LONG = 1.5