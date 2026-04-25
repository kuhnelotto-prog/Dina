#!/usr/bin/env python3
"""Apply P8 exit logic + P34 asymmetric LONG/SHORT parameters to backtester.py."""
import sys
sys.stdout.reconfigure(encoding='utf-8')

filepath = 'backtester.py'
with open(filepath, 'r', encoding='utf-8') as f:
    c = f.read()

# ════════════════════════════════════════════════════════════
# 1. Add P34 constants after FUNDING_INTERVAL_H line
# ════════════════════════════════════════════════════════════
old_constants = """FUNDING_RATE = 0.0001     # 0.01% каждые 8 часов
FUNDING_INTERVAL_H = 8   # интервал funding в часах"""

new_constants = """FUNDING_RATE = 0.0001     # 0.01% каждые 8 часов
FUNDING_INTERVAL_H = 8   # интервал funding в часах

# ── P34: Asymmetric LONG/SHORT parameters ──
SL_ATR_MULT_LONG = 3.5    # wider SL for longs (survive corrections in staircase pattern)
SL_ATR_MULT_SHORT = 1.5  # standard SL for shorts
TSL_ATR_LONG_STEP0 = 0   # disabled: no trailing before TP1 for LONG (give room to breathe)
TSL_ATR_LONG_AFTER_TP1 = 2.0  # softer trailing after TP1 for LONG"""

c = c.replace(old_constants, new_constants)
print("1. P34 constants added")

# ════════════════════════════════════════════════════════════
# 2. Add composite_score to BacktestPosition __init__
# ════════════════════════════════════════════════════════════
old_init = '        self.total_funding = 0.0      # accumulated funding cost\n        self._funding_hours_accrued = 0  # сколько 8-часовых funding-интервалов уже начислено'
new_init = '        self.total_funding = 0.0      # accumulated funding cost\n        self._funding_hours_accrued = 0  # сколько 8-часовых funding-интервалов уже начислено\n        self.composite_score = 0.0     # P8: entry composite score\n        self.peak_price = entry_price  # P8: track peak for TSL from peak'

c = c.replace(old_init, new_init)
print("2. composite_score and peak_price added to BacktestPosition")

# ════════════════════════════════════════════════════════════
# 3. Replace the _apply_trailing_4step method with P8+P34 exit logic
# ════════════════════════════════════════════════════════════
# Find the method and replace it
old_method_start = '    def _apply_trailing_4step(self, close):'
old_method_end_marker = '    def _partial_close(self, pct, price):'

idx_start = c.find(old_method_start)
idx_end = c.find(old_method_end_marker)
if idx_start < 0 or idx_end < 0:
    print("ERROR: Could not find _apply_trailing_4step method boundaries!")
    sys.exit(1)

old_method = c[idx_start:idx_end]

