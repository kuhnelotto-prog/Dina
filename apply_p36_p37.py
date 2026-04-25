#!/usr/bin/env python3
"""Apply P36 (MIN_PNL_LONG) + P37 (CVD) to backtester.py"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

filepath = 'backtester.py'
with open(filepath, 'r', encoding='utf-8') as f:
    c = f.read()

# ════════════════════════════════════════════════════════════
# 1. Add P36 + P37 constants after MIN_PNL_CHECK_H
# ════════════════════════════════════════════════════════════
old1 = 'MIN_PNL_CHECK_H = 48          # проверять PnL после 48ч\nSTART_DATE'
new1 = '''MIN_PNL_CHECK_H = 48          # проверять PnL после 48ч (SHORT)
# P36: LONG-specific MIN_PNL parameters (overridable from scripts)
MIN_PNL_CHECK_H_LONG = 48     # проверять PnL после N часов для LONG
MIN_EXPECTED_PNL_PCT_LONG = -0.5  # закрыть LONG если PnL < X% после MIN_PNL_CHECK_H_LONG
MIN_PNL_LONG_ENABLED = True   # False = disable MIN_PNL_TIMEOUT for LONG entirely
# P37: CVD (Cumulative Volume Delta) as LONG-only signal booster
CVD_WEIGHT_LONG = 0.1         # weight of CVD signal in composite (LONG only)
CVD_LOOKBACK = 20             # compare CVD to rolling mean over N candles
START_DATE'''

assert old1 in c, f"Cannot find: {old1[:50]}"
c = c.replace(old1, new1, 1)
print("1. P36+P37 constants added")

# ════════════════════════════════════════════════════════════
# 2. Add CVD precomputation before master timeline
# ════════════════════════════════════════════════════════════
old2 = '        # Master timeline: use BTCUSDT if available, otherwise first symbol'
new2 = '''        # ── P37: Precompute CVD (Cumulative Volume Delta) per symbol ──
        # CVD per candle = taker_buy_vol - taker_sell_vol = 2*taker_buy_vol - total_vol
        # Requires 'taker_buy_vol' column in the DataFrame (field 9 from Binance klines API)
        cvd_data = {}  # symbol -> Series of CVD values
        cvd_mean_data = {}  # symbol -> Series of rolling mean CVD
        for sym in active_symbols:
            sym_df = dfs.get(sym)
            if sym_df is not None and 'taker_buy_vol' in sym_df.columns:
                cvd = 2.0 * sym_df['taker_buy_vol'] - sym_df['volume']
                cvd_data[sym] = cvd
                cvd_mean_data[sym] = cvd.rolling(CVD_LOOKBACK, min_periods=1).mean()
            else:
                cvd_data[sym] = None
                cvd_mean_data[sym] = None

        # Master timeline: use BTCUSDT if available, otherwise first symbol'''

assert old2 in c, f"Cannot find: {old2[:50]}"
c = c.replace(old2, new2, 1)
print("2. CVD precomputation added")

# ════════════════════════════════════════════════════════════
# 3. Add CVD score to LONG signal generation
# ════════════════════════════════════════════════════════════
old3 = '                # ── LONG signal ──\n                if composite > threshold_long and is_bullish and rsi < 70 and btc_1d_allows_long:\n                    pending_signals[sym] = {"side": "long", "sl_pct": sl_pct, "tp_pct": tp_pct, "composite": composite, "atr": atr_value}'
new3 = '''                # ── P37: CVD boost for LONG signals only ──
                cvd_score_long = 0.0
                if cvd_data.get(sym) is not None:
                    try:
                        cvd_val = cvd_data[sym].iloc[sym_idx]
                        cvd_mean_val = cvd_mean_data[sym].iloc[sym_idx]
                        if not pd.isna(cvd_val) and not pd.isna(cvd_mean_val) and cvd_val > 0 and cvd_val > cvd_mean_val:
                            cvd_score_long = CVD_WEIGHT_LONG
                    except Exception:
                        pass
                composite_long = composite + cvd_score_long if cvd_score_long > 0 else composite

                # ── LONG signal ──
                if composite_long > threshold_long and is_bullish and rsi < 70 and btc_1d_allows_long:
                    pending_signals[sym] = {"side": "long", "sl_pct": sl_pct, "tp_pct": tp_pct, "composite": composite_long, "atr": atr_value}'''

assert old3 in c, f"Cannot find LONG signal block"
c = c.replace(old3, new3, 1)
print("3. CVD score added to LONG signal")

# ════════════════════════════════════════════════════════════
# 4. Replace MIN_PNL check with LONG/SHORT specific logic
# ════════════════════════════════════════════════════════════
old4 = '''                # Min PnL check after MIN_PNL_CHECK_H hours
                if pos_age_h >= MIN_PNL_CHECK_H:
                    current_pnl_pct = (candle_close - pos.entry_price) / pos.entry_price * 100
                    if pos.side == "short":
                        current_pnl_pct = -current_pnl_pct
                    if current_pnl_pct < MIN_EXPECTED_PNL_PCT:
                        if pos.side == "long":
                            close_price = candle_close * (1 - SLIPPAGE_PCT)
                        else:
                            close_price = candle_close * (1 + SLIPPAGE_PCT)
                        pos._close(close_price, "MIN_PNL_TIMEOUT", timestamp=timestamp)
                        closed_syms.append(sym)
                        result.add_trade(pos)
                        continue'''

new4 = '''                # Min PnL check — LONG uses separate parameters (P36)
                if pos.side == "long" and MIN_PNL_LONG_ENABLED:
                    if pos_age_h >= MIN_PNL_CHECK_H_LONG:
                        current_pnl_pct = (candle_close - pos.entry_price) / pos.entry_price * 100
                        if current_pnl_pct < MIN_EXPECTED_PNL_PCT_LONG:
                            close_price = candle_close * (1 - SLIPPAGE_PCT)
                            pos._close(close_price, "MIN_PNL_TIMEOUT", timestamp=timestamp)
                            closed_syms.append(sym)
                            result.add_trade(pos)
                            continue
                elif pos.side == "short":
                    if pos_age_h >= MIN_PNL_CHECK_H:
                        current_pnl_pct = -((candle_close - pos.entry_price) / pos.entry_price * 100)
                        if current_pnl_pct < MIN_EXPECTED_PNL_PCT:
                            close_price = candle_close * (1 + SLIPPAGE_PCT)
                            pos._close(close_price, "MIN_PNL_TIMEOUT", timestamp=timestamp)
                            closed_syms.append(sym)
                            result.add_trade(pos)
                            continue'''

assert old4 in c, f"Cannot find MIN_PNL check block"
c = c.replace(old4, new4, 1)
print("4. MIN_PNL_LONG check replaced")

# ════════════════════════════════════════════════════════════
# Write and verify
# ════════════════════════════════════════════════════════════
with open(filepath, 'w', encoding='utf-8') as f:
    f.write(c)

print("\nAll P36+P37 modifications applied!")

import py_compile
try:
    py_compile.compile(filepath, doraise=True)
    print("SYNTAX: OK")
except py_compile.PyCompileError as e:
    print(f"SYNTAX ERROR: {e}")

# Verify key additions
checks = {
    'MIN_PNL_CHECK_H_LONG': 'P36 LONG MIN_PNL check hours',
    'MIN_EXPECTED_PNL_PCT_LONG': 'P36 LONG MIN_PNL threshold',
    'MIN_PNL_LONG_ENABLED': 'P36 LONG MIN_PNL enabled',
    'CVD_WEIGHT_LONG': 'P37 CVD weight',
    'CVD_LOOKBACK': 'P37 CVD lookback',
    'cvd_data': 'P37 CVD data',
    'cvd_score_long': 'P37 CVD score in LONG signal',
    'composite_long': 'P37 composite_long',
}

print("\nVerification:")
for kw, desc in checks.items():
    if kw in c:
        print(f"  OK: {desc}")
    else:
        print(f"  MISSING: {desc} ({kw})")