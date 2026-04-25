#!/usr/bin/env python3
"""P14-V1 regime test: 3 historical periods, risk=2%, $1000 deposit, MIN_PNL_TIMEOUT=72h"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester
backtester.MIN_PNL_CHECK_H = 72
backtester.START_BALANCE = 1000.0
backtester.BASE_RISK_PCT = 2.0

from backtester import Backtester, SYMBOLS
from datetime import datetime, timezone, timedelta
import requests, pandas as pd
from collections import Counter

PERIODS = [
    {"name": "BULL (2023-10 -> 2024-03)", "start": "2023-10-01", "end": "2024-03-31"},
    {"name": "BEAR (2022-04 -> 2022-10)", "start": "2022-04-01", "end": "2022-10-31"},
    {"name": "SIDEWAYS (2024-05 -> 2024-09)", "start": "2024-05-01", "end": "2024-09-30"},
]
BALANCE = 1000.0

def fetch_binance(sym, start_dt, end_dt, interval="4h"):
    """Fetch candles from Binance USDT-Futures API with forward pagination."""
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

def fetch1d(sym, start_dt, end_dt, limit=500):
    """Fetch 1D candles from Binance."""
    return fetch_binance(sym, start_dt, end_dt, interval="1d")

def run_period(period, data, btc_df, btc_1d):
    print(f"\n{'='*100}")
    print(f"  {period['name']}")
    print(f"{'='*100}")
    print(f"{'Symbol':<12} {'Trades':>6} {'WR%':>6} {'PnL$':>10} {'PF':>6} {'Step0%':>7}")
    print(f"{'-'*50}")

    all_trades = []
    per_symbol = {}

    for s in SYMBOLS:
        df = data.get(s)
        if df is None or df.empty or len(df) < 50:
            print(f"{s:<12} SKIP (<50 candles)")
            continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs={s: df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        trades = res.trades
        all_trades.extend(trades)
        t = len(trades)
        if t == 0:
            print(f"{s:<12} 0 trades")
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
        per_symbol[s] = {"t": t, "wr": wr, "pnl_usd": pnl_usd, "pf": pf, "step0_pct": step0_pct}
        print(f"{s:<12} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pf:>6.2f} {step0_pct:>7.1f}")

    t = len(all_trades)
    if t == 0:
        print("  NO TRADES")
        return
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

    print(f"{'-'*50}")
    print(f"{'TOTAL':<12} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pf:>6.2f} {step0_pct:>7.1f}")
    print(f"  PnL%: {pnl_pct:+.2f}% | AvgWin: ${avg_win:+.2f} | AvgLoss: ${avg_loss:+.2f}")
    print(f"\nEXIT REASONS:")
    for reason, count in reasons.most_common():
        r_pnl = sum(x.pnl_usd for x in all_trades if getattr(x, 'exit_reason', '') == reason)
        print(f"  {reason:20s}: {count:3d} ({count/t*100:5.1f}%) | PnL: ${r_pnl:+.2f}")

def main():
    print(f"P14-V1 REGIME TEST | risk=2%, $1000, MIN_PNL_TIMEOUT=72h")
    print(f"Params: ADX=20, SL=3.0ATR, TP1=+1ATR/30%, TP2=+2ATR/30%, TSL=1.5ATR after TP2")
    print(f"        Close confirmation filter, BTC 1D EMA50 regime")
    print()

    for period in PERIODS:
        start_dt = datetime.strptime(period["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(period["end"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)

        print(f"\nFetching data for {period['name']}...")
        btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
        btc_1d = fetch1d("BTCUSDT", start_dt, end_dt)
        print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d)} candles")

        data = {"BTCUSDT": btc_df}
        for s in SYMBOLS[1:]:
            data[s] = fetch_binance(s, start_dt, end_dt)
            time.sleep(0.3)
            print(f"  {s}: {len(data[s])} candles")

        run_period(period, data, btc_df, btc_1d)

    # Restore defaults
    backtester.MIN_PNL_CHECK_H = 48
    backtester.START_BALANCE = 10000.0
    backtester.BASE_RISK_PCT = 1.0

if __name__ == "__main__":
    main()