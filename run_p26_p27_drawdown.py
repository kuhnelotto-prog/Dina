#!/usr/bin/env python3
"""P26/P27: Portfolio-level MAX_DRAWDOWN + BEAR/SIDE anomaly analysis"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester
import experiments.params as ep

# ── Shared thresholds ──
ep.TSL_AFTER_TP2_ATR = 2.0
ep.LONG_THRESHOLD_BULL = 0.40
ep.LONG_THRESHOLD_BEAR = 0.45
ep.SHORT_THRESHOLD_BULL = 0.45
ep.SHORT_THRESHOLD_BEAR = 0.35

from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd
from collections import Counter

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
           "LINKUSDT", "DOGEUSDT", "SUIUSDT",
           "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT", "ATOMUSDT"]

PERIODS = [
    ("BULL (2023-11 -> 2024-04)", datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE (2024-05 -> 2024-10)", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT (2025-10 -> 2026-04)", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
]

CONFIGS = [
    ("P26", 15, 100.0, 2.0),
    ("P27", 20, 100.0, 2.0),
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

# ── Fetch data once ──
data_cache = {}
print("Fetching data for all periods...")
for period_name, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    data_cache[f"BTCUSDT_{start_dt.date()}"] = (btc_df, btc_1d)
    print(f"  BTC 4H: {len(btc_df)}, 1D: {len(btc_1d)}")
    for s in SYMBOLS[1:]:
        df = fetch_binance(s, start_dt, end_dt)
        time.sleep(0.15)
        data_cache[f"{s}_{start_dt.date()}"] = df
        print(f"  {s}: {len(df)}")

# ── Run portfolio backtests ──
print("\n" + "="*100)
print("  PORTFOLIO-LEVEL BACKTEST (all symbols together, max 3 simultaneous)")
print("="*100)

for config_name, leverage, balance, risk_pct in CONFIGS:
    backtester.START_BALANCE = balance
    backtester.BASE_RISK_PCT = risk_pct
    backtester.LEVERAGE = leverage
    backtester.MIN_PNL_CHECK_H = 72
    backtester.MIN_EXPECTED_PNL_PCT = -0.5
    backtester.SYMBOLS = SYMBOLS

    print(f"\n{'#'*100}")
    print(f"  {config_name}: LEVERAGE={leverage}, DEPOSIT=${balance}, RISK={risk_pct}%")
    print(f"{'#'*100}")

    for period_name, start_dt, end_dt in PERIODS:
        btc_df, btc_1d = data_cache[f"BTCUSDT_{start_dt.date()}"]
        dfs = {"BTCUSDT": btc_df}
        for s in SYMBOLS[1:]:
            dfs[s] = data_cache[f"{s}_{start_dt.date()}"]

        bt = Backtester(initial_balance=balance)
        res = bt.run(dfs=dfs, symbols=SYMBOLS, btc_df=btc_df, btc_1d_df=btc_1d)

        trades = res.trades
        t = len(trades)
        if t == 0: continue

        wins = [x for x in trades if x.pnl_usd > 0]
        losses = [x for x in trades if x.pnl_usd <= 0]
        wr = len(wins)/t*100
        pnl_usd = res.total_pnl_usd
        pnl_pct = pnl_usd/balance*100
        sum_w = sum(x.pnl_usd for x in wins)
        sum_l = abs(sum(x.pnl_usd for x in losses))
        pf = sum_w/sum_l if sum_l > 0 else 0
        dd_pct = res.max_drawdown_pct
        dd_usd = res.max_drawdown_usd

        reasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in trades)
        step0_pct = sum(1 for x in trades if x.trailing_step == 0)/t*100

        # LONG vs SHORT breakdown
        long_t = [x for x in trades if getattr(x, 'side', '') == 'long']
        short_t = [x for x in trades if getattr(x, 'side', '') == 'short']
        long_pnl = sum(x.pnl_usd for x in long_t)
        short_pnl = sum(x.pnl_usd for x in short_t)
        long_wr = len([x for x in long_t if x.pnl_usd > 0])/len(long_t)*100 if long_t else 0
        short_wr = len([x for x in short_t if x.pnl_usd > 0])/len(short_t)*100 if short_t else 0

        print(f"\n  {period_name}")
        print(f"  {'='*70}")
        print(f"  Trades: {t} | WR: {wr:.1f}% | PnL: ${pnl_usd:+.2f} ({pnl_pct:+.1f}%) | PF: {pf:.2f}")
        print(f"  MAX_DRAWDOWN: {dd_pct:.1f}% (${dd_usd:+.2f})")
        print(f"  Step0%: {step0_pct:.1f}%")
        print(f"  LONG: {len(long_t)} trades, WR={long_wr:.1f}%, PnL=${long_pnl:+.2f}")
        print(f"  SHORT: {len(short_t)} trades, WR={short_wr:.1f}%, PnL=${short_pnl:+.2f}")
        print(f"  EXIT REASONS:")
        for reason, count in reasons.most_common():
            r_pnl = sum(x.pnl_usd for x in trades if getattr(x, 'exit_reason', '') == reason)
            print(f"    {reason:20s}: {count:3d} ({count/t*100:5.1f}%) | PnL: ${r_pnl:+.2f}")

# ── BEAR/SIDE Anomaly Analysis ──
print(f"\n\n{'='*100}")
print(f"  BEAR/SIDE ANOMALY ANALYSIS")
print(f"{'='*100}")
print("""
WHY P26/P27 BEAR/SIDE shows huge improvement vs P24:

P24 baseline was: LEV=10 (reference only, NOT in calc), $1000 deposit, 2% risk
  → risk_usd = $1000 × 2% = $20
  → position_size = $20 / sl_pct (LEVERAGE not multiplied)

P26: LEV=15 (IN calc), $100 deposit, 2% risk
  → risk_usd = $100 × 2% = $2
  → position_size = ($2 / sl_pct) × 15 = $30 / sl_pct

P27: LEV=20 (IN calc), $100 deposit, 2% risk
  → risk_usd = $100 × 2% = $2
  → position_size = ($2 / sl_pct) × 20 = $40 / sl_pct

RATIO vs P24:  P26 = 30/20 = 1.5×  |  P27 = 40/20 = 2.0×

But the per-symbol results show BIGGER ratios because:
1. P24 ran only 10 symbols, P26/P27 run 12 symbols (added RUNE/OP/INJ/TIA/ATOM)
2. Each symbol starts with its OWN balance, so 12 × $100 = $1200 effective capital
3. SHORT trades are especially profitable in BEAR — leverage amplifies them
4. The extra 2 symbols (RUNE, OP, INJ, TIA, ATOM) add BEAR-period shorts

PORTFOLIO-LEVEL drawdown above shows the TRUE risk with leverage.
""")

# Restore defaults
backtester.MIN_PNL_CHECK_H = 48
backtester.MIN_EXPECTED_PNL_PCT = -0.5
backtester.START_BALANCE = 10000.0
backtester.BASE_RISK_PCT = 1.0
backtester.LEVERAGE = 1
backtester.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]