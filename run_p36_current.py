#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SL=3.6 CURRENT only."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt_mod
bt_mod.MIN_PNL_CHECK_H = 72
bt_mod.MIN_EXPECTED_PNL_PCT = -0.5
bt_mod.START_BALANCE = 1000.0
bt_mod.BASE_RISK_PCT = 2.0
bt_mod.USE_P34_LONG_EXIT = True
bt_mod.SL_ATR_MULT_LONG = 3.6
bt_mod.TP1_ATR_MULT = 1.0
bt_mod.TP1_CLOSE_PCT = 0.30
bt_mod.TP2_ATR_MULT = 2.0
bt_mod.TP2_CLOSE_PCT = 0.30
bt_mod.TSL_ATR_LONG = 2.0
SYMBOLS_12 = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
              "LINKUSDT", "DOGEUSDT", "SUIUSDT",
              "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT", "ATOMUSDT"]
bt_mod.SYMBOLS = SYMBOLS_12
import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0
ep.LONG_THRESHOLD_BULL = 0.40
ep.LONG_THRESHOLD_BEAR = 0.45
ep.SHORT_THRESHOLD_BULL = 0.45
ep.SHORT_THRESHOLD_BEAR = 0.35
from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd
from collections import Counter
BALANCE = 1000.0

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

start_dt = datetime(2025,10,1,tzinfo=timezone.utc)
end_dt = datetime(2026,4,17,tzinfo=timezone.utc)

print("Fetching CURRENT data...")
btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
dfs = {"BTCUSDT": btc_df}
for s in SYMBOLS_12[1:]:
    df = fetch_binance(s, start_dt, end_dt)
    time.sleep(0.15)
    dfs[s] = df

all_trades = []
for s in SYMBOLS_12:
    sym_df = dfs.get(s)
    if sym_df is None or sym_df.empty or len(sym_df) < 50: continue
    bt = Backtester(initial_balance=BALANCE)
    res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
    all_trades.extend(res.trades)

long_t = [x for x in all_trades if getattr(x, 'side', '') == 'long']
short_t = [x for x in all_trades if getattr(x, 'side', '') == 'short']
long_pnl = sum(x.pnl_usd for x in long_t)
short_pnl = sum(x.pnl_usd for x in short_t)
total_pnl = sum(x.pnl_usd for x in all_trades)
long_wr = len([x for x in long_t if x.pnl_usd > 0])/len(long_t)*100 if long_t else 0
long_reasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in long_t)
long_sl = long_reasons.get('SL', 0)
long_sl_rate = long_sl/len(long_t)*100 if long_t else 0

print(f"\n  SL=3.6 CURRENT:")
print(f"  LONG:  {len(long_t):>3} WR={long_wr:.1f}% PnL=${long_pnl:+.2f} SL-hit={long_sl_rate:.1f}%")
print(f"  SHORT: {len(short_t):>3} PnL=${short_pnl:+.2f}")
print(f"  TOTAL: {len(all_trades):>3} PnL=${total_pnl:+.2f}")
print(f"\n  SL=3.6 GRAND: BULL=-295.12 + BEAR=+210.91 + CURRENT={total_pnl:+.2f} = ${-295.12+210.91+total_pnl:+.2f}")

bt_mod.USE_P34_LONG_EXIT = False
bt_mod.SL_ATR_MULT_LONG = 1.5