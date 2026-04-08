"""Backtest each symbol from .env individually and aggregate results."""
import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import time
import logging

logging.disable(logging.CRITICAL)

from dotenv import load_dotenv
load_dotenv()
from backtester import Backtester

now = datetime.now(tz=timezone.utc)

def fetch_candles(symbol, start_date, end_date, granularity="4H"):
    """Fetch historical candles from Bitget."""
    all_candles = []
    end_time = int(end_date.timestamp() * 1000)
    start_time = int(start_date.timestamp() * 1000)
    current_end = end_time
    for _ in range(10):
        params = {
            "symbol": symbol,
            "granularity": granularity,
            "limit": 1000,
            "endTime": current_end,
            "startTime": start_time,
            "productType": "umcbl",
        }
        resp = requests.get(
            "https://api.bitget.com/api/v2/mix/market/candles",
            params=params,
            timeout=30,
        )
        data = resp.json()
        if data.get("code") != "00000" or not data.get("data"):
            break
        candles = data["data"]
        for c in candles:
            all_candles.append([
                int(c[0]), float(c[1]), float(c[2]),
                float(c[3]), float(c[4]), float(c[5])
            ])
        if len(candles) < 1000:
            break
        earliest = int(candles[-1][0])
        if earliest >= current_end:
            break
        current_end = earliest - 1
        time.sleep(0.1)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=[
        "timestamp", "open", "high", "low", "close", "volume"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

def run_symbol_backtest(symbol):
    """Run backtest for a single symbol (last 90 days, 4H)."""
    start_date = now - timedelta(days=90)
    df = fetch_candles(symbol, start_date, now, "4H")
    if len(df) < 30:
        print(f"WARN {symbol}: insufficient data ({len(df)} candles)")
        return None

    bt = Backtester(initial_balance=10000.0)
    result = bt.run(df=df, symbol=symbol)
    return result

def main():
    symbols_str = os.getenv("SYMBOLS", "BTCUSDT")
    symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
    print(f"Testing {len(symbols)} symbols: {', '.join(symbols)}")
    print()

    all_trades = []          # all trades across all symbols for portfolio PF
    results_by_symbol = {}   # symbol -> dict of metrics
    portfolio_balance = 10000.0
    portfolio_peak = 10000.0
    portfolio_drawdown = 0.0

    for sym in symbols:
        print(f"Running {sym}... ", end="", flush=True)
        result = run_symbol_backtest(sym)
        if result is None:
            print("[X] no data")
            continue

        # Individual metrics
        winrate = (result.winning_trades / result.total_trades * 100) if result.total_trades > 0 else 0
        pnl_pct = result.total_pnl_usd / result.initial_balance * 100

        # Profit Factor
        total_win = sum(t.pnl_usd for t in result.trades if t.pnl_usd > 0)
        total_loss = sum(t.pnl_usd for t in result.trades if t.pnl_usd < 0)
        pf = abs(total_win / total_loss) if total_loss != 0 else (999 if total_win > 0 else 0)

        results_by_symbol[sym] = {
            "trades": result.total_trades,
            "winrate": winrate,
            "pnl_usd": result.total_pnl_usd,
            "pnl_pct": pnl_pct,
            "maxdd_pct": result.max_drawdown_pct,
            "profit_factor": pf,
            "trades_list": result.trades,
        }
        all_trades.extend(result.trades)

        # Simple portfolio simulation: assume we allocate equal capital per symbol,
        # but only one position at a time (so we cannot sum directly).
        # We'll just sum PnL across symbols for total portfolio PnL.
        print(f"{result.total_trades} trades, {winrate:.1f}%, PnL={result.total_pnl_usd:+.2f}$ ({pnl_pct:+.2f}%)")

    # ===== Aggregated metrics =====
    print()
    print("=" * 90)
    print("INDIVIDUAL SYMBOL RESULTS")
    print("=" * 90)
    print(f"{'Symbol':<10} {'Trades':>6} {'WinRate':>8} {'PnL$':>10} {'PnL%':>8} {'MaxDD%':>8} {'PF':>6}")
    print("-" * 90)

    sorted_symbols = sorted(
        results_by_symbol.items(),
        key=lambda x: x[1]["pnl_pct"],
        reverse=True
    )

    total_pnl_usd = 0
    total_trades = 0
    total_winning_trades = 0
    worst_drawdown = 0.0

    for sym, metrics in sorted_symbols:
        print(f"{sym:<10} {metrics['trades']:>6} {metrics['winrate']:>7.1f}% "
              f"{metrics['pnl_usd']:>+9.2f} {metrics['pnl_pct']:>+7.2f}% "
              f"{metrics['maxdd_pct']:>7.2f}% {metrics['profit_factor']:>5.2f}")
        total_pnl_usd += metrics['pnl_usd']
        total_trades += metrics['trades']
        if metrics['trades_list']:
            winning = sum(1 for t in metrics['trades_list'] if t.pnl_usd > 0)
            total_winning_trades += winning
        if metrics['maxdd_pct'] > worst_drawdown:
            worst_drawdown = metrics['maxdd_pct']

    # Overall Profit Factor (across all trades)
    all_wins = sum(t.pnl_usd for t in all_trades if t.pnl_usd > 0)
    all_losses = sum(t.pnl_usd for t in all_trades if t.pnl_usd < 0)
    portfolio_pf = abs(all_wins / all_losses) if all_losses != 0 else (999 if all_wins > 0 else 0)

    total_pnl_pct = total_pnl_usd / 10000.0 * 100  # starting 10k per symbol simulation

    print("=" * 90)
    print("PORTFOLIO AGGREGATION (simple sum, 1 position max not modelled)")
    print("=" * 90)
    print(f"Total PnL:        ${total_pnl_usd:+.2f} ({total_pnl_pct:+.2f}%)")
    print(f"Total trades:     {total_trades}")
    print(f"Overall WinRate:  {(total_winning_trades / total_trades * 100) if total_trades > 0 else 0:.1f}%")
    print(f"Portfolio PF:     {portfolio_pf:.2f}")
    print(f"Worst MaxDD:      {worst_drawdown:.2f}%")
    print()

    # Best/worst symbols
    if sorted_symbols:
        best = sorted_symbols[0]
        worst = sorted_symbols[-1]
        print(f"Best:  {best[0]} (+{best[1]['pnl_pct']:.2f}%, {best[1]['trades']} trades)")
        print(f"Worst: {worst[0]} ({worst[1]['pnl_pct']:+.2f}%, {worst[1]['trades']} trades)")

    # Signal competition simulation would require multi-symbol backtest (future improvement)
    print()
    print("NOTE: Signal competition simulation (top-1 per day) not implemented.")
    print("      Would need multi-symbol Backtester with priority queue.")

if __name__ == "__main__":
    main()