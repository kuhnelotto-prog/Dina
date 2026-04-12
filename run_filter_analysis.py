#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_filter_analysis.py — Диагностика фильтров Дины.

1. FilterFunnel: сколько сигналов срезает каждый фильтр
2. RejectedSignalAnalyzer: hypothetical PnL отсеянных сигналов
3. Ablation: 7 бэктестов (base + без каждого из 6 фильтров)
4. Результат: filter_analysis.json

Период: 2026-01-12 → 2026-04-12, 5 монет, 4H.
"""

import sys, os, time, logging, json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from copy import deepcopy
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indicators_calc import IndicatorsCalculator
from backtester import Backtester, BacktestPosition, BacktestResult, ADXFilter, ADX_BLACKLIST

logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT"]
START_DATE = datetime(2026, 1, 12)
END_DATE = datetime(2026, 4, 12)

FILTER_NAMES = [
    "adx_threshold",      # ADX < 18
    "adx_growth",         # ADX falling (growth < 0.5)
    "composite_threshold", # composite < dynamic threshold
    "ema_trend",          # EMA trend direction
    "rsi_filter",         # RSI < 70 (long) / RSI > 30 (short)
    "btc_1d_ema50",       # BTC 1D EMA50 master filter
]

# ============================================================
# Data fetching (reused from run_full_backtest.py)
# ============================================================

def fetch_candles(symbol, start_dt, end_dt, granularity="4H"):
    all_candles = []
    end_time = int(end_dt.timestamp() * 1000)
    start_time = int(start_dt.timestamp() * 1000)
    current_end = end_time
    for _ in range(10):
        params = {"symbol": symbol, "granularity": granularity, "limit": 1000,
                  "endTime": current_end, "startTime": start_time, "productType": "USDT-FUTURES"}
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

def fetch_candles_1d(symbol, limit=200):
    params = {"symbol": symbol, "granularity": "1D", "limit": limit, "productType": "USDT-FUTURES"}
    resp = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=30)
    data = resp.json()
    if data.get("code") != "00000" or not data.get("data"):
        return pd.DataFrame()
    candles = data["data"]
    rows = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in candles]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

# ============================================================
# FilterFunnel + RejectedSignalAnalyzer
# ============================================================

class FilterFunnel:
    """Tracks how many signals each filter rejects."""
    def __init__(self):
        self.total_candles = 0
        self.signals_generated = 0  # candles where position could be checked
        self.filter_rejections = {f: 0 for f in FILTER_NAMES}
        self.passed_all = 0

    def to_dict(self):
        return {
            "total_candles": self.total_candles,
            "signals_evaluated": self.signals_generated,
            "filter_rejections": dict(self.filter_rejections),
            "passed_all_filters": self.passed_all,
            "rejection_rates": {
                f: round(v / max(self.signals_generated, 1) * 100, 1)
                for f, v in self.filter_rejections.items()
            }
        }


class RejectedSignal:
    """A signal that was rejected by a filter, with hypothetical PnL."""
    def __init__(self, timestamp, symbol, side, entry_price, sl_price, tp_price,
                 rejected_by, composite, indicators):
        self.timestamp = timestamp
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.tp_price = tp_price
        self.rejected_by = rejected_by
        self.composite = composite
        self.hypo_pnl_pct = None
        self.hypo_hit = None  # "sl", "tp", "timeout"


class RejectedSignalAnalyzer:
    """Computes hypothetical PnL for rejected signals (no look-ahead bias)."""
    def __init__(self):
        self.rejected: list[RejectedSignal] = []

    def add(self, sig: RejectedSignal):
        self.rejected.append(sig)

    def compute_hypothetical(self, df: pd.DataFrame):
        """Walk forward from rejection point to compute hypo PnL."""
        for sig in self.rejected:
            idx = df.index.get_indexer([sig.timestamp], method='nearest')[0]
            if idx < 0:
                continue
            # Walk forward max 120 candles (20 days at 4H)
            hit = "timeout"
            exit_price = df.iloc[min(idx + 120, len(df) - 1)]['close']
            for j in range(idx + 1, min(idx + 121, len(df))):
                row = df.iloc[j]
                if sig.side == "long":
                    if row['low'] <= sig.sl_price:
                        exit_price = sig.sl_price
                        hit = "sl"
                        break
                    if row['high'] >= sig.tp_price:
                        exit_price = sig.tp_price
                        hit = "tp"
                        break
                else:
                    if row['high'] >= sig.sl_price:
                        exit_price = sig.sl_price
                        hit = "sl"
                        break
                    if row['low'] <= sig.tp_price:
                        exit_price = sig.tp_price
                        hit = "tp"
                        break

            if sig.side == "long":
                sig.hypo_pnl_pct = (exit_price - sig.entry_price) / sig.entry_price * 100
            else:
                sig.hypo_pnl_pct = (sig.entry_price - exit_price) / sig.entry_price * 100
            sig.hypo_hit = hit

    def summary_by_filter(self):
        result = {}
        for fname in FILTER_NAMES:
            sigs = [s for s in self.rejected if s.rejected_by == fname]
            if not sigs:
                result[fname] = {"count": 0, "hypo_wr": 0, "hypo_pnl_pct": 0}
                continue
            wins = sum(1 for s in sigs if s.hypo_pnl_pct and s.hypo_pnl_pct > 0)
            avg_pnl = np.mean([s.hypo_pnl_pct for s in sigs if s.hypo_pnl_pct is not None])
            result[fname] = {
                "count": len(sigs),
                "hypo_wr": round(wins / len(sigs) * 100, 1) if sigs else 0,
                "hypo_avg_pnl_pct": round(float(avg_pnl), 3) if not np.isnan(avg_pnl) else 0,
            }
        return result


# ============================================================
# Backtest with filter funnel (instrumented)
# ============================================================

def run_instrumented_backtest(df, symbol, btc_df, btc_1d_df, disabled_filters=None, verbose=True):
    """
    Run backtest with filter funnel instrumentation.
    disabled_filters: set of filter names to skip (for ablation).
    Returns (BacktestResult, FilterFunnel, RejectedSignalAnalyzer).
    """
    disabled = disabled_filters or set()
    result = BacktestResult(10000.0)
    funnel = FilterFunnel()
    analyzer = RejectedSignalAnalyzer()
    open_positions = {}
    calc = IndicatorsCalculator()
    adx_filter = ADXFilter(threshold=18.0, min_growth=0.5)

    weights = {
        "ema_cross": 1.0, "volume_spike": 1.0, "engulfing": 0.8,
        "fvg": 0.6, "macd_cross": 0.5, "rsi_filter": 0.4,
        "bb_squeeze": 0.3, "sweep": 0.7,
    }

    THRESHOLD_LONG_BULL = 0.30
    THRESHOLD_LONG_BEAR = 0.45
    THRESHOLD_SHORT_BULL = 0.45
    THRESHOLD_SHORT_BEAR = 0.30

    # BTC EMA50 4H
    btc_ema50 = None
    if symbol == "BTCUSDT":
        btc_ema50 = df['close'].ewm(span=50, adjust=False).mean()
    elif btc_df is not None and len(btc_df) >= 50:
        btc_ema50 = btc_df['close'].ewm(span=50, adjust=False).mean()

    # BTC 1D EMA50
    btc_1d_ema50 = None
    if btc_1d_df is not None and len(btc_1d_df) >= 50:
        btc_1d_ema50 = btc_1d_df['close'].ewm(span=50, adjust=False).mean()

    # Pending signal for next-candle-open entry (no look-ahead bias)
    pending_signal = None

    for i, (timestamp, row) in enumerate(df.iterrows()):
        current_price = row['close']
        candle_open = row['open']
        candle_high = row['high']
        candle_low = row['low']

        # Execute pending signal at this candle's open
        if pending_signal is not None and len(open_positions) == 0:
            sig = pending_signal
            pending_signal = None
            entry_price = candle_open
            if sig["side"] == "long":
                sl_price = entry_price * (1 - sig["sl_pct"])
                tp_price = entry_price * (1 + sig["tp_pct"])
            else:
                sl_price = entry_price * (1 + sig["sl_pct"])
                tp_price = entry_price * (1 - sig["tp_pct"])
            funnel.passed_all += 1
            pos = BacktestPosition(symbol, sig["side"], entry_price, result.final_balance * 0.1,
                                   sl_price, tp_price, timestamp)
            open_positions[symbol] = pos
        else:
            pending_signal = None

        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            closed, _ = pos.update(current_price, high=candle_high, low=candle_low)
            if closed:
                del open_positions[sym]
                result.add_trade(pos)

        if len(open_positions) > 0 or i < 50:
            funnel.total_candles += 1
            continue

        funnel.total_candles += 1
        funnel.signals_generated += 1

        slice_df = df.iloc[:i+1].copy()
        indicators = calc.compute(slice_df)
        if "error" in indicators:
            continue

        adx_val = indicators.get("adx", 0.0)
        adx_prev = indicators.get("adx_prev", 0.0)
        composite = Backtester._compute_composite(indicators, weights)
        is_bullish = indicators["ema_fast"] > indicators["ema_slow"]
        rsi = indicators.get("rsi", 50)

        # BTC regime
        if btc_ema50 is not None:
            if symbol == "BTCUSDT":
                btc_regime = "BULL" if current_price > btc_ema50.iloc[i] else "BEAR"
            else:
                try:
                    idx = btc_ema50.index.get_indexer([timestamp], method='nearest')[0]
                    btc_price = btc_df['close'].iloc[idx]
                    btc_regime = "BULL" if btc_price > btc_ema50.iloc[idx] else "BEAR"
                except Exception:
                    btc_regime = "BULL"
        else:
            btc_regime = "BULL"

        threshold_long = THRESHOLD_LONG_BULL if btc_regime == "BULL" else THRESHOLD_LONG_BEAR
        threshold_short = THRESHOLD_SHORT_BEAR if btc_regime == "BEAR" else THRESHOLD_SHORT_BULL

        # BTC 1D EMA50
        btc_1d_allows_long = True
        btc_1d_allows_short = True
        if btc_1d_ema50 is not None:
            try:
                idx_1d = btc_1d_ema50.index.get_indexer([timestamp], method='pad')[0]
                if idx_1d >= 0:
                    btc_1d_close = btc_1d_df['close'].iloc[idx_1d]
                    btc_1d_ema_val = btc_1d_ema50.iloc[idx_1d]
                    btc_1d_allows_long = btc_1d_close > btc_1d_ema_val
                    btc_1d_allows_short = btc_1d_close < btc_1d_ema_val
            except Exception:
                pass

        # Helper to compute SL/TP
        atr_pct = indicators.get("atr_pct", 0)
        if atr_pct > 0.1:
            sl_pct = 1.5 * atr_pct / 100
            tp_pct = 3.0 * atr_pct / 100
        else:
            sl_pct = 0.03
            tp_pct = 0.05

        def make_rejected(side, rejected_by):
            if side == "long":
                sl_p = current_price * (1 - sl_pct)
                tp_p = current_price * (1 + tp_pct)
            else:
                sl_p = current_price * (1 + sl_pct)
                tp_p = current_price * (1 - tp_pct)
            sig = RejectedSignal(timestamp, symbol, side, current_price, sl_p, tp_p,
                                 rejected_by, composite, indicators)
            analyzer.add(sig)
            funnel.filter_rejections[rejected_by] += 1

        # ── Try LONG ──
        long_rejected = False
        if "adx_threshold" not in disabled and adx_val < 18.0:
            make_rejected("long", "adx_threshold")
            long_rejected = True
        elif "adx_growth" not in disabled and (adx_val - adx_prev) < 0.5:
            make_rejected("long", "adx_growth")
            long_rejected = True
        elif "composite_threshold" not in disabled and composite <= threshold_long:
            make_rejected("long", "composite_threshold")
            long_rejected = True
        elif "ema_trend" not in disabled and not is_bullish:
            make_rejected("long", "ema_trend")
            long_rejected = True
        elif "rsi_filter" not in disabled and rsi >= 70:
            make_rejected("long", "rsi_filter")
            long_rejected = True
        elif "btc_1d_ema50" not in disabled and not btc_1d_allows_long:
            make_rejected("long", "btc_1d_ema50")
            long_rejected = True

        if not long_rejected:
            adx_ok = adx_val >= 18.0 and (adx_val - adx_prev) >= 0.5
            if ("adx_threshold" in disabled or "adx_growth" in disabled):
                adx_ok = True
            passes_long = (
                (composite > threshold_long or "composite_threshold" in disabled) and
                (is_bullish or "ema_trend" in disabled) and
                (rsi < 70 or "rsi_filter" in disabled) and
                (btc_1d_allows_long or "btc_1d_ema50" in disabled) and
                adx_ok
            )
            if passes_long:
                pending_signal = {"side": "long", "sl_pct": sl_pct, "tp_pct": tp_pct}
                continue

        # ── Try SHORT ──
        short_rejected = False
        if "adx_threshold" not in disabled and adx_val < 18.0:
            if not long_rejected:
                make_rejected("short", "adx_threshold")
            short_rejected = True
        elif "adx_growth" not in disabled and (adx_val - adx_prev) < 0.5:
            if not long_rejected:
                make_rejected("short", "adx_growth")
            short_rejected = True
        elif "composite_threshold" not in disabled and composite >= -threshold_short:
            make_rejected("short", "composite_threshold")
            short_rejected = True
        elif "ema_trend" not in disabled and is_bullish:
            make_rejected("short", "ema_trend")
            short_rejected = True
        elif "rsi_filter" not in disabled and rsi <= 30:
            make_rejected("short", "rsi_filter")
            short_rejected = True
        elif "btc_1d_ema50" not in disabled and not btc_1d_allows_short:
            make_rejected("short", "btc_1d_ema50")
            short_rejected = True

        if not short_rejected:
            adx_ok = adx_val >= 18.0 and (adx_val - adx_prev) >= 0.5
            if ("adx_threshold" in disabled or "adx_growth" in disabled):
                adx_ok = True
            passes_short = (
                (composite < -threshold_short or "composite_threshold" in disabled) and
                (not is_bullish or "ema_trend" in disabled) and
                (rsi > 30 or "rsi_filter" in disabled) and
                (btc_1d_allows_short or "btc_1d_ema50" in disabled) and
                adx_ok
            )
            if passes_short:
                pending_signal = {"side": "short", "sl_pct": sl_pct, "tp_pct": tp_pct}

    # Close remaining
    for sym, pos in list(open_positions.items()):
        pos._close(df.iloc[-1]['close'], "END_OF_BACKTEST")
        result.add_trade(pos)

    # Compute hypothetical PnL for rejected signals
    analyzer.compute_hypothetical(df)

    return result, funnel, analyzer


def extract_metrics(result: BacktestResult):
    """Extract key metrics from BacktestResult."""
    trades = result.total_trades
    wr = (result.winning_trades / trades * 100) if trades > 0 else 0
    pnl_pct = (result.final_balance - result.initial_balance) / result.initial_balance * 100
    pnl_usd = result.total_pnl_usd
    return {
        "trades": trades,
        "win_rate": round(wr, 1),
        "pnl_pct": round(pnl_pct, 3),
        "pnl_usd": round(pnl_usd, 2),
        "max_dd_pct": round(result.max_drawdown_pct, 2),
    }


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 80)
    print("FILTER ANALYSIS")
    print(f"Period: {START_DATE.date()} -> {END_DATE.date()}")
    print(f"Symbols: {', '.join(SYMBOLS)}")
    print("=" * 80)

    # Load data
    print("\nLoading data...")
    btc_df = fetch_candles("BTCUSDT", START_DATE, END_DATE)
    btc_1d_df = fetch_candles_1d("BTCUSDT", limit=200)
    print(f"  BTC 4H: {len(btc_df)} candles, BTC 1D: {len(btc_1d_df)} candles")

    symbol_data = {}
    for sym in SYMBOLS:
        if sym == "BTCUSDT":
            symbol_data[sym] = btc_df
        else:
            symbol_data[sym] = fetch_candles(sym, START_DATE, END_DATE)
            time.sleep(0.5)
        print(f"  {sym}: {len(symbol_data[sym])} candles")

    # -- 1. Base backtest with funnel --
    print("\n" + "=" * 80)
    print("1. BASE BACKTEST (all filters ON)")
    print("=" * 80)

    base_results = {}
    base_funnels = {}
    base_rejected = {}

    for sym in SYMBOLS:
        df = symbol_data[sym]
        if df.empty or len(df) < 100:
            print(f"  WARNING {sym}: not enough data")
            continue
        result, funnel, analyzer = run_instrumented_backtest(df, sym, btc_df, btc_1d_df)
        base_results[sym] = result
        base_funnels[sym] = funnel
        base_rejected[sym] = analyzer
        m = extract_metrics(result)
        print(f"  {sym}: {m['trades']} trades, WR={m['win_rate']}%, PnL={m['pnl_pct']}%")

    # -- 2. Ablation: disable each filter one at a time --
    print("\n" + "=" * 80)
    print("2. ABLATION (disable one filter at a time)")
    print("=" * 80)

    ablation = {}
    for fname in FILTER_NAMES:
        print(f"\n  --- without {fname} ---")
        ablation[fname] = {}
        for sym in SYMBOLS:
            df = symbol_data[sym]
            if df.empty or len(df) < 100:
                continue
            result, funnel, analyzer = run_instrumented_backtest(
                df, sym, btc_df, btc_1d_df, disabled_filters={fname}
            )
            m = extract_metrics(result)
            ablation[fname][sym] = m
            print(f"    {sym}: {m['trades']} trades, WR={m['win_rate']}%, PnL={m['pnl_pct']}%")

    # -- 3. Build filter_analysis.json --
    print("\n" + "=" * 80)
    print("3. Building filter_analysis.json")
    print("=" * 80)

    analysis = {
        "period": f"{START_DATE.date()} -> {END_DATE.date()}",
        "symbols": SYMBOLS,
        "verbose_filter_logging": True,
        "funnel_by_symbol": {},
        "base_metrics": {},
        "ablation": {},
        "rejected_signals": {},
        "conclusion": {},
    }

    # Funnel by symbol
    for sym in SYMBOLS:
        if sym in base_funnels:
            analysis["funnel_by_symbol"][sym] = base_funnels[sym].to_dict()

    # Base metrics
    for sym in SYMBOLS:
        if sym in base_results:
            analysis["base_metrics"][sym] = extract_metrics(base_results[sym])

    # Ablation comparison
    for fname in FILTER_NAMES:
        analysis["ablation"][fname] = {}
        for sym in SYMBOLS:
            base_m = analysis["base_metrics"].get(sym, {})
            abl_m = ablation.get(fname, {}).get(sym, {})
            if base_m and abl_m:
                analysis["ablation"][fname][sym] = {
                    "with_filter": base_m,
                    "without_filter": abl_m,
                    "delta_trades": abl_m.get("trades", 0) - base_m.get("trades", 0),
                    "delta_wr": round(abl_m.get("win_rate", 0) - base_m.get("win_rate", 0), 1),
                    "delta_pnl_pct": round(abl_m.get("pnl_pct", 0) - base_m.get("pnl_pct", 0), 3),
                }

    # Rejected signals summary
    all_rejected_summary = {}
    for sym in SYMBOLS:
        if sym in base_rejected:
            all_rejected_summary[sym] = base_rejected[sym].summary_by_filter()
    analysis["rejected_signals"] = all_rejected_summary

    # ── Conclusion ──
    # Determine if filters are working, overtight, or need adjustment
    total_base_trades = sum(m.get("trades", 0) for m in analysis["base_metrics"].values())
    total_base_pnl = sum(m.get("pnl_pct", 0) for m in analysis["base_metrics"].values())

    recommendations = []
    filters_status = {}

    for fname in FILTER_NAMES:
        total_delta_trades = 0
        total_delta_pnl = 0
        for sym in SYMBOLS:
            abl = analysis["ablation"].get(fname, {}).get(sym, {})
            total_delta_trades += abl.get("delta_trades", 0)
            total_delta_pnl += abl.get("delta_pnl_pct", 0)

        # Check rejected signals hypothetical performance
        total_rejected = 0
        total_hypo_wr = 0
        for sym in SYMBOLS:
            rs = all_rejected_summary.get(sym, {}).get(fname, {})
            total_rejected += rs.get("count", 0)
            total_hypo_wr += rs.get("hypo_wr", 0)
        avg_hypo_wr = total_hypo_wr / len(SYMBOLS) if SYMBOLS else 0

        if total_delta_trades > 5 and total_delta_pnl > 0.5:
            status = "overtight"
            recommendations.append(
                f"{fname}: OVERTIGHT — removing adds {total_delta_trades} trades "
                f"with +{total_delta_pnl:.2f}% PnL. Consider relaxing."
            )
        elif total_delta_pnl < -0.5:
            status = "working"
            recommendations.append(
                f"{fname}: WORKING — removing hurts PnL by {total_delta_pnl:.2f}%. Keep it."
            )
        elif total_rejected > 0 and avg_hypo_wr > 50:
            status = "overtight"
            recommendations.append(
                f"{fname}: OVERTIGHT — rejects {total_rejected} signals with "
                f"hypo WR={avg_hypo_wr:.0f}%. Consider relaxing."
            )
        else:
            status = "neutral"
            recommendations.append(
                f"{fname}: NEUTRAL — minimal impact ({total_delta_trades} trades, "
                f"{total_delta_pnl:+.2f}% PnL)."
            )
        filters_status[fname] = status

    analysis["conclusion"] = {
        "total_base_trades": total_base_trades,
        "total_base_pnl_pct": round(total_base_pnl, 3),
        "filters_status": filters_status,
        "recommendations": recommendations,
    }

    # Save
    with open("filter_analysis.json", "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False, default=str)

    print(f"\nSaved to filter_analysis.json")

    # Print summary
    print("\n" + "=" * 80)
    print("CONCLUSIONS")
    print("=" * 80)
    for rec in recommendations:
        icon = "[OK]" if "WORKING" in rec else "[!!]" if "OVERTIGHT" in rec else "[--]"
        print(f"  {icon} {rec}")

    print(f"\n  Total trades (base): {total_base_trades}")
    print(f"  Total PnL (base): {total_base_pnl:.3f}%")


if __name__ == "__main__":
    main()
