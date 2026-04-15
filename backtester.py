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
from datetime import datetime, timedelta, timezone
import json
import requests
import time

from indicators_calc import IndicatorsCalculator
from config import TRAILING_STAGES  # единый источник правды (shared with trailing_manager.py)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── ADX Filter ──────────────────────────────────────
# Symbols removed from trading (low liquidity / poor ADX performance)
ADX_BLACKLIST = {"UNIUSDT", "NEARUSDT", "FILUSDT"}  # these are no longer in SYMBOLS list

class ADXFilter:
    """
    ADX(14) trend-strength filter.
    Rejects entries when ADX < threshold.
    Note: adx_growth check removed in P3 (was overtight, rejected profitable signals).
    """
    def __init__(self, threshold: float = 18.0, min_growth: float = None):
        self.threshold = threshold
        self.min_growth = min_growth  # deprecated, kept for API compat

    def check(self, adx: float, adx_prev: float = 0.0) -> tuple:
        """
        Returns (passed: bool, reason: str).
        passed=True means trend is strong enough to trade.
        """
        if adx < self.threshold:
            return False, f"ADX={adx:.1f} < {self.threshold} (no trend)"
        return True, f"ADX={adx:.1f} OK"

# ─────────────────────────────────────────────────────

START_BALANCE = 10000.0
BASE_RISK_PCT = 1.0      # % баланса на риск за сделку (как в PositionSizer)
LEVERAGE = 10             # плечо (как в живой системе)
SLIPPAGE_PCT = 0.0005     # 0.05% slippage на market ордера
FUNDING_RATE = 0.0001     # 0.01% каждые 8 часов
FUNDING_INTERVAL_H = 8   # интервал funding в часах
DAILY_LOSS_LIMIT_PCT = 5.0  # 5% дневной лимит потерь
MAX_PORTFOLIO_VAR_PCT = 15.0  # максимум 15% портфеля под риском (VaR)
MAX_SHORT_OPEN = 3           # не более 3 шортов одновременно
MAX_OPEN_POSITIONS = 3       # максимум 3 позиции одновременно
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT", "ADAUSDT", "SUIUSDT"]
ATR_CRISIS_MULTIPLIER = 3.0   # ATR > 3× среднего → CRISIS
ATR_VOLATILE_MULTIPLIER = 2.0  # ATR > 2× среднего → VOLATILE
VOLATILE_SIZE_REDUCTION = 0.5  # при VOLATILE: размер позиции × 0.5
POSITION_TIMEOUT_H = 96        # максимум 96 часов в позиции
MIN_EXPECTED_PNL_PCT = -0.5   # закрыть если PnL < -0.5% после 48ч
MIN_PNL_CHECK_H = 48          # проверять PnL после 48ч
START_DATE = datetime.now(timezone.utc) - timedelta(days=90)
END_DATE = datetime.now(timezone.utc) - timedelta(minutes=5)


