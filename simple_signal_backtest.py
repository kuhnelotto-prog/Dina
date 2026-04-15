#!/usr/bin/env python3
"""
Упрощённый бэктест, который использует SignalBuilder для генерации сигналов.
Сравнивает старый порог 0.40 flat vs новый динамический.
"""
import sys, asyncio, json, logging, warnings
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, ".")

from config import settings

from signal_builder import SignalBuilder

# ============================================================================
# Конфигурация
# ============================================================================
SYMBOLS = settings.trading.symbols
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
# Упрощённый бэктест
# ============================================================================
class SimpleSignalBacktest:
    def __init__(self, initial_balance=10000.0, use_dynamic_threshold=True):
        self.initial_balance = initial_balance
        self.use_dynamic_threshold = use_dynamic_threshold
        self.balance = initial_balance
        self.positions = []
        self.trades = []
        self.regime_history = []
        
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
        signal_builder = SignalBuilder(
            symbols=[symbol],
            timeframes=["15m", "1h", "4h"],
            direction="SHORT",
            bus=None,
            learning=None
        )
        
        # Заполняем кэш свечами (только 4H для простоты)
        signal_builder._candle_cache[(symbol, "4h")] = df
        
        # Симуляция торговли
        self.balance = self.initial_balance
        self.trades = []
        position = None
        
        # Проходим по свечам
        for i in range(50, len(df)):
            window = df.iloc[:i+1]
            timestamp = window.index[-1]
            
            # Получаем режим для порога
            regime_at_ts = "SIDEWAYS"
            for ts, reg in self.regime_history:
                if ts <= timestamp:
                    regime_at_ts = reg
                else:
                    break
            
            # Получаем сигнал от SignalBuilder
            # Для упрощения используем только 4H таймфрейм
            signal_builder._candle_cache[(symbol, "4h")] = window
            try:
                signal = asyncio.run(signal_builder.compute(symbol, current_tf="4h"))
            except Exception as e:
                continue
            
            if "error" in signal:
                continue
            
            composite_score = signal.get("composite_score", 0.0)
            current_price = window["close"].iloc[-1]
            threshold = self._get_short_threshold(timestamp, regime_at_ts)
            
            # Логика входа/выхода (упрощённая)
            if position is None:
                # Вход в SHORT если composite_score < -threshold
                if composite_score < -threshold:
                    position = {
                        "entry_time": timestamp,
                        "entry_price": current_price,
                        "size_usd": 1000,  # фиксированный размер
                        "regime": regime_at_ts
                    }
            else:
                # Выход если composite_score > -0.1 (сигнал ослаб)
                if composite_score > -0.1:
                    exit_price = current_price
                    pnl_pct = (position["entry_price"] - exit_price) / position["entry_price"] * 100
                    pnl_usd = position["size_usd"] * pnl_pct / 100
                    
                    self.trades.append({
                        "entry_time": position["entry_time"],
                        "exit_time": timestamp,
                        "entry_price": position["entry_price"],
                        "exit_price": exit_price,
                        "pnl_usd": pnl_usd,
                        "pnl_pct": pnl_pct,
                        "regime": position["regime"]
                    })
                    
                    self.balance += pnl_usd
                    position = None
        
        # Закрываем открытую позицию в конце
        if position is not None:
            exit_price = df["close"].iloc[-1]
            pnl_pct = (position["entry_price"] - exit_price) / position["entry_price"] * 100
            pnl_usd = position["size_usd"] * pnl_pct / 100
            
            self.trades.append({
                "entry_time": position["entry_time"],
                "exit_time": df.index[-1],
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "regime": position["regime"]
            })
            
            self.balance += pnl_usd
        
        return self

# ============================================================================
# Основная функция
# ============================================================================
async def main():
    print("=" * 100)
    print("УПРОЩЁННЫЙ БЭКТЕСТ С SIGNALBUILDER (STATE-based composite_score)")
    print(f"Период: {START_DATE.date()} — {END_DATE.date()} (90 дней)")
    print(f"Символы: {', '.join(SYMBOLS)}")
    print("=" * 100)
    
    all_results = []
    
    for symbol in SYMBOLS:
        print(f"  {symbol}: fetching candles...")
        df = fetch_candles(symbol, START_DATE, END_DATE, "4H")
        if df.empty or len(df) < 100:
            print(f"  {symbol}: insufficient data")
            continue
        
        # Распределение режимов
        regime_dist = {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}
        for i in range(50, len(df)):
            window = df.iloc[:i+1]
            regime = detect_regime_from_df(window)
            regime_dist[regime] += 1
        
        # Старый порог
        print(f"  {symbol}: old threshold (0.40 flat)...")
        bt_old = SimpleSignalBacktest(use_dynamic_threshold=False)
        bt_old.run(df, symbol)
        
        # Новый динамический порог
        print(f"  {symbol}: new dynamic threshold...")
        bt_new = SimpleSignalBacktest(use_dynamic_threshold=True)
        bt_new.run(df, symbol)
        
        # Распределение сделок по режимам
        trades_by_regime = {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}
        for trade in bt_new.trades:
            trades_by_regime[trade["regime"]] += 1
        
        # PnL
        total_pnl_old = sum(t["pnl_usd"] for t in bt_old.trades)
        total_pnl_new = sum(t["pnl_usd"] for t in bt_new.trades)
        pnl_pct_old = total_pnl_old / 10000 * 100
        pnl_pct_new = total_pnl_new / 10000 * 100
        
        # Win rate
        winrate_old = len([t for t in bt_old.trades if t["pnl_usd"] > 0]) / len(bt_old.trades) * 100 if bt_old.trades else 0
        winrate_new = len([t for t in bt_new.trades if t["pnl_usd"] > 0]) / len(bt_new.trades) * 100 if bt_new.trades else 0
        
        # Profit factor
        total_win_old = sum(t["pnl_usd"] for t in bt_old.trades if t["pnl_usd"] > 0)
        total_loss_old = sum(t["pnl_usd"] for t in bt_old.trades if t["pnl_usd"] < 0)
        pf_old = abs(total_win_old / total_loss_old) if total_loss_old != 0 else (999 if total_win_old > 0 else 0)
        
        total_win_new = sum(t["pnl_usd"] for t in bt_new.trades if t["pnl_usd"] > 0)
        total_loss_new = sum(t["pnl_usd"] for t in bt_new.trades if t["pnl_usd"] < 0)
        pf_new = abs(total_win_new / total_loss_new) if total_loss_new != 0 else (999 if total_win_new > 0 else 0)
        
        results = {
            "symbol": symbol,
            "regime_distribution": regime_dist,
            "old": {
                "trades": len(bt_old.trades),
                "winrate": winrate_old,
                "pnl_usd": total_pnl_old,
                "pnl_pct": pnl_pct_old,
                "pf": pf_old
            },
            "new": {
                "trades": len(bt_new.trades),
                "winrate": winrate_new,
                "pnl_usd": total_pnl_new,
                "pnl_pct": pnl_pct_new,
                "pf": pf_new,
                "trades_by_regime": trades_by_regime
            }
        }
        
        all_results.append(results)
    
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
    
    with open("backtest_simple_signal.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\nРезультаты сохранены в backtest_simple_signal.json")

if __name__ == "__main__":
    asyncio.run(main())
