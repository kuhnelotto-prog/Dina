#!/usr/bin/env python3
"""Sweep P6: in-process backtest with param overrides."""
import sys, os, time, json, importlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import experiments.params as ep
from backtester import Backtester, SYMBOLS, START_BALANCE
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd

DAYS = 180
END = datetime.now(timezone.utc) - timedelta(minutes=5)
START = END - timedelta(days=DAYS)

def fetch(sym, start_dt, end_dt, gran="4H"):
    all_c = []
    et = int(end_dt.timestamp() * 1000)
    st = int(start_dt.timestamp() * 1000)
    ce = et
    for _ in range(20):
        p = {"symbol": sym, "granularity": gran, "limit": 1000,
             "endTime": ce, "productType": "USDT-FUTURES"}
        r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=p, timeout=30).json()
        if r.get("code") != "00000" or not r.get("data"):
            break
        for c in r["data"]:
            ts = int(c[0])
            if ts >= st:
                all_c.append([ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        earliest_ts = int(r["data"][-1][0])
        if earliest_ts <= st or len(r["data"]) < 1000 or earliest_ts >= ce:
            break
        ce = earliest_ts - 1
        time.sleep(0.15)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

def fetch1d(sym, limit=500):
    p = {"symbol": sym, "granularity": "1D", "limit": limit, "productType": "USDT-FUTURES"}
    r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=p, timeout=30).json()
    if r.get("code") != "00000" or not r.get("data"):
        return pd.DataFrame()
    rows = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in r["data"]]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

def run_backtest(data, btc_df, btc_1d, adx, sl_mult):
    """Run backtest in-process with overridden params."""
    ep.ADX_THRESHOLD = adx
    ep.SL_ATR_MULT = sl_mult
    importlib.reload(sys.modules['backtester'])
    from backtester import Backtester as BT2
    syms = list(data.keys())
    bt = BT2(initial_balance=START_BALANCE)
    res = bt.run(dfs=data, symbols=syms, btc_df=btc_df, btc_1d_df=btc_1d)
    all_trades = res.trades
    t = len(all_trades)
    if t == 0:
        return {"trades": 0, "wr_pct": 0, "pnl_pct": 0, "pf": 0, "step0_pct": 0}
    wins = [x for x in all_trades if x.pnl_usd > 0]
    losses = [x for x in all_trades if x.pnl_usd <= 0]
    wr = len(wins) / t * 100
    pnl_usd = sum(x.pnl_usd for x in all_trades)
    pnl_pct = pnl_usd / START_BALANCE * 100
    sum_w = sum(x.pnl_usd for x in wins)
    sum_l = abs(sum(x.pnl_usd for x in losses))
    pf = sum_w / sum_l if sum_l > 0 else 0
    step0 = sum(1 for x in all_trades if x.trailing_step == 0)
    return {
        "trades": t, "wr_pct": round(wr, 1), "pnl_pct": round(pnl_pct, 2),
        "pf": round(pf, 2), "step0_pct": round(step0/t*100, 1)
    }

def main():
    # Load or fetch data
    cache_file = "_sweep_data.pkl"
    if os.path.exists(cache_file):
        print("Loading cached data...")
        import pickle
        with open(cache_file, "rb") as f:
            d = pickle.load(f)
        data, btc_df, btc_1d = d["data"], d["btc_df"], d["btc_1d"]
        print(f"  Loaded {len(data)} symbols")
    else:
        print(f"Fetching {DAYS} days of data...")
        btc_df = fetch("BTCUSDT", START, END)
        btc_1d = fetch1d("BTCUSDT", limit=500)
        data = {"BTCUSDT": btc_df}
        print(f"  BTC 4H: {len(btc_df)}, BTC 1D: {len(btc_1d)}")
        for s in SYMBOLS[1:]:
            data[s] = fetch(s, START, END)
            time.sleep(0.3)
            print(f"  {s}: {len(data[s])}")
        import pickle
        with open(cache_file, "wb") as f:
            pickle.dump({"data": data, "btc_df": btc_df, "btc_1d": btc_1d}, f)

    # Sweep 1: ADX
    adx_values = [18, 20, 22, 25]
    sweep1 = {}
    print("\nSWEEP 1: ADX (SL_ATR_MULT=2.0)")
    for adx in adx_values:
        t0 = time.time()
        m = run_backtest(data, btc_df, btc_1d, adx, 2.0)
        sweep1[adx] = m
        print(f"  ADX={adx}: Trades={m['trades']} WR={m['wr_pct']}% PnL={m['pnl_pct']}% PF={m['pf']} Step0={m['step0_pct']}% ({time.time()-t0:.0f}s)")

    # Sweep 2: SL
    sl_values = [1.5, 2.0, 2.5, 3.0]
    sweep2 = {}
    print("\nSWEEP 2: SL_ATR_MULT (ADX=20)")
    for sl in sl_values:
        t0 = time.time()
        m = run_backtest(data, btc_df, btc_1d, 20, sl)
        sweep2[sl] = m
        print(f"  SL={sl}: Trades={m['trades']} WR={m['wr_pct']}% PnL={m['pnl_pct']}% PF={m['pf']} Step0={m['step0_pct']}% ({time.time()-t0:.0f}s)")

    # Restore defaults
    ep.ADX_THRESHOLD = 20
    ep.SL_ATR_MULT = 2.0

    # Tables
    print("\n" + "="*70)
    print("TABLE 1: ADX sweep (SL_ATR_MULT=2.0)")
    print("="*70)
    print(f"  {'ADX':>4} | {'Trades':>6} | {'WR%':>6} | {'PnL%':>8} | {'PF':>6} | {'Step0%':>7}")
    print("  " + "-"*50)
    for adx in adx_values:
        m = sweep1[adx]
        print(f"  {adx:>4} | {m['trades']:>6} | {m['wr_pct']:>6.1f} | {m['pnl_pct']:>+8.2f} | {m['pf']:>6.2f} | {m['step0_pct']:>7.1f}")

    print("\n" + "="*70)
    print("TABLE 2: SL_ATR_MULT sweep (ADX=20)")
    print("="*70)
    print(f"  {'SL':>4} | {'Trades':>6} | {'WR%':>6} | {'PnL%':>8} | {'PF':>6} | {'Step0%':>7}")
    print("  " + "-"*50)
    for sl in sl_values:
        m = sweep2[sl]
        print(f"  {sl:>4} | {m['trades']:>6} | {m['wr_pct']:>6.1f} | {m['pnl_pct']:>+8.2f} | {m['pf']:>6.2f} | {m['step0_pct']:>7.1f}")

    # Save
    with open("sweep_results.json", "w") as f:
        json.dump({"sweep1_adx": {str(k): v for k,v in sweep1.items()},
                    "sweep2_sl": {str(k): v for k,v in sweep2.items()}}, f, indent=2)
    print("\nSaved to sweep_results.json")

    # Cleanup cache
    if os.path.exists(cache_file):
        os.remove(cache_file)

if __name__ == "__main__":
    main()