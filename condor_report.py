#!/usr/bin/env python3
"""
Evaluate the paper condor test — everything PER CONTRACT, so the result doesn't
depend on how many condors the sizing happened to trade that day.

    python condor_report.py             # REAL P&L rebuilt from the account's fills (needs API)
    python condor_report.py --offline   # only what logs/condor_paper.csv says

The REAL figure is what counts: on 2026-11-02/03 Alpaca bought the short legs
back at 15:45 ET although the bot was set to hold to expiry, so the logged
"theoretical settlement" P&L was off by -77 / +22 $.

Backtest reference (13:00 ET, 2$ away, 3$ wide, 690 sessions 2024-02..2026-10),
$/contract at base | high slippage:
    exit 15:45 (eod_1545)     -3.48 | -11.41   <- what Alpaca paper actually allows
    hold to expiry            +10.25 |  +6.38   <- not realizable on Alpaca (15:45 close)
"""
import argparse
import csv
import math
import os
import statistics as st
import sys

import config

PATH = os.path.join(config.LOG_DIR, "condor_paper.csv")
REF = {"eod_1545": -3.48, "expiry": 10.25}


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def real_pnl_by_date(rows: list[dict]) -> dict[str, dict]:
    """{date: reconcile record} from the account activities; {} if unavailable."""
    try:
        import datetime as dt
        from alpaca.trading.client import TradingClient
        from condor_reconcile import fetch_activities, reconcile
        dates = [dt.date.fromisoformat(r["date"]) for r in rows if r.get("date")]
        trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET, paper=config.PAPER)
        acts = fetch_activities(trading, min(dates) - dt.timedelta(days=1))
        return {x["date"]: x for x in reconcile(rows, acts)}
    except Exception as exc:
        print(f"(account activities unavailable: {exc} — falling back to the logged P&L)")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="don't query the account; use the CSV only")
    args = ap.parse_args()

    if not os.path.exists(PATH):
        print(f"No data yet: {PATH}")
        return 0
    rows = list(csv.DictReader(open(PATH, encoding="utf-8")))
    traded = [r for r in rows if f(r.get("fill_credit")) is not None]
    no_fill = sum(1 for r in rows if r.get("status") == "no_fill")
    skips: dict[str, int] = {}
    for r in rows:
        if (r.get("status") or "").startswith("skip:"):
            skips[r["status"][5:]] = skips.get(r["status"][5:], 0) + 1
    print(f"Sessions logged: {len(rows)}  |  traded: {len(traded)}  |  no fill: {no_fill}"
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
    if not traded:
        return 0

    real = {} if args.offline else real_pnl_by_date(traded)
    pnl, logged, src_real = [], [], 0
    for r in traded:
        qty = f(r.get("qty")) or 1
        rec = real.get(r["date"])
        lg = f(r.get("pnl_per_contract"))
        if rec is not None:
            pnl.append(rec["real_pnl"] / qty)
            src_real += 1
        elif lg is not None:
            pnl.append(lg)
        else:
            continue
        if lg is not None:
            logged.append(lg)
    if not pnl:
        return 0

    n, mean = len(pnl), st.mean(pnl)
    wins = sum(p > 0 for p in pnl)
    print(f"\nP&L source:          {src_real}/{n} from account fills (REAL), "
          f"{n - src_real} from the CSV log")
    print(f"Win rate:            {wins / n:.1%}  ({wins}W / {n - wins}L)")
    print(f"P&L per contract:    {mean:+.2f} $ avg   (total {sum(pnl):+.2f} $/contract)")
    if src_real and logged and len(logged) == n:
        print(f"  logged (CSV):      {st.mean(logged):+.2f} $ avg   -> gap REAL-logged "
              f"{mean - st.mean(logged):+.2f} $/contract")
    if n > 2:
        se = st.stdev(pnl) / math.sqrt(n)
        print(f"  95% interval:      [{mean - 1.96 * se:+.2f}, {mean + 1.96 * se:+.2f}]   t = {mean / se:+.2f}")
    print(f"Avg win / avg loss:  {st.mean([p for p in pnl if p > 0] or [0]):+.2f} / "
          f"{st.mean([p for p in pnl if p <= 0] or [0]):+.2f} $ per contract")
    print(f"Worst single trade:  {min(pnl):+.2f} $ per contract")

    broker = [d for d, x in real.items() if x["how"].startswith("BROKER")]
    if broker:
        print(f"Broker early close:  {len(broker)}/{src_real} session(s) — exit was NOT expiry")
    modes = {r.get("exit_mode") for r in traded}
    ref = REF["eod_1545"] if (broker or modes - {"expiry"}) else REF["expiry"]
    print(f"Backtest reference:  {ref:+.2f} $/contract "
          f"({'15:45 exit' if ref == REF['eod_1545'] else 'hold to expiry'}, base slippage)")

    slips = [f(r["slip_per_leg"]) for r in traded if f(r.get("slip_per_leg")) is not None]
    if slips:
        print(f"Slippage vs mid:     {st.mean(slips) * 100:.2f} cents/leg avg  "
              f"(backtest assumed ~1 cent/leg ≈ 1%)")
    creds = [f(r["fill_credit"]) for r in traded]
    print(f"Avg filled credit:   {st.mean(creds):.2f} $   (backtest 13:00: ~0.39 $)")
    if n < 40:
        print(f"\nOnly {n} trades — the interval is still very wide; judge after 40-60.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
