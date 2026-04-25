#!/usr/bin/env python3
"""P22-fix: P21 BULL with LONG/SHORT breakdown (side is lowercase)"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester
backtester.MIN_PNL_CHECK_H = 72
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 1000.0
backtester.BASE_RISK_PCT = 2.0

import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0

SYMBOLS_12 = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
              "LINKUSDT", "DOGEUSDT", "SUIUSDT",
              "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT",
              "ATOMUSDT"]
backtester.SYMBOLS = SYMBOLS_12

from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd
from collections import Counter

BALANCE = 1000.0
START_DT = datetime(2023,11,1,tzinfo=timezone.utc)
END_DT = datetime(2024,4,30,tzinfo=timezone.utc)

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

print(f"P21 BULL — LONG vs SHORT breakdown")
print(f"Period: 2023-11-01 -> 2024-04-30 (BULL)\n")

print("Loading data...")
btc_df = fetch_binance("BTCUSDT", START_DT, END_DT)
btc_1d = fetch_binance("BTCUSDT", START_DT, END_DT, interval="1d")
print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d)} candles")

data = {"BTCUSDT": btc_df}
for s in SYMBOLS_12[1:]:
    data[s] = fetch_binance(s, START_DT, END_DT)
    time.sleep(0.2)
    print(f"  {s}: {len(data[s])} candles")

all_trades = []

for s in SYMBOLS_12:
    df = data.get(s)
    if df is None or df.empty or len(df) < 50: continue
    bt = Backtester(initial_balance=BALANCE)
    res = bt.run(dfs={s: df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
    all_trades.extend(res.trades)

# Check side attribute
sample = all_trades[0] if all_trades else None
if sample:
    print(f"\n  Trade side sample: '{getattr(sample, 'side', 'MISSING')}'")
    print(f"  Trade attrs: {[a for a in dir(sample) if not a.startswith('_') and len(a) < 20]}")

# LONG vs SHORT breakdown (lowercase!)
long_trades = [x for x in all_trades if getattr(x, 'side', '') == 'long']
short_trades = [x for x in all_trades if getattr(x, 'side', '') == 'short']
other_trades = [x for x in all_trades if getattr(x, 'side', '') not in ('long', 'short')]

print(f"\n{'='*70}")
print(f"  P21 BULL: LONG vs SHORT BREAKDOWN")
print(f"{'='*70}")
print(f"  Total trades: {len(all_trades)} | LONG: {len(long_trades)} | SHORT: {len(short_trades)} | OTHER: {len(other_trades)}")

for label, trades_list in [("LONG", long_trades), ("SHORT", short_trades)]:
    if not trades_list:
        print(f"\n  {label}: 0 trades")
        continue
    tl = len(trades_list)
    tw = [x for x in trades_list if x.pnl_usd > 0]
    tloss = [x for x in trades_list if x.pnl_usd <= 0]
    twr = len(tw)/tl*100
    tpnl = sum(x.pnl_usd for x in trades_list)
    tsum_w = sum(x.pnl_usd for x in tw)
    tsum_l = abs(sum(x.pnl_usd for x in tloss))
    tpf = tsum_w/tsum_l if tsum_l > 0 else 0
    tstep0 = sum(1 for x in trades_list if x.trailing_step == 0)
    tstep0_pct = tstep0/tl*100
    reasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in trades_list)
    tsl_count = reasons.get('TSL', 0)
    sl_count = reasons.get('SL', 0)
    
    print(f"\n  {label}: {tl} trades | WR={twr:.1f}% | PnL=${tpnl:+.2f} | PF={tpf:.2f} | Step0={tstep0_pct:.1f}%")
    print(f"    TSL: {tsl_count} ({tsl_count/tl*100:.0f}%) | SL: {sl_count} ({sl_count/tl*100:.0f}%)")
    print(f"    Avg Win: ${tsum_w/len(tw):+.2f} | Avg Loss: ${sum(x.pnl_usd for x in tloss)/len(tloss):+.2f}" if tloss else "")
    print(f"    Exit reasons:")
    for reason, count in reasons.most_common():
        r_pnl = sum(x.pnl_usd for x in trades_list if getattr(x, 'exit_reason', '') == reason)
        print(f"      {reason:20s}: {count:3d} ({count/tl*100:5.1f}%) | PnL: ${r_pnl:+.2f}")

# Overall
t = len(all_trades)
wins = [x for x in all_trades if x.pnl_usd > 0]
losses = [x for x in all_trades if x.pnl_usd <= 0]
wr = len(wins)/t*100
pnl_usd = sum(x.pnl_usd for x in all_trades)
pf = sum(x.pnl_usd for x in wins)/abs(sum(x.pnl_usd for x in losses)) if losses else 0
print(f"\n{'='*70}")
print(f"  TOTAL: {t} trades | WR={wr:.1f}% | PnL=${pnl_usd:+.2f} | PF={pf:.2f}")

# Per-symbol breakdown
print(f"\n{'='*70}")
print(f"  PER-SYMBOL: LONG vs SHORT PnL")
print(f"{'='*70}")
print(f"{'Symbol':<16} {'#L':>3} {'L_PnL$':>10} {'L_PF':>6} {'#S':>3} {'S_PnL$':>10} {'S_PF':>6}")
print(f"{'-'*70}")

for s in SYMBOLS_12:
    s_long = [x for x in all_trades if x.symbol == s and getattr(x, 'side', '') == 'long']
    s_short = [x for x in all_trades if x.symbol == s and getattr(x, 'side', '') == 'short']
    
    l_pnl = sum(x.pnl_usd for x in s_long)
    s_pnl = sum(x.pnl_usd for x in s_short)
    
    l_wins = [x for x in s_long if x.pnl_usd > 0]
    l_losses = [x for x in s_long if x.pnl_usd <= 0]
    s_wins = [x for x in s_short if x.pnl_usd > 0]
    s_losses = [x for x in s_short if x.pnl_usd <= 0]
    
    l_pf = sum(x.pnl_usd for x in l_wins)/abs(sum(x.pnl_usd for x in l_losses)) if l_losses else 0
    s_pf = sum(x.pnl_usd for x in s_wins)/abs(sum(x.pnl_usd for x in s_losses)) if s_losses else 0
    
    l_cnt = len(s_long)
    s_cnt = len(s_short)
    
    l_pnl_str = f"{l_pnl:+.2f}" if l_cnt > 0 else "---"
    l_pf_str = f"{l_pf:.2f}" if l_cnt > 0 else "---"
    s_pnl_str = f"{s_pnl:+.2f}" if s_cnt > 0 else "---"
    s_pf_str = f"{s_pf:.2f}" if s_cnt > 0 else "---"
    
    print(f"{s:<16} {l_cnt:>3} {l_pnl_str:>10} {l_pf_str:>6} {s_cnt:>3} {s_pnl_str:>10} {s_pf_str:>6}")

# Restore defaults
backtester.MIN_PNL_CHECK_H = 48
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 10000.0
backtester.BASE_RISK_PCT = 1.0
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]
ep.TSL_AFTER_TP2_ATR = 1.5