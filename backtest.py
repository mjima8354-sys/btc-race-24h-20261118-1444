#!/usr/bin/env python3
"""Local backtest driver for the bars-replay/v1 bot in main.py.

Replays a gzipped NDJSON bar file (the kit's dataset/bars/<SYM>.ndjson.gz format) through the
agent-fun/bars-replay/v1 protocol, runs the bot (main.py) as a subprocess the same way the grader
does (python3 -P -s main.py, PYTHONHASHSEED=0), and fills orders with the harness's market model:

  buy:  spend = notional * (10000 - fee_bps) / 10000        (fee charged on the notional)
        price = open * (10000 + slippage_bps) * M / 10000   (M = 1 + a/D, a = spend, D = o_depth)
  sell: price = max(1, open * (10000 - slippage_bps) / (10000 * M))  (a = qty valued at the open)
        gross = qty * price; proceeds = gross * (10000 - fee_bps) / 10000; fee = gross - proceeds

Everything still held at the end is flattened at the last close against that close's depth.
Prints net PnL (USDC), trades, fees, max drawdown and an equity curve summary.

Usage: python3 backtest.py [--bars kit/dataset/bars/CBBTC.ndjson.gz] [--bot main.py] [--verbose]
Deterministic: no clock, no network, no unseeded randomness.
"""

import argparse
import gzip
import json
import subprocess
import sys
from decimal import Decimal

BPS = 10_000
QUOTE_SCALE = 6
PRICE_SCALE = 5
QTY_SCALE = 8
FEE_BPS = 4
SLIP_BPS = 2
MIN_NOTIONAL = 1_000_000
START_UNITS = 1_000_000_000  # 1,000 USDC in micro-USDC
SYMBOL = "CBBTC"


def load_bars(path):
    bars = []
    with gzip.open(path, "rt") as f:
        for line in f:
            line = line.strip()
            if line:
                bars.append(json.loads(line))
    return bars


def bars_hello(first_close_ms, seed="0"):
    return {
        "type": "hello",
        "protocol": "agent-fun/bars-replay/v1",
        "seed": seed,
        "symbols": [{"symbol": SYMBOL, "mint": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
                     "price_scale": PRICE_SCALE, "qty_scale": QTY_SCALE}],
        "capital": {"quote": "USDC", "quote_scale": QUOTE_SCALE, "quote_units": str(START_UNITS)},
        "bar_interval": "1m",
        "first_close_ms": first_close_ms,
        "caps": {"max_open_positions": 1, "max_orders_per_bar": 16, "max_weight_bps": 10000,
                 "shorts": False, "leverage": False},
        "fees": {"fee_bps": FEE_BPS, "slippage_bps": SLIP_BPS, "min_notional_quote_units": str(MIN_NOTIONAL)},
        "latency_bars": 1,
    }


class Pool:
    """A constant-product pool in USDC raw units (depth D per hop)."""

    def __init__(self, depth):
        self.d = depth

    def m(self, a):
        return Decimal(1) + Decimal(a) / Decimal(self.d)


