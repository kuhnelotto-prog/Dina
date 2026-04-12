#!/usr/bin/env python3
"""
Анализ паттернов winning/losing сделок из backtest_regime_signalbuilder.json
"""
import json
import pandas as pd
from datetime import datetime, timezone, timedelta
import warnings
warnings.filterwarnings("ignore")

# Загружаем результаты
with open("backtest_regime_signalbuilder.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# Собираем все сделки из реального бэктеста
# Для этого нужно запустить Backtester с детальным логированием
# Создадим упрощённый анализ по агрегированным данным

print("=" * 100)
print("АНАЛИЗ WINNING/LOSING СДЕЛОК")
print("=" * 100)

# Агрегированные данные по символам
symbol_stats = []
for res in data["results"]:
    symbol = res["symbol"]
    new = res["new"]
    trades = new["trades"]
    winrate = new["winrate"]
    pnl = new["pnl_pct"]
    pf = new["pf"]
    avg_win = new["avg_win"]
    avg_loss = new["avg_loss"]
    regime_trades = new["trades_by_regime"]
    
    # Вычисляем количество winning/losing сделок
    winning = int(trades * winrate / 100)
    losing = trades - winning
    
    symbol_stats.append({
        "symbol": symbol,
        "trades": trades,
        "winning": winning,
        "losing": losing,
        "winrate": winrate,
        "pnl": pnl,
        "pf": pf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "regime_BEAR": regime_trades["BEAR"],
        "regime_SIDEWAYS": regime_trades["SIDEWAYS"],
        "regime_BULL": regime_trades["BULL"],
    })

df = pd.DataFrame(symbol_stats)

print("\n1. СИМВОЛЫ С НАИХУДШИМ WinRate:")
worst = df.sort_values("winrate").head(5)
for _, row in worst.iterrows():
    print(f"  {row['symbol']:<8} {row['winrate']:>5.1f}% ({row['winning']}W/{row['losing']}L) | "
          f"PnL: {row['pnl']:+.2f}% | PF: {row['pf']:.2f} | "
          f"Regime: BEAR={row['regime_BEAR']} SIDE={row['regime_SIDEWAYS']} BULL={row['regime_BULL']}")

print("\n2. СИМВОЛЫ С НАИЛУЧШИМ WinRate:")
best = df.sort_values("winrate", ascending=False).head(5)
for _, row in best.iterrows():
    print(f"  {row['symbol']:<8} {row['winrate']:>5.1f}% ({row['winning']}W/{row['losing']}L) | "
          f"PnL: {row['pnl']:+.2f}% | PF: {row['pf']:.2f} | "
          f"Regime: BEAR={row['regime_BEAR']} SIDE={row['regime_SIDEWAYS']} BULL={row['regime_BULL']}")

print("\n3. РАСПРЕДЕЛЕНИЕ СДЕЛОК ПО РЕЖИМАМ:")
total_BEAR = df["regime_BEAR"].sum()
total_SIDE = df["regime_SIDEWAYS"].sum()
total_BULL = df["regime_BULL"].sum()
total_all = total_BEAR + total_SIDE + total_BULL
print(f"  BEAR:     {total_BEAR} сделок ({total_BEAR/total_all*100:.1f}%)")
print(f"  SIDEWAYS: {total_SIDE} сделок ({total_SIDE/total_all*100:.1f}%)")
print(f"  BULL:     {total_BULL} сделок ({total_BULL/total_all*100:.1f}%)")

# Анализ по режимам
print("\n4. WINRATE ПО РЕЖИМАМ (оценка):")
# Для оценки нужно знать распределение winning/losing по режимам
# Создадим приблизительную оценку на основе данных по символам
bear_win_est = 0
bear_total_est = 0
for _, row in df.iterrows():
    # Предположим, что winrate одинаков во всех режимах для символа
    bear_win_est += row["regime_BEAR"] * (row["winrate"] / 100)
    bear_total_est += row["regime_BEAR"]

side_win_est = 0
side_total_est = 0
for _, row in df.iterrows():
    side_win_est += row["regime_SIDEWAYS"] * (row["winrate"] / 100)
    side_total_est += row["regime_SIDEWAYS"]

bull_win_est = 0
bull_total_est = 0
for _, row in df.iterrows():
    bull_win_est += row["regime_BULL"] * (row["winrate"] / 100)
    bull_total_est += row["regime_BULL"]

print(f"  BEAR:     {bear_win_est/bear_total_est*100:.1f}% winrate (оценка)")
print(f"  SIDEWAYS: {side_win_est/side_total_est*100:.1f}% winrate (оценка)")
print(f"  BULL:     {bull_win_est/bull_total_est*100:.1f}% winrate (оценка)")

print("\n5. ПРЕДЛОЖЕНИЯ ПО ФИЛЬТРАМ:")
print("  a) Исключить XRPUSDT, BNBUSDT — худшие WinRate (17.6%, 20.0%)")
print("  b) Увеличить порог composite_score для BEAR режима (сейчас 0.35)")
print("  c) Добавить фильтр по ATR: пропускать входы при ATR < 0.5% (малая волатильность)")
print("  d) Требовать минимум 2 STATE-компонента в одном направлении")
print("  e) В SIDEWAYS режиме требовать composite > 0.45 (более сильный сигнал)")

# Запустим детальный анализ с реальными сделками
print("\n" + "=" * 100)
print("ДЕТАЛЬНЫЙ АНАЛИЗ (нужен запуск Backtester с логированием)")
print("=" * 100)
print("Для анализа composite_score на входе нужно:")
print("  1. Модифицировать Backtester.run() для записи composite_score каждой сделки")
print("  2. Записать режим (BEAR/SIDEWAYS/BULL) на момент входа")
print("  3. Собрать статистику по winning/losing сделкам")
print("\nРекомендуемый фильтр для SignalBuilder:")
print("  - Если режим BEAR: composite_score > 0.45 для LONG, < -0.45 для SHORT")
print("  - Если режим SIDEWAYS: composite_score > 0.50 для LONG, < -0.50 для SHORT")
print("  - Если ATR < 0.5%: пропускать вход (малая волатильность)")
print("  - Требовать минимум 2 из 4 STATE компонентов в одном направлении")

# Сохраняем вывод
with open("winloss_analysis.txt", "w", encoding="utf-8") as f:
    f.write("Анализ паттернов winning/losing сделок\n")
    f.write("=" * 50 + "\n")
    f.write(df.to_string())
    f.write("\n\nПредложения по фильтрам:\n")
    f.write("1. Исключить символы с WinRate < 25% (XRPUSDT, BNBUSDT, SOLUSDT)\n")
    f.write("2. BEAR режим: повысить порог до 0.45\n")
    f.write("3. SIDEWAYS режим: повысить порог до 0.50\n")
    f.write("4. Добавить фильтр ATR > 0.5%\n")
    f.write("5. Требовать минимум 2 STATE компонента в одном направлении\n")

print(f"\nАнализ сохранён в winloss_analysis.txt")