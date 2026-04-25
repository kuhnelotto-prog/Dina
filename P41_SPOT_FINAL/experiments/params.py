"""
experiments/params.py — параметры экспериментов (единственный источник правды для backtester).

P34: Asymmetric LONG/SHORT exit logic.
  LONG:  SL=3.5×ATR, no trailing before TP1, TSL at 2.0×ATR from peak after TP1
  SHORT: SL=3.0×ATR, no trailing before TP1, TSL at 2.0×ATR from valley after TP1

P8 Exit Logic (replaces TRAILING_STAGES):
  TP1 at +1.0×ATR → close 30%, SL to breakeven
  TP2 at +2.0×ATR → close 30%
  After TP1: TSL tracks peak/valley at configurable ATR distance
  Remaining 40% exits on TSL or timeout
"""

# ── SL distance (asymmetric for LONG/SHORT) ──
SL_ATR_MULT_LONG = 3.5       # P34: wider SL for longs (was 3.0)
SL_ATR_MULT_SHORT = 3.0       # SHORT unchanged

# ── TP1: first take profit ──
TP1_ATR_MULT = 1.0            # TP1 activation at +1.0×ATR
TP1_CLOSE_PCT = 0.30           # close 30% of position at TP1

# ── TP2: second take profit ──
TP2_ATR_MULT = 2.0            # TP2 activation at +2.0×ATR
TP2_CLOSE_PCT = 0.30           # close 30% of position at TP2

# ── TSL distance from peak/valley (after TP1) ──
TSL_ATR_LONG = 2.0            # P34: softer trailing for longs
TSL_ATR_SHORT = 2.0           # SHORT trailing

# ── Legacy compatibility (used by older scripts) ──
SL_ATR_MULT = 3.0             # default (scripts may override)
TSL_FROM_ENTRY_ATR = 1.5      # legacy
TSL_AFTER_TP2_ATR = 2.0      # legacy

# ── Position limits ──
MAX_SIMULTANEOUS_TRADES = 4

# ── Dynamic thresholds ──
LONG_THRESHOLD_BULL = 0.40
LONG_THRESHOLD_BEAR = 0.45
SHORT_THRESHOLD_BULL = 0.45
SHORT_THRESHOLD_BEAR = 0.35