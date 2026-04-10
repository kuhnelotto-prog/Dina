#!/usr/bin/env python3
"""
Сравнение порогов SHORT с использованием реального SignalBuilder (STATE-based).
"""
import os, sys, asyncio, json, logging, warnings
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from signal_builder import SignalBuilder
from backtester import Backtester

# ============================================================================
# Конфигурация
# ============================================================================
SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,AVAXUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,UNIUSDT").split(",")
START_DATE = datetime.now(timezone.utc) - timedelta(days=90)
END_DATE = datetime.now(timezone.utc) - timedelta(minutes=5)

# ============================================================================
# Вспомогательные функции
# ============================================================================
def fetch_candles(symbol, start_date, end_date, granularity="4H"):
    import requests, time
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

def detect_regime_from_df(df_4h: pd.DataFrame) -> str:
    """Определяет режим по EMA20 vs EMA50 на 4H."""
    if df_4h is None or len(df_4h) < 50:
        return "SIDEWAYS"
    close = df_4h["close"] if "close" in df_4h.columns else df_4h.iloc[:, 3]
    ema_fast = close.ewm(span=20, adjust=False).mean()
    ema_slow = close.ewm(span=50, adjust=False).mean()
    current_fast = float(ema_fast.iloc[-1])
    current_slow = float(ema_slow.iloc[-1])
    if current_slow == 0:
        return "SIDEWAYS"
    diff_pct = (current_fast - current_slow) / current_slow * 100
    if diff_pct > 0.5:
        return "BULL"
    elif diff_pct < -0.5:
        return "BEAR"
    else:
        return "SIDEWAYS"

# ============================================================================
# Модифицированный Backtester с реальным SignalBuilder
# ============================================================================
class RealSignalBacktester(Backtester):
    def __init__(self, initial_balance=10000.0, use_dynamic_threshold=True):
        super().__init__(initial_balance=initial_balance, use_real_data=False)
        self.use_dynamic_threshold = use_dynamic_threshold
        self.regime_history = []
        self.trades_with_regime = []
        self.signal_builder = None
    
    def _get_short_threshold(self, timestamp, regime=None):
        if not self.use_dynamic_threshold:
            return 0.40
        if regime is None:
            for ts, reg in reversed(self.regime_history):
                if ts <= timestamp:
                    return self._regime_to_threshold(reg)
            return 0.40
        return self._regime_to_threshold(regime)
    
    def _regime_to_threshold(self, regime):
        if regime == "BEAR":
            return 0.30
        elif regime == "BULL":
            return 0.50
        else:
            return 0.40
    
    def run(self, df, symbol):
        # Вычисляем режим для каждой свечи
        self.regime_history = []
        for i in range(50, len(df)):
            window = df.iloc[:i+1]
            regime = detect_regime_from_df(window)
            ts = window.index[-1]
            self.regime_history.append((ts, regime))
        
        # Создаём SignalBuilder (SHORT direction)
        self.signal_builder = SignalBuilder(
            symbols=[symbol],
            timeframes=["15m", "1h", "4h"],
            direction="SHORT",
            bus=None,
            learning=None
        )
        
        # Заполняем кэш свечами (упрощённо — только 4H)
        self.signal_builder._candle_cache[(symbol, "4h")] = df
        
        # Запускаем стандартный бэктест
        result = super().run(df=df, symbol=symbol)
        
        # Связываем сделки с режимом
        self.trades_with_regime = []
        for trade in result.trades:
            entry_time = trade.entry_time
            regime_at_entry = "SIDEWAYS"
            for ts, reg in self.regime_history:
                if ts <= entry_time:
                    regime_at_entry = reg
                else:
                    break
            self.trades_with_regime.append((trade, regime_at_entry))
        
        return result

