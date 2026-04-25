#!/usr/bin/env python3
"""
Baseline P5: 180 days, 4H, 10 coins.
Changes from P4: ADX=20 (was 18), SL=2.0ATR (was 1.5ATR)
Thresholds: LONG_BULL=0.30, LONG_BEAR=0.40, SHORT_BULL=0.45, SHORT_BEAR=0.35
Reports: PF, WR, PnL, Step0%, trade count.
"""
import sys, os, time, math, json
import pandas as pd
from datetime import datetime, timezone, timedelta
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtester import Backtester

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]
DAYS = 180
END = datetime.now(timezone.utc) - timedelta(minutes=5)
START = END - timedelta(days=DAYS)
INITIAL_BALANCE = 10000.0


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
        if earliest_ts <= st:
            break
        if len(r["data"]) < 1000:
            break
        if earliest_ts >= ce:
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


# ── Load data ──
print(f"BASELINE P4: {DAYS} days, 4H, {len(SYMBOLS)} coins")
print(f"Period: {START.date()} -> {END.date()}")
print(f"Thresholds: LONG_BULL=0.30, LONG_BEAR=0.40, SHORT_BULL=0.45, SHORT_BEAR=0.35")
print()

print("Loading BTC data...")
btc_df = fetch("BTCUSDT", START, END)
btc_1d = fetch1d("BTCUSDT", limit=500)
print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d)} candles")

data = {"BTCUSDT": btc_df}
for s in SYMBOLS[1:]:
    data[s] = fetch(s, START, END)
    time.sleep(0.3)
    print(f"  {s}: {len(data[s])} candles")

# ── Run backtest per symbol ──
print()
print("=" * 100)
print(f"{'Symbol':<12} {'Trades':>6} {'WR%':>6} {'PnL$':>10} {'PnL%':>8} {'PF':>6} {'Step0%':>7} {'AvgWin':>8} {'AvgLoss':>9}")
print("-" * 100)

all_trades = []
per_symbol = {}

for s in SYMBOLS:
    df = data[s]
    if df.empty or len(df) < 100:
        print(f"{s:<12} SKIP ({len(df)} candles)")
        continue
    bt = Backtester(initial_balance=INITIAL_BALANCE)
    res = bt.run(dfs={s: df, "BTCUSDT": btc_df}, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
    trades = res.trades
    all_trades.extend(trades)

    t = len(trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    wr = len(wins) / t * 100 if t > 0 else 0
    pnl_usd = sum(t.pnl_usd for t in trades)
    pnl_pct = pnl_usd / INITIAL_BALANCE * 100
    sum_wins = sum(t.pnl_usd for t in wins)
    sum_losses = abs(sum(t.pnl_usd for t in losses))
    pf = sum_wins / sum_losses if sum_losses > 0 else float('inf')
    step0 = sum(1 for t in trades if t.trailing_step == 0)
    step0_pct = step0 / t * 100 if t > 0 else 0
    avg_win = sum_wins / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0

    per_symbol[s] = {
        "trades": t, "wr": wr, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
        "pf": pf, "step0_pct": step0_pct, "avg_win": avg_win, "avg_loss": avg_loss
    }
    print(f"{s:<12} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pnl_pct:>+8.3f} {pf:>6.2f} {step0_pct:>7.1f} {avg_win:>+8.2f} {avg_loss:>+9.2f}")

# ── TOTALS ──
t = len(all_trades)
wins = [t for t in all_trades if t.pnl_usd > 0]
losses = [t for t in all_trades if t.pnl_usd <= 0]
wr = len(wins) / t * 100 if t > 0 else 0
pnl_usd = sum(t.pnl_usd for t in all_trades)
pnl_pct = pnl_usd / INITIAL_BALANCE * 100
sum_wins = sum(t.pnl_usd for t in wins)
sum_losses = abs(sum(t.pnl_usd for t in losses))
pf = sum_wins / sum_losses if sum_losses > 0 else float('inf')
step0 = sum(1 for t in all_trades if t.trailing_step == 0)
step0_pct = step0 / t * 100 if t > 0 else 0
avg_win = sum_wins / len(wins) if wins else 0
avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0

# Exit reason breakdown
from collections import Counter
reasons = Counter(getattr(t, 'exit_reason', 'UNKNOWN') for t in all_trades)

print("-" * 100)
print(f"{'TOTAL':<12} {t:>6} {wr:>6.1f} {pnl_usd:>+10.2f} {pnl_pct:>+8.3f} {pf:>6.2f} {step0_pct:>7.1f} {avg_win:>+8.2f} {avg_loss:>+9.2f}")
print()
print("EXIT REASONS:")
for reason, count in reasons.most_common():
    r_pnl = sum(t.pnl_usd for t in all_trades if getattr(t, 'exit_reason', '') == reason)
    print(f"  {reason:20s}: {count:3d} ({count/t*100:5.1f}%) | PnL: ${r_pnl:+.2f}")

# Save
result = {
    "period": f"{START.date()} -> {END.date()}",
    "days": DAYS,
    "symbols": SYMBOLS,
    "thresholds": {"LONG_BULL": 0.30, "LONG_BEAR": 0.40, "SHORT_BULL": 0.45, "SHORT_BEAR": 0.35},
    "total": {"trades": t, "wr_pct": round(wr, 1), "pnl_usd": round(pnl_usd, 2), "pnl_pct": round(pnl_pct, 3),
              "pf": round(pf, 2), "step0_pct": round(step0_pct, 1), "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2)},
    "per_symbol": {s: {k: round(v, 3) if isinstance(v, float) else v for k, v in d.items()} for s, d in per_symbol.items()},
    "exit_reasons": {r: c for r, c in reasons.most_common()},
}
with open("baseline_p4_results.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to baseline_p4_results.json")