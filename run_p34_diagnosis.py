#!/usr/bin/env python3
"""P34 Diagnosis: Deep dive into WHY longs underperform shorts.
Collect per-trade statistics: avg win/loss size, exit reason distribution, 
hold time, SL hit rate, TSL hit rate by direction."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtester as bt_mod

# P24 baseline parameters
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
bt_mod.MAX_SIMULTANEOUS_TRADES = 4

# Reset P33 changes
bt_mod.TSL_FROM_ENTRY_ATR_LONG = 1.5
bt_mod.TSL_FROM_ENTRY_ATR_SHORT = 1.5

from backtester import Backtester
from datetime import datetime, timezone
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

# Fetch data
print("Fetching P24 diagnosis data...")
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

def analyze_trades(pk, dfs, btc_df, btc_1d):
    all_trades = []
    for s in SYMBOLS:
        df = dfs.get(s)
        if df is None or df.empty or len(df) < 50:
            continue
        bt = Backtester(initial_balance=BALANCE)
        res = bt.run(dfs=dfs, symbols=[s], btc_df=btc_df, btc_1d_df=btc_1d)
        all_trades.extend(res.trades)
    
    if not all_trades:
        return None
    
    longs = [t for t in all_trades if t.side == 'long']
    shorts = [t for t in all_trades if t.side == 'short']
    
    def analyze_side(trades, label):
        if not trades:
            return
        
        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd <= 0]
        
        # Exit reason distribution
        exit_reasons = defaultdict(list)
        for t in trades:
            reason = getattr(t, 'exit_reason', 'UNKNOWN')
            exit_reasons[reason].append(t)
        
        print(f"\n  === {label} ({len(trades)} trades) ===")
        print(f"  Win Rate: {len(wins)/len(trades)*100:.1f}%")
        print(f"  Avg Win:  ${sum(t.pnl_usd for t in wins)/len(wins):+.2f}" if wins else "  Avg Win: N/A")
        print(f"  Avg Loss: ${sum(t.pnl_usd for t in losses)/len(losses):+.2f}" if losses else "  Avg Loss: N/A")
        
        # Risk/Reward ratio
        avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t.pnl_usd for t in losses) / len(losses)) if losses else 0
        if avg_loss > 0:
            print(f"  R:R ratio: {avg_win/avg_loss:.2f}:1")
        
        # Exit reason breakdown
        print(f"  --- Exit Reasons ---")
        for reason in sorted(exit_reasons.keys()):
            r_trades = exit_reasons[reason]
            r_wins = [t for t in r_trades if t.pnl_usd > 0]
            r_pnl = sum(t.pnl_usd for t in r_trades)
            print(f"  {reason:20s}: {len(r_trades):3d} ({len(r_trades)/len(trades)*100:5.1f}%) | WR={len(r_wins)/len(r_trades)*100:.0f}% | PnL=${r_pnl:+.2f}")
        
        # Step distribution (how far trades progress)
        step_dist = defaultdict(int)
        for t in trades:
            step = getattr(t, 'trailing_step', 0)
            step_dist[step] += 1
        print(f"  --- Step Distribution ---")
        for step in sorted(step_dist.keys()):
            s_trades = [t for t in trades if getattr(t, 'trailing_step', 0) == step]
            s_pnl = sum(t.pnl_usd for t in s_trades)
            print(f"  Step {step}: {step_dist[step]:3d} ({step_dist[step]/len(trades)*100:5.1f}%) | PnL=${s_pnl:+.2f}")
        
        # Composite score distribution for wins vs losses
        if any(hasattr(t, 'composite_score') for t in trades):
            win_scores = [t.composite_score for t in wins if hasattr(t, 'composite_score')]
            loss_scores = [t.composite_score for t in losses if hasattr(t, 'composite_score')]
            if win_scores:
                print(f"  --- Composite Score ---")
                print(f"  Wins:  avg={sum(win_scores)/len(win_scores):.3f} | min={min(win_scores):.3f} | max={max(win_scores):.3f}")
            if loss_scores:
                print(f"  Losses: avg={sum(loss_scores)/len(loss_scores):.3f} | min={min(loss_scores):.3f} | max={max(loss_scores):.3f}")
        
        # SL vs TSL analysis (step 0 exits = SL/TSL before TP1)
        step0 = [t for t in trades if getattr(t, 'trailing_step', 0) == 0]
        if step0:
            sl_trades = [t for t in step0 if getattr(t, 'exit_reason', '') == 'SL']
            tsl_trades = [t for t in step0 if getattr(t, 'exit_reason', '') == 'TSL']
            sl_pnl = sum(t.pnl_usd for t in sl_trades) if sl_trades else 0
            tsl_pnl = sum(t.pnl_usd for t in tsl_trades) if tsl_trades else 0
            print(f"  --- Step 0 Breakdown ---")
            print(f"  SL (fixed stop):  {len(sl_trades):3d} | PnL=${sl_pnl:+.2f}")
            print(f"  TSL (trail stop): {len(tsl_trades):3d} | PnL=${tsl_pnl:+.2f}")
            if sl_trades:
                avg_sl_loss = sum(t.pnl_usd for t in sl_trades) / len(sl_trades)
                print(f"  Avg SL loss: ${avg_sl_loss:+.2f}")
            if tsl_trades:
                avg_tsl_pnl = sum(t.pnl_usd for t in tsl_trades) / len(tsl_trades)
                print(f"  Avg TSL PnL: ${avg_tsl_pnl:+.2f}")
        
        # PnL by remaining_pct (partial close analysis)
        print(f"  --- Remaining at Exit ---")
        for rem in sorted(set(getattr(t, 'remaining_pct', 1.0) for t in trades)):
            rem_trades = [t for t in trades if abs(getattr(t, 'remaining_pct', 1.0) - rem) < 0.01]
            rem_pnl = sum(t.pnl_usd for t in rem_trades)
            print(f"  Remaining={rem*100:.0f}%: {len(rem_trades):3d} | PnL=${rem_pnl:+.2f}")
    
    analyze_side(longs, "LONG")
    analyze_side(shorts, "SHORT")
    return {"longs": len(longs), "shorts": len(shorts)}

# Run analysis
print(f"\n{'='*90}")
print(f"  P24 DEEP DIAGNOSIS: WHY LONGS UNDERPER SHORTS")
print(f"{'='*90}")

for pk, start_dt, end_dt in PERIODS:
    dfs, btc_df, btc_1d = data_cache[pk]
    print(f"\n{'='*60}")
    print(f"  {pk}")
    print(f"{'='*60}")
    analyze_trades(pk, dfs, btc_df, btc_1d)