# Solana Constant-Product Slippage Backtest

This repository supports the MATH 1020 report `ResearchPaper.tex`.

The workflow uses public GeckoTerminal data for the Raydium SOL/USDC AMM v4 pool and asks a
specific execution question: if a trader attempted to trade a fixed percentage of each hour's
observed volume, how large would the constant-product slippage have been?

## Main Command

Run from this directory:

```bash
python3 scripts/run_slippage_backtest.py
```

By default, the script uses the saved data set for 7 May 2026 through 20 May 2026. To fetch the
same window again from GeckoTerminal, run:

```bash
python3 scripts/run_slippage_backtest.py --refresh
```

## Outputs

- `data/geckoterminal_sol_usdc_ohlcv_2026-05-07_2026-05-20.csv`
- `outputs/slippage_backtest.csv`
- `outputs/slippage_backtest_summary.json`
- `outputs/slippage_backtest_summary.txt`
- `outputs/figures/slippage_backtest.png`

The public GitHub repository is <https://github.com/ggiordann/solana>.
