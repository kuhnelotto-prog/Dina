#!/usr/bin/env python3
"""P29c CURRENT period only - quick run."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester as bt_mod
bt_mod.MIN_PNL_CHECK_H = 72
bt_mod.MIN_EXPECTED_PNL_PCT = -0.5
bt_mod.START_BALANCE = 1000.0
bt_mod.BASE_RISK_PCT = 2.0
bt_mod.LEVERAGE = 1
bt_mod.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "LINKUSDT",
                  "DOGEUSDT", "SUIUSDT", "RUNEUSDT", "OPUSDT", "INJUSDT",
                  "TIAUSDT", "ATOMUSDT"]
import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0
ep.LONG_THRESHOLD_BULL = 0.40
ep.LONG_THRESHOLD_BEAR = 0.45
ep.SHORT_THRESHOLD_BULL = 0.45
ep.SHORT_THRESHOLD_BEAR = 0.35
bt_mod.LONG_THRESHOLD_BULL = 0.40
bt_mod.LONG_THRESHOLD_BEAR = 0.45
bt_mod.SHORT_THRESHOLD_BULL = 0.45
bt_mod.SHORT_THRESHOLD_BEAR = 0.35
bt_mod.TSL_AFTER_TP2_ATR = 2.0
bt_mod.ADX_THRESHOLD = 20
bt_mod.SL_ATR_MULT = 3.0
bt_mod.TP1_ATR_MULT = 1.0
bt_mod.TP1_CLOSE_PCT = 0.30
bt_mod.TP2_ATR_MULT = 2.0
bt_mod.TP2_CLOSE_PCT = 0.30
bt_mod.TSL_FROM_ENTRY_ATR = 1.5
bt_mod.MAX_SIMULTANEOUS_TRADES = 4

# P29c combo filter
bt_mod.SLOPE_FILTER_1D = False
bt_mod.REGIME_LOCK_4H = False
bt_mod.COMBO_FILTER = True

from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd

SYMBOLS = bt_mod.SYMBOLS
start_dt = datetime(2025, 10, 1, tzinfo=timezone.utc)
end_dt = datetime(2026, 4, 17, tzinfo=timezone.utc)

def fetch_binance(sym, start, end, interval="4h"):
    all_c = []
    st = int(start.timestamp() * 1000)
    et = int(end.timestamp() * 1000)
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
    df = pd.DataFrame(all_c, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

print("Fetching CURRENT data for P29c...")
btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
dfs = {"BTCUSDT": btc_df}
for s in SYMBOLS[1:]:
    dfs[s] = fetch_binance(s, start_dt, end_dt)
    time.sleep(0.15)

print(f"BTC 4H={len(btc_df)} 1D={len(btc_1d)}")

all_trades = []
for s in SYMBOLS:
    df = dfs.get(s)
    if df is None or df.empty or len(df) < 50:
        continue
    bt = Backtester(initial_balance=1000.0)
    res = bt.run(dfs=dfs, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
    all_trades.extend(res.trades)

t = len(all_trades)
if t == 0:
    print("NO TRADES")
else:
    long_t = [x for x in all_trades if getattr(x, "side", "") == "long"]
    short_t = [x for x in all_trades if getattr(x, "side", "") == "short"]
    total_pnl = sum(x.pnl_usd for x in all_trades)
    long_pnl = sum(x.pnl_usd for x in long_t)
    short_pnl = sum(x.pnl_usd for x in short_t)
    long_wr = len([x for x in long_t if x.pnl_usd > 0]) / len(long_t) * 100 if long_t else 0
    short_wr = len([x for x in short_t if x.pnl_usd > 0]) / len(short_t) * 100 if short_t else 0
    total_wr = len([x for x in all_trades if x.pnl_usd > 0]) / t * 100
    print(f"\n  CURRENT P29c (combo)")
    print(f"  Trades: {t} | WR: {total_wr:.1f}% | PnL: ${total_pnl:+.2f} ({total_pnl/1000*100:+.1f}%)")
    print(f"  LONG:  {len(long_t):>3} trades | WR={long_wr:.1f}% | PnL=${long_pnl:+.2f}")
    print(f"  SHORT: {len(short_t):>3} trades | WR={short_wr:.1f}% | PnL=${short_pnl:+.2f}")
    p24 = (13, -5.29, 208, 823.57)
    ld = len(long_t) - p24[0]
    lpd = long_pnl - p24[1]
    spd = short_pnl - p24[3]
    print(f"  --- P24 CURRENT ---")
    print(f"  P24 LONG:  {p24[0]:>3} trades | PnL=${p24[1]:+.2f}")
    print(f"  P24 SHORT: {p24[2]:>3} trades | PnL=${p24[3]:+.2f}")
    print(f"  LONG delta:  {ld:+d} trades | PnL ${lpd:+.2f}")
    print(f"  SHORT delta: PnL ${spd:+.2f}")
    print(f"  TOTAL delta: PnL ${lpd+spd:+.2f}")