class BacktestPosition:
    """
    Position with 4-step trailing stop matching trailing_manager.py TRAILING_STAGES:
      Step 1: +0.5×ATR → SL to breakeven
      Step 2: +1.0×ATR → partial close 25%, SL to +0.5×ATR
      Step 3: +1.5×ATR → partial close 25%, SL to +1.0×ATR
      Step 4: +2.0×ATR → close everything (hard TP)
    Uses ATR (not R) for activation — synced with trailing_manager.py.
    ATR is fixed at entry time (same as live system).
    """
    def __init__(self, symbol, side, entry_price, size_usd, sl_price, tp_price, timestamp, entry_atr=0.0):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.size_usd = size_usd
        self.sl_price = sl_price
        self.tp_price = None           # TP disabled — trailing handles exits
        self.initial_sl = sl_price
        self.initial_tp = tp_price     # kept for reference only
        self.entry_time = timestamp
        self.exit_time = None
        self.exit_price = None
        self.pnl_usd = 0.0
        self.pnl_pct = 0.0
        self.is_closed = False
        # 4-step trailing state (mirrors trailing_manager.py)
        self.entry_atr = entry_atr if entry_atr > 0 else (
            abs(entry_price - sl_price) / 1.5 if abs(entry_price - sl_price) > 0
            else entry_price * 0.015  # fallback: 1.5% от цены (synced with position_monitor.py)
        )
        self.initial_risk = abs(entry_price - sl_price)  # SL distance in price units (= 1.5×ATR)
        self.trailing_step = 0         # 0-4
        self.remaining_pct = 1.0       # fraction of original size still open
        self.partial_pnl_usd = 0.0     # accumulated PnL from partial closes
        self.total_funding = 0.0      # accumulated funding cost
        self._funding_hours_accrued = 0  # сколько 8-часовых funding-интервалов уже начислено

    def update(self, current_price, high=None, low=None, timestamp=None):
        """
        Update position with candle data.
        1. Check SL hit by intra-candle high/low
        2. Apply 4-step trailing using close price
        Returns (closed: bool, exit_price or None)
        """
        if self.is_closed:
            return False, None

        self._current_timestamp = timestamp  # store for _close()

        candle_high = high if high is not None else current_price
        candle_low = low if low is not None else current_price

        # ── Check SL hit (with slippage — market order) ──
        if self.side == "long":
            if candle_low <= self.sl_price:
                reason = "TSL" if self.trailing_step > 0 else "SL"
                exit_price = self.sl_price * (1 - SLIPPAGE_PCT)
                self._close(exit_price, reason)
                return True, exit_price
        else:  # short
            if candle_high >= self.sl_price:
                reason = "TSL" if self.trailing_step > 0 else "SL"
                exit_price = self.sl_price * (1 + SLIPPAGE_PCT)
                self._close(exit_price, reason)
                return True, exit_price

        # ── 4-step trailing (using close price) ──
        closed = self._apply_trailing_4step(current_price)
        if closed:
            return True, current_price

        return False, None

    def _apply_trailing_4step(self, close):
        """
        4-step trailing synced with config.TRAILING_STAGES (single source of truth).
        partial_close_pct = доля ОРИГИНАЛЬНОЙ позиции (как в trailing_manager.py).
        Система пересчитывает в долю от текущего остатка автоматически.
        Returns True if position fully closed at step 4.
        """
        ATR = self.entry_atr
        if ATR <= 0:
            return False

        # Calculate price movement in ATR units
        if self.side == "long":
            atr_move = (close - self.entry_price) / ATR
        else:
            atr_move = (self.entry_price - close) / ATR

        step = self.trailing_step

        for stage_cfg in TRAILING_STAGES:
            stage_num = stage_cfg["stage"]
            activation = stage_cfg["activation_atr"]

            if step >= stage_num:
                continue  # already passed this stage
            if atr_move < activation:
                break  # not yet reached this level

            # === Stage activated ===

            if stage_num == 4:
                # Step 4 — close everything
                self.trailing_step = 4
                tp_price = close * (1 - SLIPPAGE_PCT) if self.side == "long" else close * (1 + SLIPPAGE_PCT)
                self._close(tp_price, "TP_2ATR")
                logger.debug(f"  TSL step 4: {self.symbol} full close at +{activation}×ATR")
                return True

            # Compute new SL
            sl_atr_offset = stage_cfg["sl_atr"]
            if self.side == "long":
                new_sl = self.entry_price + ATR * sl_atr_offset
            else:
                new_sl = self.entry_price - ATR * sl_atr_offset
            self.sl_price = new_sl
            self.trailing_step = stage_num

            # Partial close — partial_close_pct = доля от ОРИГИНАЛА
            # _partial_close(pct_of_current) = доля от текущего остатка
            # Конвертируем: pct_of_current = partial_close_pct / remaining_pct
            partial_pct = stage_cfg["partial_close_pct"]  # доля от оригинала
            if partial_pct > 0 and self.remaining_pct > 0:
                pct_of_current = partial_pct / self.remaining_pct
                pct_of_current = min(pct_of_current, 1.0)  # safety clamp
                self._partial_close(pct_of_current, close)
                desc = stage_cfg["description"]
                logger.debug(
                    f"  TSL step {stage_num}: {self.symbol} {desc} "
                    f"(pct_of_current={pct_of_current*100:.1f}%, remaining={self.remaining_pct*100:.0f}%), "
                    f"SL->{new_sl:.2f}"
                )
            elif sl_atr_offset == 0.0:
                # Step 1: breakeven, no partial close
                logger.debug(f"  TSL step {stage_num}: {self.symbol} SL->breakeven {new_sl:.2f}")

        return False

    def _partial_close(self, pct, price):
        """Close pct of remaining position, book partial PnL (incl. commission + slippage)."""
        # Apply slippage to partial close price (market order)
        if self.side == "long":
            exec_price = price * (1 - SLIPPAGE_PCT)
        else:
            exec_price = price * (1 + SLIPPAGE_PCT)
        close_fraction = self.remaining_pct * pct
        if self.side == "long":
            pnl_pct = (exec_price - self.entry_price) / self.entry_price * 100
        else:
            pnl_pct = (self.entry_price - exec_price) / self.entry_price * 100
        partial_size = self.size_usd * close_fraction
        partial_pnl = partial_size * pnl_pct / 100
        # Commission: 0.06% on the partial close (exit side only; entry already deducted)
        partial_pnl -= partial_size * 0.0006
        self.partial_pnl_usd += partial_pnl
        self.remaining_pct -= close_fraction

    def _close(self, exit_price, reason, timestamp=None):
        self.exit_price = exit_price
        self.exit_time = timestamp or getattr(self, '_current_timestamp', None) or datetime.now()
        self.is_closed = True
        self.exit_reason = reason

        if self.side == "long":
            exit_pnl_pct = (exit_price - self.entry_price) / self.entry_price * 100
        else:
            exit_pnl_pct = (self.entry_price - exit_price) / self.entry_price * 100

        # PnL = partial closes already booked + remaining fraction at exit
        remaining_size = self.size_usd * self.remaining_pct
        remaining_pnl = remaining_size * exit_pnl_pct / 100
        # Commission: 0.06% entry (full size) + 0.06% exit (remaining size)
        entry_commission = self.size_usd * 0.0006
        exit_commission = remaining_size * 0.0006
        self.pnl_usd = self.partial_pnl_usd + remaining_pnl - entry_commission - exit_commission - self.total_funding
        self.pnl_pct = self.pnl_usd / self.size_usd * 100 if self.size_usd else 0

        step_info = f" step={self.trailing_step}" if self.trailing_step > 0 else ""
        logger.info(
            f"Position closed: {self.symbol} {self.side} [{reason}]{step_info} | "
            f"PnL: {self.pnl_usd:+.2f}$ ({self.pnl_pct:+.2f}%) "
            f"remaining={self.remaining_pct*100:.0f}%"
        )


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

                sum_losses = sum(t.pnl_usd for t in self.trades if t.pnl_usd < 0)
                sum_wins = sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0)
                profit_factor = abs(sum_wins / sum_losses) if sum_losses != 0 else float('inf')
                print(f"Profit factor:   {profit_factor:.2f}")

        # Exit reason breakdown
        if self.total_trades > 0:
            from collections import Counter
            reasons = Counter()
            for t in self.trades:
                reason = getattr(t, 'exit_reason', 'UNKNOWN')
                reasons[reason] += 1
            print(f"\nExit reasons:")
            for reason, count in reasons.most_common():
                pct = count / self.total_trades * 100
                # Calculate PnL for this reason
                reason_pnl = sum(t.pnl_usd for t in self.trades if getattr(t, 'exit_reason', '') == reason)
                print(f"  {reason:20s}: {count:3d} ({pct:5.1f}%) | PnL: ${reason_pnl:+,.2f}")

        print("="*60)


