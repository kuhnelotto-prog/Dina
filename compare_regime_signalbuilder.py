#!/usr/bin/env python3
"""
Сравнение порогов SHORT с использованием обновлённого Backtester.
Backtester теперь использует STATE-based _compute_composite() и поддерживает LONG+SHORT.
"""
import sys, json, logging, warnings, time
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, ".")

from config import settings
from backtester import Backtester

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

def detect_regime_from_df(df_4h):
    if df_4h is None or len(df_4h) < 50:
        return "SIDEWAYS"
    close = df_4h["close"]
    ema_fast = close.ewm(span=20, adjust=False).mean()
    ema_slow = close.ewm(span=50, adjust=False).mean()
    diff_pct = (float(ema_fast.iloc[-1]) - float(ema_slow.iloc[-1])) / float(ema_slow.iloc[-1]) * 100
    if diff_pct > 0.5:
        return "BULL"
    elif diff_pct < -0.5:
        return "BEAR"
    return "SIDEWAYS"

# ============================================================================
# Основная функция
# ============================================================================
def compare_symbol(symbol):
    print(f"  {symbol}: fetching 4H candles...")
    df_4h = fetch_candles(symbol, START_DATE, END_DATE, "4H")
    if df_4h.empty or len(df_4h) < 100:
        print(f"  {symbol}: insufficient data")
        return None
    
    results = {
        "symbol": symbol,
        "old": {"trades": 0, "long": 0, "short": 0, "winrate": 0.0, "pnl_pct": 0.0, "pf": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0},
        "new": {"trades": 0, "long": 0, "short": 0, "winrate": 0.0, "pnl_pct": 0.0, "pf": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0,
                "trades_by_regime": {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}},
        "regime_distribution": {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}
    }
    
    # Распределение режимов
    regime_history = []
    for i in range(50, len(df_4h)):
        window = df_4h.iloc[:i+1]
        regime = detect_regime_from_df(window)
        results["regime_distribution"][regime] += 1
        regime_history.append((window.index[-1], regime))
    
    # Старый порог (0.35 flat — как в Backtester)
    print(f"  {symbol}: old threshold (0.35 flat)...")
    bt_old = Backtester(initial_balance=10000.0, use_real_data=False)
    result_old = bt_old.run(df_4h, symbol)
    
    if result_old.total_trades > 0:
        results["old"]["trades"] = result_old.total_trades
        results["old"]["long"] = sum(1 for t in result_old.trades if t.side == "long")
        results["old"]["short"] = sum(1 for t in result_old.trades if t.side == "short")
        results["old"]["winrate"] = result_old.winning_trades / result_old.total_trades * 100
        results["old"]["pnl_pct"] = result_old.total_pnl_usd / 10000 * 100
        wins = [t.pnl_pct for t in result_old.trades if t.pnl_usd > 0]
        losses = [t.pnl_pct for t in result_old.trades if t.pnl_usd <= 0]
        total_win = sum(t.pnl_usd for t in result_old.trades if t.pnl_usd > 0)
        total_loss = sum(t.pnl_usd for t in result_old.trades if t.pnl_usd < 0)
        results["old"]["pf"] = abs(total_win / total_loss) if total_loss != 0 else (999 if total_win > 0 else 0)
        results["old"]["avg_win"] = sum(wins) / len(wins) if wins else 0.0
        results["old"]["avg_loss"] = sum(losses) / len(losses) if losses else 0.0
    
    # Новый порог — тот же Backtester (STATE-based уже встроен)
    print(f"  {symbol}: new threshold (STATE-based)...")
    bt_new = Backtester(initial_balance=10000.0, use_real_data=False)
    result_new = bt_new.run(df_4h, symbol)
    
    if result_new.total_trades > 0:
        results["new"]["trades"] = result_new.total_trades
        results["new"]["long"] = sum(1 for t in result_new.trades if t.side == "long")
        results["new"]["short"] = sum(1 for t in result_new.trades if t.side == "short")
        results["new"]["winrate"] = result_new.winning_trades / result_new.total_trades * 100
        results["new"]["pnl_pct"] = result_new.total_pnl_usd / 10000 * 100
        wins = [t.pnl_pct for t in result_new.trades if t.pnl_usd > 0]
        losses = [t.pnl_pct for t in result_new.trades if t.pnl_usd <= 0]
        total_win = sum(t.pnl_usd for t in result_new.trades if t.pnl_usd > 0)
        total_loss = sum(t.pnl_usd for t in result_new.trades if t.pnl_usd < 0)
        results["new"]["pf"] = abs(total_win / total_loss) if total_loss != 0 else (999 if total_win > 0 else 0)
        results["new"]["avg_win"] = sum(wins) / len(wins) if wins else 0.0
        results["new"]["avg_loss"] = sum(losses) / len(losses) if losses else 0.0
        
        # Распределение сделок по режимам
        for trade in result_new.trades:
            entry_time = trade.entry_time
            regime_at_entry = "SIDEWAYS"
            for ts, reg in regime_history:
                if ts <= entry_time:
                    regime_at_entry = reg
                else:
                    break
            results["new"]["trades_by_regime"][regime_at_entry] += 1
    
    return results

