#!/usr/bin/env python3
"""
Бэктест с фильтрами SignalBuilder (коммит 8201e54)
"""
import sys, json, logging, warnings, time, asyncio
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, ".")

from config import settings
from backtester import Backtester
from signal_builder import SignalBuilder

# ============================================================================
# Конфигурация
# ============================================================================
SYMBOLS = settings.trading.symbols
START_DATE = datetime.now(timezone.utc) - timedelta(days=90)
END_DATE = datetime.now(timezone.utc) - timedelta(minutes=5)

def fetch_candles(symbol, start_date, end_date, granularity="4H"):
    import requests
    all_candles = []
    end_time = int(end_date.timestamp() * 1000)
    start_time = int(start_date.timestamp() * 1000)
    current_end = end_time
    for _ in range(10):
        params = {"symbol": symbol, "granularity": granularity, "limit": 1000,
                  "endTime": current_end, "startTime": start_time, "productType": "umcbl"}
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
    if not all_candles:
        return pd.DataFrame()
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

async def test_symbol(symbol):
    print(f"  {symbol}: fetching 4H candles...")
    df_4h = fetch_candles(symbol, START_DATE, END_DATE, "4H")
    if df_4h.empty or len(df_4h) < 100:
        print(f"  {symbol}: insufficient data")
        return None
    
    # Создаём SignalBuilder для LONG и SHORT
    sb_long = SignalBuilder(symbols=[symbol], direction="LONG")
    sb_short = SignalBuilder(symbols=[symbol], direction="SHORT")
    
    # Загружаем данные в кэш
    await sb_long.update_candle(symbol, "4h", df_4h)
    await sb_short.update_candle(symbol, "4h", df_4h)
    
    # Собираем статистику по фильтрам
    total_signals = 0
    filtered_out = 0
    filtered_reasons = {"threshold": 0, "atr": 0, "state_components": 0}
    
    # Проходим по всем свечам (после 30-й)
    for i in range(30, len(df_4h)):
        slice_df = df_4h.iloc[:i+1].copy()
        await sb_long.update_candle(symbol, "4h", slice_df)
        await sb_short.update_candle(symbol, "4h", slice_df)
        
        # Проверяем LONG
        signal_long = await sb_long.compute(symbol, "4h")
        if "error" not in signal_long:
            total_signals += 1
            if signal_long.get("filtered", False):
                filtered_out += 1
                # Анализируем причину (упрощённо)
                regime = sb_long.detect_regime(symbol)
                composite = signal_long.get("composite_score", 0)
                atr_pct = signal_long.get("atr_pct", 0)
                
                # Проверяем порог
                if regime == "BEAR" and composite < 0.45:
                    filtered_reasons["threshold"] += 1
                elif regime == "SIDEWAYS" and composite < 0.50:
                    filtered_reasons["threshold"] += 1
                elif regime == "BULL" and composite < 0.35:
                    filtered_reasons["threshold"] += 1
                
                # Проверяем ATR
                if atr_pct < 0.5:
                    filtered_reasons["atr"] += 1
        
        # Проверяем SHORT
        signal_short = await sb_short.compute(symbol, "4h")
        if "error" not in signal_short:
            total_signals += 1
            if signal_short.get("filtered", False):
                filtered_out += 1
                regime = sb_short.detect_regime(symbol)
                composite = signal_short.get("composite_score", 0)
                atr_pct = signal_short.get("atr_pct", 0)
                
                if regime == "BEAR" and composite > -0.45:
                    filtered_reasons["threshold"] += 1
                elif regime == "SIDEWAYS" and composite > -0.50:
                    filtered_reasons["threshold"] += 1
                elif regime == "BULL" and composite > -0.35:
                    filtered_reasons["threshold"] += 1
                
                if atr_pct < 0.5:
                    filtered_reasons["atr"] += 1
    
    # Запускаем обычный бэктест для сравнения
    bt = Backtester(initial_balance=10000.0, use_real_data=False)
    result = bt.run(df_4h, symbol)
    
    return {
        "symbol": symbol,
        "total_signals": total_signals,
        "filtered_out": filtered_out,
        "passed_filters": total_signals - filtered_out,
        "filtered_reasons": filtered_reasons,
        "backtest_trades": result.total_trades,
        "backtest_long": sum(1 for t in result.trades if t.side == "long"),
        "backtest_short": sum(1 for t in result.trades if t.side == "short"),
        "backtest_winrate": result.winning_trades / result.total_trades * 100 if result.total_trades > 0 else 0,
        "backtest_pnl": result.total_pnl_usd / 10000 * 100,
    }

