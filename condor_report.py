#!/usr/bin/env python3
"""
Evaluate the paper condor test — everything PER CONTRACT, so the result doesn't
depend on how many condors the "max" sizing happened to trade that day.

    python condor_report.py            # reads logs/condor_paper.csv

Backtest reference (13:00 ET, 2$ away, 3$ wide, hold to expiry, 690 sessions):
    1% slip/leg: +10.25 $/contract   |   2% slip/leg: +6.38 $/contract
"""
import csv
import math
import os
import statistics as st
import sys

import config

PATH = os.path.join(config.LOG_DIR, "condor_paper.csv")


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> int:
    if not os.path.exists(PATH):
        print(f"No data yet: {PATH}")
        return 0
    rows = list(csv.DictReader(open(PATH, encoding="utf-8")))
    done = [r for r in rows if f(r.get("pnl_per_contract")) is not None]
    no_fill = sum(1 for r in rows if r.get("status") == "no_fill")
    skips: dict[str, int] = {}
    for r in rows:
        if (r.get("status") or "").startswith("skip:"):
            skips[r["status"][5:]] = skips.get(r["status"][5:], 0) + 1
    print(f"Sessions logged: {len(rows)}  |  with result: {len(done)}  |  no fill: {no_fill}"
          f"  |  skipped: {sum(skips.values())} {skips or ''}")
    acct_path = os.path.join(config.LOG_DIR, "account_daily.csv")
    if os.path.exists(acct_path):
        snaps = list(csv.DictReader(open(acct_path, encoding="utf-8")))
        if snaps:
            first, last = f(snaps[0]["equity"]), f(snaps[-1]["equity"])
            print(f"Account equity (real): {first:.2f} -> {last:.2f}  = {last - first:+.2f} $  "
                  f"({snaps[0]['date']} .. {snaps[-1]['date']})")
            overnight = [s for s in snaps if s["tag"] == "morning" and s["positions"]]
            if overnight:
                print(f"  !! {len(overnight)} morning(s) with positions left overnight "
                      f"(assignment?): {', '.join(s['date'] for s in overnight)}")
    if not done:
        return 0

    pnl = [f(r["pnl_per_contract"]) for r in done]
    n, mean = len(pnl), st.mean(pnl)
    wins = sum(p > 0 for p in pnl)
    print(f"Win rate:            {wins / n:.1%}  ({wins}W / {n - wins}L)")
    print(f"P&L per contract:    {mean:+.2f} $ avg   (total {sum(pnl):+.2f} $/contract)")
    if n > 2:
        sd = st.stdev(pnl)
        se = sd / math.sqrt(n)
        print(f"  95% interval:      [{mean - 1.96 * se:+.2f}, {mean + 1.96 * se:+.2f}]   t = {mean / se:+.2f}")
    print(f"Avg win / avg loss:  {st.mean([p for p in pnl if p > 0] or [0]):+.2f} / "
          f"{st.mean([p for p in pnl if p <= 0] or [0]):+.2f} $ per contract")
    worst = min(pnl)
    print(f"Worst single trade:  {worst:+.2f} $ per contract")

    slips = [f(r["slip_per_leg"]) for r in done if f(r.get("slip_per_leg")) is not None]
    if slips:
        print(f"Slippage vs mid:     {st.mean(slips) * 100:.2f} cents/leg avg  "
              f"(backtest assumed ~1 cent/leg ≈ 1%)")
    creds = [f(r["fill_credit"]) for r in done if f(r.get("fill_credit")) is not None]
    if creds:
        print(f"Avg filled credit:   {st.mean(creds):.2f} $   (backtest 13:00: ~0.39 $)")
    if n < 40:
        print(f"\nOnly {n} trades — the interval is still very wide; judge after 40-60.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
