#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_full_backtest.py - Запуск полного бэктеста на коммите 767aa95 по всем монетам из конфига.
Только запуск и вывод результатов, без изменений кода.
"""
import sys, os, time, logging, json, asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtester import Backtester

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Символы из .env
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

def run_symbol_backtest(symbol, btc_df=None):
    """Run backtest for a single symbol."""
    logger.info(f"=== {symbol} ===")
    try:
        if symbol == "BTCUSDT" and btc_df is not None and not btc_df.empty:
            df = btc_df
        else:
            df = fetch_candles(symbol, days=90)
        if df.empty or len(df) < 100:
            logger.error(f"  ❌ Недостаточно данных: {len(df)} свечей")
            return None
        
        bt = Backtester(initial_balance=10000.0, use_real_data=False)
        result = bt.run(df=df, symbol=symbol, btc_df=btc_df)
        
        # Собираем статистику
        total_trades = result.total_trades
        winning_trades = result.winning_trades
        losing_trades = result.losing_trades
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
        
        # Реальный расчёт Risk/Reward для каждой сделки
        # Используем initial_sl (оригинальный SL) для расчёта risk
        rr_values = []
        debug_trades = []
        tsl_count = 0
        for t in result.trades:
            # Use initial_sl for consistent RR calculation
            init_sl = getattr(t, 'initial_sl', t.sl_price)
            trailing = getattr(t, 'trailing_activated', False)
            if trailing:
                tsl_count += 1
            if t.side == "long":
                risk = abs(t.entry_price - init_sl)
                reward = t.exit_price - t.entry_price if t.exit_price else 0
            else:  # short
                risk = abs(init_sl - t.entry_price)
                reward = t.entry_price - t.exit_price if t.exit_price else 0
            rr = reward / risk if risk > 0 else 0.0
            rr_values.append(rr)
            tsl_tag = " [TSL]" if trailing else ""
            debug_trades.append(
                f"{t.side:>5} entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
                f"sl={init_sl:.2f}->{t.sl_price:.2f} risk={risk:.2f} reward={reward:.2f} "
                f"RR={rr:.2f} pnl={t.pnl_pct:+.2f}%{tsl_tag}"
            )
        avg_rr = np.mean(rr_values) if rr_values else 0.0
        
        # PnL %
        total_return_pct = (result.final_balance - result.initial_balance) / result.initial_balance * 100
        
        # Разделение LONG/SHORT
        long_trades = [t for t in result.trades if t.side == "long"]
        short_trades = [t for t in result.trades if t.side == "short"]
        
        long_wins = sum(1 for t in long_trades if t.pnl_usd > 0)
        short_wins = sum(1 for t in short_trades if t.pnl_usd > 0)
        
        long_win_rate = (long_wins / len(long_trades) * 100) if long_trades else 0.0
        short_win_rate = (short_wins / len(short_trades) * 100) if short_trades else 0.0
        
        return {
            "symbol": symbol,
            "total_trades": total_trades,
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": win_rate,
            "long_win_rate": long_win_rate,
            "short_win_rate": short_win_rate,
            "avg_rr": avg_rr,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_pnl_usd": result.total_pnl_usd,
            "total_pnl_pct": total_return_pct,
            "rr_list": rr_values,
            "debug_trades": debug_trades,
            "final_balance": result.final_balance,
            "success": True
        }
    except Exception as e:
        logger.error(f"  ❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        return {
            "symbol": symbol,
            "error": str(e),
            "success": False
        }

def main():
    print("=" * 100)
    print("ПОЛНЫЙ БЭКТЕСТ (динамические пороги по BTC EMA50)")
    print("Символы:", ", ".join(SYMBOLS))
    print("Период: 90 дней, таймфрейм 4H")
    print("=" * 100)
    
    # Загружаем BTC свечи первыми — для режима всех символов
    print("Загрузка BTC данных для режима...")
    btc_df = fetch_candles("BTCUSDT", days=90)
    print(f"BTC свечей: {len(btc_df)}")
    
    results = []
    for symbol in SYMBOLS:
        res = run_symbol_backtest(symbol, btc_df=btc_df)
        if res:
            results.append(res)
        time.sleep(1)  # rate limiting
    
    # Вывод сводной таблицы
    print("\n" + "=" * 100)
    print("РЕЗУЛЬТАТЫ БЭКТЕСТА")
    print("=" * 100)
    print(f"{'Symbol':<10} {'Trades':<8} {'LONG':<6} {'SHORT':<7} {'WinRate%':<10} {'L-WR%':<8} {'S-WR%':<8} {'AvgRR':<8} {'MaxDD%':<8} {'PnL%':<8}")
    print("-" * 110)
    
    for res in results:
        if not res.get("success", False):
            print(f"{res['symbol']:<10} {'ERROR':<8} {'-':<6} {'-':<7} {'-':<10} {'-':<8} {'-':<8} {'-':<8} {'-':<8} {'-':<8}")
            continue
        
        print(f"{res['symbol']:<10} "
              f"{res['total_trades']:<8} "
              f"{res['long_trades']:<6} "
              f"{res['short_trades']:<7} "
              f"{res['win_rate']:<10.1f} "
              f"{res['long_win_rate']:<8.1f} "
              f"{res['short_win_rate']:<8.1f} "
              f"{res['avg_rr']:<8.2f} "
              f"{res['max_drawdown_pct']:<8.2f} "
              f"{res['total_pnl_pct']:<8.2f}")
    
    # Итоги
    print("-" * 100)
    successful = [r for r in results if r.get("success", False)]
    if successful:
        total_trades = sum(r["total_trades"] for r in successful)
        avg_win_rate = np.mean([r["win_rate"] for r in successful])
        avg_pnl = np.mean([r["total_pnl_pct"] for r in successful])
        total_pnl_usd = sum(r["total_pnl_usd"] for r in successful)
        
        avg_rr_all = np.mean([r["avg_rr"] for r in successful])
        
        print(f"ИТОГО: {len(successful)} символов, {total_trades} сделок")
        print(f"Средний WinRate: {avg_win_rate:.1f}%")
        print(f"Средний RR: {avg_rr_all:.2f}")
        print(f"Средний PnL: {avg_pnl:.2f}%")
        print(f"Суммарный PnL: ${total_pnl_usd:+.2f}")
    
    # ── RR Distribution ──
    print("\n" + "=" * 60)
    print("RR DISTRIBUTION (все сделки)")
    print("=" * 60)
    all_rr = []
    for res in successful:
        all_rr.extend(res.get("rr_list", []))
    
    if all_rr:
        rr_gt2 = sum(1 for r in all_rr if r >= 2.0)
        rr_1_2 = sum(1 for r in all_rr if 1.0 <= r < 2.0)
        rr_0_1 = sum(1 for r in all_rr if 0.0 <= r < 1.0)
        rr_neg1_0 = sum(1 for r in all_rr if -1.0 <= r < 0.0)
        rr_lt_neg1 = sum(1 for r in all_rr if r < -1.0)
        
        print(f"  RR >= 2.0  (полный TP):  {rr_gt2:>4} ({rr_gt2/len(all_rr)*100:.1f}%)")
        print(f"  1.0 <= RR < 2.0:         {rr_1_2:>4} ({rr_1_2/len(all_rr)*100:.1f}%)")
        print(f"  0.0 <= RR < 1.0:         {rr_0_1:>4} ({rr_0_1/len(all_rr)*100:.1f}%)")
        print(f"  -1.0 <= RR < 0.0:        {rr_neg1_0:>4} ({rr_neg1_0/len(all_rr)*100:.1f}%)")
        print(f"  RR < -1.0 (beyond SL):   {rr_lt_neg1:>4} ({rr_lt_neg1/len(all_rr)*100:.1f}%)")
        print(f"  Всего сделок с RR:       {len(all_rr)}")
        print(f"  Медиана RR:              {np.median(all_rr):.2f}")
        print(f"  Средний RR:              {np.mean(all_rr):.2f}")
        
        # Debug: первые 5 сделок BTC для проверки формулы
        print("\n  Debug: первые 5 сделок BTCUSDT:")
        btc_res = next((r for r in successful if r["symbol"] == "BTCUSDT"), None)
        if btc_res and btc_res.get("debug_trades"):
            for dt in btc_res["debug_trades"][:5]:
                print(f"    {dt}")
    
    # Сохраняем в файл
    with open("backtest_results_full.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nДетальные результаты сохранены в backtest_results_full.json")

if __name__ == "__main__":
    main()
