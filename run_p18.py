#!/usr/bin/env python3
"""P18: Remove 3 weakest coins (SOL, AVAX, ADA), rest = P17"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester
backtester.MIN_PNL_CHECK_H = 72
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 1000.0
backtester.BASE_RISK_PCT = 2.0

# P17 params
import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0

# P18: Remove SOLUSDT, AVAXUSDT, ADAUSDT
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT"]

from backtester import Backtester
SYMBOLS_7 = backtester.SYMBOLS
from datetime import datetime, timezone, timedelta
import requests, pandas as pd
from collections import Counter

DAYS = 180
END = datetime.now(timezone.utc) - timedelta(minutes=5)
START = END - timedelta(days=DAYS)
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

def fetch1d_binance(sym, start_dt, end_dt):
    return fetch_binance(sym, start_dt, end_dt, interval="1d")

print(f"P18: 7 coins (removed SOL, AVAX, ADA) | {DAYS} days, 4H, $1000, risk=2%")
print(f"Period: {START.date()} -> {END.date()}")
print(f"Coins: {SYMBOLS_7}")
print(f"MAX_SIMULTANEOUS_TRADES = 4 (current setting)")
print(f"Baseline P17 (10 coins): Trades 200 | WR 67.0% | PnL +$352.44 (+35.24%) | PF 1.40\n")

print("Loading data...")
btc_df = fetch_binance("BTCUSDT", START, END)
btc_1d = fetch1d_binance("BTCUSDT", START, END)
print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d)} candles")

data = {"BTCUSDT": btc_df}
for s in SYMBOLS_7[1:]:
    data[s] = fetch_binance(s, START, END)
    time.sleep(0.3)
    print(f"  {s}: {len(data[s])} candles")

print(f"\n{'='*100}")
print(f"  P18: 7 coins (no SOL/AVAX/ADA), TSL=2.0ATR, MIN_PNL_TIMEOUT=72h, $1000, risk=2%")
print(f"{'='*100}")
print(f"{'Symbol':<12} {'Trades':>6} {'WR%':>6} {'PnL$':>10} {'PnL%':>8} {'PF':>6} {'Step0%':>7} {'AvgWin':>8} {'AvgLoss':>9}")
print(f"{'-'*100}")

all_trades = []
per_symbol = {}

for s in SYMBOLS_7:
    df = data.get(s)
    if df is None or df.empty or len(df) < 100: continue
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

# Show removed coins' contribution
print(f"\nRemoved coins (P17 data):")
print(f"  SOLUSDT:  -$9.92 (PF 0.93)")
print(f"  AVAXUSDT:  -$8.39 (PF 0.94)")
print(f"  ADAUSDT:  -$32.38 (PF 0.78)")
print(f"  Total removed: -$50.69 (drag on portfolio)")

# Restore defaults
backtester.MIN_PNL_CHECK_H = 48
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 10000.0
backtester.BASE_RISK_PCT = 1.0
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]
ep.TSL_AFTER_TP2_ATR = 1.5