#!/usr/bin/env python3
"""P19B: Screen round 2 candidates for third slot"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester
backtester.MIN_PNL_CHECK_H = 72
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 1000.0
backtester.BASE_RISK_PCT = 2.0

import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0

from backtester import Backtester
from datetime import datetime, timezone, timedelta
import requests, pandas as pd
from collections import Counter

CANDIDATES = ["ATOMUSDT", "TONUSDT", "FTMUSDT", "RUNEUSDT",
              "1000PEPEUSDT", "WIFUSDT", "LDOUSDT", "TIAUSDT",
              "STXUSDT", "RENDERUSDT"]
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

print(f"P19B: Screen round 2 candidates | {DAYS} days, 4H, $1000, risk=2%")
print(f"Candidates: {CANDIDATES}")
print(f"Criteria: PF > 1.40, SL < 20%")
print(f"P19 winners: INJUSDT PF=1.93, OPUSDT PF=2.17\n")

print("Loading BTC data...")
btc_df = fetch_binance("BTCUSDT", START, END)
btc_1d = fetch1d_binance("BTCUSDT", START, END)
print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d)} candles\n")

print(f"{'Symbol':<16} {'Trades':>6} {'WR%':>6} {'PnL$':>10} {'PF':>6} {'Step0%':>7} {'TSL%':>6} {'SL%':>6} {'Verdict':>10}")
print(f"{'-'*85}")

results = {}

for sym in CANDIDATES:
    df = fetch_binance(sym, START, END)
    time.sleep(0.3)
    
    if df.empty or len(df) < 100:
        print(f"{sym:<16} SKIP ({len(df)} candles)")
        continue
    
    backtester.SYMBOLS = [sym]
    bt = Backtester(initial_balance=BALANCE)
    res = bt.run(dfs={sym: df, "BTCUSDT": btc_df}, symbols=[sym], btc_df=btc_df, btc_1d_df=btc_1d)
    trades = res.trades
    t = len(trades)
    if t == 0:
        print(f"{sym:<16} 0 trades (ADX filtered)")
        results[sym] = {"t": 0, "pf": 0, "verdict": "FAIL"}
        continue
    
    wins = [x for x in trades if x.pnl_usd > 0]
    losses = [x for x in trades if x.pnl_usd <= 0]
    wr = len(wins)/t*100
    pnl_usd = sum(x.pnl_usd for x in trades)
    sum_w = sum(x.pnl_usd for x in wins)
    sum_l = abs(sum(x.pnl_usd for x in losses))
    pf = sum_w/sum_l if sum_l > 0 else 0
    step0 = sum(1 for x in trades if x.trailing_step == 0)
    step0_pct = step0/t*100
    
    reasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in trades)
    tsl_pct = reasons.get('TSL', 0)/t*100
    sl_pct = reasons.get('SL', 0)/t*100
    
    verdict = "PASS" if pf > 1.40 and sl_pct < 20 else "FAIL"
    results[sym] = {"t": t, "wr": wr, "pnl_usd": pnl_usd, "pf": pf, "step0_pct": step0_pct,
                     "tsl_pct": tsl_pct, "sl_pct": sl_pct, "verdict": verdict}
    
    print(f"{sym:<16} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pf:>6.2f} {step0_pct:>7.1f} {tsl_pct:>6.1f} {sl_pct:>6.1f} {verdict:>10}")

# Summary
print(f"\n{'='*85}")
print(f"  PASSED (PF > 1.40, SL < 20%):")
passed = [(s, r) for s, r in results.items() if r["verdict"] == "PASS"]
for s, r in sorted(passed, key=lambda x: -x[1]["pf"]):
    print(f"    {s:<16} PF={r['pf']:.2f}  WR={r['wr']:.1f}%  PnL=${r['pnl_usd']:+.2f}  SL={r['sl_pct']:.1f}%")

print(f"\n  FAILED:")
failed = [(s, r) for s, r in results.items() if r["verdict"] != "PASS"]
for s, r in failed:
    if r["t"] == 0:
        print(f"    {s:<16} NO TRADES")
    else:
        reason = "PF" if r['pf'] <= 1.40 else ""
        if r['sl_pct'] >= 20: reason += "+SL%" if reason else "SL%"
        print(f"    {s:<16} PF={r['pf']:.2f}  WR={r['wr']:.1f}%  PnL=${r['pnl_usd']:+.2f}  SL={r['sl_pct']:.1f}%  ({reason})")

# Restore
backtester.MIN_PNL_CHECK_H = 48
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 10000.0
backtester.BASE_RISK_PCT = 1.0
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]
ep.TSL_AFTER_TP2_ATR = 1.5