def main():
    print("=" * 100)
    print("BACKTEST: STATE-based composite (LONG + SHORT)")
    print(f"Period: {START_DATE.date()} -- {END_DATE.date()} (90 days)")
    print(f"Symbols: {', '.join(SYMBOLS)}")
    print(f"Threshold: 0.35 (LONG: composite > 0.35, SHORT: composite < -0.35)")
    print("=" * 100)
    
    all_results = []
    for symbol in SYMBOLS:
        res = compare_symbol(symbol)
        if res:
            all_results.append(res)
    
    # Вывод результатов
    print("\n" + "=" * 100)
    print("RESULTS BY SYMBOL")
    print("=" * 100)
    print(f"{'Symbol':<10} {'Regime B/S/U':<12} {'Trades':<8} {'L/S':<8} {'WinRate':<10} {'PnL%':<10} {'PF':<8}")
    print("-" * 70)
    
    total_trades = 0
    total_long = 0
    total_short = 0
    total_pnl = 0.0
    
    for res in all_results:
        symbol = res["symbol"]
        rd = res["regime_distribution"]
        total_candles = sum(rd.values()) or 1
        regime_str = f"{rd['BEAR']*100//total_candles}/{rd['SIDEWAYS']*100//total_candles}/{rd['BULL']*100//total_candles}"
        
        trades = res["new"]["trades"]
        long_t = res["new"]["long"]
        short_t = res["new"]["short"]
        winrate = res["new"]["winrate"]
        pnl = res["new"]["pnl_pct"]
        pf = res["new"]["pf"]
        
        print(f"{symbol:<10} {regime_str:<12} {trades:<8} {long_t}/{short_t:<5} {winrate:<10.1f} {pnl:<+10.2f} {pf:<8.2f}")
        
        total_trades += trades
        total_long += long_t
        total_short += short_t
        total_pnl += pnl
    
    # Распределение сделок по режимам
    print("\n" + "=" * 100)
    print("TRADES BY REGIME")
    print("=" * 100)
    regime_trades = {"BULL": 0, "BEAR": 0, "SIDEWAYS": 0}
    for res in all_results:
        for regime in ["BULL", "BEAR", "SIDEWAYS"]:
            regime_trades[regime] += res["new"]["trades_by_regime"][regime]
    
    for regime in ["BEAR", "SIDEWAYS", "BULL"]:
        count = regime_trades[regime]
        pct = count / total_trades * 100 if total_trades > 0 else 0
        print(f"  {regime:<10} {count:>4} trades ({pct:.1f}%)")
    
    # Агрегированные метрики
    all_wins = []
    all_losses = []
    for res in all_results:
        all_wins.append(res["new"]["avg_win"])
        all_losses.append(res["new"]["avg_loss"])
    
    total_winning = sum(res["new"].get("trades", 0) * res["new"].get("winrate", 0) / 100 for res in all_results)
    total_losing = total_trades - total_winning
    avg_winrate = total_winning / total_trades * 100 if total_trades > 0 else 0
    
    # Средний win/loss по всем символам (взвешенный по количеству сделок)
    weighted_avg_win = 0.0
    weighted_avg_loss = 0.0
    total_win_count = 0
    total_loss_count = 0
    for res in all_results:
        n = res["new"]["trades"]
        wr = res["new"]["winrate"] / 100
        w_count = int(n * wr)
        l_count = n - w_count
        weighted_avg_win += res["new"]["avg_win"] * w_count
        weighted_avg_loss += res["new"]["avg_loss"] * l_count
        total_win_count += w_count
        total_loss_count += l_count
    
    avg_win = weighted_avg_win / total_win_count if total_win_count > 0 else 0
    avg_loss = weighted_avg_loss / total_loss_count if total_loss_count > 0 else 0
    
    # Profit Factor
    total_gross_win = avg_win * total_win_count
    total_gross_loss = abs(avg_loss) * total_loss_count
    total_pf = total_gross_win / total_gross_loss if total_gross_loss > 0 else 0
    
    # Итог
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Total trades:    {total_trades}")
    print(f"  LONG:          {total_long}")
    print(f"  SHORT:         {total_short}")
    print(f"WinRate:         {avg_winrate:.1f}% ({int(total_winning)}W / {int(total_losing)}L)")
    print(f"Profit Factor:   {total_pf:.2f}")
    print(f"Avg Win:         {avg_win:+.2f}%")
    print(f"Avg Loss:        {avg_loss:+.2f}%")
    print(f"Total PnL%:      {total_pnl:+.2f}%")
    
    # Сохраняем
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period": {"start": START_DATE.isoformat(), "end": END_DATE.isoformat()},
        "symbols": SYMBOLS,
        "results": all_results,
        "summary": {
            "total_trades": total_trades,
            "total_long": total_long,
            "total_short": total_short,
            "total_pnl": total_pnl,
            "winrate": avg_winrate,
            "profit_factor": total_pf,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "regime_distribution": dict(regime_trades),
        }
    }
    
    with open("backtest_regime_signalbuilder.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\nResults saved to backtest_regime_signalbuilder.json")

if __name__ == "__main__":
    main()