class Backtester:
    """Main backtester class for Dina."""
    def __init__(self, initial_balance=10000.0, use_real_data=False):
        self.initial_balance = initial_balance
        self.use_real_data = use_real_data
        self.result = None

    def run(self, dfs=None, symbols=None, btc_df=None, btc_1d_df=None):
        """
        Run backtest on provided DataFrames.
        dfs: dict {symbol: DataFrame} — if None, fetches/generates for all symbols.
        symbols: list of symbols — if None, uses SYMBOLS constant.
        btc_df: optional BTC 4H DataFrame for regime detection.
        btc_1d_df: optional BTC 1D DataFrame for EMA50 master filter.
        Returns BacktestResult.
        """
        if symbols is None:
            symbols = SYMBOLS
        if dfs is None:
            dfs = {}
            for sym in symbols:
                if self.use_real_data:
                    dfs[sym] = self._fetch_real_data(sym)
                else:
                    dfs[sym] = self._generate_test_data(sym)
        self.result = self._run_backtest(dfs, symbols, btc_df=btc_df, btc_1d_df=btc_1d_df)
        return self.result

    def _generate_test_data(self, symbol):
        """Generate synthetic OHLCV data for testing."""
        dates = pd.date_range(start=START_DATE, end=END_DATE, freq='4h')
        # Different seed per symbol for unique data
        np.random.seed(hash(symbol) % (2**32))
        price_bases = {"BTCUSDT": 50000, "ETHUSDT": 3000, "BNBUSDT": 600,
                       "XRPUSDT": 0.6, "SOLUSDT": 100, "LINKUSDT": 15,
                       "DOGEUSDT": 0.1, "AVAXUSDT": 30, "ADAUSDT": 0.45,
                       "SUIUSDT": 1.5}
        base = price_bases.get(symbol, 100)
        # Proportional random walk — never goes negative
        returns = np.random.randn(len(dates)) * 0.02  # 2% std per candle
        returns[0] = 0  # start at base
        prices = base * np.exp(np.cumsum(returns))
        # OHLCV with proportional noise (scaled to price level)
        noise_scale = base * 0.005  # 0.5% noise for O/H/L
        df = pd.DataFrame({
            'timestamp': dates,
            'open': prices * (1 + np.random.randn(len(dates)) * 0.003),
            'high': prices * (1 + np.abs(np.random.randn(len(dates))) * 0.01),
            'low': prices * (1 - np.abs(np.random.randn(len(dates))) * 0.01),
            'close': prices,
            'volume': np.abs(np.random.randn(len(dates)) * base * 10) + base * 100
        })
        # Ensure high >= max(open, close) and low <= min(open, close)
        df['high'] = df[['high', 'open', 'close']].max(axis=1)
        df['low'] = df[['low', 'open', 'close']].min(axis=1)
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

    def _run_backtest(self, dfs, symbols, btc_df=None, btc_1d_df=None):
        """Core backtest logic — multi-symbol portfolio with IndicatorsCalculator + composite score."""
        result = BacktestResult(self.initial_balance)
        open_positions = {}       # symbol -> BacktestPosition
        pending_signals = {}     # symbol -> dict with side, sl_pct, tp_pct, composite

        # Per-symbol indicator calculators
        calcs = {sym: IndicatorsCalculator() for sym in symbols}
        adx_filter = ADXFilter(threshold=18.0, min_growth=0.5)

        # Filter out blacklisted symbols
        active_symbols = [s for s in symbols if s not in ADX_BLACKLIST]

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

        # Динамические пороги по BTC EMA50 на 4H (P3 tuned, synced with strategist_client)
        THRESHOLD_LONG_BULL = 0.30
        THRESHOLD_LONG_BEAR = 0.40
        THRESHOLD_SHORT_BULL = 0.45   # synced with strategist_client: ENTRY_THRESHOLD_SHORT_BULL
        THRESHOLD_SHORT_BEAR = 0.35   # synced with strategist_client: ENTRY_THRESHOLD_SHORT_BEAR

        # Precompute BTC EMA50 for regime detection (4H) — shared across all symbols
        btc_ema50 = None
        if "BTCUSDT" in dfs and len(dfs["BTCUSDT"]) >= 50:
            btc_ema50 = dfs["BTCUSDT"]['close'].ewm(span=50, adjust=False).mean()
        elif btc_df is not None and len(btc_df) >= 50:
            btc_ema50 = btc_df['close'].ewm(span=50, adjust=False).mean()

        # ── BTC ATR for MarketRegime detection (bug 12) ──
        btc_atr = None
        btc_atr_mean = None
        btc_df_for_atr = dfs.get("BTCUSDT", btc_df)
        if btc_df_for_atr is not None and len(btc_df_for_atr) >= 14:
            btc_hlc = btc_df_for_atr[['high', 'low', 'close']].copy()
            tr = pd.concat([
                btc_hlc['high'] - btc_hlc['low'],
                (btc_hlc['high'] - btc_hlc['close'].shift(1)).abs(),
                (btc_hlc['low'] - btc_hlc['close'].shift(1)).abs()
            ], axis=1).max(axis=1)
            btc_atr = tr.rolling(14).mean()
            btc_atr_mean = btc_atr.rolling(50).mean()
            logger.info("BTC ATR regime detection ENABLED")

        # ── BTC 1D EMA50 Master Filter ──
        btc_1d_ema50 = None
        if btc_1d_df is not None and len(btc_1d_df) >= 50:
            btc_1d_ema50 = btc_1d_df['close'].ewm(span=50, adjust=False).mean()
            logger.info(f"BTC 1D EMA50 master filter ENABLED ({len(btc_1d_df)} daily candles)")
        else:
            logger.info("BTC 1D EMA50 master filter DISABLED (no 1D data)")

        # Master timeline: use BTCUSDT if available, otherwise first symbol
        master_sym = "BTCUSDT" if "BTCUSDT" in dfs else active_symbols[0] if active_symbols else None
        if master_sym is None or master_sym not in dfs:
            logger.warning("No data available for backtest")
            return result
        master_df = dfs[master_sym]

        logger.info(f"Multi-symbol backtest: {active_symbols} | master timeline: {master_sym}")

        # ── Daily loss limit tracking ──
        current_day = None
        day_start_balance = self.initial_balance

        for i, (timestamp, row) in enumerate(master_df.iterrows()):
            if i % 50 == 0:
                logger.info(f"Processed {i}/{len(master_df)} candles | open={len(open_positions)}")

            # ── Daily loss limit: reset at new day ──
            candle_date = timestamp.date() if hasattr(timestamp, 'date') else timestamp
            if candle_date != current_day:
                current_day = candle_date
                day_start_balance = result.final_balance

            # ── MarketRegime check (bug 12): compute regime for this candle ──
            market_regime = "NORMAL"
            if btc_atr is not None and btc_atr_mean is not None:
                try:
                    atr_idx = btc_atr.index.get_indexer([timestamp], method='nearest')[0]
                    atr_current = btc_atr.iloc[atr_idx]
                    atr_mean_val = btc_atr_mean.iloc[atr_idx]
                    if not pd.isna(atr_current) and not pd.isna(atr_mean_val) and atr_mean_val > 0:
                        if atr_current > atr_mean_val * ATR_CRISIS_MULTIPLIER:
                            market_regime = "CRISIS"
                        elif atr_current > atr_mean_val * ATR_VOLATILE_MULTIPLIER:
                            market_regime = "VOLATILE"
                except Exception:
                    pass

            # ── Funding rate: accrue for each open position ──
            # Funding is charged every 8 hours (3×/day).
            # Используем elapsed hours с момента входа — надёжно для любого таймфрейма свечей.
            for sym in open_positions:
                pos = open_positions[sym]
                try:
                    elapsed_h = (timestamp - pos.entry_time).total_seconds() / 3600
                except Exception:
                    elapsed_h = 0
                # Сколько 8-часовых интервалов пройдено с момента входа
                intervals_elapsed = int(elapsed_h / FUNDING_INTERVAL_H)
                new_intervals = intervals_elapsed - pos._funding_hours_accrued
                if new_intervals > 0:
                    pos.total_funding += pos.size_usd * FUNDING_RATE * new_intervals
                    pos._funding_hours_accrued = intervals_elapsed

            # ── Execute pending signals at this candle's open ──
            for sym in list(pending_signals.keys()):
                sig = pending_signals.pop(sym)
                if sym in open_positions:
                    continue
                if len(open_positions) >= MAX_OPEN_POSITIONS:
                    continue

                # Short limit check (bug 8)
                if sig["side"] == "short":
                    open_shorts = sum(1 for p in open_positions.values() if p.side == "short")
                    if open_shorts >= MAX_SHORT_OPEN:
                        continue

                sym_df = dfs.get(sym)
                if sym_df is None or timestamp not in sym_df.index:
                    continue
                sym_row = sym_df.loc[timestamp]
                candle_open = sym_row['open']

                # Slippage on entry (market order)
                if sig["side"] == "long":
                    entry_price = candle_open * (1 + SLIPPAGE_PCT)
                else:
                    entry_price = candle_open * (1 - SLIPPAGE_PCT)
                if sig["side"] == "long":
                    sl_price = entry_price * (1 - sig["sl_pct"])
                    tp_price = entry_price * (1 + sig["tp_pct"])
                else:
                    sl_price = entry_price * (1 + sig["sl_pct"])
                    tp_price = entry_price * (1 - sig["tp_pct"])

                # PositionSizer logic (matches live system)
                risk_usd = result.final_balance * BASE_RISK_PCT / 100
                notional_usd = risk_usd / sig["sl_pct"]
                # Volatile regime: reduce position size (bug 12)
                if market_regime == "VOLATILE":
                    notional_usd *= VOLATILE_SIZE_REDUCTION
                position_size = notional_usd
                position = BacktestPosition(
                    symbol=sym,
                    side=sig["side"],
                    entry_price=entry_price,
                    size_usd=position_size,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    timestamp=timestamp,
                    entry_atr=sig.get("atr", 0.0)  # pass ATR at entry for trailing (synced with live system)
                )
                open_positions[sym] = position
                logger.info(f"Opened {sig['side'].upper()}: {sym} | Price: {entry_price:.2f} | Score: {sig['composite']:.3f}")

            # ── Update open positions with their symbol's candle data ──
            closed_syms = []
            for sym in list(open_positions.keys()):
                pos = open_positions[sym]
                sym_df = dfs.get(sym)
                if sym_df is None or timestamp not in sym_df.index:
                    continue
                sym_row = sym_df.loc[timestamp]
                closed, _ = pos.update(
                    sym_row['close'],
                    high=sym_row['high'],
                    low=sym_row['low'],
                    timestamp=timestamp
                )
                if closed:
                    closed_syms.append(sym)
                    result.add_trade(pos)
                    continue

                # ── Position timeout / min PnL check (bug 13) ──
                candle_close = sym_row['close']
                pos_age_h = 0
                try:
                    pos_age_h = (timestamp - pos.entry_time).total_seconds() / 3600
                except Exception:
                    pass

                # Timeout: forced close after POSITION_TIMEOUT_H hours
                if pos_age_h >= POSITION_TIMEOUT_H:
                    if pos.side == "long":
                        close_price = candle_close * (1 - SLIPPAGE_PCT)
                    else:
                        close_price = candle_close * (1 + SLIPPAGE_PCT)
                    pos._close(close_price, "TIMEOUT", timestamp=timestamp)
                    closed_syms.append(sym)
                    result.add_trade(pos)
                    continue

                # Min PnL check after MIN_PNL_CHECK_H hours
                if pos_age_h >= MIN_PNL_CHECK_H:
                    current_pnl_pct = (candle_close - pos.entry_price) / pos.entry_price * 100
                    if pos.side == "short":
                        current_pnl_pct = -current_pnl_pct
                    if current_pnl_pct < MIN_EXPECTED_PNL_PCT:
                        if pos.side == "long":
                            close_price = candle_close * (1 - SLIPPAGE_PCT)
                        else:
                            close_price = candle_close * (1 + SLIPPAGE_PCT)
                        pos._close(close_price, "MIN_PNL_TIMEOUT", timestamp=timestamp)
                        closed_syms.append(sym)
                        result.add_trade(pos)
                        continue

            for sym in closed_syms:
                if sym in open_positions:
                    del open_positions[sym]

            # ── Daily loss limit: block entries if exceeded ──
            if day_start_balance > 0:
                daily_loss = (result.final_balance - day_start_balance) / day_start_balance * 100
                if daily_loss <= -DAILY_LOSS_LIMIT_PCT:
                    continue

            # ── VaR check: block entries if portfolio risk too high ──
            open_risk_usd = sum(
                abs(p.entry_price - p.sl_price) / p.entry_price * p.size_usd
                for p in open_positions.values()
            )
            portfolio_var_pct = open_risk_usd / result.final_balance * 100 if result.final_balance > 0 else 0
            if portfolio_var_pct >= MAX_PORTFOLIO_VAR_PCT:
                continue

            # ── MarketRegime: CRISIS blocks entries (bug 12, computed above) ──
            if market_regime == "CRISIS":
                continue  # block all entries during crisis

            # ── Generate signals for symbols without positions ──
            for sym in active_symbols:
                if sym in open_positions:
                    continue
                if sym in pending_signals:
                    continue
                if len(open_positions) >= MAX_OPEN_POSITIONS:
                    break

                sym_df = dfs.get(sym)
                if sym_df is None or timestamp not in sym_df.index:
                    continue

                sym_idx = sym_df.index.get_loc(timestamp)
                if sym_idx < 50:
                    continue

                slice_df = sym_df.iloc[:sym_idx + 1].copy()
                indicators = calcs[sym].compute(slice_df)

                if "error" in indicators:
                    continue

                # ── ADX Filter ──
                adx_val = indicators.get("adx", 0.0)
                adx_prev = indicators.get("adx_prev", 0.0)
                adx_ok, _ = adx_filter.check(adx_val, adx_prev)
                if not adx_ok:
                    continue

                # Calculate composite score
                composite = self._compute_composite(indicators, weights)

                # Determine BTC regime for dynamic thresholds
                if btc_ema50 is not None:
                    try:
                        btc_idx = btc_ema50.index.get_indexer([timestamp], method='nearest')[0]
                        if "BTCUSDT" in dfs:
                            btc_close_at_ts = dfs["BTCUSDT"]['close'].iloc[btc_idx]
                        elif btc_df is not None:
                            btc_close_at_ts = btc_df['close'].iloc[btc_idx]
                        else:
                            btc_close_at_ts = 0
                        btc_regime = "BULL" if btc_close_at_ts > btc_ema50.iloc[btc_idx] else "BEAR"
                    except Exception:
                        btc_regime = "BULL"
                else:
                    btc_regime = "BULL"

                threshold_long = THRESHOLD_LONG_BULL if btc_regime == "BULL" else THRESHOLD_LONG_BEAR
                threshold_short = THRESHOLD_SHORT_BEAR if btc_regime == "BEAR" else THRESHOLD_SHORT_BULL

                is_bullish = indicators["ema_fast"] > indicators["ema_slow"]
                rsi = indicators.get("rsi", 50)

                # ── BTC 1D EMA50 Master Filter ──
                btc_1d_allows_long = True
                btc_1d_allows_short = True
                if btc_1d_ema50 is not None:
                    try:
                        idx_1d = btc_1d_ema50.index.get_indexer([timestamp], method='pad')[0]
                        if idx_1d >= 0:
                            btc_1d_close = btc_1d_df['close'].iloc[idx_1d]
                            btc_1d_ema_val = btc_1d_ema50.iloc[idx_1d]
                            btc_1d_allows_long = btc_1d_close > btc_1d_ema_val
                            btc_1d_allows_short = btc_1d_close < btc_1d_ema_val
                    except Exception:
                        pass

                # Compute SL/TP percentages from ATR (synced with strategist_client: sl=1.5×ATR, tp=2.0×ATR)
                atr_pct = indicators.get("atr_pct", 0)
                atr_value = indicators.get("atr", 0)  # absolute ATR for trailing
                if atr_pct > 0.1:
                    sl_pct = 1.5 * atr_pct / 100
                    tp_pct = 2.0 * atr_pct / 100  # synced with strategist_client (was 3.0)
                else:
                    sl_pct = 0.03
                    tp_pct = 0.04  # proportional: 2.0/1.5 × 3% = 4%

                # ── LONG signal ──
                if composite > threshold_long and is_bullish and rsi < 70 and btc_1d_allows_long:
                    pending_signals[sym] = {"side": "long", "sl_pct": sl_pct, "tp_pct": tp_pct, "composite": composite, "atr": atr_value}

                # ── SHORT signal (with short limit check) ──
                elif composite < -threshold_short and not is_bullish and rsi > 30 and btc_1d_allows_short:
                    open_shorts = sum(1 for p in open_positions.values() if p.side == "short")
                    if open_shorts < MAX_SHORT_OPEN:
                        pending_signals[sym] = {"side": "short", "sl_pct": sl_pct, "tp_pct": tp_pct, "composite": composite, "atr": atr_value}

        # ── END_OF_BACKTEST: close all remaining positions ──
        for sym, position in list(open_positions.items()):
            sym_df = dfs.get(sym)
            if sym_df is not None and len(sym_df) > 0:
                last_price = sym_df.iloc[-1]['close']
                last_timestamp = sym_df.index[-1]
            else:
                last_price = master_df.iloc[-1]['close']
                last_timestamp = master_df.index[-1]
            # Slippage on forced close
            if position.side == "long":
                close_price = last_price * (1 - SLIPPAGE_PCT)
            else:
                close_price = last_price * (1 + SLIPPAGE_PCT)
            position._close(close_price, "END_OF_BACKTEST", timestamp=last_timestamp)
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
        state_max = 3.3  # synced with signal_builder.py (RSI soft zones + BB squeeze give partial scores)

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


