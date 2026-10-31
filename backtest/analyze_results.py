#!/usr/bin/env python3
"""Compact analysis of the sweep + condor result CSVs.

Reads backtest/results/sweep_summary.csv and condor_summary.csv and prints
tables that feed the findings document. No plotting dependencies.
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
IRON_CONDOR_TRADES = RESULTS / "condor_trades.csv"


def load_condor_trades(variant: str) -> list[dict]:
    path = IRON_CONDOR_TRADES
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["variant"] != variant:
                continue
            for key in ("spot", "credit", "max_risk", "exit_cost", "pnl",
                        "prev_atr", "short_call", "short_put",
                        "long_call", "long_put"):
                row[key] = float(row[key])
            out.append(row)
    return out


def quintile_table(rows: list[dict], keyfn, label: str):
    if not rows:
        return
    values = sorted(keyfn(r) for r in rows)
    cuts = [values[min(len(values) - 1, int(len(values) * q / 5))]
            for q in (1, 2, 3, 4)]
    buckets: list[list[dict]] = [[] for _ in range(5)]
    for r in rows:
        v = keyfn(r)
        idx = sum(1 for c in cuts if v > c)
        buckets[idx].append(r)
    print(f"  {'bucket':<10} {'n':>5} {'avg_' + label:>12} {'net':>10} "
          f"{'WR':>6} {'avg_credit':>10}")
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        n = len(bucket)
        avg_key = sum(keyfn(r) for r in bucket) / n
        net = sum(r["pnl"] for r in bucket)
        wr = sum(1 for r in bucket if r["pnl"] > 0) / n * 100
        credit = sum(r["credit"] for r in bucket) / n
        print(f"  Q{i + 1:<9} {n:>5} {avg_key:>12.3f} {net:>+10.1f} "
              f"{wr:>5.1f}% {credit:>10.2f}")


def load(name: str) -> list[dict]:
    path = RESULTS / name
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for key in ("sessions", "trades", "tp_n", "stop_n", "eod_n",
                        "expiry_n", "spy_stop_n", "peak_trail_n", "time_stop_n"):
                if key in row and row[key] not in ("", None):
                    row[key] = int(float(row[key]))
            for key in ("net_pnl", "pnl_per_day", "win_rate_pct", "breakeven_wr_pct",
                        "profit_factor", "expectancy", "avg_win", "avg_loss",
                        "max_drawdown", "avg_credit", "avg_max_risk",
                        "return_on_risk_pct", "coverage_pct", "tp_pnl", "stop_pnl",
                        "eod_pnl", "expiry_pnl", "spy_stop_pnl", "peak_trail_pnl",
                        "time_stop_pnl", "stop_pnl"):
                if key in row and row[key] not in ("", None):
                    row[key] = float(row[key])
            rows.append(row)
    return rows


def pf(row: dict) -> float:
    return row["profit_factor"] if row.get("profit_factor") is not None else 0.0


def print_period_table(rows: list[dict], variant: str = "baseline"):
    subset = [r for r in rows if r["variant"] == variant]
    if not subset:
        print(f"  (no rows for {variant})")
        return
    order = ["full", "year_2024", "year_2025", "year_2026",
             "h1_2024", "h2_2024", "h1_2025", "h2_2025", "h1_2026", "h2_2026",
             "live_window", "last_30", "last_60", "last_90", "last_180", "last_365"]
    by_name = {r["period"]: r for r in subset}
    print(f"  {'period':<12} {'sess':>5} {'trades':>6} {'net':>11} {'WR':>6} "
          f"{'PF':>5} {'DD':>10} {'exp':>8}")
    for name in order:
        r = by_name.get(name)
        if not r:
            continue
        print(f"  {name:<12} {r['sessions']:>5} {r['trades']:>6} "
              f"{r['net_pnl']:>+11.2f} {r['win_rate_pct']:>5.1f}% "
              f"{pf(r):>5.2f} {r['max_drawdown']:>10.2f} {r['expectancy']:>+8.2f}")


def print_quarters(rows: list[dict], variant: str = "baseline"):
    subset = [r for r in rows
              if r["variant"] == variant and r["period"].startswith("q")]
    subset.sort(key=lambda r: r["start"])
    print(f"  {'quarter':<10} {'sess':>5} {'trades':>6} {'net':>11} {'WR':>6} {'PF':>5}")
    for r in subset:
        print(f"  {r['period']:<10} {r['sessions']:>5} {r['trades']:>6} "
              f"{r['net_pnl']:>+11.2f} {r['win_rate_pct']:>5.1f}% {pf(r):>5.2f}")


def print_leaderboard(rows: list[dict], limit: int = 15):
    full = [r for r in rows if r["period"] == "full"]
    ranked = sorted(full, key=lambda r: r["net_pnl"], reverse=True)
    print(f"  {'variant':<24} {'trades':>6} {'net':>11} {'WR':>6} {'PF':>5} "
          f"{'DD':>10} {'exp':>8} {'spy_stop':>9} {'trail':>9} {'tp':>8}")
    for r in ranked[:limit]:
        print(f"  {r['variant']:<24} {r['trades']:>6} {r['net_pnl']:>+11.2f} "
              f"{r['win_rate_pct']:>5.1f}% {pf(r):>5.2f} {r['max_drawdown']:>10.2f} "
              f"{r['expectancy']:>+8.2f} {r.get('spy_stop_pnl', 0):>+9.0f} "
              f"{r.get('peak_trail_pnl', 0):>+9.0f} {r.get('tp_pnl', 0):>+8.0f}")


def consistency(rows: list[dict]) -> list[dict]:
    """Variant ranking by quarter consistency and worst quarter."""
    quarters = [r for r in rows if r["period"].startswith("q")
                and r["sessions"] >= 10]
    by_variant: dict[str, list[dict]] = {}
    for r in quarters:
        by_variant.setdefault(r["variant"], []).append(r)
    out = []
    for variant, qs in by_variant.items():
        nets = [q["net_pnl"] for q in qs]
        if not nets:
            continue
        out.append({
            "variant": variant,
            "quarters": len(nets),
            "positive": sum(1 for n in nets if n > 0),
            "median": sorted(nets)[len(nets) // 2],
            "worst": min(nets),
            "best": max(nets),
            "total": sum(nets),
        })
    out.sort(key=lambda r: (-r["positive"], -r["median"]))
    return out


def main() -> int:
    directional = load("sweep_summary.csv")
    condor = load("condor_summary.csv")

    if directional:
        print("=" * 100)
        print("DIRECTIONAL — baseline by period")
        print("=" * 100)
        print_period_table(directional, "baseline")
        print()
        print("DIRECTIONAL — baseline by quarter")
        print_quarters(directional, "baseline")
        print()
        print("DIRECTIONAL — full-range leaderboard")
        print_leaderboard(directional, 20)
        print()
        print("DIRECTIONAL — quarter consistency (>=10 sessions)")
        for r in consistency(directional)[:15]:
            print(f"  {r['variant']:<24} {r['positive']:>2}/{r['quarters']:<2} "
                  f"median {r['median']:>+9.0f}  worst {r['worst']:>+9.0f} "
                  f"best {r['best']:>+9.0f}  total {r['total']:>+10.0f}")

    if condor:
        print()
        print("=" * 100)
        print("IRON CONDOR — full-range top variants")
        print("=" * 100)
        full = [r for r in condor if r["period"] == "full"]
        ranked = sorted(full, key=lambda r: r["net_pnl"], reverse=True)
        print(f"  {'variant':<40} {'tr':>4} {'net':>10} {'WR':>6} {'BE':>6} "
              f"{'PF':>5} {'credit':>7} {'risk':>6} {'ROR':>6} {'DD':>9}")
        for r in ranked[:25]:
            print(f"  {r['variant']:<40} {r['trades']:>4} {r['net_pnl']:>+10.2f} "
                  f"{r['win_rate_pct']:>5.1f}% {r['breakeven_wr_pct']:>5.1f}% "
                  f"{pf(r):>5.2f} {r['avg_credit']:>7.2f} {r['avg_max_risk']:>6.2f} "
                  f"{r['return_on_risk_pct']:>5.1f}% {r['max_drawdown']:>9.0f}")
        print()
        print("IRON CONDOR — full-range bottom variants")
        for r in ranked[-10:]:
            print(f"  {r['variant']:<40} {r['trades']:>4} {r['net_pnl']:>+10.2f} "
                  f"{r['win_rate_pct']:>5.1f}% {r['breakeven_wr_pct']:>5.1f}% "
                  f"{pf(r):>5.2f} {r['avg_credit']:>7.2f} {r['avg_max_risk']:>6.2f} "
                  f"{r['return_on_risk_pct']:>5.1f}% {r['max_drawdown']:>9.0f}")

        # Averages by structural choice, base slip + any filter, full period.
        # Variant name: {time}_d{dist}_w{width}_{exit}_{filter}_{slip}
        def parts(name: str) -> tuple[str, str, str, str, str, str]:
            bits = name.split("_")
            return (bits[0], bits[1], bits[2], "_".join(bits[3:-2]),
                    bits[-2], bits[-1])

        base_full = [r for r in full if parts(r["variant"])[5] == "base"]
        for idx, label in ((0, "entry time"), (1, "distance"), (2, "width"),
                           (3, "exit"), (4, "filter")):
            groups: dict[str, list[dict]] = {}
            for r in base_full:
                groups.setdefault(parts(r["variant"])[idx], []).append(r)
            print()
            print(f"IRON CONDOR — averages by {label} (base slip, full range)")
            print(f"  {label:<14} {'n':>4} {'avg_net':>10} {'avg_WR':>7} "
                  f"{'avg_BE':>7} {'avg_credit':>10} {'avg_ROR':>8}")
            for key in sorted(groups):
                rs = groups[key]
                n = len(rs)
                print(f"  {key:<14} {n:>4} "
                      f"{sum(r['net_pnl'] for r in rs) / n:>+10.1f} "
                      f"{sum(r['win_rate_pct'] for r in rs) / n:>6.1f}% "
                      f"{sum(r['breakeven_wr_pct'] for r in rs) / n:>6.1f}% "
                      f"{sum(r['avg_credit'] for r in rs) / n:>10.2f} "
                      f"{sum(r['return_on_risk_pct'] for r in rs) / n:>7.1f}%")

        print()
        print("IRON CONDOR — representative variants by period")
        candidates = [
            "1030_d3_w2_expiry_any_base",
            "1030_d3_w2_tp50_stop100_any_base",
            "1030_d5_w3_expiry_any_base",
            "0945_d3_w2_expiry_any_base",
            "1030_d3_w2_expiry_neutral_base",
        ]
        for v in candidates:
            subset = [r for r in condor if r["variant"] == v]
            if not subset:
                continue
            print(f"\n  {v}")
            by_name = {r["period"]: r for r in subset}
            for name in ("full", "year_2024", "year_2025", "year_2026",
                         "live_window", "last_90", "last_180", "last_365"):
                r = by_name.get(name)
                if not r:
                    continue
                print(f"    {name:<12} {r['sessions']:>4} sess {r['trades']:>4} tr "
                      f"{r['net_pnl']:>+10.2f}  WR {r['win_rate_pct']:>5.1f}%  "
                      f"PF {pf(r):>5.2f}  credit {r['avg_credit']:>5.2f}")

        print()
        print("IRON CONDOR — regime buckets (1030_d3_w2_expiry_any_base)")
        tr = load_condor_trades("1030_d3_w2_expiry_any_base")
        quintile_table(tr, lambda r: r["prev_atr"], "prev_atr")
        print()
        print("IRON CONDOR — credit/width buckets (same variant)")
        quintile_table(tr, lambda r: r["credit"] / (r["long_call"] - r["short_call"]),
                       "credit_width")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
