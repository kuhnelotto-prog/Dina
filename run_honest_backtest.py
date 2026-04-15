#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_honest_backtest.py — Первый честный бэктест Dina на реальных данных Bitget.

Особенности:
- 10 монет, 180 дней, 4H таймфрейм
- Единый портфель (shared balance, max 3 позиции одновременно)
- BTC 1D EMA50 master filter
- BTC ATR regime detection (CRISIS/VOLATILE/NORMAL)
- Все 7 багфиков применены

Запуск:
  python run_honest_backtest.py          # полный прогон (10 монет, 180 дней)
  python run_honest_backtest.py --test   # тест на 1 монете (BTCUSDT, 90 дней)
"""

import sys
import os
import time
import logging
import json
import traceback
from datetime import datetime, timedelta, timezone

# Fix Windows console encoding for emoji/unicode
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtester import Backtester, SYMBOLS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── 10 монет для бэктеста ──
BACKTEST_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "AVAXUSDT",
    "DOGEUSDT", "ADAUSDT", "LINKUSDT", "SOLUSDT", "SUIUSDT"
]

# ── Bitget API ──
BITGET_URL = "https://api.bitget.com/api/v2/mix/market/candles"


def fetch_candles(symbol: str, days: int = 180, granularity: str = "4H") -> pd.DataFrame:
    """
    Fetch real candles from Bitget API.
    Bitget limits startTime-endTime range to 90 days for 4H candles.
    Strategy: split request into 90-day chunks with startTime+endTime.
    Returns DataFrame with timestamp index and OHLCV columns.
    """
    all_candles = []
    now = datetime.now(timezone.utc)
    overall_end = int((now - timedelta(minutes=5)).timestamp() * 1000)
    overall_start = int((now - timedelta(days=days)).timestamp() * 1000)
    chunk_days = 89  # stay under 90-day limit per request

    logger.info(f"Fetching {symbol} {granularity} data: {days} days")

    # Split into 89-day chunks
    chunk_starts = []
    chunk_end = overall_end
    while chunk_end > overall_start:
        chunk_start = max(overall_start, chunk_end - chunk_days * 24 * 3600 * 1000)
        chunk_starts.append((chunk_start, chunk_end))
        chunk_end = chunk_start - 1  # 1ms gap to avoid overlap

    for i, (chunk_start, chunk_end) in enumerate(chunk_starts):
        params = {
            "symbol": symbol,
            "granularity": granularity,
            "limit": 1000,
            "startTime": chunk_start,
            "endTime": chunk_end,
            "productType": "USDT-FUTURES",
        }
        try:
            resp = requests.get(BITGET_URL, params=params, timeout=30)
            data = resp.json()
            if data.get("code") != "00000" or not data.get("data"):
                logger.warning(f"API error for {symbol} chunk {i+1}: {data.get('msg', 'unknown')}")
                continue
            candles = data["data"]
            for c in candles:
                all_candles.append([
                    int(c[0]), float(c[1]), float(c[2]),
                    float(c[3]), float(c[4]), float(c[5])
                ])
            logger.info(f"  {symbol}: chunk {i+1}/{len(chunk_starts)}, got {len(candles)} candles (total: {len(all_candles)})")
            time.sleep(0.15)  # rate limiting
        except Exception as e:
            logger.error(f"Error fetching {symbol} chunk {i+1}: {e}")
            continue

    if not all_candles:
        logger.error(f"No data for {symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)

    expected = days * 6  # 6 four-hour candles per day
    logger.info(f"  {symbol}: {len(df)} candles fetched (expected ~{expected})")
    return df


def fetch_btc_1d(days: int = 200, limit: int = 200) -> pd.DataFrame:
    """
    Fetch BTC 1D candles for EMA50 master filter.
    Uses limit-based fetch (no startTime/endTime for 1D).
    """
    params = {
        "symbol": "BTCUSDT",
        "granularity": "1D",
        "limit": str(limit),
        "productType": "USDT-FUTURES",
    }
    try:
        resp = requests.get(BITGET_URL, params=params, timeout=15)
        data = resp.json()
        if data.get("code") != "00000" or not data.get("data"):
            logger.warning(f"BTC 1D API error: {data.get('msg')}")
            return pd.DataFrame()
        candles = data["data"]
        rows = []
        for c in candles:
            rows.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        df.set_index("timestamp", inplace=True)
        logger.info(f"BTC 1D: {len(df)} candles fetched")
        return df
    except Exception as e:
        logger.error(f"Error fetching BTC 1D: {e}")
        return pd.DataFrame()


def print_metrics(result, symbols, days):
    """Print comprehensive backtest metrics."""
    if result.total_trades == 0:
        print("\n⚠️  Нет сделок — бэктест не сгенерировал ни одного входа.")
        print("   Возможные причины: ADX фильтр слишком строгий, пороги слишком высокие, или данных недостаточно.")
        return

    total_return_pct = (result.final_balance - result.initial_balance) / result.initial_balance * 100
    win_rate = result.winning_trades / result.total_trades * 100

    # Profit Factor
    sum_wins = sum(t.pnl_usd for t in result.trades if t.pnl_usd > 0)
    sum_losses = abs(sum(t.pnl_usd for t in result.trades if t.pnl_usd < 0))
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else float('inf')

    # Average trade
    avg_win = sum_wins / result.winning_trades if result.winning_trades > 0 else 0
    avg_loss = sum_losses / result.losing_trades if result.losing_trades > 0 else 0

    # Exit reason breakdown
    from collections import Counter
    reasons = Counter()
    reason_pnl = {}
    for t in result.trades:
        reason = getattr(t, 'exit_reason', 'UNKNOWN')
        reasons[reason] += 1
        reason_pnl.setdefault(reason, 0.0)
        reason_pnl[reason] += t.pnl_usd

    # Long/Short breakdown
    long_trades = [t for t in result.trades if t.side == "long"]
    short_trades = [t for t in result.trades if t.side == "short"]
    long_wins = sum(1 for t in long_trades if t.pnl_usd > 0)
    short_wins = sum(1 for t in short_trades if t.pnl_usd > 0)

    # Trailing step distribution
    step_counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for t in result.trades:
        step = getattr(t, 'trailing_step', 0)
        step_counts[step] = step_counts.get(step, 0) + 1

    # Per-symbol breakdown
    sym_stats = {}
    for t in result.trades:
        sym = t.symbol
        if sym not in sym_stats:
            sym_stats[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
        sym_stats[sym]["trades"] += 1
        if t.pnl_usd > 0:
            sym_stats[sym]["wins"] += 1
        sym_stats[sym]["pnl"] += t.pnl_usd

    print("\n" + "=" * 80)
    print("📊  ЧЕСТНЫЙ БЭКТЕСТ DINA — РЕЗУЛЬТАТЫ")
    print("=" * 80)
    print(f"Символы:       {', '.join(symbols)}")
    print(f"Период:        {days} дней, таймфрейм 4H")
    print(f"Начальный бал: ${result.initial_balance:,.2f}")
    print(f"─────────────────────────────────────────────────────────────")
    print(f"Финальный бал: ${result.final_balance:,.2f}")
    print(f"PnL:           ${result.total_pnl_usd:+,.2f} ({total_return_pct:+.2f}%)")
    print(f"Max Drawdown:  ${result.max_drawdown_usd:,.2f} ({result.max_drawdown_pct:.2f}%)")
    print(f"─────────────────────────────────────────────────────────────")
    print(f"Всего сделок:  {result.total_trades}")
    print(f"Win Rate:      {win_rate:.1f}% ({result.winning_trades}W / {result.losing_trades}L)")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Avg Win:       ${avg_win:+,.2f}")
    print(f"Avg Loss:      ${avg_loss:+,.2f}")
    if avg_loss != 0:
        print(f"Avg Win/Loss:  {avg_win/avg_loss:.2f}:1")
    print(f"─────────────────────────────────────────────────────────────")

    # Long/Short
    print(f"LONG:  {len(long_trades)} trades, {long_wins} wins "
          f"({long_wins/len(long_trades)*100:.1f}% WR)" if long_trades else "LONG: 0 trades")
    print(f"SHORT: {len(short_trades)} trades, {short_wins} wins "
          f"({short_wins/len(short_trades)*100:.1f}% WR)" if short_trades else "SHORT: 0 trades")

    # Trailing steps
    print(f"\n{'─'*60}")
    print("TRAILING STEP DISTRIBUTION:")
    total = sum(step_counts.values())
    for step, count in sorted(step_counts.items()):
        if count > 0:
            pct = count / total * 100 if total > 0 else 0
            labels = {0: "SL (no trailing)", 1: "Breakeven", 2: "Partial 25%",
                      3: "Partial 50%", 4: "Full TP (+2ATR)"}
            print(f"  Step {step} ({labels.get(step, '?'):20s}): {count:3d} ({pct:5.1f}%)")

    # Exit reasons
    print(f"\n{'─'*60}")
    print("EXIT REASONS:")
    for reason, count in reasons.most_common():
        pnl = reason_pnl.get(reason, 0.0)
        pct = count / result.total_trades * 100
        print(f"  {reason:20s}: {count:3d} ({pct:5.1f}%) | PnL: ${pnl:+,.2f}")

    # Per-symbol breakdown
    print(f"\n{'─'*60}")
    print("PER-SYMBOL BREAKDOWN:")
    print(f"  {'Symbol':<10} {'Trades':<8} {'Wins':<6} {'WR%':<7} {'PnL$':<12}")
    print(f"  {'─'*43}")
    for sym in sorted(sym_stats.keys()):
        s = sym_stats[sym]
        wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
        print(f"  {sym:<10} {s['trades']:<8} {s['wins']:<6} {wr:<7.1f} {s['pnl']:+,.2f}")

    print("=" * 80)

    # Save detailed results
    output = {
        "config": {
            "symbols": symbols,
            "days": days,
            "timeframe": "4H",
            "initial_balance": result.initial_balance,
            "date_range": f"{result.trades[0].entry_time} → {result.trades[-1].exit_time}" if result.trades else "N/A",
        },
        "summary": {
            "final_balance": result.final_balance,
            "total_pnl_usd": result.total_pnl_usd,
            "total_pnl_pct": total_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "max_drawdown_usd": result.max_drawdown_usd,
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        },
        "trades": [
            {
                "symbol": t.symbol,
                "side": t.side,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time) if t.exit_time else None,
                "pnl_usd": t.pnl_usd,
                "pnl_pct": t.pnl_pct,
                "exit_reason": getattr(t, 'exit_reason', '?'),
                "trailing_step": getattr(t, 'trailing_step', 0),
                "remaining_pct": getattr(t, 'remaining_pct', 1.0),
            }
            for t in result.trades
        ],
    }
    with open("backtest_honest_results.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n✅ Детальные результаты сохранены в backtest_honest_results.json")


def run_single_test():
    """Быстрый тест на 1 монете (BTCUSDT, 90 дней)."""
    print("=" * 80)
    print("🧪 ТЕСТ: BTCUSDT, 90 дней, 4H")
    print("=" * 80)

    days = 90
    symbol = "BTCUSDT"

    # Fetch data
    print(f"\n📥 Загрузка данных {symbol}...")
    df = fetch_candles(symbol, days=days, granularity="4H")
    if df.empty:
        print(f"❌ Не удалось загрузить данные для {symbol}")
        return

    print(f"  Получено {len(df)} свечей для {symbol}")
    print(f"  Период: {df.index[0]} → {df.index[-1]}")

    # Fetch BTC 1D for EMA50 master filter
    print(f"\n📥 Загрузка BTC 1D данных для EMA50 master filter...")
    btc_1d_df = fetch_btc_1d(days=200)
    if not btc_1d_df.empty and len(btc_1d_df) >= 50:
        last_close = btc_1d_df['close'].iloc[-1]
        ema50 = btc_1d_df['close'].ewm(span=50, adjust=False).mean()
        last_ema = ema50.iloc[-1]
        regime = "BULL 🐂" if last_close > last_ema else "BEAR 🐻"
        print(f"  BTC 1D: close={last_close:.0f}, EMA50={last_ema:.0f} → {regime}")
    else:
        print(f"  ⚠️ BTC 1D данных недостаточно ({len(btc_1d_df)} свечей)")

    # Run backtest
    print(f"\n🔄 Запуск бэктеста...")
    bt = Backtester(initial_balance=10000.0)
    result = bt.run(
        dfs={symbol: df},
        symbols=[symbol],
        btc_df=df,  # BTC 4H for regime detection
        btc_1d_df=btc_1d_df if not btc_1d_df.empty else None,
    )

    print_metrics(result, [symbol], days)


def run_full_backtest():
    """Полный бэктест: 10 монет, 180 дней."""
    print("=" * 80)
    print("📊 ПОЛНЫЙ БЭКТЕСТ DINA: 10 монет, 180 дней, 4H")
    print("=" * 80)

    days = 180

    # Fetch all symbol data
    dfs = {}
    for symbol in BACKTEST_SYMBOLS:
        print(f"\n📥 Загрузка данных {symbol}...")
        df = fetch_candles(symbol, days=days, granularity="4H")
        if df.empty or len(df) < 100:
            print(f"  ⚠️ {symbol}: недостаточно данных ({len(df)} свечей), пропускаем")
            continue
        dfs[symbol] = df
        print(f"  ✅ {symbol}: {len(df)} свечей ({df.index[0].date()} → {df.index[-1].date()})")
        time.sleep(0.5)

    if not dfs:
        print("❌ Не удалось загрузить данные ни для одной монеты")
        return

    # Fetch BTC 1D
    print(f"\n📥 Загрузка BTC 1D данных...")
    btc_1d_df = fetch_btc_1d(days=200)
    if not btc_1d_df.empty:
        print(f"  ✅ BTC 1D: {len(btc_1d_df)} свечей")

    active_symbols = list(dfs.keys())
    print(f"\n📊 Активные символы: {', '.join(active_symbols)}")

    # Use BTC 4H data for regime detection
    btc_df = dfs.get("BTCUSDT")

    # Run multi-symbol portfolio backtest
    print(f"\n🔄 Запуск мульти-символьного бэктеста (единый портфель)...")
    bt = Backtester(initial_balance=10000.0)
    result = bt.run(
        dfs=dfs,
        symbols=active_symbols,
        btc_df=btc_df,
        btc_1d_df=btc_1d_df if not btc_1d_df.empty else None,
    )

    print_metrics(result, active_symbols, days)


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_single_test()
    else:
        run_full_backtest()