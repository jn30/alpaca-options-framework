#!/usr/bin/env python3
"""
Reconcile the paper condor log against what Alpaca actually booked.

For every session in logs/condor_paper.csv this pulls the account activities
(fills, fees, expiries, assignments/exercises) and rebuilds the REAL cash P&L,
then puts it next to the theoretical settlement P&L the bot logged.

    python condor_reconcile.py                 # all logged sessions
    python condor_reconcile.py --csv out.csv   # also write the per-day table

Why: with exit_mode=expiry the bot never closes the condor itself, yet Alpaca
may close (some) legs before the bell -- e.g. the short legs at 15:45 ET. Then
the realized P&L is an "early close" P&L, not the expiry P&L the backtest
reference (+10 $/contract) assumed, and only the real column is evidence.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import os
import statistics as st
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient

import config

ET = ZoneInfo("America/New_York")
PAPER_CSV = os.path.join(config.LOG_DIR, "condor_paper.csv")
LEG_KEYS = ("short_put", "long_put", "short_call", "long_call")
CASH_TYPES = {"FEE", "OPASN", "OPEXC", "OPEXP", "OPTRD", "DIV", "INT"}   # net_amount bookings


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_activities(trading: TradingClient, after: dt.date) -> list[dict]:
    """All account activities since `after` (paginated, oldest first)."""
    out, token = [], None
    while True:
        params = {"after": f"{after.isoformat()}T00:00:00Z", "direction": "asc", "page_size": 100}
        if token:
            params["page_token"] = token
        page = trading.get("/account/activities", params) or []
        out.extend(page)
        if len(page) < 100:
            return out
        token = page[-1]["id"]


def occ(day: dt.date, right: str, strike: float) -> str:
    return f"SPY{day:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def fill_day(a: dict) -> tuple[dt.date, dt.datetime]:
    t = dt.datetime.fromisoformat(a["transaction_time"].replace("Z", "+00:00")).astimezone(ET)
    return t.date(), t


def reconcile(rows: list[dict], acts: list[dict]) -> list[dict]:
    fills_by_day = defaultdict(list)
    cash_by_day = defaultdict(list)
    for a in acts:
        if a.get("activity_type") == "FILL":
            d, t = fill_day(a)
            fills_by_day[d].append((t, a))
        elif a.get("activity_type") in CASH_TYPES:
            cash_by_day[dt.date.fromisoformat(a["date"])].append(a)

    out = []
    for r in rows:
        if r.get("underlying", "SPY") != "SPY" or f(r.get("fill_credit")) is None:
            continue
        day = dt.date.fromisoformat(r["date"])
        qty = int(f(r.get("qty")) or 1)
        syms = {
            "short_put": occ(day, "P", f(r["short_put"])), "long_put": occ(day, "P", f(r["long_put"])),
            "short_call": occ(day, "C", f(r["short_call"])), "long_call": occ(day, "C", f(r["long_call"])),
        }
        leg_of = {v: k for k, v in syms.items()}
        entry_t = dt.datetime.fromisoformat(r["entry_time"]).astimezone(ET)

        open_cash = close_cash = stock_cash = 0.0
        closes = []
        for t, a in sorted(fills_by_day.get(day, []), key=lambda x: x[0]):
            px, q = float(a["price"]), float(a["qty"])
            sign = 1.0 if a["side"].startswith("sell") else -1.0
            if a["symbol"] in leg_of:
                cash = sign * px * q * 100
                if t <= entry_t + dt.timedelta(minutes=5):
                    open_cash += cash
                else:
                    close_cash += cash
                    closes.append(f"{leg_of[a['symbol']]}@{px:.2f} {t:%H:%M}")
            elif a["symbol"] == "SPY":                      # stock from assignment/exercise
                stock_cash += sign * px * q
        fees = settle = 0.0
        events = []
        for a in cash_by_day.get(day, []):
            amt = f(a.get("net_amount")) or 0.0
            if a["activity_type"] == "FEE":
                fees += amt
            else:
                settle += amt
                if a.get("symbol") in leg_of and a["activity_type"] != "OPEXP":
                    events.append(f"{a['activity_type']} {leg_of[a['symbol']]}")
                elif a.get("symbol") in leg_of:
                    events.append(f"exp {leg_of[a['symbol']]}")

        real = open_cash + close_cash + stock_cash + settle + fees
        theo = f(r.get("theoretical_pnl"))
        if theo is None and f(r.get("close_cost")) is not None:   # bot-closed: logged order prices
            theo = round((f(r["fill_credit"]) - f(r["close_cost"])) * 100 * qty, 2)
        if closes and r.get("exit_mode") == "expiry":
            how = "BROKER close " + ", ".join(closes)
        elif closes:
            how = "bot close " + ", ".join(closes)
        else:
            how = "held to expiry"
        out.append({
            "date": r["date"], "qty": qty, "spot_entry": f(r.get("spot")), "spy_close": f(r.get("spy_close")),
            "credit_logged": f(r["fill_credit"]) * 100 * qty, "credit_booked": round(open_cash, 2),
            "close_cash": round(close_cash, 2), "fees": round(fees, 2),
            "assign_exercise": round(settle + stock_cash, 2),
            "real_pnl": round(real, 2), "theoretical_pnl": theo,
            "diff": round(real - theo, 2) if theo is not None else None,
            "equity_change": f(r.get("equity_change")),
            "how": how + ("; " + ", ".join(events) if events else ""),
        })
    return out


def realized_for_row(trading: TradingClient, row: dict) -> dict | None:
    """Reconcile one session (a condor_paper row) against the account. Returns
    the reconcile() record or None if the activities can't be read. Note: fees
    and expiry bookings arrive in the evening, so right after the close the
    figure covers fills only (fees are ~0.30 $/condor)."""
    try:
        day = dt.date.fromisoformat(row["date"])
        res = reconcile([row], fetch_activities(trading, day - dt.timedelta(days=1)))
        return res[0] if res else None
    except Exception:
        return None


def summarize(res: list[dict]) -> None:
    print(f"{'date':<11}{'credit':>8}{'close':>9}{'fees':>7}{'asg/ex':>8}{'REAL':>9}{'logged':>9}"
          f"{'diff':>8}  how")
    for x in res:
        print(f"{x['date']:<11}{x['credit_booked']:>8.2f}{x['close_cash']:>9.2f}{x['fees']:>7.2f}"
              f"{x['assign_exercise']:>8.2f}{x['real_pnl']:>9.2f}{(x['theoretical_pnl'] or 0):>9.2f}"
              f"{(x['diff'] or 0):>8.2f}  {x['how']}")
    real = [x["real_pnl"] / x["qty"] for x in res]
    theo = [x["theoretical_pnl"] / x["qty"] for x in res if x["theoretical_pnl"] is not None]
    n = len(real)
    if not n:
        return
    logged = f"   |   logged {st.mean(theo):+.2f} $ avg (sum {sum(theo):+.2f})" if theo else ""
    print(f"\nPer contract:  REAL {st.mean(real):+.2f} $ avg (sum {sum(real):+.2f}){logged}")
    if n > 2:
        se = st.stdev(real) / math.sqrt(n)
        print(f"  REAL 95% interval [{st.mean(real) - 1.96 * se:+.2f}, {st.mean(real) + 1.96 * se:+.2f}]"
              f"   t = {st.mean(real) / se:+.2f}")
    broker = sum(1 for x in res if x["how"].startswith("BROKER"))
    if broker:
        print(f"  !! {broker}/{n} session(s) were closed early by the BROKER although exit_mode=expiry.\n"
              f"     The realized exit is then an intraday close, so compare against the backtest's\n"
              f"     intraday-exit rows (e.g. eod_1545), not the hold-to-expiry +10 $/contract.")
    if n < 40:
        print(f"  Only {n} sessions -- still noise; judge after 40-60.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="write the per-day reconciliation to this CSV")
    args = ap.parse_args()
    if not os.path.exists(PAPER_CSV):
        print(f"No data yet: {PAPER_CSV}")
        return 0
    rows = list(csv.DictReader(open(PAPER_CSV, encoding="utf-8")))
    dates = [dt.date.fromisoformat(r["date"]) for r in rows if r.get("date")]
    if not dates:
        print("No sessions logged.")
        return 0
    trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET, paper=config.PAPER)
    acts = fetch_activities(trading, min(dates) - dt.timedelta(days=1))
    res = reconcile(rows, acts)
    summarize(res)
    if args.csv and res:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(res[0]))
            w.writeheader()
            w.writerows(res)
        print(f"\nWritten: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
