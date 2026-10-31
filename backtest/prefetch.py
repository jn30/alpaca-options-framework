#!/usr/bin/env python3
"""Prefetch and cache SPY daily + 1-minute bars for the backtest suite."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config
from alpaca.data.historical import StockHistoricalDataClient
from backtest import engine as bt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2024, 2, 1))
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.date(2026, 10, 30))
    args = parser.parse_args()

    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)

    print(f"Daily bars {args.start} .. {args.end} ...", flush=True)
    daily, trading_days = bt.load_daily(client, args.start, args.end, use_cache=True)
    print(f"  {len(daily)} daily bars, {len(trading_days)} trading days", flush=True)

    print(f"Minute bars {args.start} .. {args.end} ...", flush=True)
    all_bars, bars_by_day = bt.load_minutes(client, args.start, args.end, use_cache=True)
    print(f"  {len(all_bars)} minute bars on {len(bars_by_day)} days", flush=True)

    missing = [d for d in trading_days if d >= args.start and d not in bars_by_day]
    if missing:
        print(f"  WARNING: {len(missing)} trading days without minute bars "
              f"(first: {missing[0]}, last: {missing[-1]})")
    print(f"Cache: {bt.CACHE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
