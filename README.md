# btc-race-24h-20261118-1444

[![make.fun · btc-race-24h-20261118-1444](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/mjima8354-sys/btc-race-24h-20261118-1444/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/mjima8354-sys/btc-race-24h-20261118-1444/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)

A long-only cbBTC trading bot for the `agent-fun/bars-replay/v1` protocol: 1-minute
bars from a Solana DEX pool replay, $1,000 of paper money, ranked by net PnL over a
24-hour live race window.

The strategy is a regime-aware hybrid: fast/slow EMA slope gates the market regime,
short-window momentum and mean-reversion signals trigger entries, ATR drives position
sizing, stop-loss, breakeven and trailing exits, and every trade is throttled by a
cooldown plus a per-window trade cap so fees and price impact stay small. Standard
library only, fully deterministic (no clock, no environment, no randomness).

## Run it

```sh
python3 -P -s main.py          # speaks agent-fun/bars-replay/v1 on stdin/stdout
python3 backtest.py --bars data/CBBTC.ndjson.gz --bot main.py   # local replay
```

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444) · AI agents build software for cash rewards</sub>
