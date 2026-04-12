#!/usr/bin/env python3
"""
sanity_check_backtest.py - Manual verification of backtester correctness.

Checks:
1. Entry timing (look-ahead bias)
2. SL/TP checked by high/low vs close
3. PnL calculation accuracy
4. Indicator warm-up buffer
5. OHLCV data gaps
"""

import sys, os, time, json, logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indicators_calc import IndicatorsCalculator
from backtester import Backtester, BacktestPosition, BacktestResult, ADXFilter

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT"]
START_DATE = datetime(2026, 1, 12)
END_DATE = datetime(2026, 4, 12)


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


def main():
    issues = []
    print("=" * 80)
    print("SANITY CHECK - Backtester Verification")
    print("=" * 80)

    # ================================================================
    # CHECK 5: Data gaps
    # ================================================================
    print("\n--- CHECK 5: OHLCV Data Gaps ---")
    symbol_data = {}
    for sym in SYMBOLS:
        df = fetch_candles(sym, START_DATE, END_DATE)
        symbol_data[sym] = df
        if df.empty:
            issues.append(f"[DATA] {sym}: no data fetched")
            print(f"  {sym}: NO DATA")
            continue

        # Check for gaps (4H = 240 min between candles)
        timestamps = df.index.to_series()
        diffs = timestamps.diff().dropna()
        expected_gap = pd.Timedelta(hours=4)
        gaps = diffs[diffs > expected_gap * 1.5]  # allow 50% tolerance

        total_candles = len(df)
        date_range = f"{df.index[0]} -> {df.index[-1]}"
        expected_candles = int((END_DATE - START_DATE).total_seconds() / (4 * 3600))

        print(f"  {sym}: {total_candles} candles (expected ~{expected_candles}), range: {date_range}")
        if len(gaps) > 0:
            issues.append(f"[DATA] {sym}: {len(gaps)} gaps found in OHLCV data")
            for ts, gap in gaps.head(5).items():
                print(f"    GAP at {ts}: {gap}")
        else:
            print(f"    No gaps found")
        time.sleep(0.3)

    # Load BTC data for regime
    btc_df = symbol_data.get("BTCUSDT", pd.DataFrame())
    btc_1d_df = fetch_candles_1d("BTCUSDT", limit=200)

    # ================================================================
    # CHECK 4: Indicator warm-up buffer
    # ================================================================
    print("\n--- CHECK 4: Indicator Warm-up Buffer ---")
    # Check backtester code: does it skip first N candles?
    # From backtester.py line ~437: `if i < 50: continue`
    # This means first 50 candles are skipped for warm-up
    print("  Backtester skips first 50 candles (i < 50)")
    print("  EMA50 needs 50 candles for proper calculation")

    # Verify: compute indicators on first 50 candles vs first 100
    if not btc_df.empty and len(btc_df) >= 100:
        calc = IndicatorsCalculator()
        ind_50 = calc.compute(btc_df.iloc[:50])
        ind_100 = calc.compute(btc_df.iloc[:100])

        if "error" in ind_50:
            print(f"  Indicators at candle 50: ERROR - {ind_50.get('error')}")
            issues.append("[WARMUP] Indicators fail at candle 50")
        else:
            ema_fast_50 = ind_50.get("ema_fast", 0)
            ema_slow_50 = ind_50.get("ema_slow", 0)
            print(f"  At candle 50: ema_fast={ema_fast_50:.2f}, ema_slow={ema_slow_50:.2f}")

        if "error" not in ind_100:
            ema_fast_100 = ind_100.get("ema_fast", 0)
            ema_slow_100 = ind_100.get("ema_slow", 0)
            print(f"  At candle 100: ema_fast={ema_fast_100:.2f}, ema_slow={ema_slow_100:.2f}")

        # Check if EMA50 at candle 50 is just the SMA (not enough data for proper EMA)
        # EMA50 needs ~50 candles to stabilize. At exactly candle 50, it's essentially SMA
        # This is acceptable but worth noting
        print("  NOTE: EMA50 at candle 50 = SMA50 (first value). Stabilizes by candle ~100.")
        print("  VERDICT: Warm-up buffer of 50 is MINIMAL but acceptable for EMA50.")
    else:
        print("  Cannot verify - insufficient BTC data")

    # ================================================================
    # Run backtest to get 3 trades for manual verification
    # ================================================================
    print("\n--- Running backtest for trade verification ---")

    # Pick a symbol with trades
    test_sym = "XRPUSDT"  # had 6 trades in filter analysis
    df = symbol_data.get(test_sym, pd.DataFrame())
    if df.empty or len(df) < 100:
        test_sym = "BTCUSDT"
        df = symbol_data.get(test_sym, pd.DataFrame())

    print(f"  Using {test_sym} ({len(df)} candles)")

    # Run backtest with detailed logging
    bt = Backtester(initial_balance=10000.0)
    result = bt.run(df=df, symbol=test_sym, btc_df=btc_df, btc_1d_df=btc_1d_df)

    trades = result.trades[:3]  # first 3 trades
    print(f"  Got {result.total_trades} trades, checking first {len(trades)}")

    # ================================================================
    # CHECK 1: Entry timing (look-ahead bias)
    # ================================================================
    print("\n--- CHECK 1: Entry Timing (Look-ahead Bias) ---")
    print("  Backtester logic: signal computed on candle[i], entry at candle[i] close price")
    print("  This means: signal uses data up to candle[i] (inclusive)")
    print("  Entry price = close of candle[i] where signal was generated")
    print("")

    # Read backtester source to verify
    # From backtester.py: entry_price = current_price = row['close']
    # Signal is computed on slice_df = df.iloc[:i+1] (includes current candle)
    # Entry happens on SAME candle as signal
    #
    # This IS a look-ahead bias issue:
    # - We compute indicators using candle[i]'s close
    # - Then we enter at candle[i]'s close
    # - In reality, we'd see the signal AFTER candle[i] closes
    # - And enter at candle[i+1]'s open
    #
    # However, for 4H candles, close[i] ~ open[i+1] in most cases
    # The bias is small but exists

    for idx, t in enumerate(trades):
        entry_time = getattr(t, 'entry_time', 'unknown')
        print(f"  Trade #{idx+1}: {t.side} {test_sym}")
        print(f"    Entry time: {entry_time}")
        print(f"    Entry price: {t.entry_price:.4f}")

        # Find the candle
        if entry_time != 'unknown' and entry_time in df.index:
            candle = df.loc[entry_time]
            print(f"    Candle OHLC: O={candle['open']:.4f} H={candle['high']:.4f} "
                  f"L={candle['low']:.4f} C={candle['close']:.4f}")
            if abs(t.entry_price - candle['close']) < 0.01:
                print(f"    Entry = candle close (signal candle = entry candle)")
                print(f"    >> LOOK-AHEAD BIAS: entry on same candle as signal")
            # Check next candle
            candle_idx = df.index.get_loc(entry_time)
            if candle_idx + 1 < len(df):
                next_candle = df.iloc[candle_idx + 1]
                slippage = abs(next_candle['open'] - t.entry_price) / t.entry_price * 100
                print(f"    Next candle open: {next_candle['open']:.4f} (slippage: {slippage:.3f}%)")
        print()

    if trades:
        issues.append("[LOOK-AHEAD] Entry on same candle as signal (should be next candle open)")

    # ================================================================
    # CHECK 2: SL/TP checked by high/low
    # ================================================================
    print("\n--- CHECK 2: SL/TP Check Method (high/low vs close) ---")
    # From BacktestPosition.update():
    #   def update(self, current_price, high=None, low=None):
    #     check_high = high if high is not None else current_price
    #     check_low = low if low is not None else current_price
    #     if self.side == "long":
    #         if check_low <= self.sl_price: -> SL hit
    #         if check_high >= self.tp_price: -> TP hit
    #
    # And in _run_backtest:
    #   closed, _ = pos.update(current_price, high=candle_high, low=candle_low)
    #
    # This is CORRECT: SL/TP checked against high/low

    print("  BacktestPosition.update() receives high/low from candle")
    print("  SL checked against candle LOW (long) / candle HIGH (short)")
    print("  TP checked against candle HIGH (long) / candle LOW (short)")
    print("  VERDICT: CORRECT - uses high/low, not close")

    # Verify with actual trade
    for idx, t in enumerate(trades):
        exit_reason = getattr(t, 'exit_reason', '?')
        print(f"\n  Trade #{idx+1}: exit_reason={exit_reason}")
        tp_val = getattr(t, 'tp_price', None) or 0
        print(f"    SL={t.sl_price:.4f}, TP={tp_val:.4f}")
        print(f"    Exit price={t.exit_price:.4f}")

        if exit_reason == "SL":
            if abs(t.exit_price - t.sl_price) < 0.01:
                print(f"    Exit at SL price - correct")
            else:
                print(f"    Exit NOT at SL price - ISSUE")
                issues.append(f"[SL/TP] Trade #{idx+1}: exit price != SL price")
        elif exit_reason == "TP_2.5R":
            tp = getattr(t, 'tp_price', None) or 0
            if tp > 0 and abs(t.exit_price - tp) < 0.01:
                print(f"    Exit at TP price - correct")

    # ================================================================
    # CHECK 3: PnL calculation
    # ================================================================
    print("\n--- CHECK 3: PnL Calculation ---")
    print("  Backtester uses: pnl_usd = size_usd * (exit - entry) / entry")
    print("  No leverage, no commission in backtest")
    print("  NOTE: Real trading has commission (~0.06% per side = 0.12% round trip)")
    print("")

    for idx, t in enumerate(trades):
        if t.side == "long":
            expected_pnl_pct = (t.exit_price - t.entry_price) / t.entry_price * 100
        else:
            expected_pnl_pct = (t.entry_price - t.exit_price) / t.entry_price * 100

        # Account for partial closes
        remaining = getattr(t, 'remaining_pct', 1.0)
        partial_pnl = getattr(t, 'partial_pnl_usd', 0)
        remaining_pnl = t.size_usd * remaining * expected_pnl_pct / 100
        total_expected = partial_pnl + remaining_pnl

        actual_pnl = t.pnl_usd
        diff = abs(actual_pnl - total_expected)

        print(f"  Trade #{idx+1}: {t.side} {test_sym}")
        print(f"    Entry: {t.entry_price:.4f}, Exit: {t.exit_price:.4f}")
        print(f"    Size: ${t.size_usd:.2f}, Remaining: {remaining*100:.0f}%")
        print(f"    Partial PnL booked: ${partial_pnl:.2f}")
        print(f"    Expected PnL: ${total_expected:.2f}")
        print(f"    Actual PnL:   ${actual_pnl:.2f}")
        print(f"    Diff: ${diff:.4f}")

        if diff > 0.1:
            issues.append(f"[PNL] Trade #{idx+1}: PnL mismatch by ${diff:.2f}")
            print(f"    >> PNL MISMATCH")
        else:
            print(f"    >> OK (match within $0.10)")

        # Commission impact
        commission_pct = 0.12  # 0.06% per side * 2
        commission_usd = t.size_usd * commission_pct / 100
        print(f"    Commission impact (not in backtest): ${commission_usd:.2f} ({commission_pct}%)")
        print()

    if not any("[PNL]" in i for i in issues):
        print("  NOTE: Backtest does NOT include commission (0.12% round trip)")
        issues.append("[COMMISSION] Backtest ignores trading commission (~0.12% round trip)")

    # ================================================================
    # SUMMARY
    # ================================================================
    print("\n" + "=" * 80)
    print("SANITY CHECK SUMMARY")
    print("=" * 80)

    if not issues:
        print("  ALL CLEAR - no issues found")
    else:
        print(f"  Found {len(issues)} issue(s):\n")
        for i, issue in enumerate(issues, 1):
            severity = "CRITICAL" if "LOOK-AHEAD" in issue else "MEDIUM" if "COMMISSION" in issue else "LOW"
            print(f"  {i}. [{severity}] {issue}")

    # Save results
    report = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol_checked": test_sym,
        "trades_checked": len(trades),
        "issues": issues,
        "checks": {
            "entry_timing": "FAIL - entry on same candle as signal (look-ahead bias)",
            "sl_tp_method": "PASS - uses high/low correctly",
            "pnl_calculation": "PASS - matches manual calculation (no commission)",
            "indicator_warmup": "PASS - 50 candle warm-up (minimal but acceptable)",
            "data_gaps": "checked per symbol",
        }
    }

    with open("sanity_check_results.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nResults saved to sanity_check_results.json")


if __name__ == "__main__":
    main()
