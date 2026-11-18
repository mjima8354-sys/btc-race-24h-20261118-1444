#!/usr/bin/env python3
"""cbBTC trend-follower for make.fun btc-race-24h-20261118-1444 (bars-replay/v1).

EMA(10) > EMA(90) and EMA(10) rising over 120 bars gates entries; full-equity
buy, 0.7% trailing stop or EMA exit, 30-bar cooldown. Stdlib only, deterministic.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
EMA_FAST = 10
EMA_SLOW = 90
RISING_LOOKBACK = 120
TRAIL_PCT = 0.007
EMA_EXIT_SLACK = 0.995
COOLDOWN_BARS = 30
FRAC_NUM = 100
FRAC_DEN = 100


class BotState:
    def __init__(self):
        self.closes = []
        self.ema_f = None
        self.ema_s = None
        self.pending = False
        self.peak = None
        self.cool = 0


def on_batch(state, batch):
    if not hasattr(on_batch, "st"):
        on_batch.st = BotState()
    st = on_batch.st
    bar = batch["bars"][0]
    c = int(bar["c"])
    fills = batch.get("fills") or []

    if st.pending and fills:
        for f in fills:
            if f.get("status") == "filled":
                st.pending = False
                st.peak = c
                break
        else:
            st.pending = False

    st.closes.append(float(c))
    if len(st.closes) > RISING_LOOKBACK + EMA_SLOW + 10:
        st.closes.pop(0)

    kf = 2.0 / (EMA_FAST + 1)
    ks = 2.0 / (EMA_SLOW + 1)
    if st.ema_f is None:
        st.ema_f = float(c)
        st.ema_s = float(c)
    else:
        st.ema_f += kf * (c - st.ema_f)
        st.ema_s += ks * (c - st.ema_s)

    pos = state.positions.get(SYMBOL)
    qty = int(pos.qty) if pos is not None else 0
    price = float(state.close(SYMBOL))

    orders = []
    if st.cool > 0:
        st.cool -= 1

    if qty > 0:
        if st.peak is None:
            st.peak = price
        st.peak = max(st.peak, price)
        if price <= st.peak * (1.0 - TRAIL_PCT) or price < st.ema_f * EMA_EXIT_SLACK:
            orders.append({
                "side": "sell",
                "symbol": SYMBOL,
                "qty": qty,
                "max_slippage_bps": 50,
            })
            st.cool = COOLDOWN_BARS
            st.peak = None
            return orders
        return orders

    if st.pending or st.cool > 0 or len(st.closes) < EMA_SLOW + RISING_LOOKBACK + 5:
        return orders

    closes = st.closes
    hh_ok = price > max(closes[-RISING_LOOKBACK - 1:-1])
    rising_ok = st.ema_f > closes[-RISING_LOOKBACK - 1]
    trend_ok = st.ema_f > st.ema_s
    if not (hh_ok and rising_ok and trend_ok):
        return orders

    equity = int(state.equity)
    cash = int(state.cash)
    notional = min(equity * FRAC_NUM // FRAC_DEN, cash)
    min_notional = int(state.hello["fees"]["min_notional_quote_units"])
    if notional < min_notional:
        return orders
    st.pending = True
    st.peak = None
    orders.append({
        "side": "buy",
        "symbol": SYMBOL,
        "notional_quote_units": notional,
        "max_slippage_bps": 50,
    })
    return orders


def on_done(state, done):
    pass


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_done=on_done)