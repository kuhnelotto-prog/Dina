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
import requests
import time

from indicators_calc import IndicatorsCalculator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── ADX Filter ──────────────────────────────────────
# Symbols removed from trading (low liquidity / poor ADX performance)
ADX_BLACKLIST = {"UNIUSDT", "NEARUSDT", "FILUSDT"}  # these are no longer in SYMBOLS list

class ADXFilter:
    """
    ADX(14) trend-strength filter.
    Rejects entries when ADX < threshold or ADX is falling.
    """
    def __init__(self, threshold: float = 18.0, min_growth: float = 0.5):
        self.threshold = threshold
        self.min_growth = min_growth  # ADX must grow by at least this vs prev candle

    def check(self, adx: float, adx_prev: float) -> tuple:
        """
        Returns (passed: bool, reason: str).
        passed=True means trend is strong enough to trade.
        """
        if adx < self.threshold:
            return False, f"ADX={adx:.1f} < {self.threshold} (no trend)"
        growth = adx - adx_prev
        if growth < self.min_growth:
            return False, f"ADX falling: {adx_prev:.1f}->{adx:.1f} (growth={growth:+.1f} < {self.min_growth})"
        return True, f"ADX={adx:.1f} OK (growth={growth:+.1f})"

# ─────────────────────────────────────────────────────

START_BALANCE = 10000.0
START_DATE = datetime.utcnow() - timedelta(days=90)
END_DATE = datetime.utcnow() - timedelta(minutes=5)


