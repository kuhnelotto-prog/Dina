#!/usr/bin/env python3
"""P24 Diagnosis: detailed LONG vs SHORT statistics."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester as bt_mod

# Reset all filters to P24 baseline
bt_mod.BTC_HEALTH_FILTER = False
bt_mod.SLOPE_FILTER_1D = False
bt_mod.REGIME_LOCK_4H = False
bt_mod.COMBO_FILTER = False

bt_mod.MIN_PNL_CHECK_H = 72
bt_mod.MIN_EXPECTED_PNL_PCT = -0.5
bt_mod.START_BALANCE = 1000.0
bt_mod.BASE_RISK_PCT = 2.0
bt_mod.LEVERAGE = 1
bt_mod.SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT",
                  "LINKUSDT", "DOGEUSDT", "SUIUSDT",
                  "RUNEUSDT", "OPUSDT", "INJUSDT", "TIAUSDT", "ATOMUSDT"]

import experiments.params as ep
ep.TSL_AFTER_TP2_ATR = 2.0
ep.LONG_THRESHOLD_BULL = 0.40
ep.LONG_THRESHOLD_BEAR = 0.45
ep.SHORT_THRESHOLD_BULL = 0.45
ep.SHORT_THRESHOLD_BEAR = 0.35

bt_mod.LONG_THRESHOLD_BULL = 0.40
bt_mod.LONG_THRESHOLD_BEAR = 0.45
bt_mod.SHORT_THRESHOLD_BULL = 0.45
bt_mod.SHORT_THRESHOLD_BEAR = 0.35
bt_mod.TSL_AFTER_TP2_ATR = 2.0
bt_mod.ADX_THRESHOLD = 20
bt_mod.SL_ATR_MULT = 3.0
bt_mod.TP1_ATR_MULT = 1.0
bt_mod.TP1_CLOSE_PCT = 0.30
bt_mod.TP2_ATR_MULT = 2.0
bt_mod.TP2_CLOSE_PCT = 0.30
bt_mod.TSL_FROM_ENTRY_ATR = 1.5
bt_mod.MAX_SIMULTANEOUS_TRADES = 4

from backtester import Backtester, BacktestPosition
from datetime import datetime, timezone, timedelta
import requests, pandas as pd
from collections import defaultdict

SYMBOLS = bt_mod.SYMBOLS
BALANCE = 1000.0

PERIODS = [
    ("BULL", datetime(2023,11,1,tzinfo=timezone.utc), datetime(2024,4,30,tzinfo=timezone.utc)),
    ("BEAR/SIDE", datetime(2024,5,1,tzinfo=timezone.utc), datetime(2024,10,31,tzinfo=timezone.utc)),
    ("CURRENT", datetime(2025,10,1,tzinfo=timezone.utc), datetime(2026,4,17,tzinfo=timezone.utc)),
]

def fetch_binance(sym, start_dt, end_dt, interval="4h"):
    all_c = []
    st = int(start_dt.timestamp() * 1000)
    et = int(end_dt.timestamp() * 1000)
    cs = st
    for _ in range(30):
        p = {"symbol": sym, "interval": interval, "startTime": cs, "endTime": et, "limit": 1500}
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/klines", params=p, timeout=30).json()
        except Exception:
            break
        if not isinstance(r, list) or len(r) == 0:
            break
        for c in r:
            all_c.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
        last_close = int(r[-1][6])
        if last_close >= et or len(r) < 1500:
            break
        cs = last_close + 1
        time.sleep(0.1)
    if not all_c: return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df

# ── Fetch ALL data once ──
print("Fetching P24 data...")
data_cache = {}
for pk, start_dt, end_dt in PERIODS:
    btc_df = fetch_binance("BTCUSDT", start_dt, end_dt)
    btc_1d = fetch_binance("BTCUSDT", start_dt, end_dt, interval="1d")
    dfs = {"BTCUSDT": btc_df}
    print(f"  {pk}: BTC 4H={len(btc_df)} 1D={len(btc_1d)}")
    for s in SYMBOLS[1:]:
        df = fetch_binance(s, start_dt, end_dt)
        time.sleep(0.15)
        dfs[s] = df
    data_cache[pk] = (dfs, btc_df, btc_1d)

class TradeStats:
    def __init__(self):
        self.count = 0
        self.wins = 0
        self.total_pnl = 0.0
        self.total_duration_h = 0.0
        self.tp_closes = 0
        self.sl_closes = 0
        self.timeout_closes = 0
        self.other_closes = 0
        self.avg_entry_to_tp_pct = 0.0
        self.avg_entry_to_sl_pct = 0.0
        self._tp_sum = 0.0
        self._sl_sum = 0.0
        self.composite_scores = []
        self.pnl_list = []

    def add_trade(self, trade, entry_to_tp_pct, entry_to_sl_pct):
        self.count += 1
        if trade.pnl_usd > 0:
            self.wins += 1
        self.total_pnl += trade.pnl_usd
        self.pnl_list.append(trade.pnl_usd)
        try:
            duration_h = (trade.exit_time - trade.entry_time).total_seconds() / 3600
            self.total_duration_h += duration_h
        except:
            pass
        reason = getattr(trade, 'exit_reason', '')
        if 'TP' in reason:
            self.tp_closes += 1
        elif 'SL' in reason:
            self.sl_closes += 1
        elif 'TIMEOUT' in reason or 'MIN_PNL' in reason:
            self.timeout_closes += 1
        else:
            self.other_closes += 1
        self._tp_sum += entry_to_tp_pct
        self._sl_sum += entry_to_sl_pct
        # P32: use composite_score from trade
        self.composite_scores.append(getattr(trade, 'composite_score', 0.0))

    def summary(self):
        if self.count == 0:
            return {}
        sorted_scores = sorted(self.composite_scores)
        n = len(sorted_scores)
        return {
            'count': self.count,
            'win_rate': self.wins / self.count * 100 if self.count > 0 else 0,
            'total_pnl': self.total_pnl,
            'avg_pnl': self.total_pnl / self.count,
            'avg_duration_h': self.total_duration_h / self.count,
            'tp_pct': self.tp_closes / self.count * 100,
            'sl_pct': self.sl_closes / self.count * 100,
            'timeout_pct': self.timeout_closes / self.count * 100,
            'avg_entry_to_tp_pct': self._tp_sum / self.count,
            'avg_entry_to_sl_pct': self._sl_sum / self.count,
            'avg_composite': sum(self.composite_scores) / len(self.composite_scores) if self.composite_scores else 0,
            'median_pnl': sorted(self.pnl_list)[len(self.pnl_list)//2] if self.pnl_list else 0,
            # P32: percentile breakdown for composite score
            'p25_composite': sorted_scores[int(n*0.25)] if n > 0 else 0,
            'p50_composite': sorted_scores[int(n*0.50)] if n > 0 else 0,
            'p75_composite': sorted_scores[int(n*0.75)] if n > 0 else 0,
            'p90_composite': sorted_scores[int(n*0.90)] if n > 0 else 0,
        }

def run_diagnosis(pk, dfs, btc_df, btc_1d):
    """Run backtest and collect detailed stats."""
    long_stats = TradeStats()
    short_stats = TradeStats()
    all_trades = []
    pending_composites = {}

    for s in SYMBOLS:
        df = dfs.get(s)
        if df is None or df.empty or len(df) < 50:
            continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs=dfs, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        all_trades.extend(res.trades)

    for t in all_trades:
        side = getattr(t, 'side', '')
        # Entry to TP/SL distance
        entry_to_tp_pct = abs(t.initial_tp - t.entry_price) / t.entry_price * 100 if t.initial_tp else 0
        entry_to_sl_pct = abs(t.initial_sl - t.entry_price) / t.entry_price * 100
        if side == 'long':
            long_stats.add_trade(t, entry_to_tp_pct, entry_to_sl_pct)
        elif side == 'short':
            short_stats.add_trade(t, entry_to_tp_pct, entry_to_sl_pct)

    return long_stats, short_stats

# ── Run Diagnosis ──
print(f"\n{'='*90}")
print(f"  P24 DIAGNOSIS: LONG vs SHORT detailed statistics")
print(f"{'='*90}")

for pk, start_dt, end_dt in PERIODS:
    dfs, btc_df, btc_1d = data_cache[pk]
    long_stats, short_stats = run_diagnosis(pk, dfs, btc_df, btc_1d)
    ls = long_stats.summary()
    ss = short_stats.summary()

    print(f"\n  {pk}")
    print(f"  {'='*80}")
    print(f"  {'Metric':<35} {'LONG':>12} {'SHORT':>12} {'Delta':>12}")
    print(f"  {'-'*80}")
    print(f"  {'Trades':<35} {ls['count']:>12.0f} {ss['count']:>12.0f} {ls['count']-ss['count']:>12.0f}")
    print(f"  {'Win Rate (%)':<35} {ls['win_rate']:>12.1f} {ss['win_rate']:>12.1f} {ls['win_rate']-ss['win_rate']:>12.1f}")
    print(f"  {'Total PnL ($)':<35} {ls['total_pnl']:>12.2f} {ss['total_pnl']:>12.2f} {ls['total_pnl']-ss['total_pnl']:>12.2f}")
    print(f"  {'Avg PnL per trade ($)':<35} {ls['avg_pnl']:>12.2f} {ss['avg_pnl']:>12.2f} {ls['avg_pnl']-ss['avg_pnl']:>12.2f}")
    print(f"  {'Median PnL ($)':<35} {ls['median_pnl']:>12.2f} {ss['median_pnl']:>12.2f} {ls['median_pnl']-ss['median_pnl']:>12.2f}")
    print(f"  {'Avg Duration (hours)':<35} {ls['avg_duration_h']:>12.1f} {ss['avg_duration_h']:>12.1f} {ls['avg_duration_h']-ss['avg_duration_h']:>12.1f}")
    print(f"  {'TP closes (%)':<35} {ls['tp_pct']:>12.1f} {ss['tp_pct']:>12.1f} {ls['tp_pct']-ss['tp_pct']:>12.1f}")
    print(f"  {'SL closes (%)':<35} {ls['sl_pct']:>12.1f} {ss['sl_pct']:>12.1f} {ls['sl_pct']-ss['sl_pct']:>12.1f}")
    print(f"  {'Timeout closes (%)':<35} {ls['timeout_pct']:>12.1f} {ss['timeout_pct']:>12.1f} {ls['timeout_pct']-ss['timeout_pct']:>12.1f}")
    print(f"  {'Avg dist to TP (%)':<35} {ls['avg_entry_to_tp_pct']:>12.2f} {ss['avg_entry_to_tp_pct']:>12.2f} {ls['avg_entry_to_tp_pct']-ss['avg_entry_to_tp_pct']:>12.2f}")
    print(f"  {'Avg dist to SL (%)':<35} {ls['avg_entry_to_sl_pct']:>12.2f} {ss['avg_entry_to_sl_pct']:>12.2f} {ls['avg_entry_to_sl_pct']-ss['avg_entry_to_sl_pct']:>12.2f}")
    print(f"  {'Avg composite score':<35} {ls['avg_composite']:>12.3f} {ss['avg_composite']:>12.3f} {ls['avg_composite']-ss['avg_composite']:>12.3f}")
    print(f"  {'-'*80}")
    print(f"  Composite Percentiles (LONG): p25={ls['p25_composite']:.3f}, p50={ls['p50_composite']:.3f}, p75={ls['p75_composite']:.3f}, p90={ls['p90_composite']:.3f}")
    print(f"  Composite Percentiles (SHORT): p25={ss['p25_composite']:.3f}, p50={ss['p50_composite']:.3f}, p75={ss['p75_composite']:.3f}, p90={ss['p90_composite']:.3f}")

    # Hypothesis checks
    print(f"\n  HYPOTHESIS CHECKS:")
    # 1. TP/SL asymmetry
    tp_ratio = ls['tp_pct'] / ss['tp_pct'] if ss['tp_pct'] > 0 else 0
    sl_ratio = ls['sl_pct'] / ss['sl_pct'] if ss['sl_pct'] > 0 else 0
    print(f"    1. TP/SL asymmetry: LONG TP%={ls['tp_pct']:.1f}, SL%={ls['sl_pct']:.1f} | SHORT TP%={ss['tp_pct']:.1f}, SL%={ss['sl_pct']:.1f}")
    print(f"       - LONG:SL ratio = {sl_ratio:.2f} (higher = more SL hits for LONG)")

    # 2. Entry timing
    print(f"    2. Entry timing: Avg composite LONG={ls['avg_composite']:.3f}, SHORT={ss['avg_composite']:.3f}")

    # 3. Position sizing (same for both in P24)
    print(f"    3. Position sizing: BASE_RISK_PCT={bt_mod.BASE_RISK_PCT}% (same for LONG/SHORT)")