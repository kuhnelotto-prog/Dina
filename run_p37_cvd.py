#!/usr/bin/env python3
"""P37: CVD (Cumulative Volume Delta) as LONG signal booster — backtest comparison."""
import sys, os, time, logging, importlib
sys.path.insert(0, '.')
logging.getLogger('backtester').setLevel(logging.WARNING)

import backtester as bt_mod
from backtester import Backtester
from datetime import datetime, timezone
import requests, pandas as pd

SYMBOLS_12 = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","LINKUSDT",
              "DOGEUSDT","AVAXUSDT","ADAUSDT","SUIUSDT","APEUSDT","ARBUSDT"]
BALANCE = 1000.0
OUT = "p37_cvd_results.txt"

PERIODS = [
    ("BULL",   datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
]

VARIANTS = [
    ("P35_baseline_CVD=0",    0.0),   # CVD disabled
    ("P37_CVD=0.1",          0.1),   # CVD weight 0.1
    ("P37_CVD=0.2",          0.2),   # CVD weight 0.2
    ("P37_CVD=0.3",          0.3),   # CVD weight 0.3
]

def fetch_binance_with_cvd(sym, start_dt, end_dt, interval="4h"):
    """Fetch klines WITH taker_buy_vol (field 9) for CVD calculation."""
    all_c = []
    st = int(start_dt.timestamp()*1000)
    et = int(end_dt.timestamp()*1000)
    cs = st
    for _ in range(30):
        p = {"symbol": sym, "interval": interval, "startTime": cs, "endTime": et, "limit": 1500}
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/klines", params=p, timeout=30).json()
        except:
            break
        if not isinstance(r, list) or len(r) == 0:
            break
        for c in r:
            # Fields: 0=ts, 1=open, 2=high, 3=low, 4=close, 5=volume, 6=close_ts,
            #          7=quote_vol, 8=trades, 9=taker_buy_base_vol, 10=taker_buy_quote_vol
            all_c.append([
                int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                float(c[5]), float(c[9])  # volume, taker_buy_vol
            ])
        last_close = int(r[-1][6])
        if last_close >= et or len(r) < 1500:
            break
        cs = last_close + 1
        time.sleep(0.1)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp","open","high","low","close","volume","taker_buy_vol"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

# Fetch data once
print("Fetching data with taker_buy_vol...")
data_cache = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance_with_cvd("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance_with_cvd("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    for s in SYMBOLS_12[1:]:
        df = fetch_binance_with_cvd(s, start_dt, end_dt)
        time.sleep(0.15)
        dfs[s] = df
    data_cache[pk] = (dfs, btc_df, btc_1d)
    print(f"  {pk}: BTC taker_buy_vol={'taker_buy_vol' in btc_df.columns if len(btc_df)>0 else 'N/A'}")

results = []

for vi, (label, cvd_weight) in enumerate(VARIANTS):
    importlib.reload(bt_mod)
    bt_mod.SYMBOLS = SYMBOLS_12
    bt_mod.CVD_WEIGHT_LONG = cvd_weight
    bt_mod.CVD_LOOKBACK = 20
    print(f"Variant {vi+1}/4: {label} | CVD_WEIGHT_LONG={bt_mod.CVD_WEIGHT_LONG}")

    from backtester import Backtester as BT

    grand_pnl = 0; grand_dd = 0; grand_long_pnl = 0; grand_short_pnl = 0
    grand_long_trades = 0; grand_short_trades = 0
    grand_long_wins = 0; grand_short_wins = 0
    grand_long_timeout = 0; grand_long_sl = 0; grand_long_cvd_boosted = 0

    for pk, start_dt, end_dt in PERIODS:
        dfs, btc_df, btc_1d = data_cache[pk]
        all_trades = []
        for s in SYMBOLS_12:
            sym_df = dfs.get(s)
            if sym_df is None or len(sym_df) < 50:
                continue
            bt = BT(initial_balance=BALANCE)
            res = bt.run(dfs={s: sym_df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
            all_trades.extend(res.trades)

        long_trades = [t for t in all_trades if t.side == "long"]
        short_trades = [t for t in all_trades if t.side == "short"]
        long_pnl = sum(t.pnl_usd for t in long_trades)
        short_pnl = sum(t.pnl_usd for t in short_trades)
        total_pnl = long_pnl + short_pnl

        cum = 0; peak = 0; max_dd = 0
        for t in sorted(all_trades, key=lambda x: x.exit_time if hasattr(x, 'exit_time') else 0):
            cum += t.pnl_usd
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd

        grand_pnl += total_pnl
        if max_dd > grand_dd: grand_dd = max_dd
        grand_long_pnl += long_pnl
        grand_short_pnl += short_pnl
        grand_long_trades += len(long_trades)
        grand_short_trades += len(short_trades)
        grand_long_wins += sum(1 for t in long_trades if t.pnl_usd > 0)
        grand_short_wins += sum(1 for t in short_trades if t.pnl_usd > 0)
        grand_long_timeout += sum(1 for t in long_trades if getattr(t, 'exit_reason', '') == 'MIN_PNL_TIMEOUT')
        grand_long_sl += sum(1 for t in long_trades if getattr(t, 'exit_reason', '') == 'SL')
        # Count trades boosted by CVD (composite > threshold only because of CVD)
        grand_long_cvd_boosted += sum(1 for t in long_trades if getattr(t, 'composite_score', 0) > 0 and cvd_weight > 0)

    results.append({
        'label': label, 'long_trades': grand_long_trades,
        'long_wr': grand_long_wins/grand_long_trades*100 if grand_long_trades else 0,
        'long_pnl': grand_long_pnl,
        'long_timeout_pct': grand_long_timeout/grand_long_trades*100 if grand_long_trades else 0,
        'long_sl_pct': grand_long_sl/grand_long_trades*100 if grand_long_trades else 0,
        'total_pnl': grand_pnl, 'max_dd': grand_dd, 'dd_pct': grand_dd/BALANCE*100,
        'short_trades': grand_short_trades, 'short_pnl': grand_short_pnl,
    })
    print(f"  => LONG:{grand_long_trades} WR={results[-1]['long_wr']:.1f}% PnL=${grand_long_pnl:+.2f} Total=${grand_pnl:+.2f}")

with open(OUT, 'w', encoding='utf-8') as f:
    f.write("P37 SWEEP: CVD (Cumulative Volume Delta) for LONG\n")
    f.write("CVD = 2*taker_buy_vol - total_vol per candle. Boost composite if CVD > 0 AND CVD > rolling mean(20).\n")
    f.write("="*110 + "\n")
    f.write(f"{'Variant':<25} {'LONG#':>6} {'WR%':>5} {'PnL$':>10} {'Timeout%':>9} {'SL%':>5} {'Total$':>10} {'MaxDD%':>7}\n")
    f.write("-"*110 + "\n")
    for r in results:
        f.write(f"{r['label']:<25} {r['long_trades']:>6} {r['long_wr']:>5.1f} {r['long_pnl']:>+10.2f} {r['long_timeout_pct']:>8.1f}% {r['long_sl_pct']:>5.1f} {r['total_pnl']:>+10.2f} {r['dd_pct']:>6.1f}%\n")
    f.write("="*110 + "\n")
    f.write(f"\nSHORT metrics (unchanged across variants): trades={results[0]['short_trades']} PnL=${results[0]['short_pnl']:+.2f}\n")

print(f"\nResults written to {OUT}")
with open(OUT, 'r') as f: print(f.read())