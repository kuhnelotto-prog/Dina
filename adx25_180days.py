#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adx25_180days.py - Validate ADX=25 on 180 days of data with pagination.
Uses existing fetch_candles from backtester.py, does NOT modify core logic.
"""
import sys, os, time, logging, json, asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtester import Backtester, ADXFilter

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Символы из .env (12 монет)
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", 
           "AVAXUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "DOTUSDT",
           "ATOMUSDT", "SUIUSDT"]

def fetch_candles_paginated(symbol, days=180, granularity="4H"):
    """
    Fetch candles with pagination (Bitget returns max 1000 per request).
    Returns DataFrame with columns: timestamp, open, high, low, close, volume.
    """
    all_candles = []
    end_time = int((datetime.utcnow() - timedelta(minutes=5)).timestamp() * 1000)
    start_time = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    current_end = end_time
    max_iterations = 20  # safety limit for 180 days
    iteration = 0
    
    logger.info(f"Fetching {days} days of {granularity} data for {symbol}...")
    
    while current_end > start_time and iteration < max_iterations:
        iteration += 1
        params = {
            "symbol": symbol,
            "granularity": granularity,
            "limit": 1000,
            "endTime": current_end,
            "startTime": start_time,
            "productType": "USDT-FUTURES"
        }
        try:
            resp = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=30)
            data = resp.json()
            if data.get("code") != "00000" or not data.get("data"):
                logger.warning(f"API error for {symbol}: {data.get('msg')}")
                break
            candles = data["data"]
            if not candles:
                logger.info(f"No more candles for {symbol}")
                break
            
            for c in candles:
                all_candles.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
            
            logger.info(f"Iteration {iteration}: got {len(candles)} candles, total {len(all_candles)}")
            
            # Move window backward
            earliest_ts = int(candles[-1][0])
            if earliest_ts >= current_end:
                logger.info(f"Earliest timestamp {earliest_ts} >= current_end {current_end}, breaking")
                break
            current_end = earliest_ts - 1
            time.sleep(0.1)  # rate limiting
            
        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")
            break
    
    if not all_candles:
        logger.warning(f"No candles fetched for {symbol}")
        return pd.DataFrame()
    
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    
    expected_candles = days * 6  # 4H candles per day
    if len(df) < expected_candles * 0.8:
        logger.warning(f"Got only {len(df)} candles for {symbol}, expected ~{expected_candles}")
    
    logger.info(f"Fetched {len(df)} candles for {symbol} ({days} days)")
    return df

class ADX25Backtester(Backtester):
    """
    Subclass with ADX threshold=25 fixed.
    All other logic (Score, trailing, SL/TP) remains unchanged.
    """
    def __init__(self, initial_balance=10000.0, use_real_data=False):
        super().__init__(initial_balance, use_real_data)
        self.adx_threshold = 25.0

    def _run_backtest(self, df, symbol, btc_df=None):
        """Override to use ADX threshold=25."""
        from backtester import BacktestResult, ADXFilter, ADX_BLACKLIST
        result = BacktestResult(self.initial_balance)
        open_positions = {}
        from indicators_calc import IndicatorsCalculator
        calc = IndicatorsCalculator()
        adx_filter = ADXFilter(threshold=self.adx_threshold, min_growth=0.5)

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
            if i % 100 == 0:
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

                    from backtester import BacktestPosition
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

                    from backtester import BacktestPosition
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

def run_symbol_backtest(symbol, days=180):
    """Run backtest for a single symbol with ADX=25."""
    logger.info(f"\n{'='*80}")
    logger.info(f"ADX=25, {days} days: {symbol}")
    logger.info(f"{'='*80}")
    
    # Load BTC data first for regime detection
    if symbol == "BTCUSDT":
        btc_df = None
    else:
        btc_df = fetch_candles_paginated("BTCUSDT", days=days)
        if btc_df.empty:
            logger.error(f"Failed to fetch BTC data for regime detection")
            return None
    
    df = fetch_candles_paginated(symbol, days=days)
    if df.empty or len(df) < 100:
        logger.error(f"  ❌ Недостаточно данных: {len(df)} свечей")
        return None
    
    bt = ADX25Backtester(initial_balance=10000.0, use_real_data=False)
    result = bt.run(df=df, symbol=symbol, btc_df=btc_df)
    
    total_trades = result.total_trades
    winning_trades = result.winning_trades
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
    
    total_return_pct = (result.final_balance - result.initial_balance) / result.initial_balance * 100
    
    # RR calculation
    rr_values = []
    for t in result.trades:
        init_sl = getattr(t, 'initial_sl', t.sl_price)
        if t.side == "long":
            risk = abs(t.entry_price - init_sl)
            reward = t.exit_price - t.entry_price if t.exit_price else 0
        else:
            risk = abs(init_sl - t.entry_price)
            reward = t.entry_price - t.exit_price if t.exit_price else 0
        rr = reward / risk if risk > 0 else 0.0
        rr_values.append(rr)
    avg_rr = np.mean(rr_values) if rr_values else 0.0
    
    return {
        "symbol": symbol,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "total_pnl_usd": result.total_pnl_usd,
        "total_pnl_pct": total_return_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "avg_rr": avg_rr,
        "final_balance": result.final_balance,
        "success": True
    }

def main():
    days = 180
    print("="*100)
    print(f"ADX=25 VALIDATION ON {days} DAYS")
    print("Symbols:", ", ".join(SYMBOLS))
    print("="*100)
    
    results = []
    for symbol in SYMBOLS:
        try:
            res = run_symbol_backtest(symbol, days=days)
            if res:
                results.append(res)
            time.sleep(2)  # rate limiting
        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                "symbol": symbol,
                "error": str(e),
                "success": False
            })
    
    # Print summary table
    print("\n" + "="*100)
    print(f"RESULTS: ADX=25, {days} DAYS")
    print("="*100)
    print(f"{'Symbol':<10} {'Trades':<8} {'WinRate%':<10} {'PnL$':<12} {'PnL%':<10} {'MaxDD%':<10} {'AvgRR':<8}")
    print("-"*100)
    
    total_trades = 0
    total_pnl = 0.0
    profitable_symbols = []
    losing_symbols = []
    
    for res in results:
        if not res.get("success", False):
            print(f"{res['symbol']:<10} {'ERROR':<8} {'-':<10} {'-':<12} {'-':<10} {'-':<10} {'-':<8}")
            continue
        
        trades = res["total_trades"]
        wr = res["win_rate"]
        pnl = res["total_pnl_usd"]
        pnl_pct = res["total_pnl_pct"]
        maxdd = res["max_drawdown_pct"]
        avg_rr = res["avg_rr"]
        
        print(f"{res['symbol']:<10} {trades:<8} {wr:<10.1f} {pnl:<12.2f} {pnl_pct:<10.2f} {maxdd:<10.2f} {avg_rr:<8.2f}")
        
        total_trades += trades
        total_pnl += pnl
        if pnl > 0:
            profitable_symbols.append((res['symbol'], pnl))
        else:
            losing_symbols.append((res['symbol'], pnl))
    
    print("-"*100)
    avg_win_rate = np.mean([r["win_rate"] for r in results if r.get("success", False)])
    avg_maxdd = np.mean([r["max_drawdown_pct"] for r in results if r.get("success", False)])
    
    print(f"TOTAL: {total_trades} trades, ${total_pnl:.2f}")
    print(f"Avg WinRate: {avg_win_rate:.1f}%")
    print(f"Avg MaxDD: {avg_maxdd:.2f}%")
    
    # Comparison with 90-day test
    print("\n" + "="*100)
    print("COMPARISON WITH 90-DAY TEST (ADX=25)")
    print("="*100)
    print(f"{'Period':<10} {'Trades':<8} {'WinRate%':<10} {'Total PnL$':<12} {'Avg MaxDD%':<12}")
    print("-"*100)
    print(f"{'90 days':<10} {'61':<8} {'34.6':<10} {'-186.77':<12} {'0.92':<12}")
    print(f"{'180 days':<10} {total_trades:<8} {avg_win_rate:<10.1f} {total_pnl:<12.2f} {avg_maxdd:<12.2f}")
    
    # Breakdown by symbols
    print("\n" + "="*100)
    print("PROFITABLE SYMBOLS:")
    print("="*100)
    for sym, pnl in sorted(profitable_symbols, key=lambda x: x[1], reverse=True):
        print(f"{sym}: ${pnl:+.2f}")
    
    print("\n" + "="*100)
    print("LOSING SYMBOLS:")
    print("="*100)
    for sym, pnl in sorted(losing_symbols, key=lambda x: x[1]):
        print(f"{sym}: ${pnl:+.2f}")
    
    # Save results
    with open(f"adx25_{days}days.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nDetailed results saved to adx25_{days}days.json")

if __name__ == "__main__":
    main()
