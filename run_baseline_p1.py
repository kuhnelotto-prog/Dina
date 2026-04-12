#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_baseline_p1.py — Создание baseline метрик после P1.
TOP-5 символов: BTCUSDT, ETHUSDT, XRPUSDT, DOGEUSDT, LINKUSDT
Сохраняет результаты в baseline_p1.json
"""
import sys, os, time, logging, json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtester import Backtester

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT"]
DAYS = 90


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
    """Fetch candles without startTime/endTime."""
    params = {"symbol": symbol, "granularity": granularity, "limit": limit,
              "productType": "USDT-FUTURES"}
    resp = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=30)
    data = resp.json()
    if data.get("code") != "00000" or not data.get("data"):
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


def run_symbol(symbol, btc_df, btc_1d_df):
    """Run backtest for one symbol, return metrics dict."""
    logger.info(f"=== {symbol} ===")
    try:
        if symbol == "BTCUSDT":
            df = btc_df
        else:
            df = fetch_candles(symbol, days=DAYS)
        if df.empty or len(df) < 100:
            logger.error(f"  ❌ {symbol}: недостаточно данных ({len(df)} свечей)")
            return None

        bt = Backtester(initial_balance=10000.0, use_real_data=False)
        result = bt.run(df=df, symbol=symbol, btc_df=btc_df, btc_1d_df=btc_1d_df)

        total_trades = result.total_trades
        winning = result.winning_trades
        losing = result.losing_trades
        win_rate = (winning / total_trades * 100) if total_trades > 0 else 0.0
        total_pnl_pct = (result.final_balance - result.initial_balance) / result.initial_balance * 100
        total_pnl_usd = result.total_pnl_usd

        # Profit factor
        gross_profit = sum(t.pnl_usd for t in result.trades if t.pnl_usd > 0)
        gross_loss = abs(sum(t.pnl_usd for t in result.trades if t.pnl_usd < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # Sharpe ratio (daily returns proxy)
        pnl_list = [t.pnl_pct for t in result.trades]
        if len(pnl_list) > 1:
            arr = np.array(pnl_list)
            sharpe = (arr.mean() / arr.std()) * np.sqrt(252) if arr.std() > 0 else 0
        else:
            sharpe = 0

        # Avg RR
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
        avg_rr = float(np.mean(rr_values)) if rr_values else 0.0

        # Long/Short breakdown
        long_trades = [t for t in result.trades if t.side == "long"]
        short_trades = [t for t in result.trades if t.side == "short"]
        long_wins = sum(1 for t in long_trades if t.pnl_usd > 0)
        short_wins = sum(1 for t in short_trades if t.pnl_usd > 0)

        return {
            "symbol": symbol,
            "total_trades": total_trades,
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": round(win_rate, 2),
            "long_win_rate": round((long_wins / len(long_trades) * 100) if long_trades else 0, 2),
            "short_win_rate": round((short_wins / len(short_trades) * 100) if short_trades else 0, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "total_pnl_usd": round(total_pnl_usd, 2),
            "max_drawdown_pct": round(result.max_drawdown_pct, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 999.0,
            "sharpe_ratio": round(float(sharpe), 2),
            "avg_rr": round(avg_rr, 2),
            "final_balance": round(result.final_balance, 2),
        }
    except Exception as e:
        logger.error(f"  ❌ {symbol}: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    print("=" * 80)
    print("BASELINE P1 — POST-AUDIT BACKTEST")
    print(f"Symbols: {', '.join(SYMBOLS)}")
    print(f"Period: {DAYS} days, timeframe 4H")
    print("=" * 80)

    # BTC data
    print("\nЗагрузка BTC 4H...")
    btc_df = fetch_candles("BTCUSDT", days=DAYS, granularity="4H")
    print(f"BTC 4H: {len(btc_df)} свечей")

    print("Загрузка BTC 1D...")
    btc_1d_df = fetch_candles_no_range("BTCUSDT", granularity="1D", limit=200)
    print(f"BTC 1D: {len(btc_1d_df)} свечей")

    # Определяем период
    if not btc_df.empty:
        period_start = str(btc_df.index[0].date())
        period_end = str(btc_df.index[-1].date())
    else:
        period_start = "unknown"
        period_end = "unknown"

    # Run backtests
    by_symbol = {}
    for symbol in SYMBOLS:
        res = run_symbol(symbol, btc_df, btc_1d_df)
        if res:
            by_symbol[symbol] = res
        time.sleep(1)

    # Aggregate metrics
    successful = list(by_symbol.values())
    if not successful:
        print("❌ Нет успешных бэктестов!")
        return

    total_trades = sum(r["total_trades"] for r in successful)
    total_pnl_usd = sum(r["total_pnl_usd"] for r in successful)
    avg_pnl_pct = np.mean([r["total_pnl_pct"] for r in successful])
    avg_win_rate = np.mean([r["win_rate"] for r in successful])
    avg_sharpe = np.mean([r["sharpe_ratio"] for r in successful])
    max_dd = max(r["max_drawdown_pct"] for r in successful)
    avg_profit_factor = np.mean([r["profit_factor"] for r in successful if r["profit_factor"] < 999])

    baseline = {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "branch": "feature/post-audit-improvements",
        "commit": "post-P1",
        "symbols": SYMBOLS,
        "period": {
            "start": period_start,
            "end": period_end,
            "days": DAYS,
            "timeframe": "4H",
        },
        "metrics": {
            "total_pnl_pct": round(float(avg_pnl_pct), 2),
            "total_pnl_usd": round(float(total_pnl_usd), 2),
            "win_rate": round(float(avg_win_rate), 2),
            "sharpe_ratio": round(float(avg_sharpe), 2),
            "max_drawdown_pct": round(float(max_dd), 2),
            "profit_factor": round(float(avg_profit_factor), 2),
            "total_trades": int(total_trades),
        },
        "by_symbol": by_symbol,
    }

    # Save
    with open("baseline_p1.json", "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)

    # Print summary
    print("\n" + "=" * 80)
    print("BASELINE P1 RESULTS")
    print("=" * 80)
    print(f"Period: {period_start} -> {period_end} ({DAYS} days)")
    print(f"Symbols: {len(successful)}/{len(SYMBOLS)}")
    print()
    print(f"{'Symbol':<10} {'Trades':<8} {'WR%':<8} {'PnL%':<10} {'PnL$':<10} {'MaxDD%':<8} {'Sharpe':<8} {'PF':<8}")
    print("-" * 70)
    for sym in SYMBOLS:
        r = by_symbol.get(sym)
        if not r:
            print(f"{sym:<10} {'ERROR'}")
            continue
        print(f"{r['symbol']:<10} "
              f"{r['total_trades']:<8} "
              f"{r['win_rate']:<8.1f} "
              f"{r['total_pnl_pct']:<10.2f} "
              f"{r['total_pnl_usd']:<10.2f} "
              f"{r['max_drawdown_pct']:<8.2f} "
              f"{r['sharpe_ratio']:<8.2f} "
              f"{r['profit_factor']:<8.2f}")
    print("-" * 70)
    m = baseline["metrics"]
    print(f"{'TOTAL':<10} "
          f"{m['total_trades']:<8} "
          f"{m['win_rate']:<8.1f} "
          f"{m['total_pnl_pct']:<10.2f} "
          f"{m['total_pnl_usd']:<10.2f} "
          f"{m['max_drawdown_pct']:<8.2f} "
          f"{m['sharpe_ratio']:<8.2f} "
          f"{m['profit_factor']:<8.2f}")

    print(f"\n[OK] Baseline saved to baseline_p1.json")


if __name__ == "__main__":
    main()
