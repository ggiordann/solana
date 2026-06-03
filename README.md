# Solana Slippage Backtest

This is the code I used for my MATH 1020 research paper on slippage in a Solana
liquidity pool.

The script uses public GeckoTerminal data for the Raydium SOL/USDC pool. It takes
the hourly trading volume from 7 May 2026 to 20 May 2026 and checks how much
slippage a constant-product pool would create for a few different trade sizes.

## How to run it

Run from this directory:

```bash
python3 scripts/run_slippage_backtest.py
```

The saved data is already included, so the command should work without needing to
download anything new.

To fetch the same data window again from GeckoTerminal, run:

```bash
python3 scripts/run_slippage_backtest.py --refresh
```

## What it creates

- `data/geckoterminal_sol_usdc_ohlcv_2026-05-07_2026-05-20.csv`
- `outputs/slippage_backtest.csv`
- `outputs/slippage_backtest_summary.json`
- `outputs/slippage_backtest_summary.txt`
- `outputs/figures/slippage_backtest.png`

The main figure from the paper is `outputs/figures/slippage_backtest.png`.
