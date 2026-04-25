#!/usr/bin/env python3
"""P21: Multi-period validation — 12 coins, 3 periods"""
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
from datetime import datetime, timezone, timedelta
import requests, pandas as pd
from collections import Counter

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

print(f"P21: Multi-period validation | 12 coins, $1000, risk=2%")
print(f"Baseline P20: Trades 232 | WR 70.7% | PnL +$715.78 (+71.58%) | PF 1.85\n")

# Cache data to avoid re-fetching
data_cache = {}

for period_name, start_dt, end_dt in PERIODS:
    print(f"\n{'='*110}")
    print(f"  {period_name}")
    print(f"{'='*110}")
    
    # Fetch BTC data for this period
    cache_key_btc = f"BTC_{start_dt.date()}_{end_dt.date()}"
    if cache_key_btc not in data_cache:
        print(f"Fetching data for {period_name}...")
        btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
        btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
        print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d)} candles")
        data_cache[cache_key_btc] = (btc_df, btc_1d)
    else:
        btc_df, btc_1d = data_cache[cache_key_btc]
    
    # Fetch all symbol data
    sym_data = {"BTCUSDT": btc_df}
    for s in SYMBOLS_12[1:]:
        cache_key = f"{s}_{start_dt.date()}_{end_dt.date()}"
        if cache_key not in data_cache:
            df = fetch_binance(s, start_dt, end_dt)
            time.sleep(0.2)
            data_cache[cache_key] = df
            print(f"  {s}: {len(df)} candles")
        else:
            df = data_cache[cache_key]
        sym_data[s] = df
    
    print(f"\n{'Symbol':<16} {'Trades':>6} {'WR%':>6} {'PnL$':>10} {'PnL%':>8} {'PF':>6} {'Step0%':>7} {'SL%':>6}")
    print(f"{'-'*80}")
    
    all_trades = []
    
    for s in SYMBOLS_12:
        df = sym_data.get(s)
        if df is None or df.empty or len(df) < 50: continue
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
        reasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in trades)
        sl_pct = reasons.get('SL', 0)/t*100
        print(f"{s:<16} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pnl_pct:>+8.3f} {pf:>6.2f} {step0_pct:>7.1f} {sl_pct:>6.1f}")
    
    t = len(all_trades)
    if t == 0:
        print("  No trades")
        continue
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
    
    print(f"{'-'*80}")
    print(f"{'TOTAL':<16} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pnl_pct:>+8.3f} {pf:>6.2f} {step0_pct:>7.1f}")
    print(f"  AvgWin: ${avg_win:+.2f} | AvgLoss: ${avg_loss:+.2f}")
    print(f"\n  EXIT REASONS:")
    for reason, count in reasons.most_common():
        r_pnl = sum(x.pnl_usd for x in all_trades if getattr(x, 'exit_reason', '') == reason)
        print(f"    {reason:20s}: {count:3d} ({count/t*100:5.1f}%) | PnL: ${r_pnl:+.2f}")

# Restore defaults
backtester.MIN_PNL_CHECK_H = 48
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 10000.0
backtester.BASE_RISK_PCT = 1.0
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]
ep.TSL_AFTER_TP2_ATR = 1.5