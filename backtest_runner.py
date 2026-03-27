"""
backtest_runner.py — CLI для бэктеста.
"""

import argparse
import asyncio

from backtester import Backtester

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args()

    print(f"Backtest for {args.symbol} over {args.days} days (stub)")
    bt = Backtester()
    result = bt.run(None, None)
    print(result)

if __name__ == "__main__":
    asyncio.run(main())