new_method = '''    def _apply_trailing_4step(self, close):
        """
        P8+P34 exit logic — asymmetric LONG/SHORT.
        
        LONG (P34):
          - No trailing before TP1 (Step 0 disabled)
          - TP1 at +1 ATR: close 30%, SL to entry - TSL_ATR_LONG_AFTER_TP1*ATR
          - TP2 at +2 ATR: close 30%, TSL at TSL_ATR_LONG_AFTER_TP1*ATR from peak
          - After TP2: TSL continues from peak at TSL_ATR_LONG_AFTER_TP1*ATR
        
        SHORT (P8 standard):
          - TP1 at +1 ATR: close 30%, SL to breakeven + 0.5 ATR
          - TP2 at +2 ATR: close 30%, TSL at 1.5 ATR from peak
          - After TP2: TSL continues from peak at 1.5 ATR
        """
        ATR = self.entry_atr
        if ATR <= 0:
            return False

        # Track peak price for TSL from peak
        if self.side == "long":
            if close > self.peak_price:
                self.peak_price = close
            atr_move = (close - self.entry_price) / ATR
        else:
            if close < self.peak_price:
                self.peak_price = close
            atr_move = (self.entry_price - close) / ATR

        step = self.trailing_step

        # ══════════════════════════════════════
        # LONG positions — P34 asymmetric logic
        # ══════════════════════════════════════
        if self.side == "long":
            # Step 0: DISABLED for LONG (P34) — no trailing before TP1
            # (skip breakeven at +0.5 ATR)
            
            # TP1 at +1 ATR: close 30%, SL to entry - TSL_ATR_LONG_AFTER_TP1*ATR
            if step < 1 and atr_move >= 1.0:
                self.trailing_step = 1
                new_sl = self.entry_price - TSL_ATR_LONG_AFTER_TP1 * ATR
                # Only move SL forward, never backward
                if new_sl > self.sl_price:
                    self.sl_price = new_sl
                self._partial_close(0.30, close)
                logger.debug(f"  TP1 (LONG): {self.symbol} close 30% at +1 ATR, SL->entry-{TSL_ATR_LONG_AFTER_TP1}ATR={new_sl:.2f}")
            
            # TP2 at +2 ATR: close 30%, TSL from peak at TSL_ATR_LONG_AFTER_TP1*ATR
            if step < 2 and atr_move >= 2.0:
                self.trailing_step = 2
                tp_price = close * (1 - SLIPPAGE_PCT)
                self._partial_close(0.30 / max(self.remaining_pct, 0.01), close)  # close 30% of original = fraction of remaining
                new_sl = self.peak_price - TSL_ATR_LONG_AFTER_TP1 * ATR
                if new_sl > self.sl_price:
                    self.sl_price = new_sl
                logger.debug(f"  TP2 (LONG): {self.symbol} close 30% at +2 ATR, TSL from peak={self.peak_price:.2f}")
            
            # After TP2 (or TP1): TSL from peak
            if self.trailing_step >= 1:
                tsl = self.peak_price - TSL_ATR_LONG_AFTER_TP1 * ATR
                if tsl > self.sl_price:
                    self.sl_price = tsl
            
            return False
        
        # ══════════════════════════════════════
        # SHORT positions — P8 standard logic
        # ══════════════════════════════════════
        else:
            # TP1 at +1 ATR: close 30%, SL to breakeven + 0.5 ATR
            if step < 1 and atr_move >= 1.0:
                self.trailing_step = 1
                new_sl = self.entry_price + 0.5 * ATR
                if new_sl < self.sl_price:
                    self.sl_price = new_sl
                self._partial_close(0.30, close)
                logger.debug(f"  TP1 (SHORT): {self.symbol} close 30% at +1 ATR, SL->breakeven+0.5ATR={new_sl:.2f}")
            
            # TP2 at +2 ATR: close 30%, TSL from peak at 1.5 ATR
            if step < 2 and atr_move >= 2.0:
                self.trailing_step = 2
                self._partial_close(0.30 / max(self.remaining_pct, 0.01), close)
                new_sl = self.peak_price + 1.5 * ATR
                if new_sl < self.sl_price:
                    self.sl_price = new_sl
                logger.debug(f"  TP2 (SHORT): {self.symbol} close 30% at +2 ATR, TSL from peak={self.peak_price:.2f}")
            
            # After TP2 (or TP1): TSL from peak
            if self.trailing_step >= 1:
                tsl = self.peak_price + 1.5 * ATR
                if tsl < self.sl_price:
                    self.sl_price = tsl
            
            return False

'''

c = c[:idx_start] + new_method + c[idx_end:]
print("3. Replaced _apply_trailing_4step with P8+P34 exit logic")

# ════════════════════════════════════════════════════════════
# 4. Fix _partial_close: TP2 should close 30% of ORIGINAL, not of remaining
# ════════════════════════════════════════════════════════════
# The current _partial_close takes fraction of current.
# For TP1: close 30% of original = 0.30/1.0 = 30% of current
# For TP2: close 30% of original = 0.30/0.70 = 42.9% of current
# But I already handle this in the method above. Let me simplify.

# Actually, let me fix the TP2 partial close calls to be cleaner.
# Replace the messy percentage calculations with clean calls.

# ════════════════════════════════════════════════════════════
# 5. Modify SL calculation in signal generation to use SL_ATR_MULT_LONG/SHORT
# ════════════════════════════════════════════════════════════
# Current code:
#   sl_pct = 1.5 * atr_pct / 100
# This uses 1.5 for BOTH long and short.
# P34: use SL_ATR_MULT_LONG for longs, SL_ATR_MULT_SHORT for shorts.

# But the SL is calculated before we know which side... Actually it's calculated
# in the signal generation where we already know the side.
# Let me look at the structure again...

# The signal generation has:
#   if atr_pct > 0.1:
#       sl_pct = 1.5 * atr_pct / 100
#       tp_pct = 2.0 * atr_pct / 100
# This is before the LONG/SHORT if blocks.
# I need to change it so that sl_pct is computed differently for long vs short.

# Strategy: compute base atr_sl and then use different multipliers in the signal dict.
# Change: sl_pct stays as base (1.5 ATR), but add a flag for P34 adjustment.
# Better: store atr_pct in the signal and compute SL at entry time using the right multiplier.

