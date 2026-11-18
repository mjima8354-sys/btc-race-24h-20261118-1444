#!/usr/bin/env python3
"""cbBTC 1-minute bar entry bot for make.fun btc-race-24h-20261118-1444.

Combines short-window momentum/breakout with mean-reversion entries,
ATR-based sizing and stop-loss, trailing stop, breakeven move, and
a strict trade throttle for deterministic positive carry.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# ================================================================= indicators
EMA_FAST = 10          # fast EMA half-life (bars)
EMA_SLOW = 30          # slow EMA half-life (bars)
ATR_PERIOD = 14        # Wilder ATR lookback
SLOPE_PERIOD = 60      # bars over which EMA slope is measured
LOOKBACK = 60          # history window for z-score / regime
MIN_HISTORY = 60       # warm-up bars before trading

# ================================================================= regime / entries
TREND_SLOPE_BPS = 150_000      # EFA fast slope bps over SLOPE_PERIOD = trend
RANGE_SLOPE_BPS = 40_000       # below this -> range regime
Z_ENTRY = -1800                # mean-reversion entry: z <= -1.8 sigma
Z_DEEP = -2400                 # deep capitulation bounce: z <= -2.4 sigma
Z_PULLBACK_MAX =  800          # momentum pullback: 0 <= z <= +0.8 sigma

# ================================================================= risk / stops
RISK_PCT = 18                  # base equity risk per trade (percent)
REF_ATR = 3_500_000            # reference ATR for sizing
SL_ATR_MULT = 6                # initial stop = entry - SL_ATR_MULT * ATR
BE_TRIGGER_ATR = 2.5           # move stop to breakeven after >= 2.5 ATR profit
TRAIL_TRIGGER_ATR = 3.5        # start trailing after >= 3.5 ATR profit
TRAIL_ATR_MULT = 1.5           # trailing stop = peak - TRAIL_ATR_MULT * ATR
TIME_STOP_BARS = 180           # hard exit after 180 bars in a position
MAX_POS_FRAC = 38              # cap position size at 38% of equity
MAX_SLIPPAGE_BPS = 50

# ================================================================= throttle
WINDOW_BARS = 1440             # rolling 24h window (1m bars)
MAX_TRADES_PER_WINDOW = 5      # hard cap: max 5 trades per 24h
COOLDOWN_BARS = 70             # min bars between trade exits and next entry

SCALE = 1_000_000_000
MILLI = 1_000


class BotState:
    """Indicators, position lifecycle, and risk controls (fixed-point ints)."""

    def __init__(self):
        self.closes = []
        self.highs = []
        self.lows = []
        self.prev_close = None
        self.bar_index = 0
        self.ema_fast = None
        self.ema_slow = None
        self.ema_history = []      # fast EMA samples for slope
        self.sma = 0
        self.std_dev = 0
        self.atr = None            # Wilder ATR (price units)
        self.atr_seeded = 0
        self.trade_record = None   # open-position bookkeeping
        self.cooldown_end = -1
        self.trade_count = 0       # completed trades in the rolling window
        self.trade_times = []      # bar indices of completed trades (exits)
        self.alpha_fast = 2 * MILLI // (EMA_FAST + 1)
        self.alpha_slow = 2 * MILLI // (EMA_SLOW + 1)

    # ---- indicator updates -------------------------------------------------
    def update_indicators(self, bar):
        self.bar_index += 1
        o = int(bar["o"]); h = int(bar["h"]); l = int(bar["l"]); c = int(bar["c"])
        self.closes.append(c)
        self.highs.append(h)
        self.lows.append(l)
        if len(self.closes) > LOOKBACK:
            self.closes.pop(0); self.highs.pop(0); self.lows.pop(0)

        # EMAs
        if self.ema_fast is None:
            self.ema_fast = c
            self.ema_slow = c
        else:
            self.ema_fast += (c - self.ema_fast) * self.alpha_fast // MILLI
            self.ema_slow += (c - self.ema_slow) * self.alpha_slow // MILLI
        self.ema_history.append(self.ema_fast)
        if len(self.ema_history) > SLOPE_PERIOD + 1:
            self.ema_history.pop(0)

        # SMA / std-dev for z-score
        n = len(self.closes)
        self.sma = sum(self.closes) // n
        variance = sum((x - self.sma) ** 2 for x in self.closes) // n
        self.std_dev = int(variance ** 0.5)

        # Wilder ATR
        tr = h - l
        if self.prev_close is not None:
            tr = max(tr, abs(h - self.prev_close), abs(l - self.prev_close))
        self.prev_close = c
        if self.atr is None:
            self.atr_seeded += 1
            if self.atr_seeded == 1:
                self.atr = tr
            else:
                self.atr = (self.atr * (self.atr_seeded - 1) + tr) // self.atr_seeded
        elif self.atr_seeded >= ATR_PERIOD:
            self.atr = (self.atr * (ATR_PERIOD - 1) + tr) // ATR_PERIOD
        else:
            self.atr = (self.atr * (self.atr_seeded - 1) + tr) // self.atr_seeded
            self.atr_seeded += 1

    def calculate_slope(self) -> int:
        """Fast-EMA slope in bps over SLOPE_PERIOD bars."""
        if (len(self.ema_history) < SLOPE_PERIOD + 1 or self.ema_history[0] <= 0
                or self.ema_fast <= 0):
            return 0
        return (self.ema_fast - self.ema_history[0]) * SCALE // self.ema_history[0]

    def z_score(self, price: int) -> int:
        if self.std_dev <= 0:
            return 0
        return (price - self.sma) * MILLI // self.std_dev

    def regime(self) -> str:
        """'trend_up', 'trend_down' or 'range'."""
        slope = self.calculate_slope()
        if slope >= TREND_SLOPE_BPS and self.ema_fast > self.ema_slow:
            return "trend_up"
        if slope <= -TREND_SLOPE_BPS and self.ema_fast < self.ema_slow:
            return "trend_down"
        return "range"

    def trades_in_window(self) -> int:
        cutoff = self.bar_index - WINDOW_BARS
        while self.trade_times and self.trade_times[0] <= cutoff:
            self.trade_times.pop(0)
        return len(self.trade_times)

    # ---- open-position management -----------------------------------------
    def manage_position(self, price: int, slope: int, rec):
        if price > rec["peak_price"]:
            rec["peak_price"] = price

        atr = max(rec["atr"], 1)
        gain_milli = (price - rec["entry_price"]) * MILLI // atr
        peak_gain_milli = (rec["peak_price"] - rec["entry_price"]) * MILLI // atr

        # Base stop: entry - SL_ATR_MULT * ATR
        sl_price = rec["entry_price"] - int(SL_ATR_MULT * atr)

        # Breakeven: once >= BE_TRIGGER_ATR profit, stop no worse than entry
        if peak_gain_milli >= int(BE_TRIGGER_ATR * MILLI) or gain_milli >= int(BE_TRIGGER_ATR * MILLI):
            sl_price = max(sl_price, rec["entry_price"])

        # ATR trailing stop: once >= TRAIL_TRIGGER_ATR profit, trail from peak
        if peak_gain_milli >= int(TRAIL_TRIGGER_ATR * MILLI):
            trailing = rec["peak_price"] - int(TRAIL_ATR_MULT * atr)
            sl_price = max(sl_price, trailing)

        exit_triggered = False
        if price <= sl_price:
            exit_triggered = True
        elif rec["kind"] == "momentum" and slope <= -TREND_SLOPE_BPS:
            exit_triggered = True        # regime flip: trend broke down
        elif self.bar_index - rec["entry_bar"] >= TIME_STOP_BARS:
            exit_triggered = True        # time stop

        return exit_triggered, sl_price


STATE = BotState()


def on_batch(state, batch):
    st = STATE
    price = state.close(SYMBOL)
    if price is None:
        return []

    try:
        st.update_indicators(batch["bars"][0])
    except Exception:
        return []

    orders = []
    position = state.positions.get(SYMBOL)
    slope = st.calculate_slope()
    regime_name = st.regime()

    # 1. Manage active position (ATR stop, breakeven, trailing, time stop)
    if position is not None and position.qty > 0 and st.trade_record is not None:
        rec = st.trade_record
        if rec["entry_price"] is None:
            rec["entry_price"] = int(batch["bars"][0]["o"])
            rec["peak_price"] = rec["entry_price"]
        exit_triggered, _sl_price = st.manage_position(price, slope, rec)
        if exit_triggered:
            orders.append({
                "side": "sell",
                "symbol": SYMBOL,
                "qty": position.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            st.trade_times.append(st.bar_index)
            st.trade_count += 1
            st.cooldown_end = st.bar_index + COOLDOWN_BARS
            st.trade_record = None
        return orders

    # 2. Entry gate: flat, history ready, not cooling down, within throttle
    if st.trade_record is not None or (position is not None and position.qty > 0):
        return orders
    if st.bar_index < MIN_HISTORY or st.atr is None or st.atr <= 0 or st.std_dev <= 0:
        return orders
    if st.bar_index < st.cooldown_end:
        return orders
    if st.trades_in_window() >= MAX_TRADES_PER_WINDOW:
        return orders

    equity = state.equity
    if equity <= 0:
        return orders

    # ---- ensemble signals --------------------------------------------------
    close = price
    z = st.z_score(close)
    is_uptick = len(st.closes) >= 3 and st.closes[-1] > st.closes[-2] > st.closes[-3]
    is_reclaim = len(st.closes) >= 2 and st.closes[-1] > st.closes[-2]
    is_roc_positive = len(st.closes) >= 5 and st.closes[-1] > st.closes[-5]

    signal_kind = None

    if regime_name == "range":
        # Mean reversion: stretched below SMA with a short-term reclaim
        if z <= Z_ENTRY and is_reclaim and st.closes[-2] <= st.sma:
            signal_kind = "mean_reversion"
        # Deep capitulation bounce works in any regime
        elif z <= Z_DEEP and is_uptick:
            signal_kind = "mean_reversion"
    elif regime_name == "trend_up":
        # Momentum pullback to the fast EMA in an uptrend
        if 0 <= z <= Z_PULLBACK_MAX and is_uptick and is_roc_positive:
            signal_kind = "momentum"
        # Deep oversold bounce even inside an uptrend
        elif z <= Z_DEEP and is_uptick:
            signal_kind = "mean_reversion"

    if signal_kind is None:
        return orders

    # ---- ATR-based position sizing (volatility normalised) ----------------
    base_risk = equity * RISK_PCT // 100                     # quote units
    notional = base_risk * REF_ATR // max(st.atr, 1)          # ∝ 1/ATR
    notional = min(notional, equity * MAX_POS_FRAC // 100)   # cap position size
    room = state.weight_room(SYMBOL)
    if room > 0:
        notional = min(notional, room)
    if notional > state.cash:
        notional = state.cash

    min_notional = int(state.hello["fees"]["min_notional_quote_units"])
    if notional < min_notional:
        return orders

    orders.append({
        "side": "buy",
        "symbol": SYMBOL,
        "notional_quote_units": notional,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    })
    st.trade_record = {
        "entry_price": None,
        "peak_price": None,
        "atr": st.atr,
        "entry_bar": st.bar_index,
        "kind": signal_kind,
    }
    return orders


def on_done(state, done):
    pass


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_done=on_done)