# ============================================================================
# Основная функция
# ============================================================================
async def compare_symbol(symbol):
    print(f"  {symbol}: fetching candles...")
    df = fetch_candles(symbol, START_DATE, END_DATE, "4H")
    if df.empty or len(df) < 100:
        print(f"  {symbol}: insufficient data")
        return None
    
    results = {
        "symbol": symbol,
        "old": {"trades": 0, "winrate": 0.0, "pnl_usd": 0.0, "pnl_pct": 0.0, "maxdd_pct": 0.0, "pf": 0.0},
        "new": {"trades": 0, "winrate": 0.0, "pnl_usd": 0.0, "pnl_pct": 0.0, "maxdd_pct": 0.0, "pf": 0.0, "trades_by_regime": {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}},
        "regime_distribution": {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}
    }
    
    # Распределение режимов
    for i in range(50, len(df)):
        window = df.iloc[:i+1]
        regime = detect_regime_from_df(window)
        results["regime_distribution"][regime] += 1
    
    # Старый порог
    print(f"  {symbol}: old threshold (0.40 flat)...")
    bt_old = RealSignalBacktester(use_dynamic_threshold=False)
    result_old = bt_old.run(df=df, symbol=symbol)
    
    if result_old.total_trades > 0:
        results["old"]["trades"] = result_old.total_trades
        results["old"]["winrate"] = result_old.winning_trades / result_old.total_trades * 100
        results["old"]["pnl_usd"] = result_old.total_pnl_usd
        results["old"]["pnl_pct"] = result_old.total_pnl_usd / 10000 * 100
        results["old"]["maxdd_pct"] = result_old.max_drawdown_pct
        total_win = sum(t.pnl_usd for t in result_old.trades if t.pnl_usd > 0)
        total_loss = sum(t.pnl_usd for t in result_old.trades if t.pnl_usd < 0)
        results["old"]["pf"] = abs(total_win / total_loss) if total_loss != 0 else (999 if total_win > 0 else 0)
    
    # Новый динамический порог
    print(f"  {symbol}: new dynamic threshold...")
    bt_new = RealSignalBacktester(use_dynamic_threshold=True)
    result_new = bt_new.run(df=df, symbol=symbol)
    
    if result_new.total_trades > 0:
        results["new"]["trades"] = result_new.total_trades
        results["new"]["winrate"] = result_new.winning_trades / result_new.total_trades * 100
        results["new"]["pnl_usd"] = result_new.total_pnl_usd
        results["new"]["pnl_pct"] = result_new.total_pnl_usd / 10000 * 100
        results["new"]["maxdd_pct"] = result_new.max_drawdown_pct
        total_win = sum(t.pnl_usd for t in result_new.trades if t.pnl_usd > 0)
        total_loss = sum(t.pnl_usd for t in result_new.trades if t.pnl_usd < 0)
        results["new"]["pf"] = abs(total_win / total_loss) if total_loss != 0 else (999 if total_win > 0 else 0)
        
        # Распределение сделок по режимам
        for trade, regime in bt_new.trades_with_regime:
            results["new"]["trades_by_regime"][regime] += 1
    
    return results

