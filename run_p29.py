#!/usr/bin/env python3
"""P29: Three macro-filter variants (a/b/c) vs P24 baseline.
P29a: 1D EMA50 slope filter (slope > 0 required for LONG)
P29b: 4H regime lock (bear_streak N=5 -> force BEAR)
P29c: Combo slope + streak (slope > 0 AND bear_streak < 3)
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester as bt_mod
bt_mod.MIN_PNL_CHECK_H = 72
bt_mod.MIN_EXPECTED_PNL_PCT = -0.5
bt_mod.START_BALANCE = 1000.0
bt_mod.BASE_RISK_PCT = 2.0
bt_mod.LEVERAGE = 1
bt_mod.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
                  "LINKUSDT", "DOGEUSDT", "SUIUSDT",
                  "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT", "ATOMUSDT"]

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

from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd

SYMBOLS = bt_mod.SYMBOLS
BALANCE = 1000.0

PERIODS = [
    ("BULL", datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
]

P24 = {
    "BULL":       (104, 63.5, 112.61, 42, 71.4, 60.80),
    "BEAR/SIDE":  (73,  57.5, -152.41, 78, 66.7, 172.27),
    "CURRENT":    (13,  69.2, -5.29,  208, 69.2, 823.57),
}

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

# ── Fetch ALL data once ──
print("Fetching data for all periods...")
data_cache = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    print(f"  {pk}: BTC 4H={len(btc_df)} 1D={len(btc_1d)}")
    for s in SYMBOLS[1:]:
        df = fetch_binance(s, start_dt, end_dt)
        time.sleep(0.15)
        dfs[s] = df
    data_cache[pk] = (dfs, btc_df, btc_1d)

def run_one_period(pk, dfs, btc_df, btc_1d, flags):
    """Run backtest for one period with given filter flags."""
    bt_mod.SLOPE_FILTER_1D = flags.get("slope", False)
    bt_mod.REGIME_LOCK_4H = flags.get("regime_lock", False)
    bt_mod.COMBO_FILTER = flags.get("combo", False)
    bt_mod.REGIME_LOCK_N = flags.get("lock_n", 5)

    all_trades = []
    for s in SYMBOLS:
        df = dfs.get(s)
        if df is None or df.empty or len(df) < 50:
            continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs=dfs, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        all_trades.extend(res.trades)

    t = len(all_trades)
    if t == 0: return None
    long_t = [x for x in all_trades if getattr(x, 'side', '') == 'long']
    short_t = [x for x in all_trades if getattr(x, 'side', '') == 'short']
    long_pnl = sum(x.pnl_usd for x in long_t)
    short_pnl = sum(x.pnl_usd for x in short_t)
    long_wr = len([x for x in long_t if x.pnl_usd > 0])/len(long_t)*100 if long_t else 0
    short_wr = len([x for x in short_t if x.pnl_usd > 0])/len(short_t)*100 if short_t else 0
    total_pnl = sum(x.pnl_usd for x in all_trades)
    total_wr = len([x for x in all_trades if x.pnl_usd > 0])/t*100
    return {
        "total": t, "wr": total_wr, "pnl": total_pnl,
        "long_n": len(long_t), "long_wr": long_wr, "long_pnl": long_pnl,
        "short_n": len(short_t), "short_wr": short_wr, "short_pnl": short_pnl,
    }

VARIANTS = [
    ("P29a (slope)", {"slope": True}),
    ("P29b (lock N=5)", {"regime_lock": True, "lock_n": 5}),
    ("P29c (combo)", {"combo": True}),
]

# ── Run all variants ──
print(f"\n{'='*90}")
print(f"  P29: Three macro-filter variants vs P24 baseline")
print(f"  P24 weights restored, LONG_THRESHOLD_BEAR=0.45")
print(f"{'='*90}")

for vname, vflags in VARIANTS:
    print(f"\n{'='*80}")
    print(f"  {vname}: slope={vflags.get('slope',False)} lock={vflags.get('regime_lock',False)} combo={vflags.get('combo',False)}")
    print(f"{'='*80}")

    for pk, start_dt, end_dt in PERIODS:
        dfs, btc_df, btc_1d = data_cache[pk]
        r = run_one_period(pk, dfs, btc_df, btc_1d, vflags)
        p24 = P24[pk]
        if r is None:
            print(f"\n  {pk}: NO TRADES")
            continue
        print(f"\n  {pk}")
        print(f"  {'='*60}")
        print(f"  Trades: {r['total']} | WR: {r['wr']:.1f}% | PnL: ${r['pnl']:+.2f} ({r['pnl']/BALANCE*100:+.1f}%)")
        print(f"  LONG:  {r['long_n']:>3} trades | WR={r['long_wr']:.1f}% | PnL=${r['long_pnl']:+.2f}")
        print(f"  SHORT: {r['short_n']:>3} trades | WR={r['short_wr']:.1f}% | PnL=${r['short_pnl']:+.2f}")
        print(f"  --- P24 ---")
        print(f"  P24 LONG:  {p24[0]:>3} trades | WR={p24[1]:.1f}% | PnL=${p24[2]:+.2f}")
        print(f"  P24 SHORT: {p24[3]:>3} trades | WR={p24[4]:.1f}% | PnL=${p24[5]:+.2f}")
        ld = r['long_n'] - p24[0]
        lpd = r['long_pnl'] - p24[2]
        spd = r['short_pnl'] - p24[5]
        print(f"  LONG delta:  {ld:+d} trades | PnL ${lpd:+.2f}")
        print(f"  SHORT delta: PnL ${spd:+.2f}")
        print(f"  TOTAL delta: PnL ${lpd+spd:+.2f}")

# Reset
bt_mod.SLOPE_FILTER_1D = False
bt_mod.REGIME_LOCK_4H = False
bt_mod.COMBO_FILTER = False