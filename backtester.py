# -*- coding: utf-8 -*-
"""
backtester.py - Backtester for Dina
Run: python backtester.py

This backtester tests Dina's strategy on historical data.
Uses a simple strategy: buy when price drops 2% from recent high.
"""

import asyncio
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

START_BALANCE = 10000.0
START_DATE = datetime.now() - timedelta(days=180)
END_DATE = datetime.now()


class BacktestPosition:
    def __init__(self, symbol, side, entry_price, size_usd, sl_price, tp_price, timestamp):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.size_usd = size_usd
        self.sl_price = sl_price
        self.tp_price = tp_price
        self.entry_time = timestamp
        self.exit_time = None
        self.exit_price = None
        self.pnl_usd = 0.0
        self.pnl_pct = 0.0
        self.is_closed = False

    def update(self, current_price):
        if self.is_closed:
            return False, None

        if self.side == "long":
            if current_price <= self.sl_price:
                self._close(current_price, "SL")
                return True, current_price
            if self.tp_price and current_price >= self.tp_price:
                self._close(current_price, "TP")
                return True, current_price
        else:
            if current_price >= self.sl_price:
                self._close(current_price, "SL")
                return True, current_price
            if self.tp_price and current_price <= self.tp_price:
                self._close(current_price, "TP")
                return True, current_price

        return False, None

    def _close(self, exit_price, reason):
        self.exit_price = exit_price
        self.exit_time = datetime.now()
        self.is_closed = True

        if self.side == "long":
            self.pnl_pct = (exit_price - self.entry_price) / self.entry_price * 100
        else:
            self.pnl_pct = (self.entry_price - exit_price) / self.entry_price * 100

        self.pnl_usd = self.size_usd * self.pnl_pct / 100
        logger.info(f"Position closed: {self.symbol} {self.side} | PnL: {self.pnl_usd:+.2f}$ ({self.pnl_pct:+.2f}%)")


class BacktestResult:
    def __init__(self, initial_balance):
        self.initial_balance = initial_balance
        self.final_balance = initial_balance
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.max_drawdown_pct = 0.0
        self.max_drawdown_usd = 0.0
        self.total_pnl_usd = 0.0
        self.peak_balance = initial_balance
        self.trades = []

    def add_trade(self, position):
        self.trades.append(position)
        self.total_trades += 1

        if position.pnl_usd > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        self.total_pnl_usd += position.pnl_usd
        self.final_balance += position.pnl_usd

        if self.final_balance > self.peak_balance:
            self.peak_balance = self.final_balance

        drawdown_pct = (self.peak_balance - self.final_balance) / self.peak_balance * 100
        drawdown_usd = self.peak_balance - self.final_balance

        if drawdown_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = drawdown_pct
            self.max_drawdown_usd = drawdown_usd

    def print_summary(self):
        print("\n" + "="*60)
        print("BACKTEST RESULTS")
        print("="*60)

        total_return_pct = (self.final_balance - self.initial_balance) / self.initial_balance * 100

        print(f"Initial balance: ${self.initial_balance:,.2f}")
        print(f"Final balance:   ${self.final_balance:,.2f}")
        print(f"Total PnL:       ${self.total_pnl_usd:+,.2f} ({total_return_pct:+.2f}%)")
        print(f"Max drawdown:    ${self.max_drawdown_usd:,.2f} ({self.max_drawdown_pct:.2f}%)")
        print(f"Total trades:    {self.total_trades}")

        if self.total_trades > 0:
            win_rate = self.winning_trades / self.total_trades * 100
            print(f"Win rate:        {win_rate:.1f}%")

            if self.winning_trades > 0:
                avg_win = sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0) / self.winning_trades
                print(f"Average win:     ${avg_win:+,.2f}")

            if self.losing_trades > 0:
                avg_loss = sum(t.pnl_usd for t in self.trades if t.pnl_usd < 0) / self.losing_trades
                print(f"Average loss:    ${avg_loss:+,.2f}")

                profit_factor = abs(sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0) / sum(t.pnl_usd for t in self.trades if t.pnl_usd < 0))
                print(f"Profit factor:   {profit_factor:.2f}")

        print("="*60)


