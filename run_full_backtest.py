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

# 10 монет — актуальный портфель (synced с config.py)
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "AVAXUSDT",
           "DOGEUSDT", "ADAUSDT", "LINKUSDT", "SOLUSDT", "SUIUSDT"]

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


def fetch_candles_no_range(symbol, granularity="1D", limit=200):
    """Fetch candles without startTime/endTime (Bitget rejects range for 1D >90d)."""
    params = {"symbol": symbol, "granularity": granularity, "limit": limit,
              "productType": "USDT-FUTURES"}
    resp = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=30)
    data = resp.json()
    if data.get("code") != "00000" or not data.get("data"):
        logger.warning(f"API error for {symbol} 1D: {data.get('msg')}")
        return pd.DataFrame()
    candles = data["data"]
    all_candles = []
    for c in candles:
        all_candles.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

def run_symbol_backtest(symbol, btc_df=None, btc_1d_df=None):
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
        result = bt.run(df=df, symbol=symbol, btc_df=btc_df, btc_1d_df=btc_1d_df)
        
        # Собираем статистику
        total_trades = result.total_trades
        winning_trades = result.winning_trades
        losing_trades = result.losing_trades
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
        
        # Реальный расчёт Risk/Reward для каждой сделки
        # Используем initial_sl (оригинальный SL) для расчёта risk
        rr_values = []
        debug_trades = []
        step_counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        win_rr_values = []
        for t in result.trades:
            init_sl = getattr(t, 'initial_sl', t.sl_price)
            step = getattr(t, 'trailing_step', 0)
            remaining = getattr(t, 'remaining_pct', 1.0)
            reason = getattr(t, 'exit_reason', '?')
            step_counts[step] = step_counts.get(step, 0) + 1
            if t.side == "long":
                risk = abs(t.entry_price - init_sl)
                reward = t.exit_price - t.entry_price if t.exit_price else 0
            else:
                risk = abs(init_sl - t.entry_price)
                reward = t.entry_price - t.exit_price if t.exit_price else 0
            rr = reward / risk if risk > 0 else 0.0
            rr_values.append(rr)
            if t.pnl_usd > 0:
                win_rr_values.append(rr)
            step_tag = f" [step={step}]" if step > 0 else ""
            debug_trades.append(
                f"{t.side:>5} entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
                f"sl={init_sl:.2f}->{t.sl_price:.2f} risk={risk:.2f} RR={rr:.2f} "
                f"pnl={t.pnl_pct:+.2f}% [{reason}]{step_tag} rem={remaining*100:.0f}%"
            )
        avg_rr = np.mean(rr_values) if rr_values else 0.0
        avg_win_rr = np.mean(win_rr_values) if win_rr_values else 0.0
        
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
            "avg_win_rr": avg_win_rr,
            "step_counts": step_counts,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_pnl_usd": result.total_pnl_usd,
            "total_pnl_pct": total_return_pct,
            "rr_list": rr_values,
            "win_rr_list": win_rr_values,
            "debug_trades": debug_trades,
            "raw_trades": result.trades,  # for step-4 detail analysis
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
    print("ПОЛНЫЙ БЭКТЕСТ (BTC 1D EMA50 master filter + динамические пороги)")
    print("Символы:", ", ".join(SYMBOLS))
    print("Период: 90 дней, таймфрейм 4H")
    print("Master filter: LONG only if BTC > EMA50(1D), SHORT only if BTC < EMA50(1D)")
    print("=" * 100)
    
    # Загружаем BTC 4H свечи — для режима всех символов
    print("Загрузка BTC 4H данных для режима...")
    btc_df = fetch_candles("BTCUSDT", days=90, granularity="4H")
    print(f"BTC 4H свечей: {len(btc_df)}")
    
    # Загружаем BTC 1D свечи — для EMA50 master filter (без startTime/endTime, limit=200)
    print("Загрузка BTC 1D данных для EMA50 master filter...")
    btc_1d_df = fetch_candles_no_range("BTCUSDT", granularity="1D", limit=200)
    print(f"BTC 1D свечей: {len(btc_1d_df)}")
    if len(btc_1d_df) >= 50:
        ema50 = btc_1d_df['close'].ewm(span=50, adjust=False).mean()
        last_close = btc_1d_df['close'].iloc[-1]
        last_ema = ema50.iloc[-1]
        regime = "BULL (LONG allowed)" if last_close > last_ema else "BEAR (SHORT allowed)"
        print(f"BTC 1D: close={last_close:.0f}, EMA50={last_ema:.0f} => {regime}")
    
    results = []
    for symbol in SYMBOLS:
        res = run_symbol_backtest(symbol, btc_df=btc_df, btc_1d_df=btc_1d_df)
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

    # ── Trailing Step Distribution ──
    print("\n" + "=" * 60)
    print("TRAILING STEP DISTRIBUTION (все сделки)")
    print("=" * 60)
    total_steps = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for res in successful:
        sc = res.get("step_counts", {})
        for k, v in sc.items():
            total_steps[k] = total_steps.get(k, 0) + v
    total_all = sum(total_steps.values())
    if total_all > 0:
        print(f"  Step 0 (SL без трейлинга):     {total_steps[0]:>4} ({total_steps[0]/total_all*100:.1f}%)")
        print(f"  Step 1 (breakeven):             {total_steps[1]:>4} ({total_steps[1]/total_all*100:.1f}%)")
        print(f"  Step 2 (partial 25%, SL+0.5R):  {total_steps[2]:>4} ({total_steps[2]/total_all*100:.1f}%)")
        print(f"  Step 3 (partial 50%, SL+1.0R):  {total_steps[3]:>4} ({total_steps[3]/total_all*100:.1f}%)")
        print(f"  Step 4 (full TP at +2.5R):      {total_steps[4]:>4} ({total_steps[4]/total_all*100:.1f}%)")
        trailing_activated = total_steps[1] + total_steps[2] + total_steps[3] + total_steps[4]
        print(f"  Trailing activated:             {trailing_activated}/{total_all} ({trailing_activated/total_all*100:.1f}%)")

    # ── Winning Trades RR Analysis ──
    print("\n" + "=" * 60)
    print("WINNING TRADES RR ANALYSIS")
    print("=" * 60)
    all_win_rr = []
    for res in successful:
        all_win_rr.extend(res.get("win_rr_list", []))
    if all_win_rr:
        print(f"  Winning trades:    {len(all_win_rr)}")
        print(f"  Avg win RR:        {np.mean(all_win_rr):.2f}")
        print(f"  Median win RR:     {np.median(all_win_rr):.2f}")
        print(f"  Min win RR:        {min(all_win_rr):.2f}")
        print(f"  Max win RR:        {max(all_win_rr):.2f}")
        # Per-symbol winning RR
        print(f"\n  {'Symbol':<10} {'Wins':<6} {'AvgWinRR':<10} {'MaxStep':<10}")
        print(f"  {'-'*36}")
        for res in successful:
            if not res.get("success"):
                continue
            wrr = res.get("win_rr_list", [])
            sc = res.get("step_counts", {})
            max_step = max((k for k, v in sc.items() if v > 0), default=0)
            avg_wrr = np.mean(wrr) if wrr else 0.0
            print(f"  {res['symbol']:<10} {len(wrr):<6} {avg_wrr:<10.2f} {max_step:<10}")
    else:
        print("  Нет прибыльных сделок")

    # ── Step 4 (Full TP +2.5R) Detail ──
    print("\n" + "=" * 60)
    print("STEP 4 TRADES DETAIL (Full TP at +2.5R)")
    print("=" * 60)
    step4_trades = []
    for res in successful:
        sym = res["symbol"]
        for t in res.get("raw_trades", []):
            if getattr(t, 'trailing_step', 0) == 4:
                init_sl = getattr(t, 'initial_sl', t.sl_price)
                risk = abs(t.entry_price - init_sl)
                if t.side == "long":
                    exit_rr = (t.exit_price - t.entry_price) / risk if risk > 0 else 0
                else:
                    exit_rr = (t.entry_price - t.exit_price) / risk if risk > 0 else 0
                step4_trades.append({
                    "symbol": sym,
                    "side": t.side,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "sl": init_sl,
                    "risk": risk,
                    "exit_rr": exit_rr,
                    "pnl_usd": t.pnl_usd,
                    "pnl_pct": t.pnl_pct,
                    "remaining": getattr(t, 'remaining_pct', 1.0),
                    "partial_pnl": getattr(t, 'partial_pnl_usd', 0),
                    "entry_time": str(getattr(t, 'entry_time', '?')),
                })
    if step4_trades:
        print(f"  Всего step-4 сделок: {len(step4_trades)}\n")
        for i, s4 in enumerate(step4_trades, 1):
            print(f"  #{i} {s4['symbol']} {s4['side'].upper()}")
            print(f"     Entry: {s4['entry']:.4f}  Exit: {s4['exit']:.4f}  SL: {s4['sl']:.4f}")
            print(f"     Risk(1R): {s4['risk']:.4f}  Exit RR: {s4['exit_rr']:.2f}")
            print(f"     PnL: ${s4['pnl_usd']:+.2f} ({s4['pnl_pct']:+.2f}%)")
            print(f"     Partial PnL booked: ${s4['partial_pnl']:.2f}  Remaining at exit: {s4['remaining']*100:.0f}%")
            print(f"     Entry time: {s4['entry_time']}")
            print()
    else:
        print("  Нет сделок, дошедших до step 4")

    # Сохраняем в файл
    with open("backtest_results_full.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nДетальные результаты сохранены в backtest_results_full.json")

if __name__ == "__main__":
    main()