async def main():
    print("=" * 100)
    print("СРАВНЕНИЕ ПОРОГОВ SHORT (с STATE-based composite_score)")
    print(f"Период: {START_DATE.date()} — {END_DATE.date()} (90 дней)")
    print(f"Символы: {', '.join(SYMBOLS)}")
    print("=" * 100)
    
    all_results = []
    for symbol in SYMBOLS:
        res = await compare_symbol(symbol)
        if res:
            all_results.append(res)
    
    # Вывод результатов
    print("\n" + "=" * 100)
    print("РЕЗУЛЬТАТЫ ПО СИМВОЛАМ")
    print("=" * 100)
    print(f"{'Symbol':<10} {'Regime B/E/S':<12} {'Trades O/N':<12} {'dTrades':<8} {'WinRate O/N':<14} {'PnL% O/N':<16} {'PF O/N':<12}")
    print("-" * 100)
    
    total_trades_old = 0
    total_trades_new = 0
    total_pnl_old = 0.0
    total_pnl_new = 0.0
    
    for res in all_results:
        symbol = res["symbol"]
        regime_dist = res["regime_distribution"]
        total_candles = sum(regime_dist.values())
        regime_pct = {k: v/total_candles*100 for k, v in regime_dist.items()}
        regime_str = f"{regime_pct['BEAR']:.0f}/{regime_pct['SIDEWAYS']:.0f}/{regime_pct['BULL']:.0f}"
        
        trades_old = res["old"]["trades"]
        trades_new = res["new"]["trades"]
        delta_trades = trades_new - trades_old
        
        winrate_old = res["old"]["winrate"]
        winrate_new = res["new"]["winrate"]
        
        pnl_old = res["old"]["pnl_pct"]
        pnl_new = res["new"]["pnl_pct"]
        
        pf_old = res["old"]["pf"]
        pf_new = res["new"]["pf"]
        
        print(f"{symbol:<10} {regime_str:<12} {trades_old:>2}/{trades_new:<2} {delta_trades:>+8} "
              f"{winrate_old:>5.1f}/{winrate_new:<5.1f} {pnl_old:>+6.1f}/{pnl_new:<+6.1f} {pf_old:>4.2f}/{pf_new:<4.2f}")
        
        total_trades_old += trades_old
        total_trades_new += trades_new
        total_pnl_old += pnl_old
        total_pnl_new += pnl_new
    
    # Распределение сделок по режимам
    print("\n" + "=" * 100)
    print("РАСПРЕДЕЛЕНИЕ СДЕЛОК ПО РЕЖИМАМ (НОВЫЙ ПОРОГ)")
    print("=" * 100)
    regime_trades = {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}
    for res in all_results:
        for regime in ["BULL", "BEAR", "SIDEWAYS"]:
            regime_trades[regime] += res["new"]["trades_by_regime"][regime]
    
    print(f"{'Режим':<10} {'Сделок':>8} {'Доля':>8}")
    print("-" * 30)
    for regime in ["BEAR", "SIDEWAYS", "BULL"]:
        count = regime_trades[regime]
        pct = count / total_trades_new * 100 if total_trades_new > 0 else 0
        print(f"{regime:<10} {count:>8} {pct:>7.1f}%")
    
    # Топ по приросту сделок
    print("\n" + "=" * 100)
    print("ТОП МОНЕТ ПО ПРИРОСТУ СДЕЛОК")
    print("=" * 100)
    sorted_by_delta = sorted(all_results, key=lambda x: x["new"]["trades"] - x["old"]["trades"], reverse=True)
    print(f"{'Symbol':<10} {'Trades O/N':<12} {'dTrades':<8} {'dWinRate%':<10} {'dPnL%':<10}")
    print("-" * 50)
    for res in sorted_by_delta[:5]:
        symbol = res["symbol"]
        trades_old = res["old"]["trades"]
        trades_new = res["new"]["trades"]
        delta = trades_new - trades_old
        delta_winrate = res["new"]["winrate"] - res["old"]["winrate"]
        delta_pnl = res["new"]["pnl_pct"] - res["old"]["pnl_pct"]
        print(f"{symbol:<10} {trades_old:>2}/{trades_new:<2} {delta:>+8} {delta_winrate:>+9.1f} {delta_pnl:>+9.1f}")
    
    # Итог
    print("\n" + "=" * 100)
    print("ИТОГОВАЯ СВОДКА")
    print("=" * 100)
    print(f"Всего сделок (старый): {total_trades_old}")
    print(f"Всего сделок (новый):  {total_trades_new}")
    print(f"Прирост сделок:        {total_trades_new - total_trades_old:+d} ({((total_trades_new/total_trades_old-1)*100 if total_trades_old>0 else 0):+.1f}%)")
    print(f"Суммарный PnL% (старый): {total_pnl_old:+.2f}%")
    print(f"Суммарный PnL% (новый):  {total_pnl_new:+.2f}%")
    print(f"dPnL%:                  {total_pnl_new - total_pnl_old:+.2f}%")
    
    # Сохраняем
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": {"start": START_DATE.isoformat(), "end": END_DATE.isoformat()},
        "symbols": SYMBOLS,
        "results": all_results,
        "summary": {
            "total_trades_old": total_trades_old,
            "total_trades_new": total_trades_new,
            "total_pnl_old": total_pnl_old,
            "total_pnl_new": total_pnl_new,
            "regime_distribution": dict(regime_trades),
        }
    }
    
    with open("backtest_regime_comparison_real.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\nРезультаты сохранены в backtest_regime_comparison_real.json")

if __name__ == "__main__":
    asyncio.run(main())
