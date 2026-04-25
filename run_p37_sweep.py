#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P37 Sweep: SL_ATR_MULT_LONG = 4.0, 4.1, 4.2."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt_mod
bt_mod.MIN_PNL_CHECK_H = 72
bt_mod.MIN_EXPECTED_PNL_PCT = -0.5
bt_mod.START_BALANCE = 1000.0
bt_mod.BASE_RISK_PCT = 2.0
SYMBOLS_12 = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
              "LINKUSDT", "DOGEUSDT", "SUIUSDT",
              "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT", "ATOMUSDT"]
bt_mod.SYMBOLS = SYMBOLS_12
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
BALANCE = 1000.0
PERIODS = [
    ("BULL", datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
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

SWEEP_VALUES = [4.0, 4.1, 4.2]
print("Fetching data...")
data_cache = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    print(f"  {pk}: BTC 4H={len(btc_df)} 1D={len(btc_1d)}")
    for s in SYMBOLS_12[1:]:
        df = fetch_binance(s, start_dt, end_dt)
        time.sleep(0.15)
        dfs[s] = df
    data_cache[pk] = (dfs, btc_df, btc_1d)

def run_one_period(pk, dfs, btc_df, btc_1d, sl_mult):
    bt_mod.USE_P34_LONG_EXIT = True
    bt_mod.SL_ATR_MULT_LONG = sl_mult
    bt_mod.TP1_ATR_MULT = 1.0
    bt_mod.TP1_CLOSE_PCT = 0.30
    bt_mod.TP2_ATR_MULT = 2.0
    bt_mod.TP2_CLOSE_PCT = 0.30
    bt_mod.TSL_ATR_LONG = 2.0
    all_trades = []
    for s in SYMBOLS_12:
        sym_df = dfs.get(s)
        if sym_df is None or sym_df.empty or len(sym_df) < 50: continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
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
    long_reasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in long_t)
    short_reasons = Counter(getattr(x, 'exit_reason', 'UNKNOWN') for x in short_t)
    long_sl = long_reasons.get('SL', 0)
    short_sl = short_reasons.get('SL', 0)
    return {"total": t, "pnl": total_pnl, "long_n": len(long_t), "long_wr": long_wr, "long_pnl": long_pnl,
            "short_n": len(short_t), "short_wr": short_wr, "short_pnl": short_pnl,
            "long_sl_rate": long_sl/len(long_t)*100 if long_t else 0, "short_sl_rate": short_sl/len(short_t)*100 if short_t else 0}

results = {}
for sl_val in SWEEP_VALUES:
    print(f"\n{'='*90}")
    print(f"  SL_ATR_MULT_LONG = {sl_val}")
    print(f"{'='*90}")
    results[sl_val] = {}
    for pk, start_dt, end_dt in PERIODS:
        dfs, btc_df, btc_1d = data_cache[pk]
        r = run_one_period(pk, dfs, btc_df, btc_1d, sl_val)
        if r is None: print(f"  {pk}: NO TRADES"); continue
        results[sl_val][pk] = r
        print(f"  {pk:10s}  LONG: {r['long_n']:>3} WR={r['long_wr']:.1f}% PnL=${r['long_pnl']:+.2f} SL-hit={r['long_sl_rate']:.1f}%  |  SHORT: {r['short_n']:>3} WR={r['short_wr']:.1f}% PnL=${r['short_pnl']:+.2f}  |  TOTAL: {r['total']:>3} PnL=${r['pnl']:+.2f}")
    grand = sum(r['pnl'] for r in results[sl_val].values())
    print(f"  GRAND TOTAL: ${grand:+.2f}")

print(f"\n{'='*90}")
print(f"  FULL COMPARISON TABLE (all SL values)")
print(f"{'='*90}")
hdr = "  SL  | BULL_PnL  BULL_SL | BEAR_PnL  BEAR_SL |  CUR_PnL  CUR_SL |   GRAND"
print(hdr)
print("  " + "-" * (len(hdr) - 2))
baselines = [
    (1.5, -652.86, 39.6, -219.96, 40.6, 432.80, 36.7),
    (3.5, -299.53, 17.5, 175.76, 21.1, 340.53, 19.4),
    (3.6, -295.12, 17.2, 210.91, 18.8, 354.84, 16.7),
    (3.7, -301.71, 16.2, 204.17, 18.3, 340.23, 15.3),
    (3.8, -281.85, 15.6, 185.28, 17.4, 334.88, 15.3),
    (3.9, -227.65, 13.9, 221.68, 16.6, 329.76, 15.3),
]
for sl, bp, bsl, bep, besl, cp, csl in baselines:
    grand = bp + bep + cp
    print(f"  {sl:>3.1f} | {bp:>+9.2f} {bsl:>5.1f}% | {bep:>+9.2f} {besl:>5.1f}% | {cp:>+8.2f} {csl:>5.1f}% | {grand:>+8.2f}")
for sl_val in SWEEP_VALUES:
    r = results.get(sl_val, {})
    b = r.get('BULL', {}); be = r.get('BEAR/SIDE', {}); c = r.get('CURRENT', {})
    bp = b.get('pnl', 0); bsl = b.get('long_sl_rate', 0)
    bep = be.get('pnl', 0); besl = be.get('long_sl_rate', 0)
    cp = c.get('pnl', 0); csl = c.get('long_sl_rate', 0)
    grand = bp + bep + cp
    print(f"  {sl_val:>3.1f} | {bp:>+9.2f} {bsl:>5.1f}% | {bep:>+9.2f} {besl:>5.1f}% | {cp:>+8.2f} {csl:>5.1f}% | {grand:>+8.2f}")

bt_mod.MIN_PNL_CHECK_H = 48; bt_mod.MIN_EXPECTED_PNL_PCT = -0.5; bt_mod.START_BALANCE = 10000.0; bt_mod.BASE_RISK_PCT = 1.0
bt_mod.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]
bt_mod.USE_P34_LONG_EXIT = False; bt_mod.SL_ATR_MULT_LONG = 1.5