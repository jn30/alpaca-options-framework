#!/usr/bin/env python3
"""
Full period x variant sweep for the directional 0DTE strategy.

Runs every variant over the full available history once, then slices the
resulting trades into calendar periods, rolling windows and the live-dashboard
window. This is valid because the live risk manager resets every session and
each day is simulated independently.

Outputs (in backtest/results/):
  sweep_summary.csv   one row per (strategy, variant, period)
  sweep_daily.csv     daily P&L / equity per variant (full range)
  sweep_meta.json     config + variant definitions
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from backtest import engine as bt

FIRST = dt.date(2024, 2, 1)
LAST = dt.date(2026, 10, 30)
LIVE_START = dt.date(2026, 9, 30)
LIVE_END = dt.date(2026, 10, 30)


@dataclass
class Spec:
    name: str
    arg: dict = field(default_factory=dict)
    cfg: dict = field(default_factory=dict)


def build_specs() -> list[Spec]:
    return [
        Spec("baseline"),
        # Dashboard shadow-harness variants
        Spec("dash_tight_trail", cfg={"PEAK_TRAIL_PCT": 0.92}),
        Spec("dash_wide_stop", cfg={"STOP_MULT": 0.25, "TP_MULT": 1.0}),
        Spec("dash_both", cfg={"STOP_MULT": 0.25, "TP_MULT": 1.0,
                               "PEAK_TRAIL_PCT": 0.92}),
        Spec("no_spy_stop", cfg={"SPY_STOP_FLOOR": 999.0, "SPY_STOP_ATR_MULT": 0.0}),
        # Exits
        Spec("tp_2_0", cfg={"TP_MULT": 2.0}),
        Spec("tp_1_25", cfg={"TP_MULT": 1.25}),
        Spec("stop_0_4", cfg={"STOP_MULT": 0.40}),
        Spec("stop_0_6", cfg={"STOP_MULT": 0.60}),
        Spec("trail_0_85", cfg={"PEAK_TRAIL_PCT": 0.85}),
        Spec("trail_activate_1_5", cfg={"PEAK_TRAIL_ACTIVATE": 1.5}),
        Spec("spy_floor_0_5", arg={"spy_stop_floor": 0.5}),
        Spec("spy_floor_1_0", arg={"spy_stop_floor": 1.0}),
        Spec("spy_floor_1_5", arg={"spy_stop_floor": 1.5}),
        Spec("spy_floor_2_0", arg={"spy_stop_floor": 2.0}),
        Spec("spy_mult_1_5", arg={"spy_stop_mult": 1.5}),
        Spec("time_15_00", arg={"time_stop": dt.time(15, 0)}),
        # Entry window / filters
        Spec("entry_10_00", cfg={"ENTRY_START": "10:00"}),
        Spec("entry_11_00", cfg={"ENTRY_START": "11:00"}),
        Spec("entry_end_11_00", cfg={"ENTRY_END": "11:00"}),
        Spec("entry_end_13_00", cfg={"ENTRY_END": "13:00"}),
        Spec("atr_min_0_10", cfg={"ATR5_MIN_ENTRY": 0.10}),
        Spec("atr_min_0_30", cfg={"ATR5_MIN_ENTRY": 0.30}),
        Spec("cooldown_0", cfg={"TRADE_COOLDOWN_BARS": 0}),
        Spec("no_risk_gates", cfg={"PROFIT_TARGET": 0.0, "CONSEC_WIN_STOP": 0,
                                   "MAX_DAILY_LOSS": 1e9}),
        # Strike selection
        Spec("strike_p1", arg={"strike_steps": 1}),
        Spec("strike_p2", arg={"strike_steps": 2}),
        # Execution assumptions
        Spec("slip_0", arg={"entry_slip": 0.0, "exit_slip": 0.0}),
        Spec("slip_2", arg={"entry_slip": 0.02, "exit_slip": 0.02}),
        Spec("optimistic", arg={"entry_slip": 0.0, "exit_slip": 0.0,
                                "optimistic_intrabar": True}),
        # Combos
        Spec("combo_spy10_strike1", arg={"spy_stop_floor": 1.0, "strike_steps": 1}),
        Spec("combo_spy10_trail092", arg={"spy_stop_floor": 1.0},
             cfg={"PEAK_TRAIL_PCT": 0.92}),
        Spec("combo_spy10_tp2", arg={"spy_stop_floor": 1.0}, cfg={"TP_MULT": 2.0}),
        Spec("combo_no_spy_tp2", cfg={"SPY_STOP_FLOOR": 999.0, "SPY_STOP_ATR_MULT": 0.0,
                                      "TP_MULT": 2.0}),
    ]


def make_variants(specs: list[Spec]) -> list[bt.Variant]:
    variants = []
    for s in specs:
        arg = dict(s.arg)
        # A few exits live in `args` (for per-run overrides), so mirror the
        # matching config override into args. Without this, cfg-only variants
        # such as no_spy_stop would silently keep the config defaults.
        for cfg_key, arg_key in (("SPY_STOP_FLOOR", "spy_stop_floor"),
                                 ("SPY_STOP_ATR_MULT", "spy_stop_mult")):
            if cfg_key in s.cfg and arg_key not in arg:
                arg[arg_key] = s.cfg[cfg_key]
        variants.append(bt.Variant(s.name, bt.default_args(**arg), dict(s.cfg)))
    return variants


def month_end(y: int, m: int) -> dt.date:
    if m == 12:
        return dt.date(y, 12, 31)
    return dt.date(y, m + 1, 1) - dt.timedelta(days=1)


def month_starts(first: dt.date, last: dt.date) -> list[tuple[int, int]]:
    out = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def build_periods(days: list[dt.date]) -> list[dict]:
    periods: list[dict] = []

    def add(name: str, start: dt.date, end: dt.date):
        subset = [d for d in days if start <= d <= end]
        if subset:
            periods.append({"name": name, "start": subset[0], "end": subset[-1]})

    add("full", days[0], days[-1])
    for year in sorted({d.year for d in days}):
        add(f"year_{year}", dt.date(year, 1, 1), dt.date(year, 12, 31))
        for h, (m1, m2) in enumerate(((1, 6), (7, 12)), 1):
            add(f"h{h}_{year}", dt.date(year, m1, 1), month_end(year, m2))
        for q, (m1, m2) in enumerate(((1, 3), (4, 6), (7, 9), (10, 12)), 1):
            add(f"q{q}_{year}", dt.date(year, m1, 1), month_end(year, m2))
    for y, m in month_starts(days[0], days[-1]):
        add(f"month_{y}_{m:02d}", dt.date(y, m, 1), month_end(y, m))
    for n in (10, 20, 30, 60, 90, 120, 180, 365):
        if len(days) >= n:
            add(f"last_{n}", days[-n], days[-1])
    add("live_window", LIVE_START, LIVE_END)
    # De-duplicate by (name, start, end)
    seen = set()
    out = []
    for p in periods:
        key = (p["name"], p["start"], p["end"])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def row_for(variant: bt.Variant, period: dict, trades: list[bt.Trade],
            days: list[dt.date]) -> dict:
    summary = bt._report(days, trades, variant.args, print_table=False)
    rs = summary.pop("exit_reasons", {})
    return {
        "strategy": "directional",
        "variant": variant.name,
        "period": period["name"],
        "start": period["start"].isoformat(),
        "end": period["end"].isoformat(),
        **summary,
        "spy_stop_n": rs.get("spy_stop", {}).get("trades", 0),
        "spy_stop_pnl": rs.get("spy_stop", {}).get("pnl", 0.0),
        "peak_trail_n": rs.get("peak_trail", {}).get("trades", 0),
        "peak_trail_pnl": rs.get("peak_trail", {}).get("pnl", 0.0),
        "tp_n": rs.get("tp", {}).get("trades", 0),
        "tp_pnl": rs.get("tp", {}).get("pnl", 0.0),
        "stop_n": rs.get("stop", {}).get("trades", 0),
        "stop_pnl": rs.get("stop", {}).get("pnl", 0.0),
        "time_stop_n": rs.get("time_stop", {}).get("trades", 0),
        "time_stop_pnl": rs.get("time_stop", {}).get("pnl", 0.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=dt.date.fromisoformat, default=FIRST)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=LAST)
    parser.add_argument("--out-dir", type=Path, default=bt.RESULTS_DIR)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    opt = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)

    print(f"Loading SPY bars {args.start} .. {args.end} ...", flush=True)
    daily, trading_days = bt.load_daily(stock, args.start, args.end, not args.no_cache)
    days = [d for d in trading_days if args.start <= d <= args.end]
    all_bars, bars_by_day = bt.load_minutes(stock, args.start, args.end, not args.no_cache)
    print(f"  {len(days)} sessions, {len(all_bars)} minute bars", flush=True)

    specs = build_specs()
    variants = make_variants(specs)
    print(f"Running {len(variants)} variants ...", flush=True)

    results = bt.run_variants(days, daily, all_bars, bars_by_day, opt, variants,
                              use_cache=not args.no_cache, progress=True)

    periods = build_periods(days)
    print(f"Aggregating {len(periods)} periods ...", flush=True)

    rows = []
    for spec, variant in zip(specs, variants):
        trades = results[variant.name]["trades"]
        by_day: dict[dt.date, list[bt.Trade]] = {}
        for t in trades:
            by_day.setdefault(t.day, []).append(t)
        for period in periods:
            p_days = [d for d in days if period["start"] <= d <= period["end"]]
            p_trades = [t for t in trades if period["start"] <= t.day <= period["end"]]
            rows.append(row_for(variant, period, p_trades, p_days))

    summary_path = out_dir / "sweep_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {summary_path} ({len(rows)} rows)")

    daily_path = out_dir / "sweep_daily.csv"
    with daily_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["variant", "date", "trades", "pnl", "equity"])
        for variant in variants:
            equity = 0.0
            by_day = {}
            for t in results[variant.name]["trades"]:
                by_day.setdefault(t.day, []).append(t)
            for day in days:
                day_trades = by_day.get(day, [])
                pnl = sum(t.pnl for t in day_trades)
                equity += pnl
                if day_trades or day == days[-1]:
                    writer.writerow([variant.name, day.isoformat(),
                                     len(day_trades), f"{pnl:.2f}", f"{equity:.2f}"])
    print(f"Wrote {daily_path}")

    meta = {
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "sessions": len(days),
        "variants": [
            {"name": s.name, "args": {k: str(v) for k, v in s.arg.items()},
             "config": s.cfg}
            for s in specs
        ],
        "config": {
            k: getattr(config, k) for k in (
                "TP_MULT", "STOP_MULT", "PEAK_TRAIL_ACTIVATE", "PEAK_TRAIL_PCT",
                "SPY_STOP_ATR_MULT", "SPY_STOP_FLOOR", "ENTRY_START", "ENTRY_END",
                "TIME_STOP", "ATR5_MIN_ENTRY", "MAX_RISK_PER_TRADE", "MAX_DAILY_LOSS",
                "PROFIT_TARGET", "CONSEC_WIN_STOP", "TRADE_COOLDOWN_BARS",
                "ACTIVATION_PCT", "STRIKE_STEP",
            )
        },
        "skipped": {
            v.name: [{"day": d.isoformat(), "reason": r}
                    for d, r in results[v.name]["skipped"]]
            for v in variants if results[v.name]["skipped"]
        },
    }
    with (out_dir / "sweep_meta.json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print("Wrote sweep_meta.json")

    baseline_rows = [r for r in rows if r["variant"] == "baseline"
                     and r["period"] in ("full", "year_2024", "year_2025", "year_2026")]
    print("\nBaseline:")
    for r in baseline_rows:
        print(f"  {r['period']:<12} {r['sessions']:>4} sessions  {r['trades']:>4} trades  "
              f"${r['net_pnl']:>+10.2f}  WR {r['win_rate_pct']:>5.1f}%  PF {r['profit_factor']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