class Backtester:
    """Main backtester class for Dina."""
    def __init__(self, initial_balance=10000.0):
        self.initial_balance = initial_balance
        self.result = None

    def run(self, df=None, symbol="BTCUSDT"):
        """
        Run backtest on provided DataFrame.
        If df is None, generates synthetic data.
        Returns BacktestResult.
        """
        if df is None:
            df = self._generate_test_data(symbol)
        self.result = self._run_backtest(df, symbol)
        return self.result

    def _generate_test_data(self, symbol):
        """Generate synthetic OHLCV data for testing."""
        dates = pd.date_range(start=START_DATE, end=END_DATE, freq='4h')
        np.random.seed(42)
        prices = 50000 + np.cumsum(np.random.randn(len(dates)) * 1000)

        df = pd.DataFrame({
            'timestamp': dates,
            'open': prices - np.random.randn(len(dates)) * 100,
            'high': prices + np.abs(np.random.randn(len(dates)) * 200),
            'low': prices - np.abs(np.random.randn(len(dates)) * 200),
            'close': prices,
            'volume': np.random.randn(len(dates)) * 1000 + 10000
        })
        df.set_index('timestamp', inplace=True)
        logger.info(f"Generated {len(df)} test candles for {symbol}")
        return df

    def _run_backtest(self, df, symbol):
        """Core backtest logic."""
        result = BacktestResult(self.initial_balance)
        open_positions = {}

        for i, (timestamp, row) in enumerate(df.iterrows()):
            if i % 50 == 0:
                logger.info(f"Processed {i}/{len(df)} candles...")

            current_price = row['close']

            for sym in list(open_positions.keys()):
                position = open_positions[sym]
                closed, _ = position.update(current_price)

                if closed:
                    del open_positions[sym]
                    result.add_trade(position)

            if len(open_positions) == 0 and i > 20:
                lookback = 20
                if i >= lookback:
                    max_price = df['high'].iloc[i-lookback:i].max()

                    if current_price <= max_price * 0.98:
                        sl_price = current_price * 0.97
                        tp_price = current_price * 1.05
                        position_size = result.final_balance * 0.1

                        position = BacktestPosition(
                            symbol=symbol,
                            side="long",
                            entry_price=current_price,
                            size_usd=position_size,
                            sl_price=sl_price,
                            tp_price=tp_price,
                            timestamp=timestamp
                        )

                        open_positions[symbol] = position
                        logger.info(f"Opened position: {symbol} long | Price: {current_price:.2f}")

        for sym, position in list(open_positions.items()):
            last_price = df.iloc[-1]['close']
            position._close(last_price, "END_OF_BACKTEST")
            result.add_trade(position)

        return result


async def run_backtest():
    """Standalone async entry point."""
    logger.info("Starting Dina backtest for 180 days...")
    bt = Backtester(initial_balance=START_BALANCE)
    result = bt.run()
    result.print_summary()

    with open('backtest_results.json', 'w', encoding='utf-8') as f:
        results_dict = {
            'initial_balance': result.initial_balance,
            'final_balance': result.final_balance,
            'total_trades': result.total_trades,
            'winning_trades': result.winning_trades,
            'losing_trades': result.losing_trades,
            'max_drawdown_pct': result.max_drawdown_pct,
            'max_drawdown_usd': result.max_drawdown_usd,
            'total_pnl_usd': result.total_pnl_usd
        }
        json.dump(results_dict, f, indent=2, ensure_ascii=False)

    logger.info("Results saved to backtest_results.json")


if __name__ == "__main__":
    asyncio.run(run_backtest())