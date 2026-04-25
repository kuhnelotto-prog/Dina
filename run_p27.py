#!/usr/bin/env python3
"""P27: LEVERAGE=20, DEPOSIT=$100, RISK=2%, P24 thresholds"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester
backtester.MIN_PNL_CHECK_H = 72
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 100.0      # $100 deposit
backtester.BASE_RISK_PCT = 2.0        # 2% risk per trade
backtester.LEVERAGE = 20              # 20x leverage
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
                      "LINKUSDT", "DOGEUSDT", "SUIUSDT",
                      "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT",
                      "ATOMUSDT"]

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

BALANCE = 100.0

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

print(f"P27: LEVERAGE=20, DEPOSIT=$100, RISK=2%")
print(f"P24 baseline (LEV=10, DEP=$1000, RISK=2%): BULL +$173 | BEAR +$20 | CURRENT +$818 | TOTAL +$1012")
print(f"P27 uses same thresholds as P24 but $100 deposit + 20x leverage\n")

data_cache = {}

for period_name, start_dt, end_dt in PERIODS:
    print(f"\n{'='*110}")
    print(f"  {period_name}")
    print(f"{'='*110}")
    
    cache_key_btc = f"BTC_{start_dt.date()}_{end_dt.date()}"
    if cache_key_btc not in data_cache:
        print(f"Fetching data...")
        btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
        btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
        print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d)} candles")
        data_cache[cache_key_btc] = (btc_df, btc_1d)
    else:
        btc_df, btc_1d = data_cache[cache_key_btc]
    
    sym_data = {"BTCUSDT": btc_df}
    for s in backtester.SYMBOLS[1:]:
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
    
    for s in backtester.SYMBOLS:
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
    if t == 0: continue
    wins = [x for x in all_trades if x.pnl_usd > 0]
    losses = [x for x in all_trades if x.pnl_usd <= 0]
    wr = len(wins)/t*100
    pnl_usd = sum(x.pnl_usd for x in all_trades)
    pnl_pct = pnl_usd/BALANCE*100
    sum_w = sum(x.pnl_usd for x in wins)
    sum_l = abs(sum(x.pnl_usd for x in losses))
    pf = sum_w/sum_l if sum_l > 0 else 0
    step0_pct = sum(1 for x in all_trades if x.trailing_step == 0)/t*100
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
    
    if "BULL" in period_name:
        long_trades = [x for x in all_trades if getattr(x, 'side', '') == 'long']
        short_trades = [x for x in all_trades if getattr(x, 'side', '') == 'short']
        print(f"\n  {'='*60}")
        print(f"  LONG vs SHORT BREAKDOWN (BULL)")
        print(f"  {'='*60}")
        print(f"  Total: {len(all_trades)} | LONG: {len(long_trades)} | SHORT: {len(short_trades)}")
        for label, trades_list in [("LONG", long_trades), ("SHORT", short_trades)]:
            if not trades_list:
                print(f"  {label}: 0 trades")
                continue
            tl = len(trades_list)
            tw = [x for x in trades_list if x.pnl_usd > 0]
            tloss = [x for x in trades_list if x.pnl_usd <= 0]
            twr = len(tw)/tl*100
            tpnl = sum(x.pnl_usd for x in trades_list)
            tpf = sum(x.pnl_usd for x in tw)/abs(sum(x.pnl_usd for x in tloss)) if tloss else 0
            tstep0 = sum(1 for x in trades_list if x.trailing_step == 0)/tl*100
            treasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in trades_list)
            tsl_c = treasons.get('TSL', 0)
            sl_c = treasons.get('SL', 0)
            print(f"\n  {label}: {tl} trades | WR={twr:.1f}% | PnL=${tpnl:+.2f} | PF={tpf:.2f} | Step0={tstep0:.1f}%")
            print(f"    TSL: {tsl_c} ({tsl_c/tl*100:.0f}%) | SL: {sl_c} ({sl_c/tl*100:.0f}%)")

# Restore defaults
backtester.MIN_PNL_CHECK_H = 48
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 10000.0
backtester.BASE_RISK_PCT = 1.0
backtester.LEVERAGE = 1
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]