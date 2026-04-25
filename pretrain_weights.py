#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pretrain_weights.py — Pre-train signal weights from backtest trade log.

Reads trades from SQLite (or runs a backtest to populate it),
computes per-source contribution to PnL, and outputs calibrated weights.

Usage:
  python pretrain_weights.py --db trade_log/trades.db --out pretrained_weights.json
  python pretrain_weights.py --backtest-first --out pretrained_weights.json
"""

import argparse
import json
import os
import sys
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

import numpy as np

# Signal sources matching backtester._compute_composite
SIGNAL_SOURCES = [
    "rsi", "macd", "bb", "trend", "ema_cross",
    "engulfing", "fvg", "sweep", "volume_spike",
    "onchain", "whale", "macro", "deepseek"
]

MIN_TRADES_FOR_WEIGHT = 10  # minimum trades to compute a weight
MIN_TRADES_TOTAL = 150     # minimum total trades to compute weights at all
WEIGHT_CLAMP_MIN = 0.5
WEIGHT_CLAMP_MAX = 2.0


def run_backtest_to_db(db_path: str):
    """Run backtest and save trades to SQLite DB."""
    from backtester import Backtester, SYMBOLS, fetch_bitget_klines

    print("=" * 60)
    print("RUNNING BACKTEST TO POPULATE TRADE DB")
    print("=" * 60)

    bt = Backtester(initial_balance=10000.0, use_real_data=False)
    result = bt.run()

    # Save trades to SQLite
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            exit_price REAL,
            pnl_usd REAL,
            pnl_pct REAL,
            exit_reason TEXT,
            composite_score REAL DEFAULT 0.0,
            signals_fired TEXT,
            entry_time TEXT,
            exit_time TEXT
        )
    """)
    # Ensure signals_fired column exists
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN signals_fired TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN composite_score REAL DEFAULT 0.0")
    except Exception:
        pass

    count = 0
    for t in result.trades:
        signals_json = json.dumps(getattr(t, 'signals_fired', {}))
        composite = getattr(t, 'composite_score', 0.0)
        conn.execute("""
            INSERT INTO trades (symbol, side, entry_price, exit_price, pnl_usd, pnl_pct,
                                exit_reason, composite_score, signals_fired)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            t.symbol, t.side, t.entry_price, t.exit_price,
            t.pnl_usd, t.pnl_pct, getattr(t, 'exit_reason', ''),
            composite, signals_json
        ))
        count += 1

    conn.commit()
    conn.close()

    result.print_summary()
    print(f"\nSaved {count} trades to {db_path}")
    return count


def compute_weights(db_path: str):
    """Compute per-source weights from trade log."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Load all trades with signals_fired
    cursor.execute("""
        SELECT symbol, side, pnl_pct, pnl_usd, exit_reason, 
               composite_score, signals_fired
        FROM trades 
        WHERE signals_fired IS NOT NULL AND signals_fired != ''
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("ERROR: No trades with signals_fired found in DB")
        return None

    print(f"\nLoaded {len(rows)} trades with signals_fired")

    # Parse trades
    trades = []
    for row in rows:
        try:
            sf = json.loads(row['signals_fired']) if row['signals_fired'] else {}
        except (json.JSONDecodeError, TypeError):
            sf = {}
        trades.append({
            'symbol': row['symbol'],
            'side': row['side'],
            'pnl_pct': row['pnl_pct'] or 0.0,
            'pnl_usd': row['pnl_usd'] or 0.0,
            'exit_reason': row['exit_reason'] or '',
            'composite_score': row['composite_score'] or 0.0,
            'signals_fired': sf,
        })

    # Global check: minimum total trades
    if len(trades) < MIN_TRADES_TOTAL:
        print(f"WARNING: Not enough data ({len(trades)} < {MIN_TRADES_TOTAL})")
        print("Writing neutral weights (all = 1.0)")
        neutral_weights = {s: 1.0 for s in SIGNAL_SOURCES}
        return neutral_weights, {}, len(trades), True

    # Global average PnL%
    global_avg_pnl = np.mean([t['pnl_pct'] for t in trades])
    print(f"Global avg PnL%: {global_avg_pnl:.4f}%")

    # Per-source analysis
    source_stats = {}
    for source in SIGNAL_SOURCES:
        # Trades where this source was active (abs(value) > 0)
        active_trades = []
        for t in trades:
            val = t['signals_fired'].get(source, 0.0)
            if abs(val) > 0.01:  # source was active
                # Adjust PnL by signal direction:
                # If source said buy (val > 0) and side=long, or source said sell (val < 0) and side=short
                # → signal was in the right direction
                # We weight PnL by whether the signal direction matched the trade direction
                side_sign = 1.0 if t['side'] == 'long' else -1.0
                # Directional PnL: how much PnL was attributable to this source being right
                directional_pnl = t['pnl_pct'] * np.sign(val * side_sign)
                active_trades.append({
                    'pnl_pct': t['pnl_pct'],
                    'directional_pnl': directional_pnl,
                    'signal_val': val,
                    'side': t['side'],
                })

        n_trades = len(active_trades)
        if n_trades < MIN_TRADES_FOR_WEIGHT:
            # Not enough data — use neutral weight
            source_stats[source] = {
                'n_trades': n_trades,
                'avg_pnl': 0.0,
                'avg_directional_pnl': 0.0,
                'weight': 1.0,
                'reason': f'< {MIN_TRADES_FOR_WEIGHT} trades'
            }
            continue

        avg_pnl = np.mean([t['pnl_pct'] for t in active_trades])
        avg_directional = np.mean([t['directional_pnl'] for t in active_trades])

        # Weight = ratio of this source's directional PnL to global average
        if abs(global_avg_pnl) < 0.001:
            raw_weight = 1.0  # avoid division by zero
        else:
            raw_weight = avg_directional / global_avg_pnl

        # Clamp
        clamped_weight = max(WEIGHT_CLAMP_MIN, min(WEIGHT_CLAMP_MAX, raw_weight))

        source_stats[source] = {
            'n_trades': n_trades,
            'avg_pnl': round(avg_pnl, 4),
            'avg_directional_pnl': round(avg_directional, 4),
            'weight': round(clamped_weight, 4),
            'reason': 'computed'
        }

    # Normalize: mean of all weights should be 1.0
    computed_weights = {s: source_stats[s]['weight'] for s in SIGNAL_SOURCES}
    mean_weight = np.mean(list(computed_weights.values()))
    if mean_weight > 0:
        for s in computed_weights:
            computed_weights[s] = round(computed_weights[s] / mean_weight, 4)

    # Re-clamp after normalization
    for s in computed_weights:
        computed_weights[s] = max(WEIGHT_CLAMP_MIN, min(WEIGHT_CLAMP_MAX, computed_weights[s]))

    # Final normalization pass
    mean_weight = np.mean(list(computed_weights.values()))
    if mean_weight > 0:
        for s in computed_weights:
            computed_weights[s] = round(computed_weights[s] / mean_weight, 4)

    return computed_weights, source_stats, len(trades), False


def main():
    parser = argparse.ArgumentParser(description="Pre-train signal weights from backtest trade log")
    parser.add_argument("--db", default="trade_log/trades.db", help="Path to SQLite trade DB")
    parser.add_argument("--out", default="pretrained_weights.json", help="Output JSON file")
    parser.add_argument("--backtest-first", action="store_true", help="Run backtest to populate DB first")
    args = parser.parse_args()

    # Ensure DB directory exists
    os.makedirs(os.path.dirname(args.db) if os.path.dirname(args.db) else '.', exist_ok=True)

    # If DB doesn't exist or --backtest-first, run backtest
    if args.backtest_first or not os.path.exists(args.db):
        print(f"DB not found at {args.db} or --backtest-first requested")
        n_trades = run_backtest_to_db(args.db)
        if n_trades == 0:
            print("ERROR: No trades generated from backtest")
            sys.exit(1)

    # Compute weights from DB
    result = compute_weights(args.db)
    if result is None:
        sys.exit(1)

    weights, source_stats, n_trades, neutral = result

    # Print table
    print("\n" + "=" * 80)
    print("SIGNAL WEIGHT PRE-TRAINING RESULTS")
    print("=" * 80)
    print(f"{'Source':<15} {'Trades':>8} {'Avg PnL%':>10} {'Dir PnL%':>10} {'Weight':>8} {'Note':<20}")
    print("-" * 80)
    for source in SIGNAL_SOURCES:
        stats = source_stats.get(source, {'n_trades': 0, 'avg_pnl': 0.0, 'avg_directional_pnl': 0.0, 'reason': 'neutral' if neutral else ''})
        note = stats.get('reason', 'neutral' if neutral else '')
        print(f"{source:<15} {stats['n_trades']:>8} {stats.get('avg_pnl', 0):>10.2f} "
              f"{stats.get('avg_directional_pnl', 0):>10.2f} {weights[source]:>8.2f} {note:<20}")
    print("=" * 80)
    print(f"Trades used: {n_trades}")
    print()

    # Save JSON
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trades_used": n_trades,
        "neutral": neutral,
        "weights": weights
    }

    with open(args.out, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Saved to {args.out}")
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()