from __future__ import annotations

import argparse
import csv
import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

import matplotlib.pyplot as plt


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "outputs"
FIGURE_DIR = OUTPUT_DIR / "figures"

# GeckoTerminal identifies the Raydium SOL/USDC pool by this address.
API_ROOT = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"
POOL_ADDRESS = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
POOL_LABEL = "Raydium SOL/USDC AMM v4"
GITHUB_URL = "https://github.com/ggiordann/solana"

# This is the two-week window used in the paper.
START_UTC = datetime(2026, 5, 7, tzinfo=timezone.utc)
END_UTC = datetime(2026, 5, 20, 23, tzinfo=timezone.utc)
FEE_RATE = 0.0025
PARTICIPATION_RATES = (0.01, 0.10, 0.50)


def fetch_json(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    # Keep the API call in one place so the rest of the script can work with dictionaries.
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    request = urllib.request.Request(
        f"{API_ROOT}{path}{query}",
        headers={
            "Accept": "application/json;version=20230302",
            "User-Agent": "math1020-solana-slippage-backtest/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def as_float(value: Any) -> float:
    if value in (None, ""):
        return math.nan
    return float(value)


def percentile(values: list[float], p: float) -> float:
    # Linear interpolation gives a smoother percentile than simply rounding to an index.
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def curve_slippage_bps(relative_trade_size: float) -> float:
    # For xy = k, curve slippage only depends on trade size relative to one pool side.
    return 1e4 * relative_trade_size / (1.0 + relative_trade_size)


def fee_inclusive_cost_bps(relative_trade_size: float, fee_rate: float = FEE_RATE) -> float:
    # Raydium's 0.25% fee is added here so it can be compared with curve-only slippage.
    gamma = 1.0 - fee_rate
    return 1e4 * (1.0 - gamma / (1.0 + gamma * relative_trade_size))


def parse_pool_snapshot() -> dict[str, Any]:
    # The reserve snapshot is used as a fixed depth benchmark for the replay.
    payload = fetch_json(f"/networks/{NETWORK}/pools/{POOL_ADDRESS}")
    attributes = payload["data"]["attributes"]
    reserve_usd = as_float(attributes["reserve_in_usd"])
    return {
        "pool_name": attributes["name"],
        "pool_label": POOL_LABEL,
        "pool_address": attributes["address"],
        "reserve_usd_snapshot": reserve_usd,
        "one_side_reserve_usd_snapshot": reserve_usd / 2.0,
        "base_token_price_usd_snapshot": as_float(attributes["base_token_price_usd"]),
        "quote_token_price_usd_snapshot": as_float(attributes["quote_token_price_usd"]),
        "volume_usd_h24_snapshot": as_float(attributes["volume_usd"]["h24"]),
    }


def parse_ohlcv_window() -> list[dict[str, Any]]:
    # GeckoTerminal returns candles before a timestamp, so request just after the end of the window.
    before_timestamp = int((END_UTC + timedelta(hours=1)).timestamp())
    payload = fetch_json(
        f"/networks/{NETWORK}/pools/{POOL_ADDRESS}/ohlcv/hour",
        {
            "aggregate": "1",
            "limit": "400",
            "currency": "usd",
            "before_timestamp": str(before_timestamp),
        },
    )
    rows: list[dict[str, Any]] = []
    for timestamp, open_, high, low, close, volume in payload["data"]["attributes"]["ohlcv_list"]:
        dt = datetime.fromtimestamp(timestamp, timezone.utc)
        if START_UTC <= dt <= END_UTC:
            rows.append(
                {
                    "timestamp_utc": dt.isoformat(),
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume_usd": float(volume),
                }
            )
    rows.sort(key=lambda row: row["timestamp_utc"])
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def load_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as handle:
        for row in csv.DictReader(handle):
            parsed: dict[str, Any] = {}
            for key, value in row.items():
                if key in {"timestamp_utc", "scenario"}:
                    parsed[key] = value
                else:
                    try:
                        parsed[key] = float(value)
                    except ValueError:
                        parsed[key] = value
            rows.append(parsed)
    return rows


def build_backtest_rows(ohlcv: list[dict[str, Any]], one_side_reserve_usd: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candle in ohlcv:
        for participation in PARTICIPATION_RATES:
            # A 10% participation trade means "trade 10% of that hour's observed volume".
            trade_usd = candle["volume_usd"] * participation
            relative_trade_size = trade_usd / one_side_reserve_usd
            rows.append(
                {
                    "timestamp_utc": candle["timestamp_utc"],
                    "participation_rate": participation,
                    "scenario": f"{int(participation * 100)}pct_hourly_volume",
                    "hourly_volume_usd": candle["volume_usd"],
                    "trade_usd": trade_usd,
                    "relative_size_pct": 100.0 * relative_trade_size,
                    "curve_slippage_bps": curve_slippage_bps(relative_trade_size),
                    "fee_inclusive_cost_bps": fee_inclusive_cost_bps(relative_trade_size),
                }
            )
    return rows


def aggregate_daily_ohlcv(ohlcv: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # The figure uses daily candles so the price panel is readable in the paper.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in ohlcv:
        day = row["timestamp_utc"][:10]
        grouped.setdefault(day, []).append(row)
    daily: list[dict[str, Any]] = []
    for day, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: row["timestamp_utc"])
        daily.append(
            {
                "day": day,
                "open": rows[0]["open"],
                "high": max(row["high"] for row in rows),
                "low": min(row["low"] for row in rows),
                "close": rows[-1]["close"],
                "volume_usd": sum(row["volume_usd"] for row in rows),
            }
        )
    return daily


def scenario_stats(backtest_rows: list[dict[str, Any]], participation: float) -> dict[str, float]:
    rows = [row for row in backtest_rows if abs(row["participation_rate"] - participation) < 1e-12]
    trade_usd = [row["trade_usd"] for row in rows]
    curve = [row["curve_slippage_bps"] for row in rows]
    fee = [row["fee_inclusive_cost_bps"] for row in rows]
    return {
        "median_trade_usd": median(trade_usd),
        "p95_trade_usd": percentile(trade_usd, 0.95),
        "max_trade_usd": max(trade_usd),
        "median_curve_slippage_bps": median(curve),
        "p95_curve_slippage_bps": percentile(curve, 0.95),
        "max_curve_slippage_bps": max(curve),
        "median_fee_inclusive_cost_bps": median(fee),
        "p95_fee_inclusive_cost_bps": percentile(fee, 0.95),
        "max_fee_inclusive_cost_bps": max(fee),
    }


def build_summary(pool: dict[str, Any], ohlcv: list[dict[str, Any]], backtest_rows: list[dict[str, Any]]) -> dict[str, Any]:
    volumes = [row["volume_usd"] for row in ohlcv]
    # Store the key statistics used directly in the report text.
    scenario_summary = {
        f"{int(participation * 100)}pct_hourly_volume": scenario_stats(backtest_rows, participation)
        for participation in PARTICIPATION_RATES
    }
    one_side = pool["one_side_reserve_usd_snapshot"]
    stress_values = [10_000, 100_000, 500_000]
    stress = {
        str(value): {
            "curve_slippage_bps": curve_slippage_bps(value / one_side),
            "fee_inclusive_cost_bps": fee_inclusive_cost_bps(value / one_side),
        }
        for value in stress_values
    }
    return {
        "accessed_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "GeckoTerminal public API",
        "github_url": GITHUB_URL,
        "network": NETWORK,
        **pool,
        "fee_rate": FEE_RATE,
        "start_utc": START_UTC.isoformat(),
        "end_utc": END_UTC.isoformat(),
        "ohlcv_hours": len(ohlcv),
        "daily_candles": len(aggregate_daily_ohlcv(ohlcv)),
        "total_volume_usd": sum(volumes),
        "median_hourly_volume_usd": median(volumes),
        "p95_hourly_volume_usd": percentile(volumes, 0.95),
        "max_hourly_volume_usd": max(volumes),
        "scenario_summary": scenario_summary,
        "stress_test": stress,
        "reserve_note": "The pool reserve is a GeckoTerminal snapshot used as a fixed depth benchmark for the historical OHLCV backtest.",
    }


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".json").open("w") as handle:
        json.dump(summary, handle, indent=2)
    ten_pct = summary["scenario_summary"]["10pct_hourly_volume"]
    fifty_pct = summary["scenario_summary"]["50pct_hourly_volume"]
    lines = [
        f"source={summary['source']}",
        f"github_url={summary['github_url']}",
        f"pool={summary['pool_label']} ({summary['pool_address']})",
        f"accessed_utc={summary['accessed_utc']}",
        f"window={summary['start_utc']} to {summary['end_utc']}",
        f"ohlcv_hours={summary['ohlcv_hours']}",
        f"reserve_usd_snapshot={summary['reserve_usd_snapshot']:.2f}",
        f"one_side_reserve_usd_snapshot={summary['one_side_reserve_usd_snapshot']:.2f}",
        f"total_volume_usd={summary['total_volume_usd']:.2f}",
        f"median_hourly_volume_usd={summary['median_hourly_volume_usd']:.2f}",
        f"p95_hourly_volume_usd={summary['p95_hourly_volume_usd']:.2f}",
        f"10pct_median_curve_slippage_bps={ten_pct['median_curve_slippage_bps']:.4f}",
        f"10pct_p95_curve_slippage_bps={ten_pct['p95_curve_slippage_bps']:.4f}",
        f"50pct_median_curve_slippage_bps={fifty_pct['median_curve_slippage_bps']:.4f}",
        f"50pct_p95_curve_slippage_bps={fifty_pct['p95_curve_slippage_bps']:.4f}",
        f"50pct_max_curve_slippage_bps={fifty_pct['max_curve_slippage_bps']:.4f}",
    ]
    path.with_suffix(".txt").write_text("\n".join(lines) + "\n")


def write_backtest_figure(path: Path, summary: dict[str, Any], ohlcv: list[dict[str, Any]], backtest_rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.size": 11,
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "axes.edgecolor": "#102336",
            "axes.labelcolor": "#102336",
            "xtick.labelsize": 9,
            "xtick.color": "#102336",
            "ytick.labelsize": 9,
            "ytick.color": "#102336",
            "legend.fontsize": 9,
        }
    )
    fig, axes = plt.subplots(2, 2, figsize=(13.2, 8.4), dpi=180)
    candle_ax, series_ax, hist_ax, stress_ax = axes.ravel()

    # Panel 1: daily candles give the reader a quick view of the market over the sample.
    daily = aggregate_daily_ohlcv(ohlcv)
    max_volume = max(day["volume_usd"] for day in daily)
    price_min = min(day["low"] for day in daily)
    price_span = max(day["high"] for day in daily) - price_min
    volume_scale = price_span * 0.22 / max_volume
    for index, day in enumerate(daily):
        colour = "#0E7C66" if day["close"] >= day["open"] else "#B9463F"
        candle_ax.vlines(index, day["low"], day["high"], color=colour, linewidth=1.5)
        candle_ax.vlines(index, day["open"], day["close"], color=colour, linewidth=6.0)
        candle_ax.bar(index, day["volume_usd"] * volume_scale, bottom=price_min, color=colour, alpha=0.18, width=0.78)
    candle_ax.set_title("Raydium SOL/USDC daily candles")
    candle_ax.set_ylabel("SOL price (USD)")
    candle_ax.set_xticks(range(0, len(daily), 2))
    candle_ax.set_xticklabels([day["day"][5:] for day in daily[::2]])
    candle_ax.grid(color="#D7DEE6", linewidth=0.8, alpha=0.8)

    # Panel 2: the same formula is replayed each hour for three trade-size assumptions.
    timestamps = [datetime.fromisoformat(row["timestamp_utc"]).replace(tzinfo=timezone.utc) for row in ohlcv]
    x_values = [(dt - START_UTC).total_seconds() / 86400.0 for dt in timestamps]
    for participation, colour, label in [
        (0.01, "#145C9E", "1% of hourly volume"),
        (0.10, "#0E7C66", "10% of hourly volume"),
        (0.50, "#B9463F", "50% of hourly volume"),
    ]:
        values = [
            row["curve_slippage_bps"]
            for row in backtest_rows
            if abs(row["participation_rate"] - participation) < 1e-12
        ]
        series_ax.plot(x_values, values, color=colour, linewidth=1.6, label=label)
    series_ax.set_title("Backtested curve slippage by hour")
    series_ax.set_xlabel("Days from 7 May 2026")
    series_ax.set_ylabel("Curve slippage (bps)")
    series_ax.grid(color="#D7DEE6", linewidth=0.8, alpha=0.8)
    series_ax.legend(frameon=True, facecolor="white", edgecolor="#CCD3DA", fontsize=8, loc="upper left")

    # Panel 3: the histogram shows how often the 10% case creates small or larger slippage.
    ten_pct = [
        row["curve_slippage_bps"]
        for row in backtest_rows
        if abs(row["participation_rate"] - 0.10) < 1e-12
    ]
    hist_ax.hist(ten_pct, bins=28, color="#0E5A8A", alpha=0.82, edgecolor="white")
    hist_ax.axvline(median(ten_pct), color="#F2B84B", linewidth=2.2, label="Median")
    hist_ax.axvline(percentile(ten_pct, 0.95), color="#B9463F", linewidth=2.2, label="95th percentile")
    hist_ax.set_title("Distribution for 10% participation")
    hist_ax.set_xlabel("Curve slippage (bps)")
    hist_ax.set_ylabel("Number of hours")
    hist_ax.grid(axis="y", color="#D7DEE6", linewidth=0.8, alpha=0.8)
    hist_ax.legend(frameon=True, facecolor="white", edgecolor="#CCD3DA", fontsize=8)

    # Panel 4: fixed USD trades show how quickly costs rise as the trade gets larger.
    one_side = summary["one_side_reserve_usd_snapshot"]
    stress_usd = [100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000]
    stress_curve = [curve_slippage_bps(value / one_side) for value in stress_usd]
    stress_fee = [fee_inclusive_cost_bps(value / one_side) for value in stress_usd]
    stress_ax.plot(stress_usd, stress_curve, color="#145C9E", linewidth=2.4, label="Curve only")
    stress_ax.plot(stress_usd, stress_fee, color="#B9463F", linewidth=2.1, linestyle="--", label="With 0.25% fee")
    stress_ax.set_xscale("log")
    stress_ax.set_title("Stress test on the reserve snapshot")
    stress_ax.set_xlabel("Hypothetical swap notional (USD)")
    stress_ax.set_ylabel("Execution cost (bps)")
    stress_ax.grid(color="#D7DEE6", linewidth=0.8, alpha=0.8)
    stress_ax.legend(frameon=True, facecolor="white", edgecolor="#CCD3DA", fontsize=8, loc="upper left")

    fig.suptitle("Historical Solana Slippage Backtest: 7-20 May 2026", fontsize=16, fontweight="bold", color="#102336")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest CPMM slippage on historical Raydium SOL/USDC OHLCV data.")
    parser.add_argument("--refresh", action="store_true", help="Fetch the 7-20 May 2026 OHLCV window again from GeckoTerminal.")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)

    ohlcv_path = DATA_DIR / "geckoterminal_sol_usdc_ohlcv_2026-05-07_2026-05-20.csv"
    backtest_path = OUTPUT_DIR / "slippage_backtest.csv"
    summary_path = OUTPUT_DIR / "slippage_backtest_summary"
    summary_json_path = summary_path.with_suffix(".json")

    # Use the saved files by default so the result is reproducible without internet access.
    if args.refresh or not (ohlcv_path.exists() and backtest_path.exists() and summary_json_path.exists()):
        pool = parse_pool_snapshot()
        ohlcv = parse_ohlcv_window()
        backtest_rows = build_backtest_rows(ohlcv, pool["one_side_reserve_usd_snapshot"])
        summary = build_summary(pool, ohlcv, backtest_rows)
        write_csv(ohlcv_path, ohlcv)
        write_csv(backtest_path, backtest_rows)
        write_summary(summary_path, summary)
    else:
        ohlcv = load_csv(ohlcv_path)
        backtest_rows = load_csv(backtest_path)
        with summary_json_path.open() as handle:
            summary = json.load(handle)

    write_backtest_figure(FIGURE_DIR / "slippage_backtest.png", summary, ohlcv, backtest_rows)

    ten_pct = summary["scenario_summary"]["10pct_hourly_volume"]
    fifty_pct = summary["scenario_summary"]["50pct_hourly_volume"]
    print(f"source={summary['source']}")
    print(f"pool={summary['pool_label']}")
    print(f"hours={summary['ohlcv_hours']}")
    print(f"window={summary['start_utc']} to {summary['end_utc']}")
    print(f"10pct_median_curve_slippage_bps={ten_pct['median_curve_slippage_bps']:.4f}")
    print(f"50pct_p95_curve_slippage_bps={fifty_pct['p95_curve_slippage_bps']:.4f}")
    print(f"figure={FIGURE_DIR / 'slippage_backtest.png'}")


if __name__ == "__main__":
    main()
