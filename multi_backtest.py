"""Multi-period backtest for Dina — synthetic + real data."""
import requests
import logging
from datetime import datetime, timezone
import pandas as pd
import numpy as np
import time

logging.disable(logging.CRITICAL)

from backtester import Backtester

now = datetime.now(tz=timezone.utc)


def fetch_candles(symbol, start_date, end_date, granularity="4H"):
    all_candles = []
    end_time = int(end_date.timestamp() * 1000)
    start_time = int(start_date.timestamp() * 1000)
    current_end = end_time
    for _ in range(10):
        params = {
            "symbol": symbol, "granularity": granularity, "limit": 1000,
            "endTime": current_end, "startTime": start_time, "productType": "umcbl",
        }
        resp = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=30)
        data = resp.json()
        if data.get("code") != "00000" or not data.get("data"):
            break
        candles = data["data"]
        for c in candles:
            all_candles.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        if len(candles) < 1000:
            break
        earliest = int(candles[-1][0])
        if earliest >= current_end:
            break
        current_end = earliest - 1
        time.sleep(0.1)
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df


def generate_synthetic(regime, n_candles=540, start_price=60000):
    """Generate synthetic 4H candles for different market regimes."""
    np.random.seed(42)
    dates = pd.date_range(end=now, periods=n_candles, freq="4h")

    params = {
        "bull":          (0.0015, 0.008),   # +0.15%/candle, low vol → ~+125% in 90d
        "bear":          (-0.0012, 0.010),   # -0.12%/candle → ~-48% in 90d
        "chop":          (0.0, 0.012),       # sideways, high vol
        "volatile_bull": (0.001, 0.015),     # bull + high vol
    }
    drift, vol = params.get(regime, (0.0, 0.01))

    returns = np.random.normal(drift, vol, n_candles)
    prices = start_price * np.cumprod(1 + returns)

    highs = prices * (1 + np.abs(np.random.normal(0, 0.003, n_candles)))
    lows = prices * (1 - np.abs(np.random.normal(0, 0.003, n_candles)))
    opens = np.roll(prices, 1)
    opens[0] = start_price
    volumes = np.random.uniform(5000, 15000, n_candles)

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": prices, "volume": volumes,
    }, index=dates)
    return df


def run_backtest(name, df):
    if len(df) < 30:
        return name, len(df), None
    bt = Backtester(initial_balance=10000.0)
    result = bt.run(df=df, symbol="BTCUSDT")
    return name, len(df), result


# ── Run all tests ──
results = []

# Synthetic
for regime in ["bull", "bear", "chop", "volatile_bull"]:
    df = generate_synthetic(regime)
    label = f"Synth {regime.upper()} 90d"
    results.append(run_backtest(label, df))

# Real data
df_90 = fetch_candles("BTCUSDT", now - pd.Timedelta(days=90), now, "4H")
results.append(run_backtest("Real Bear Q1 2026 (4H)", df_90))

df_30 = fetch_candles("BTCUSDT", now - pd.Timedelta(days=30), now, "4H")
results.append(run_backtest("Real Last 30d (4H)", df_30))

# ── Print table ──
print("=" * 90)
print(f"{'Period':<28} {'Candles':>7} {'Trades':>6} {'WinRate':>8} {'PnL$':>10} {'PnL%':>8} {'MaxDD%':>8} {'PF':>6}")
print("=" * 90)

for name, n_candles, result in results:
    if result is None:
        print(f"{name:<28} {n_candles:>7}   (not enough data)")
        continue
    wr = result.winning_trades / result.total_trades * 100 if result.total_trades > 0 else 0
    pnl_pct = result.total_pnl_usd / result.initial_balance * 100
    pf = 0
    if result.losing_trades > 0:
        total_loss = sum(t.pnl_usd for t in result.trades if t.pnl_usd < 0)
        total_win = sum(t.pnl_usd for t in result.trades if t.pnl_usd > 0)
        pf = abs(total_win / total_loss) if total_loss != 0 else 999
    elif result.winning_trades > 0:
        pf = 999
    print(f"{name:<28} {n_candles:>7} {result.total_trades:>6} {wr:>7.1f}% {result.total_pnl_usd:>+9.2f} {pnl_pct:>+7.2f}% {result.max_drawdown_pct:>7.2f}% {pf:>5.2f}")

print("=" * 90)