def buy_fill(notional, bar, pool):
    spend = notional * (BPS - FEE_BPS) // BPS
    fee = notional - spend
    m = pool.m(spend)
    price = int(int(bar["o"]) * (BPS + SLIP_BPS) * m * Decimal(10 ** PRICE_SCALE)
                // (BPS * Decimal(10 ** PRICE_SCALE)))
    price = int(Decimal(int(bar["o"])) * (BPS + SLIP_BPS) * m / BPS)
    qty = spend * 10 ** (QTY_SCALE + PRICE_SCALE) // (price * 10 ** PRICE_SCALE)
    return {"fee": fee, "price": price, "qty": qty}


def sell_fill(qty, bar, pool):
    a = qty * int(bar["o"]) // 10 ** (QTY_SCALE + PRICE_SCALE)
    m = pool.m(a)
    price = max(1, int(Decimal(int(bar["o"])) * (BPS - SLIP_BPS) / (BPS * m)))
    gross = qty * price // 10 ** PRICE_SCALE
    proceeds = gross * (BPS - FEE_BPS) // BPS
    return {"fee": gross - proceeds, "proceeds": proceeds, "price": price}


def run_bot(bot_path, hello, batches):
    proc = subprocess.Popen(
        [sys.executable, "-P", "-s", bot_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
        env={"PYTHONHASHSEED": "0", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    lines = [json.dumps(hello, separators=(",", ":"))]
    lines += [json.dumps(b, separators=(",", ":")) for b in batches]
    out, _ = proc.communicate(("\n".join(lines) + "\n").encode())
    if proc.returncode != 0:
        raise SystemExit(f"bot exited with {proc.returncode}")
    replies = [json.loads(l) for l in out.decode().splitlines() if l.strip()]
    return replies


def backtest(bars_path, bot_path, verbose=False):
    bars = load_bars(bars_path)
    if len(bars) < 30:
        raise SystemExit("need at least 30 bars")
    hello = bars_hello(int(bars[0]["t"]))

    batches, pending, account = [], [], {"cash": START_UNITS, "qty": 0, "cost": 0}
    for i, bar in enumerate(bars):
        fills = []
        for o in pending:
            if o["side"] == "buy":
                pool = Pool(int(bar["o_depth"][0]))
                f = buy_fill(int(o["notional_quote_units"]), bar, pool)
                if f["qty"] > 0 and account["cash"] >= int(o["notional_quote_units"]):
                    account["cash"] -= int(o["notional_quote_units"])
                    account["qty"] += f["qty"]
                    account["cost"] += int(o["notional_quote_units"])
                    fills.append({"client_id": o["client_id"], "status": "filled", "fee": f["fee"],
                                  "qty": str(f["qty"]), "price": str(f["price"])})
                else:
                    fills.append({"client_id": o["client_id"], "status": "rejected",
                                  "reason": "insufficient_cash" if f["qty"] == 0 else "zero_amount"})
            else:
                held = account["qty"]
                if held <= 0:
                    fills.append({"client_id": o["client_id"], "status": "rejected", "reason": "insufficient_position"})
                    continue
                qty = min(held, int(o["qty"]))
                pool = Pool(int(bar["o_depth"][0]))
                f = sell_fill(qty, bar, pool)
                account["qty"] -= qty
                account["cash"] += f["proceeds"]
                fills.append({"client_id": o["client_id"], "status": "filled", "fee": f["fee"],
                              "qty": str(qty), "price": str(f["price"])})
        batch = {"type": "batch", "seq": i, "t": int(bar["t"]),
                 "bars": [{"symbol": SYMBOL, **{k: bar[k] for k in ("o", "h", "l", "c", "o_depth", "c_depth")}}],
                 "fills": fills,
                 "account": {"cash_quote_units": str(account["cash"]),
                             "positions": ([{"symbol": SYMBOL, "qty": str(account["qty"]),
                                             "cost_quote_units": str(account["cost"])}] if account["qty"] else []),
                             "equity_quote_units": str(account["cash"] + account["qty"] * int(bar["c"]) // 10 ** PRICE_SCALE)}}
        batches.append(batch)

    done = {"type": "done", "seq": len(batches),
            "account": {"cash_quote_units": str(account["cash"]), "positions": [],
                        "equity_quote_units": str(account["cash"])}}
    # Deterministic final flatten happens in the scorer; here we sell at the last close's depth.
    replies = run_bot(bot_path, hello, batches + [done])
    by_seq = {r["seq"]: r for r in replies}

    # Replay the bot's orders through the model to score (fills computed above used the bot's
    # previous replies only if pending was updated; redo faithfully: run once collecting replies,
    # then re-run the loop feeding each reply's orders as the next batch's pending orders).
    pending, account = [], {"cash": START_UNITS, "qty": 0, "cost": 0}
    fees_paid = trades = 0
    equity_curve = []
    for i, bar in enumerate(bars):
        for o in pending:
            if o["side"] == "buy":
                notional = int(o["notional_quote_units"])
                if notional < MIN_NOTIONAL or account["cash"] < notional:
                    continue
                pool = Pool(int(bar["o_depth"][0]))
                f = buy_fill(notional, bar, pool)
                if f["qty"] == 0:
                    continue
                account["cash"] -= notional
                account["qty"] += f["qty"]
                account["cost"] += notional
                fees_paid += f["fee"]
                trades += 1
            else:
                if account["qty"] <= 0:
                    continue
                qty = min(account["qty"], int(o["qty"]))
                pool = Pool(int(bar["o_depth"][0]))
                f = sell_fill(qty, bar, pool)
                account["qty"] -= qty
                account["cash"] += f["proceeds"]
                fees_paid += f["fee"]
                trades += 1
        pending = by_seq.get(i, {}).get("orders", [])
        eq = account["cash"] + account["qty"] * int(bar["c"]) // 10 ** PRICE_SCALE
        equity_curve.append(eq)

    # End-of-window flatten at the last close, same fee and slippage, against the close's depth.
    if account["qty"]:
        pool = Pool(int(bars[-1]["c_depth"][0]))
        f = sell_fill(account["qty"], {"o": bars[-1]["c"]}, pool)
        account["cash"] += f["proceeds"]
        fees_paid += f["fee"]
        trades += 1
        account["qty"] = 0
    equity_curve[-1] = account["cash"]

    net = account["cash"] - START_UNITS
    peak, mdd = START_UNITS, 0
    for eq in equity_curve:
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    usd = Decimal(net) / Decimal(10 ** QUOTE_SCALE)
    print(f"bars={len(bars)} trades={trades} net_pnl={usd} USDC  "
          f"max_drawdown={mdd:.4%}  fees={Decimal(fees_paid) / 10**QUOTE_SCALE} USDC  "
          f"start=1000 end={Decimal(account['cash']) / 10**QUOTE_SCALE} USDC")
    if verbose:
        for i in range(0, len(equity_curve), max(1, len(equity_curve) // 24)):
            print(f"  bar {i:5d} equity={Decimal(equity_curve[i]) / 10**QUOTE_SCALE:.2f} USDC")
    return net


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", default="vendor/../data/CBBTC.ndjson.gz")
    ap.add_argument("--bot", default="main.py")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    backtest(a.bars, a.bot, a.verbose)
