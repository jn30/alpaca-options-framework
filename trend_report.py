#!/usr/bin/env python3
"""
Paper-test report for trend_bot.py — is it safe to go live?

    python trend_report.py

Go-live checklist (the strategy's edge comes from the 1970-2026 backtest; the
paper test checks the MACHINERY, which is where real money gets lost):
  1. >= 20 trading days covered, no trading day without a daily check
  2. every order filled (no rejected / canceled / partial orders)
  3. at least one full switch (risk-on <-> risk-off) executed and filled
  4. paper equity follows the held ETF (no unexplained losses)
"""
import csv
import datetime as dt
import os
import sys

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

import config

DAILY = os.path.join(config.LOG_DIR, "trend_daily.csv")
TRADES = os.path.join(config.LOG_DIR, "trend_trades.csv")
MIN_DAYS = 20


def rows(path):
    return list(csv.DictReader(open(path, encoding="utf-8"))) if os.path.exists(path) else []


def main() -> int:
    daily, trades = rows(DAILY), rows(TRADES)
    if not daily:
        print("No daily checks logged yet (logs/trend_daily.csv).")
        return 0
    trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET, paper=config.PAPER)
    first = dt.date.fromisoformat(daily[0]["date"])
    today = dt.datetime.now(config.ET).date()
    cal = trading.get_calendar(GetCalendarRequest(start=first, end=today))
    now = dt.datetime.now(config.ET)
    sessions = [c.date for c in cal
                if c.date < today or now.time() >= dt.time(16, 5)]          # finished sessions only
    real = [r for r in daily if not r["action"].startswith("FORCED")]
    checked = {r["date"] for r in real}
    missed = [d for d in sessions if d.isoformat() not in checked]
    covered = len(sessions) - len(missed)

    bad = [t for t in trades if t["status"].lower().split(".")[-1] != "filled"]
    real_switch = any("SELL" in r["action"] for r in real)
    forced_switch = any(r["action"].startswith("FORCED") and "SELL" in r["action"] for r in daily)

    acct = trading.get_account()
    pos = {p.symbol: p for p in trading.get_all_positions()}
    held = ", ".join(f"{s} {float(p.qty):.4f} (${float(p.market_value):.2f}, "
                     f"{float(p.unrealized_plpc) * 100:+.1f}%)" for s, p in pos.items()) or "cash only"
    start_eq = float(daily[0]["equity"])
    eq = float(acct.equity)

    print(f"Paper test since {first}: {covered}/{len(sessions)} finished trading days checked")
    print(f"Equity ${start_eq:.2f} -> ${eq:.2f} ({(eq / start_eq - 1) * 100:+.1f}%)   holding: {held}")
    last = real[-1] if real else daily[-1]
    print(f"Last signal {last['date']}: SPY {last['spy']} vs SMA {last['sma']} -> {last['target']}")
    print("\nGo-live checklist:")
    checks = [
        (covered >= MIN_DAYS and not missed,
         f"{covered} trading days checked (need >= {MIN_DAYS}), missed: "
         f"{', '.join(map(str, missed)) or 'none'}"),
        (not bad, f"{len(trades)} orders, {len(bad)} not filled"),
        (real_switch or forced_switch,
         "switch executed: " + ("real" if real_switch else "forced paper test" if forced_switch else "none yet")),
        (None, "equity follows the held ETF — compare the line above with the ETF's move "
               "(Alpaca app / chart); no automatic check"),
    ]
    for ok, txt in checks:
        mark = "info" if ok is None else ("PASS" if ok else "OPEN")
        print(f"  [{mark}] {txt}")
    done = all(ok for ok, _ in checks if ok is not None)
    print("\n" + ("READY: machinery verified — going live is your decision (live keys, PAPER=False)."
                  if done else "Keep testing."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