def fetch_bitget_klines(symbol: str, granularity: str = "4H", limit: int = 1000) -> pd.DataFrame:
    """Fetch historical klines from Bitget public API."""
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "granularity": granularity,
        "limit": str(limit)
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("code") != "00000" or not data.get("data"):
            logger.warning(f"Bitget API error for {symbol}: {data.get('msg', 'unknown')}")
            return None
        rows = data["data"]
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "quote_volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df.sort_values("timestamp").reset_index(drop=True)
        df.set_index("timestamp", inplace=True)
        logger.info(f"Fetched {len(df)} candles for {symbol}")
        return df
    except Exception as e:
        logger.error(f"Error fetching {symbol}: {e}")
        return None


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

    if args.real:
        # Fetch real data from Bitget for all symbols
        dfs = {}
        for sym in SYMBOLS:
            print(f"Загружаю {sym}...")
            df = fetch_bitget_klines(sym, granularity="4H", limit=1000)
            if df is not None and len(df) > 50:
                dfs[sym] = df
            else:
                logger.warning(f"Failed to fetch {sym}, skipping")
        if len(dfs) == 0:
            logger.error("No data fetched from Bitget, exiting")
        else:
            bt = Backtester(initial_balance=START_BALANCE)
            result = bt.run(dfs=dfs, symbols=list(dfs.keys()))
            result.print_summary()
    else:
        asyncio.run(run_backtest(use_real_data=False))