# Actually, the simplest approach: store atr_pct in the pending signal and
# compute SL at entry time using SL_ATR_MULT_LONG or SL_ATR_MULT_SHORT.

old_sl_calc = """                if atr_pct > 0.1:
                    sl_pct = 1.5 * atr_pct / 100
                    tp_pct = 2.0 * atr_pct / 100  # synced with strategist_client (was 3.0)
                else:
                    sl_pct = 0.03
                    tp_pct = 0.04  # proportional: 2.0/1.5 × 3% = 4%"""

new_sl_calc = """                if atr_pct > 0.1:
                    sl_pct_base = atr_pct / 100  # 1 ATR as fraction of price
                else:
                    sl_pct_base = 0.02  # fallback: 2% per ATR
                
                # P34: asymmetric SL — will be computed at entry time
                # Store base for per-side calculation
                sl_pct = sl_pct_base  # placeholder, overridden at entry
                tp_pct = 2.0 * atr_pct / 100 if atr_pct > 0.1 else 0.04"""

c = c.replace(old_sl_calc, new_sl_calc)
print("4. SL calculation modified for P34")

# Now modify the entry code to use SL_ATR_MULT_LONG/SHORT
old_entry_sl = """                if sig["side"] == "long":
                    sl_price = entry_price * (1 - sig["sl_pct"])
                    tp_price = entry_price * (1 + sig["tp_pct"])
                else:
                    sl_price = entry_price * (1 + sig["sl_pct"])
                    tp_price = entry_price * (1 - sig["tp_pct"])"""

new_entry_sl = """                # P34: asymmetric SL multiplier
                if sig["side"] == "long":
                    actual_sl_pct = SL_ATR_MULT_LONG * sig["sl_pct"]
                    sl_price = entry_price * (1 - actual_sl_pct)
                    tp_price = entry_price * (1 + sig["tp_pct"])
                else:
                    actual_sl_pct = SL_ATR_MULT_SHORT * sig["sl_pct"]
                    sl_price = entry_price * (1 + actual_sl_pct)
                    tp_price = entry_price * (1 - sig["tp_pct"])"""

c = c.replace(old_entry_sl, new_entry_sl)
print("5. Entry SL uses SL_ATR_MULT_LONG/SHORT")

# ════════════════════════════════════════════════════════════
# 6. Store composite_score in BacktestPosition at entry
# ════════════════════════════════════════════════════════════
old_position_create = """                position = BacktestPosition(
                    symbol=sym,
                    side=sig["side"],
                    entry_price=entry_price,
                    size_usd=position_size,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    timestamp=timestamp,
                    entry_atr=sig.get("atr", 0.0)  # pass ATR at entry for trailing (synced with live system)
                )"""

new_position_create = """                position = BacktestPosition(
                    symbol=sym,
                    side=sig["side"],
                    entry_price=entry_price,
                    size_usd=position_size,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    timestamp=timestamp,
                    entry_atr=sig.get("atr", 0.0)  # pass ATR at entry for trailing (synced with live system)
                )
                position.composite_score = sig.get("composite", 0.0)  # P8: store entry signal quality"""

c = c.replace(old_position_create, new_position_create)
print("6. composite_score stored at position entry")

# ════════════════════════════════════════════════════════════
# Write and verify
# ════════════════════════════════════════════════════════════
with open(filepath, 'w', encoding='utf-8') as f:
    f.write(c)

print("\nAll P34 modifications applied!")

import py_compile
try:
    py_compile.compile(filepath, doraise=True)
    print("SYNTAX: OK")
except py_compile.PyCompileError as e:
    print(f"SYNTAX ERROR: {e}")

# Verify key additions
checks = {
    'SL_ATR_MULT_LONG = 3.5': 'P34 LONG SL constant',
    'SL_ATR_MULT_SHORT = 1.5': 'P34 SHORT SL constant', 
    'TSL_ATR_LONG_STEP0 = 0': 'P34 TSL step0 disabled',
    'TSL_ATR_LONG_AFTER_TP1 = 2.0': 'P34 TSL after TP1',
    'composite_score': 'P8 composite score',
    'peak_price': 'P8 peak tracking',
    'actual_sl_pct = SL_ATR_MULT_LONG': 'P34 entry SL LONG',
    'actual_sl_pct = SL_ATR_MULT_SHORT': 'P34 entry SL SHORT',
}

print("\nVerification:")
for kw, desc in checks.items():
    if kw in c:
        print(f"  OK: {desc}")
    else:
        print(f"  MISSING: {desc} ({kw})")