async def main():
    print("=" * 100)
    print("BACKTEST: SignalBuilder filters (коммит 8201e54)")
    print(f"Period: {START_DATE.date()} -- {END_DATE.date()} (90 days)")
    print(f"Symbols: {', '.join(SYMBOLS)}")
    print("=" * 100)
    
    all_results = []
    for symbol in SYMBOLS:
        res = await test_symbol(symbol)
        if res:
            all_results.append(res)
    
    # Вывод результатов
    print("\n" + "=" * 100)
    print("FILTER STATISTICS")
    print("=" * 100)
    print(f"{'Symbol':<10} {'Signals':<8} {'Filtered':<8} {'Passed':<8} {'Trades':<8} {'WinRate':<8} {'PnL%':<8}")
    print("-" * 70)
    
    total_signals = 0
    total_filtered = 0
    total_passed = 0
    total_trades = 0
    
    for res in all_results:
        symbol = res["symbol"]
        signals = res["total_signals"]
        filtered = res["filtered_out"]
        passed = res["passed_filters"]
        trades = res["backtest_trades"]
        winrate = res["backtest_winrate"]
        pnl = res["backtest_pnl"]
        
        print(f"{symbol:<10} {signals:<8} {filtered:<8} {passed:<8} {trades:<8} {winrate:<8.1f} {pnl:<+8.2f}")
        
        total_signals += signals
        total_filtered += filtered
        total_passed += passed
        total_trades += trades
    
    # Причины фильтрации
    print("\n" + "=" * 100)
    print("FILTER REASONS (aggregated)")
    print("=" * 100)
    total_reasons = {"threshold": 0, "atr": 0, "state_components": 0}
    for res in all_results:
        for reason in total_reasons:
            total_reasons[reason] += res["filtered_reasons"].get(reason, 0)
    
    for reason, count in total_reasons.items():
        pct = count / total_filtered * 100 if total_filtered > 0 else 0
        print(f"  {reason:<20} {count:>4} ({pct:.1f}%)")
    
    # Итог
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Total signals:     {total_signals}")
    print(f"Filtered out:      {total_filtered} ({total_filtered/total_signals*100:.1f}%)")
    print(f"Passed filters:    {total_passed} ({total_passed/total_signals*100:.1f}%)")
    print(f"Backtest trades:   {total_trades}")
    
    if total_passed < 50:
        print("\n⚠️  WARNING: Only {total_passed} signals passed filters — thresholds may be TOO STRICT")
        print("   Consider adjusting:")
        print("   - BEAR threshold: 0.45 → 0.40")
        print("   - SIDEWAYS threshold: 0.50 → 0.45")
        print("   - ATR filter: 0.5% → 0.3%")
    else:
        print(f"\n✅ OK: {total_passed} signals passed filters")
    
    # Сохраняем
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": {"start": START_DATE.isoformat(), "end": END_DATE.isoformat()},
        "symbols": SYMBOLS,
        "results": all_results,
        "summary": {
            "total_signals": total_signals,
            "total_filtered": total_filtered,
            "total_passed": total_passed,
            "total_trades": total_trades,
            "filter_reasons": total_reasons,
        }
    }
    
    with open("filter_test_results.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\nResults saved to filter_test_results.json")

if __name__ == "__main__":
    asyncio.run(main())
