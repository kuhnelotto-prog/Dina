#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_synthetic_bull.py - Backtest on synthetic BULL market data.
Generates realistic bullish price action for 10 symbols and runs full backtest.
"""
import sys, os, logging, json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtester import Backtester

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
           "AVAXUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "UNIUSDT"]

# Starting prices and volatility per symbol
SYMBOL_CONFIG = {
    "BTCUSDT":  {"start": 60000, "vol": 800,  "drift": 0.0008},
    "ETHUSDT":  {"start": 3000,  "vol": 50,   "drift": 0.0010},
    "SOLUSDT":  {"start": 120,   "vol": 3,    "drift": 0.0012},
    "BNBUSDT":  {"start": 550,   "vol": 8,    "drift": 0.0007},
    "XRPUSDT":  {"start": 0.55,  "vol": 0.01, "drift": 0.0009},
    "AVAXUSDT": {"start": 30,    "vol": 0.8,  "drift": 0.0011},
    "DOGEUSDT": {"start": 0.12,  "vol": 0.003,"drift": 0.0010},
    "ADAUSDT":  {"start": 0.45,  "vol": 0.008,"drift": 0.0009},
    "LINKUSDT": {"start": 14,    "vol": 0.3,  "drift": 0.0010},
    "UNIUSDT":  {"start": 7,     "vol": 0.15, "drift": 0.0011},
}


def generate_bull_data(symbol, days=90, seed=None):
    """
    Generate synthetic BULL market OHLCV data.
    
    Bull characteristics:
    - Positive drift (uptrend)
    - Higher lows pattern
    - Occasional pullbacks (3-5%) followed by recovery
    - Volume spikes on breakouts
    """
    if seed is not None:
        np.random.seed(seed)
    
    cfg = SYMBOL_CONFIG[symbol]
    start_price = cfg["start"]
    vol = cfg["vol"]           # per-candle volatility
    drift = cfg["drift"]       # positive drift per candle (bull)
    
    n_candles = days * 6  # 4H candles
    dates = pd.date_range(
        start=datetime.utcnow() - timedelta(days=days),
        periods=n_candles,
        freq='4h'
    )
    
    # Generate returns with strong positive drift for BULL market
    # drift ~0.08-0.12% per 4H candle = ~50-80% over 90 days
    returns = np.random.randn(n_candles) * 0.008 + drift  # lower noise, stronger drift
    
    # Add occasional pullbacks (every ~40-60 candles, 2-4 candle pullback, mild)
    i = 0
    while i < n_candles:
        next_pullback = np.random.randint(40, 65)
        i += next_pullback
        if i < n_candles:
            pullback_len = np.random.randint(2, 5)
            for j in range(pullback_len):
                if i + j < n_candles:
                    returns[i + j] = -abs(np.random.randn() * 0.008 + 0.003)  # -0.3% to -1.1%
            i += pullback_len
    
    # Build price series
    prices = np.zeros(n_candles)
    prices[0] = start_price
    for i in range(1, n_candles):
        prices[i] = prices[i-1] * (1 + returns[i])
        prices[i] = max(prices[i], start_price * 0.5)  # floor at 50% of start
    
    # Generate OHLCV
    opens = np.zeros(n_candles)
    highs = np.zeros(n_candles)
    lows = np.zeros(n_candles)
    closes = prices.copy()
    volumes = np.zeros(n_candles)
    
    for i in range(n_candles):
        price = prices[i]
        candle_vol = vol * (0.5 + np.random.rand())  # random volatility
        
        # Open is close of previous candle (or start)
        opens[i] = prices[i-1] if i > 0 else start_price
        
        # High/Low based on volatility
        highs[i] = max(opens[i], closes[i]) + abs(np.random.randn()) * candle_vol * 0.5
        lows[i] = min(opens[i], closes[i]) - abs(np.random.randn()) * candle_vol * 0.5
        lows[i] = max(lows[i], price * 0.95)  # don't go too low
        
        # Volume: higher on up moves, spike on breakouts
        base_vol = 10000 + np.random.rand() * 5000
        if returns[i] > 0.01:  # big up move
            base_vol *= 2.0  # volume spike
        elif returns[i] < -0.01:  # big down move
            base_vol *= 1.5
        volumes[i] = base_vol
    
    df = pd.DataFrame({
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    }, index=dates)
    
    total_return = (prices[-1] - start_price) / start_price * 100
    logger.info(f"Generated BULL data for {symbol}: {start_price:.4f} -> {prices[-1]:.4f} ({total_return:+.1f}%)")
    
    return df


def run_symbol_backtest(symbol, df, btc_df=None):
    """Run backtest for a single symbol on synthetic data."""
    try:
        bt = Backtester(initial_balance=10000.0, use_real_data=False)
        result = bt.run(df=df, symbol=symbol, btc_df=btc_df)
        
        total_trades = result.total_trades
        winning_trades = result.winning_trades
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
        
        # RR calculation
        rr_values = []
        tsl_count = 0
        for t in result.trades:
            init_sl = getattr(t, 'initial_sl', t.sl_price)
            trailing = getattr(t, 'trailing_activated', False)
            if trailing:
                tsl_count += 1
            if t.side == "long":
                risk = abs(t.entry_price - init_sl)
                reward = t.exit_price - t.entry_price if t.exit_price else 0
            else:
                risk = abs(init_sl - t.entry_price)
                reward = t.entry_price - t.exit_price if t.exit_price else 0
            rr = reward / risk if risk > 0 else 0.0
            rr_values.append(rr)
        avg_rr = np.mean(rr_values) if rr_values else 0.0
        
        total_return_pct = (result.final_balance - result.initial_balance) / result.initial_balance * 100
        
        long_trades = [t for t in result.trades if t.side == "long"]
        short_trades = [t for t in result.trades if t.side == "short"]
        long_wins = sum(1 for t in long_trades if t.pnl_usd > 0)
        short_wins = sum(1 for t in short_trades if t.pnl_usd > 0)
        long_wr = (long_wins / len(long_trades) * 100) if long_trades else 0.0
        short_wr = (short_wins / len(short_trades) * 100) if short_trades else 0.0
        
        return {
            "symbol": symbol,
            "total_trades": total_trades,
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "win_rate": win_rate,
            "long_win_rate": long_wr,
            "short_win_rate": short_wr,
            "avg_rr": avg_rr,
            "tsl_count": tsl_count,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_pnl_usd": result.total_pnl_usd,
            "total_pnl_pct": total_return_pct,
            "rr_list": rr_values,
            "success": True
        }
    except Exception as e:
        logger.error(f"Error {symbol}: {e}")
        import traceback
        traceback.print_exc()
        return {"symbol": symbol, "error": str(e), "success": False}


def main():
    print("=" * 100)
    print("SYNTHETIC BULL MARKET BACKTEST")
    print("Symbols:", ", ".join(SYMBOLS))
    print("Period: 90 days, timeframe 4H, BULL regime")
    print("=" * 100)
    
    # Generate BTC data first (for regime detection)
    print("\nGenerating synthetic BULL data...")
    btc_df = generate_bull_data("BTCUSDT", days=90, seed=42)
    btc_return = (btc_df['close'].iloc[-1] - btc_df['close'].iloc[0]) / btc_df['close'].iloc[0] * 100
    print(f"BTC: {btc_df['close'].iloc[0]:.0f} -> {btc_df['close'].iloc[-1]:.0f} ({btc_return:+.1f}%)")
    
    # Generate all symbol data
    all_data = {"BTCUSDT": btc_df}
    for i, symbol in enumerate(SYMBOLS):
        if symbol != "BTCUSDT":
            all_data[symbol] = generate_bull_data(symbol, days=90, seed=42 + i)
    
    # Run backtests
    results = []
    for symbol in SYMBOLS:
        df = all_data[symbol]
        res = run_symbol_backtest(symbol, df, btc_df=btc_df)
        if res:
            results.append(res)
    
    # Print results table
    print("\n" + "=" * 110)
    print("RESULTS (SYNTHETIC BULL)")
    print("=" * 110)
    print(f"{'Symbol':<10} {'Trades':<8} {'LONG':<6} {'SHORT':<7} {'WR%':<8} {'L-WR%':<8} {'S-WR%':<8} {'AvgRR':<8} {'TSL':<6} {'MaxDD%':<8} {'PnL%':<8}")
    print("-" * 110)
    
    for res in results:
        if not res.get("success"):
            print(f"{res['symbol']:<10} ERROR")
            continue
        print(f"{res['symbol']:<10} "
              f"{res['total_trades']:<8} "
              f"{res['long_trades']:<6} "
              f"{res['short_trades']:<7} "
              f"{res['win_rate']:<8.1f} "
              f"{res['long_win_rate']:<8.1f} "
              f"{res['short_win_rate']:<8.1f} "
              f"{res['avg_rr']:<8.2f} "
              f"{res['tsl_count']:<6} "
              f"{res['max_drawdown_pct']:<8.2f} "
              f"{res['total_pnl_pct']:<8.2f}")
    
    # Summary
    print("-" * 110)
    ok = [r for r in results if r.get("success")]
    if ok:
        total_trades = sum(r["total_trades"] for r in ok)
        avg_wr = np.mean([r["win_rate"] for r in ok])
        avg_rr = np.mean([r["avg_rr"] for r in ok])
        avg_pnl = np.mean([r["total_pnl_pct"] for r in ok])
        total_pnl = sum(r["total_pnl_usd"] for r in ok)
        total_tsl = sum(r["tsl_count"] for r in ok)
        
        print(f"TOTAL: {len(ok)} symbols, {total_trades} trades, {total_tsl} trailing stops")
        print(f"Avg WinRate: {avg_wr:.1f}%")
        print(f"Avg RR: {avg_rr:.2f}")
        print(f"Avg PnL: {avg_pnl:.2f}%")
        print(f"Total PnL: ${total_pnl:+.2f}")
    
    # RR Distribution
    print("\n" + "=" * 60)
    print("RR DISTRIBUTION (all trades)")
    print("=" * 60)
    all_rr = []
    for res in ok:
        all_rr.extend(res.get("rr_list", []))
    
    if all_rr:
        rr_gt2 = sum(1 for r in all_rr if r >= 2.0)
        rr_1_2 = sum(1 for r in all_rr if 1.0 <= r < 2.0)
        rr_0_1 = sum(1 for r in all_rr if 0.0 <= r < 1.0)
        rr_neg = sum(1 for r in all_rr if r < 0.0)
        
        print(f"  RR >= 2.0  (full TP):    {rr_gt2:>4} ({rr_gt2/len(all_rr)*100:.1f}%)")
        print(f"  1.0 <= RR < 2.0:         {rr_1_2:>4} ({rr_1_2/len(all_rr)*100:.1f}%)")
        print(f"  0.0 <= RR < 1.0 (TSL):   {rr_0_1:>4} ({rr_0_1/len(all_rr)*100:.1f}%)")
        print(f"  RR < 0.0 (loss):         {rr_neg:>4} ({rr_neg/len(all_rr)*100:.1f}%)")
        print(f"  Total: {len(all_rr)}")
        print(f"  Median RR: {np.median(all_rr):.2f}")
        print(f"  Mean RR:   {np.mean(all_rr):.2f}")
    
    # Save
    with open("backtest_results_bull.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nResults saved to backtest_results_bull.json")


if __name__ == "__main__":
    main()
