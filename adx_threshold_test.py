#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adx_threshold_test.py - Test ADX thresholds (16, 18, 20, 22, 25) on real 90-day data.
Uses existing ADXFilter from backtester.py, does NOT modify any core logic.
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

def fetch_candles(symbol, days=90, granularity="4H"):
    """Fetch real candles from Bitget."""
    all_candles = []
    end_time = int((datetime.utcnow() - timedelta(minutes=5)).timestamp() * 1000)
    start_time = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    current_end = end_time
    for _ in range(10):
        params = {"symbol": symbol, "granularity": granularity, "limit": 1000,
                  "endTime": current_end, "startTime": start_time, "productType": "USDT-FUTURES"}
        resp = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=30)
        data = resp.json()
        if data.get("code") != "00000" or not data.get("data"):
            logger.warning(f"API error for {symbol}: {data.get('msg')}")
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
    if not all_candles:
        return pd.DataFrame()
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

class ThresholdBacktester(Backtester):
    """
    Subclass that overrides ADXFilter threshold.
    All other logic (Score, trailing, SL/TP) remains unchanged.
    """
    def __init__(self, initial_balance=10000.0, use_real_data=False, adx_threshold=18.0):
        super().__init__(initial_balance, use_real_data)
        self.adx_threshold = adx_threshold

    def _run_backtest(self, df, symbol, btc_df=None):
        """Override to use custom ADX threshold."""
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

def run_threshold_test(threshold):
    """Run full backtest for all symbols with given ADX threshold."""
    logger.info(f"\n{'='*80}")
    logger.info(f"ADX THRESHOLD = {threshold}")
    logger.info(f"{'='*80}")
    
    # Загружаем BTC свечи первыми — для режима всех символов
    print("Загрузка BTC данных для режима...")
    btc_df = fetch_candles("BTCUSDT", days=90)
    print(f"BTC свечей: {len(btc_df)}")
    
    results = []
    for symbol in SYMBOLS:
        logger.info(f"=== {symbol} ===")
        try:
            if symbol == "BTCUSDT" and btc_df is not None and not btc_df.empty:
                df = btc_df
            else:
                df = fetch_candles(symbol, days=90)
            if df.empty or len(df) < 100:
                logger.error(f"  ❌ Недостаточно данных: {len(df)} свечей")
                continue
            
            bt = ThresholdBacktester(initial_balance=10000.0, use_real_data=False, adx_threshold=threshold)
            result = bt.run(df=df, symbol=symbol, btc_df=btc_df)
            
            total_trades = result.total_trades
            winning_trades = result.winning_trades
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
            
            total_return_pct = (result.final_balance - result.initial_balance) / result.initial_balance * 100
            
            results.append({
                "symbol": symbol,
                "total_trades": total_trades,
                "win_rate": win_rate,
                "total_pnl_usd": result.total_pnl_usd,
                "total_pnl_pct": total_return_pct,
                "max_drawdown_pct": result.max_drawdown_pct,
                "success": True
            })
            time.sleep(1)  # rate limiting
        except Exception as e:
            logger.error(f"  ❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                "symbol": symbol,
                "error": str(e),
                "success": False
            })
    
    # Summary
    successful = [r for r in results if r.get("success", False)]
    if successful:
        total_trades = sum(r["total_trades"] for r in successful)
        avg_win_rate = np.mean([r["win_rate"] for r in successful])
        avg_pnl = np.mean([r["total_pnl_pct"] for r in successful])
        total_pnl_usd = sum(r["total_pnl_usd"] for r in successful)
        avg_maxdd = np.mean([r["max_drawdown_pct"] for r in successful])
        
        return {
            "threshold": threshold,
            "total_trades": total_trades,
            "avg_win_rate": avg_win_rate,
            "total_pnl_usd": total_pnl_usd,
            "avg_pnl_pct": avg_pnl,
            "avg_maxdd_pct": avg_maxdd,
            "results": results
        }
    else:
        return {
            "threshold": threshold,
            "error": "No successful runs",
            "results": results
        }

def main():
    thresholds = [16, 18, 20, 22, 25]
    all_results = []
    
    for th in thresholds:
        res = run_threshold_test(th)
        all_results.append(res)
        # Save intermediate
        with open(f"adx_threshold_{th}.json", "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False, default=str)
    
    # Print summary table
    print("\n" + "="*100)
    print("ADX THRESHOLD COMPARISON (90 days, 12 symbols)")
    print("="*100)
    print(f"{'Threshold':<10} {'Trades':<8} {'WinRate%':<10} {'Total PnL$':<12} {'Avg PnL%':<10} {'Avg MaxDD%':<12}")
    print("-"*100)
    
    best_pnl = -float('inf')
    best_th = None
    for res in all_results:
        if "error" in res:
            print(f"{res['threshold']:<10} {'ERROR':<8} {'-':<10} {'-':<12} {'-':<10} {'-':<12}")
            continue
        
        trades = res["total_trades"]
        wr = res["avg_win_rate"]
        pnl = res["total_pnl_usd"]
        avg_pnl = res["avg_pnl_pct"]
        maxdd = res["avg_maxdd_pct"]
        
        print(f"{res['threshold']:<10} {trades:<8} {wr:<10.1f} {pnl:<12.2f} {avg_pnl:<10.2f} {maxdd:<12.2f}")
        
        if pnl > best_pnl:
            best_pnl = pnl
            best_th = res["threshold"]
    
    print("-"*100)
    print(f"BEST: threshold={best_th}, Total PnL=${best_pnl:.2f}")
    
    # Save final comparison
    with open("adx_threshold_comparison.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nDetailed results saved to adx_threshold_comparison.json")

if __name__ == "__main__":
    main()
