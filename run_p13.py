#!/usr/bin/env python3
"""P13: Test leverage/size variants on P12 base."""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import experiments.params as ep
from backtester import Backtester, SYMBOLS, START_BALANCE
from datetime import datetime, timezone, timedelta
import requests, pandas as pd

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

def run_variant(data, btc_df, btc_1d, risk_pct, balance, label):
    import backtester
    backtester.BASE_RISK_PCT = risk_pct
    backtester.START_BALANCE = balance
    importlib_reload = __import__('importlib').reload
    importlib_reload(backtester)
    from backtester import Backtester as BT
    syms = list(data.keys())
    bt = BT(initial_balance=balance)
    res = bt.run(dfs=data, symbols=syms, btc_df=btc_df, btc_1d_df=btc_1d)
    all_trades = res.trades
    t = len(all_trades)
    if t == 0:
        return None
    wins = [x for x in all_trades if x.pnl_usd > 0]
    losses = [x for x in all_trades if x.pnl_usd <= 0]
    wr = len(wins) / t * 100
    pnl_usd = sum(x.pnl_usd for x in all_trades)
    sum_w = sum(x.pnl_usd for x in wins)
    sum_l = abs(sum(x.pnl_usd for x in losses))
    pf = sum_w / sum_l if sum_l > 0 else 0
    step0 = sum(1 for x in all_trades if x.trailing_step == 0)
    avg_win = sum_w / len(wins) if wins else 0
    avg_loss = -sum_l / len(losses) if losses else 0

    # Per-symbol breakdown
    from collections import defaultdict, Counter
    sym_trades = defaultdict(list)
    for tr in all_trades:
        sym_trades[tr.symbol].append(tr)

    # Exit reasons
    reasons = Counter()
    reason_pnl = defaultdict(float)
    for tr in all_trades:
        r = getattr(tr, 'exit_reason', 'UNKNOWN')
        reasons[r] += 1
        reason_pnl[r] += tr.pnl_usd

    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    print(f"  Trades={t} | WR={wr:.1f}% | PnL=${pnl_usd:+.2f} | PF={pf:.2f} | Step0={step0/t*100:.1f}%")
    print(f"  AvgWin=${avg_win:+.2f} | AvgLoss=${avg_loss:+.2f}")
    print(f"\n  {'Symbol':<12} {'Trades':>6} {'WR%':>6} {'PnL$':>10} {'PF':>6} {'Step0%':>7} {'AvgWin':>8} {'AvgLoss':>8}")
    print(f"  {'-'*70}")
    for sym in SYMBOLS:
        if sym not in sym_trades:
            continue
        st = sym_trades[sym]
        st_t = len(st)
        st_w = [x for x in st if x.pnl_usd > 0]
        st_l = [x for x in st if x.pnl_usd <= 0]
        st_wr = len(st_w)/st_t*100
        st_pnl = sum(x.pnl_usd for x in st)
        st_sw = sum(x.pnl_usd for x in st_w)
        st_sl = abs(sum(x.pnl_usd for x in st_l))
        st_pf = st_sw/st_sl if st_sl > 0 else 0
        st_s0 = sum(1 for x in st if x.trailing_step == 0)/st_t*100
        st_aw = st_sw/len(st_w) if st_w else 0
        st_al = -st_sl/len(st_l) if st_l else 0
        print(f"  {sym:<12} {st_t:>6} {st_wr:>5.1f}% {st_pnl:>+10.2f} {st_pf:>6.2f} {st_s0:>6.1f}% {st_aw:>+8.2f} {st_al:>+8.2f}")

    print(f"\n  EXIT REASONS:")
    for r, cnt in reasons.most_common():
        rpnl = reason_pnl[r]
        print(f"    {r:20s}: {cnt:3d} ({cnt/t*100:5.1f}%) | PnL: ${rpnl:+.2f}")

    return {"trades": t, "wr": round(wr,1), "pnl_usd": round(pnl_usd,2), "pf": round(pf,2),
            "step0_pct": round(step0/t*100,1), "avg_win": round(avg_win,2), "avg_loss": round(avg_loss,2)}

def main():
    cache = "_p13_data.pkl"
    if os.path.exists(cache):
        import pickle
        with open(cache, "rb") as f:
            d = pickle.load(f)
        data, btc_df, btc_1d = d["data"], d["btc_df"], d["btc_1d"]
    else:
        print("Fetching data...")
        btc_df = fetch("BTCUSDT", START, END)
        btc_1d = fetch1d("BTCUSDT", limit=500)
        data = {"BTCUSDT": btc_df}
        for s in SYMBOLS[1:]:
            data[s] = fetch(s, START, END)
            time.sleep(0.3)
        import pickle
        with open(cache, "wb") as f:
            pickle.dump({"data": data, "btc_df": btc_df, "btc_1d": btc_1d}, f)

    # Variant 1: leverage=5x, risk=2%, deposit=$1000
    r1 = run_variant(data, btc_df, btc_1d, risk_pct=2.0, balance=1000, label="P13-V1: leverage=5x, risk=2%, $1000")

    # Variant 2: leverage=7x, risk=3%, deposit=$1000
    r2 = run_variant(data, btc_df, btc_1d, risk_pct=3.0, balance=1000, label="P13-V2: leverage=7x, risk=3%, $1000")

    # Restore
    import backtester
    backtester.BASE_RISK_PCT = 1.0
    backtester.START_BALANCE = 10000.0

    # Comparison
    if r1 and r2:
        print(f"\n{'='*80}")
        print(f"  COMPARISON (deposit=$1000)")
        print(f"{'='*80}")
        print(f"  {'Metric':<15} {'V1(5x/2%)':>12} {'V2(7x/3%)':>12}")
        print(f"  {'-'*42}")
        for k in ["trades", "wr", "pnl_usd", "pf", "step0_pct", "avg_win", "avg_loss"]:
            print(f"  {k:<15} {r1[k]:>12} {r2[k]:>12}")

    if os.path.exists(cache):
        os.remove(cache)

if __name__ == "__main__":
    main()