class BacktestPosition:
    def __init__(self, symbol, side, entry_price, size_usd, sl_price, tp_price, timestamp):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.size_usd = size_usd
        self.sl_price = sl_price
        self.tp_price = tp_price
        self.initial_sl = sl_price      # original SL for RR calculation
        self.initial_tp = tp_price      # original TP
        self.entry_time = timestamp
        self.exit_time = None
        self.exit_price = None
        self.pnl_usd = 0.0
        self.pnl_pct = 0.0
        self.is_closed = False
        # Trailing state
        self.best_price = entry_price   # best favorable price seen
        self.initial_risk = abs(entry_price - sl_price)  # 1R in price units
        self.trailing_activated = False  # True once price moved >= 1R in our favor

    def update(self, current_price, high=None, low=None):
        """
        Update position with candle data.
        1. Check SL/TP by high/low (intra-candle)
        2. If not closed, apply trailing stop logic using close price
        """
        if self.is_closed:
            return False, None

        candle_high = high if high is not None else current_price
        candle_low = low if low is not None else current_price

        # ── Step 1: Check SL/TP hit by high/low ──
        if self.side == "long":
            if candle_low <= self.sl_price:
                self._close(self.sl_price, "TSL" if self.trailing_activated else "SL")
                return True, self.sl_price
            if self.tp_price and candle_high >= self.tp_price:
                self._close(self.tp_price, "TP")
                return True, self.tp_price
        else:  # short
            if candle_high >= self.sl_price:
                self._close(self.sl_price, "TSL" if self.trailing_activated else "SL")
                return True, self.sl_price
            if self.tp_price and candle_low <= self.tp_price:
                self._close(self.tp_price, "TP")
                return True, self.tp_price

        # ── Step 2: Trailing stop/profit logic (using close) ──
        self._apply_trailing(current_price, candle_high, candle_low)

        return False, None

    def _apply_trailing(self, close, high, low):
        """
        Trailing logic (same ATR-based params as live bot):
        - Track best_price (highest close for LONG, lowest close for SHORT)
        - At +1.0R: move SL to breakeven (entry price)
        - At +1.5R: move SL to entry + 0.5R (lock 0.5R profit)
        - Continuous: SL trails at best_price - 1.0R (LONG) / best_price + 1.0R (SHORT)
        - TP trails: best_price + 1.0R (LONG) / best_price - 1.0R (SHORT)
        """
        R = self.initial_risk
        if R <= 0:
            return

        if self.side == "long":
            # Update best price
            if close > self.best_price:
                self.best_price = close

            favorable_move = self.best_price - self.entry_price

            if favorable_move >= 1.0 * R:
                self.trailing_activated = True
                # Trailing SL: best_price - 1.0R, but never below entry (breakeven)
                new_sl = self.best_price - 1.0 * R
                new_sl = max(new_sl, self.entry_price)  # at least breakeven
                if new_sl > self.sl_price:
                    self.sl_price = new_sl

                # Trailing TP: best_price + 1.0R
                new_tp = self.best_price + 1.0 * R
                if new_tp > self.tp_price:
                    self.tp_price = new_tp

        else:  # short
            # Update best price (lowest)
            if close < self.best_price:
                self.best_price = close

            favorable_move = self.entry_price - self.best_price

            if favorable_move >= 1.0 * R:
                self.trailing_activated = True
                # Trailing SL: best_price + 1.0R, but never above entry (breakeven)
                new_sl = self.best_price + 1.0 * R
                new_sl = min(new_sl, self.entry_price)  # at least breakeven
                if new_sl < self.sl_price:
                    self.sl_price = new_sl

                # Trailing TP: best_price - 1.0R
                new_tp = self.best_price - 1.0 * R
                if new_tp < self.tp_price:
                    self.tp_price = new_tp

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
    def __init__(self, initial_balance=10000.0, use_real_data=False):
        self.initial_balance = initial_balance
        self.use_real_data = use_real_data
        self.result = None

    def run(self, df=None, symbol="BTCUSDT", btc_df=None):
        """
        Run backtest on provided DataFrame.
        If df is None, generates synthetic or real data.
        btc_df: optional BTC DataFrame for regime detection on non-BTC symbols.
        Returns BacktestResult.
        """
        if df is None:
            if self.use_real_data:
                df = self._fetch_real_data(symbol)
            else:
                df = self._generate_test_data(symbol)
        self.result = self._run_backtest(df, symbol, btc_df=btc_df)
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

    def _fetch_real_data(self, symbol, timeframe="4h"):
        """
        Fetch real historical candles from Bitget public API.
        Returns DataFrame with columns: timestamp, open, high, low, close, volume.
        """
        # Map timeframe to Bitget granularity
        tf_map = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "1h": "1H",
            "4h": "4H",
            "12h": "12H",
            "1d": "1D",
            "1w": "1W",
        }
        granularity = tf_map.get(timeframe, "4H")
        # Ensure we use 4H granularity
        if timeframe == "4h":
            granularity = "4H"
            logger.info(f"Using granularity: {granularity} for timeframe {timeframe}")
        # Bitget USDT-FUTURES product type
        product_type = "umcbl"
        limit = 1000  # max per request
        all_candles = []
        end_time = int(END_DATE.timestamp() * 1000)
        start_time = int(START_DATE.timestamp() * 1000)

        logger.info(f"Fetching real historical data for {symbol} {timeframe} from {START_DATE} to {END_DATE}")
        logger.info(f"start_time={start_time} ({datetime.fromtimestamp(start_time/1000)}), end_time={end_time} ({datetime.fromtimestamp(end_time/1000)})")

        current_end = end_time
        iteration = 0
        max_iterations = 10  # safety limit
        while current_end > start_time and iteration < max_iterations:
            iteration += 1
            logger.info(f"Iteration {iteration}: current_end={current_end} ({datetime.fromtimestamp(current_end/1000)})")
            url = "https://api.bitget.com/api/v2/mix/market/candles"
            params = {
                "symbol": symbol,
                "granularity": granularity,
                "limit": limit,
                "endTime": current_end,
                "startTime": start_time,
                "productType": product_type,
            }
            try:
                response = requests.get(url, params=params, timeout=30)
                data = response.json()
                if data.get("code") != "00000" or not data.get("data"):
                    logger.warning(f"API error: {data.get('msg')}")
                    logger.warning(f"Response: {data}")
                    break
                candles = data["data"]
                if not candles:
                    logger.info("No more candles returned")
                    break
                logger.info(f"Received {len(candles)} candles")
                # candles are returned in reverse chronological order (newest first)
                # each candle: [ts, open, high, low, close, volume, quoteVol]
                for c in candles:
                    ts = int(c[0])
                    all_candles.append([
                        ts,
                        float(c[1]),
                        float(c[2]),
                        float(c[3]),
                        float(c[4]),
                        float(c[5]),
                    ])
                logger.info(f"Added {len(candles)} candles to dataset")
                # If we received fewer candles than limit, we've got all data
                if len(candles) < limit:
                    logger.info(f"Received {len(candles)} < limit {limit}, assuming all data fetched")
                    break
                # Move window backward: set current_end to earliest timestamp - 1
                earliest_ts = int(candles[-1][0])
                logger.info(f"Earliest timestamp: {earliest_ts} ({datetime.fromtimestamp(earliest_ts/1000)})")
                if earliest_ts >= current_end:
                    logger.info("Earliest timestamp >= current_end, breaking")
                    break
                current_end = earliest_ts - 1
                time.sleep(0.1)  # rate limiting
            except Exception as e:
                logger.error(f"Error fetching historical data: {e}")
                break
        logger.info(f"Finished fetching after {iteration} iterations, total candles: {len(all_candles)}")

        if not all_candles:
            logger.warning("No real data fetched, falling back to synthetic data")
            return self._generate_test_data(symbol)

        # Convert to DataFrame
        df = pd.DataFrame(
            all_candles,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.sort_values("timestamp").reset_index(drop=True)
        df.set_index("timestamp", inplace=True)
        logger.info(f"Fetched {len(df)} real candles for {symbol}")
        # Validate expected count
        expected = 90 * 6  # 90 days * 6 candles per day (4h)
        if len(df) != expected:
            logger.warning(f"Expected ~{expected} candles for 90 days at 4h, but got {len(df)}. Data may be incomplete or overlapping.")
        return df

    def _run_backtest(self, df, symbol, btc_df=None):
        """Core backtest logic — uses IndicatorsCalculator + composite score."""
        result = BacktestResult(self.initial_balance)
        open_positions = {}
        calc = IndicatorsCalculator()
        adx_filter = ADXFilter(threshold=18.0, min_growth=0.5)

        # ── Blacklist check ──
        if symbol in ADX_BLACKLIST:
            logger.info(f"SKIP {symbol}: in ADX blacklist")
            return result

        # Signal weights (same as signal_builder.py defaults)
        weights = {
            "ema_cross": 1.0,
            "volume_spike": 1.0,
            "engulfing": 0.8,
            "fvg": 0.6,
            "macd_cross": 0.5,
            "rsi_filter": 0.4,
            "bb_squeeze": 0.3,
            "sweep": 0.7,
        }

        # Динамические пороги по BTC EMA50 на 4H (synced with strategist_client)
        THRESHOLD_LONG_BULL = 0.30    # BTC bullish → LONG агрессивнее
        THRESHOLD_LONG_BEAR = 0.45    # BTC bearish → LONG консервативнее
        THRESHOLD_SHORT_BULL = 0.45   # BTC bullish → SHORT консервативнее
        THRESHOLD_SHORT_BEAR = 0.30   # BTC bearish → SHORT агрессивнее

        # Precompute BTC EMA50 for regime detection
        btc_ema50 = None
        if symbol == "BTCUSDT":
            close_series = df['close']
            btc_ema50 = close_series.ewm(span=50, adjust=False).mean()
        elif btc_df is not None and len(btc_df) >= 50:
            btc_close = btc_df['close']
            btc_ema50 = btc_close.ewm(span=50, adjust=False).mean()

        for i, (timestamp, row) in enumerate(df.iterrows()):
            if i % 50 == 0:
                logger.info(f"Processed {i}/{len(df)} candles...")

            current_price = row['close']

            # Update open positions with high/low for accurate SL/TP
            candle_high = row['high']
            candle_low = row['low']
            for sym in list(open_positions.keys()):
                position = open_positions[sym]
                closed, _ = position.update(current_price, high=candle_high, low=candle_low)

                if closed:
                    del open_positions[sym]
                    result.add_trade(position)

            # Need at least 50 candles for EMA50 + indicators
            if len(open_positions) == 0 and i >= 50:
                # Compute indicators on all candles up to current
                slice_df = df.iloc[:i+1].copy()
                indicators = calc.compute(slice_df)

                if "error" in indicators:
                    continue

                # ── ADX Filter (BEFORE Score) ──
                adx_val = indicators.get("adx", 0.0)
                adx_prev = indicators.get("adx_prev", 0.0)
                adx_ok, adx_reason = adx_filter.check(adx_val, adx_prev)
                if not adx_ok:
                    continue  # skip: no trend or ADX falling

                # Calculate composite score (STATE + EVENT)
                composite = self._compute_composite(indicators, weights)

                # Determine BTC regime for dynamic thresholds
                if btc_ema50 is not None:
                    if symbol == "BTCUSDT":
                        # For BTC: use current price vs BTC EMA50 at same index
                        btc_regime = "BULL" if current_price > btc_ema50.iloc[i] else "BEAR"
                    else:
                        # For alts: find closest BTC EMA50 by timestamp
                        try:
                            idx = btc_ema50.index.get_indexer([timestamp], method='nearest')[0]
                            btc_price = btc_df['close'].iloc[idx]
                            btc_regime = "BULL" if btc_price > btc_ema50.iloc[idx] else "BEAR"
                        except Exception:
                            btc_regime = "BULL"
                else:
                    btc_regime = "BULL"  # default for non-BTC symbols without BTC data

                threshold_long = THRESHOLD_LONG_BULL if btc_regime == "BULL" else THRESHOLD_LONG_BEAR
                threshold_short = THRESHOLD_SHORT_BEAR if btc_regime == "BEAR" else THRESHOLD_SHORT_BULL

                # Determine direction based on composite score
                is_bullish = indicators["ema_fast"] > indicators["ema_slow"]
                rsi = indicators.get("rsi", 50)

                # ── LONG entry ──
                if composite > threshold_long and is_bullish and rsi < 70:
                    atr_pct = indicators.get("atr_pct", 0)
                    if atr_pct > 0.1:
                        sl_pct = 1.5 * atr_pct / 100
                        tp_pct = 3.0 * atr_pct / 100
                    else:
                        sl_pct = 0.03
                        tp_pct = 0.05
                    sl_price = current_price * (1 - sl_pct)
                    tp_price = current_price * (1 + tp_pct)
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
                    logger.info(f"Opened LONG: {symbol} | Price: {current_price:.2f} | Score: {composite:.3f}")

                # ── SHORT entry ──
                elif composite < -threshold_short and not is_bullish and rsi > 30:
                    atr_pct = indicators.get("atr_pct", 0)
                    if atr_pct > 0.1:
                        sl_pct = 1.5 * atr_pct / 100
                        tp_pct = 3.0 * atr_pct / 100
                    else:
                        sl_pct = 0.03
                        tp_pct = 0.05
                    sl_price = current_price * (1 + sl_pct)
                    tp_price = current_price * (1 - tp_pct)
                    position_size = result.final_balance * 0.1

                    position = BacktestPosition(
                        symbol=symbol,
                        side="short",
                        entry_price=current_price,
                        size_usd=position_size,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        timestamp=timestamp
                    )
                    open_positions[symbol] = position
                    logger.info(f"Opened SHORT: {symbol} | Price: {current_price:.2f} | Score: {composite:.3f}")

        for sym, position in list(open_positions.items()):
            last_price = df.iloc[-1]['close']
            position._close(last_price, "END_OF_BACKTEST")
            result.add_trade(position)

        return result

    @staticmethod
    def _compute_composite(indicators: dict, weights: dict) -> float:
        """
        Compute weighted composite score from indicators.
        
        Архитектура v2 (STATE + EVENT):
        - STATE слой (60%): ema_trend + rsi_zone + macd_hist + bb_position
          Активны на КАЖДОЙ свече, дают базовый фон.
        - EVENT слой (40%): ema_cross + engulfing + fvg + sweep
          Редкие бонусы, усиливают сигнал при совпадении.
        - Volume spike = множитель (не в score).
        
        Returns score from -1 to 1.
        """
        # ── STATE слой (базовый фон, max = 4.0) ──
        state_score = 0.0
        state_max = 4.0  # 4 компонента по 1.0

        # 1. EMA trend state (вес 1.0)
        if indicators["ema_fast"] > indicators["ema_slow"]:
            state_score += 1.0   # бычий тренд
        elif indicators["ema_fast"] < indicators["ema_slow"]:
            state_score -= 1.0   # медвежий тренд

        # 2. RSI zone (вес 1.0)
        rsi = indicators.get("rsi", 50)
        if rsi > 70:
            state_score -= 1.0   # перекупленность → медвежий
        elif rsi < 30:
            state_score += 1.0   # перепроданность → бычий
        elif rsi > 60:
            state_score -= 0.4   # слабо медвежий
        elif rsi < 40:
            state_score += 0.4   # слабо бычий

        # 3. MACD histogram (вес 1.0)
        macd = indicators.get("macd", 0)
        macd_signal = indicators.get("macd_signal", 0)
        macd_hist = macd - macd_signal
        if macd_hist < 0:
            state_score -= 1.0   # медвежий
        elif macd_hist > 0:
            state_score += 1.0   # бычий

        # 4. Bollinger position (вес 1.0)
        price = indicators.get("price", 0)
        bb_upper = indicators.get("bb_upper", 0)
        bb_lower = indicators.get("bb_lower", 0)
        bb_middle = indicators.get("bb_middle", 0)
        if bb_upper > 0 and price > bb_upper:
            state_score -= 1.0   # перекупленность → медвежий
        elif bb_lower > 0 and price < bb_lower:
            state_score += 1.0   # перепроданность → бычий
        elif bb_middle > 0:
            bb_width = (bb_upper - bb_lower) / bb_middle
            if bb_width < 0.05:
                state_score += 0.3  # BB squeeze → готовность к движению

        # Нормализуем state: [-1, +1]
        state_normalized = state_score / state_max if state_max > 0 else 0.0

        # ── EVENT слой (бонусы) ──
        event_score = 0.0
        ema_cross_weight = weights.get("ema_cross", 1.0)
        engulfing_weight = weights.get("engulfing", 0.8)
        fvg_weight = weights.get("fvg", 0.6)
        sweep_weight = weights.get("sweep", 0.7)
        event_max = ema_cross_weight + engulfing_weight + fvg_weight + sweep_weight

        # EMA cross (event)
        ema_cross_bull = (
            indicators["ema_fast"] > indicators["ema_slow"] and
            indicators.get("ema_fast_prev", 0) <= indicators.get("ema_slow_prev", 0)
        )
        ema_cross_bear = (
            indicators["ema_fast"] < indicators["ema_slow"] and
            indicators.get("ema_fast_prev", 0) >= indicators.get("ema_slow_prev", 0)
        )
        if ema_cross_bull:
            event_score += ema_cross_weight
        elif ema_cross_bear:
            event_score -= ema_cross_weight

        # Engulfing
        if indicators.get("engulfing_bull", False):
            event_score += engulfing_weight
        elif indicators.get("engulfing_bear", False):
            event_score -= engulfing_weight

        # FVG
        if indicators.get("fvg_bull", False):
            event_score += fvg_weight
        elif indicators.get("fvg_bear", False):
            event_score -= fvg_weight

        # Sweep
        if indicators.get("sweep_bull", False):
            event_score += sweep_weight
        elif indicators.get("sweep_bear", False):
            event_score -= sweep_weight

        # Нормализуем event: [-1, +1]
        event_normalized = event_score / event_max if event_max > 0 else 0.0

        # ── Итоговый composite: 60% state + 40% event ──
        composite = 0.60 * state_normalized + 0.40 * event_normalized

        # Volume spike как множитель
        volume_spike_multiplier = weights.get("volume_spike", 1.2)
        if indicators.get("volume_ratio", 1.0) > 1.2:
            composite *= volume_spike_multiplier

        # Clamp to [-1, +1]
        return max(-1.0, min(1.0, composite))


async def run_backtest(use_real_data=False):
    """Standalone async entry point."""
    import os
    # Remove previous results to avoid caching
    if os.path.exists('backtest_results.json'):
        os.remove('backtest_results.json')
        logger.info("Removed previous backtest_results.json")
    
    if use_real_data:
        logger.info("Starting Dina backtest for 90 days with REAL historical data...")
    else:
        logger.info("Starting Dina backtest for 90 days with SYNTHETIC data...")
    bt = Backtester(initial_balance=START_BALANCE, use_real_data=use_real_data)
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
    import argparse
    parser = argparse.ArgumentParser(description="Run Dina backtest")
    parser.add_argument("--real", action="store_true", help="Use real historical data from Bitget")
    args = parser.parse_args()
    asyncio.run(run_backtest(use_real_data=args.real))
