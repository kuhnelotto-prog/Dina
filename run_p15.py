#!/usr/bin/env python3
"""P15: risk=4%, $1000, MIN_PNL_TIMEOUT=72h (P14-V1 base)"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester
backtester.MIN_PNL_CHECK_H = 72
backtester.START_BALANCE = 1000.0
backtester.BASE_RISK_PCT = 4.0

from backtester import Backtester, SYMBOLS
from datetime import datetime, timezone, timedelta
import requests, pandas as pd
from collections import Counter

DAYS = 180
END = datetime.now(timezone.utc) - timedelta(minutes=5)
START = END - timedelta(days=DAYS)
BALANCE = 1000.0

def fetch(sym, start_dt, end_dt, gran="4H"):
    all_c = []
    et = int(end_dt.timestamp() * 1000)
    st = int(start_dt.timestamp() * 1000)
    ce = et
    for _ in range(20):
        p = {"symbol": sym, "granularity": gran, "limit": 1000,
             "endTime": ce, "productType": "USDT-FUTURES"}
        r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=p, timeout=30).json()
        if r.get("code") != "00000" or not r.get("data"): break
        for c in r["data"]:
            ts = int(c[0])
            if ts >= st: all_c.append([ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        earliest_ts = int(r["data"][-1][0])
        if earliest_ts <= st or len(r["data"]) < 1000 or earliest_ts >= ce: break
        ce = earliest_ts - 1
        time.sleep(0.15)
    if not all_c: return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

def fetch1d(sym, limit=500):
    p = {"symbol": sym, "granularity": "1D", "limit": limit, "productType": "USDT-FUTURES"}
    r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=p, timeout=30).json()
    if r.get("code") != "00000" or not r.get("data"): return pd.DataFrame()
    rows = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in r["data"]]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

print(f"P15: risk=4%, $1000, MIN_PNL_TIMEOUT=72h | {DAYS} days, 4H, {len(SYMBOLS)} coins")
print(f"Period: {START.date()} -> {END.date()}\n")

print("Loading data...")
btc_df = fetch("BTCUSDT", START, END)
btc_1d = fetch1d("BTCUSDT", limit=500)
print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d)} candles")

data = {"BTCUSDT": btc_df}
for s in SYMBOLS[1:]:
    data[s] = fetch(s, START, END)
    time.sleep(0.3)
    print(f"  {s}: {len(data[s])} candles")

print(f"\n{'='*100}")
print(f"  P15: risk=4%, deposit=$1000, MIN_PNL_TIMEOUT=72h")
print(f"{'='*100}")
print(f"{'Symbol':<12} {'Trades':>6} {'WR%':>6} {'PnL$':>10} {'PnL%':>8} {'PF':>6} {'Step0%':>7} {'AvgWin':>8} {'AvgLoss':>9}")
print(f"{'-'*100}")

all_trades = []
per_symbol = {}

for s in SYMBOLS:
    df = data[s]
    if df.empty or len(df) < 100: continue
    bt = Backtester(initial_balance=BALANCE)
    res = bt.run(dfs={s: df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
    trades = res.trades
    all_trades.extend(trades)
    t = len(trades)
    if t == 0: continue
    wins = [x for x in trades if x.pnl_usd > 0]
    losses = [x for x in trades if x.pnl_usd <= 0]
    wr = len(wins)/t*100
    pnl_usd = sum(x.pnl_usd for x in trades)
    pnl_pct = pnl_usd/BALANCE*100
    sum_w = sum(x.pnl_usd for x in wins)
    sum_l = abs(sum(x.pnl_usd for x in losses))
    pf = sum_w/sum_l if sum_l > 0 else 0
    step0 = sum(1 for x in trades if x.trailing_step == 0)
    step0_pct = step0/t*100
    avg_win = sum_w/len(wins) if wins else 0
    avg_loss = sum(x.pnl_usd for x in losses)/len(losses) if losses else 0
    per_symbol[s] = {"t": t, "wr": wr, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "pf": pf, "step0_pct": step0_pct, "avg_win": avg_win, "avg_loss": avg_loss}
    print(f"{s:<12} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pnl_pct:>+8.3f} {pf:>6.2f} {step0_pct:>7.1f} {avg_win:>+8.2f} {avg_loss:>+9.2f}")

t = len(all_trades)
wins = [x for x in all_trades if x.pnl_usd > 0]
losses = [x for x in all_trades if x.pnl_usd <= 0]
wr = len(wins)/t*100
pnl_usd = sum(x.pnl_usd for x in all_trades)
pnl_pct = pnl_usd/BALANCE*100
sum_w = sum(x.pnl_usd for x in wins)
sum_l = abs(sum(x.pnl_usd for x in losses))
pf = sum_w/sum_l if sum_l > 0 else 0
step0 = sum(1 for x in all_trades if x.trailing_step == 0)
step0_pct = step0/t*100
avg_win = sum_w/len(wins) if wins else 0
avg_loss = sum(x.pnl_usd for x in losses)/len(losses) if losses else 0

reasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in all_trades)

print(f"{'-'*100}")
print(f"{'TOTAL':<12} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pnl_pct:>+8.3f} {pf:>6.2f} {step0_pct:>7.1f} {avg_win:>+8.2f} {avg_loss:>+9.2f}")
print(f"\nEXIT REASONS:")
for reason, count in reasons.most_common():
    r_pnl = sum(x.pnl_usd for x in all_trades if getattr(x, 'exit_reason', '') == reason)
    print(f"  {reason:20s}: {count:3d} ({count/t*100:5.1f}%) | PnL: ${r_pnl:+.2f}")

# Restore
backtester.MIN_PNL_CHECK_H = 48
backtester.START_BALANCE = 10000.0
backtester.BASE_RISK_PCT = 1.0