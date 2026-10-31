#!/usr/bin/env python3
"""
0DTE SPXW iron-condor backtest (hold to expiry, cash-settled).

Same idea as backtest/iron_condor.py but for SPX weeklies/dailies (root SPXW).

Differences that matter
-----------------------
- Alpaca's stock API has no SPX index price. The SPX level is recovered from
  option trade bars by put-call parity at each strike: S ~ K + C - P, taken at
  the strike where C and P are closest, median over a few strikes. The same is
  done near the close to settle the options (cash-settled to the SPX close).
- Option bars are fetched only for the window 11:55-16:00 ET (entry times are
  midday), which keeps the data volume manageable.
- Strike step is 5, and the SPY structure (2$ away / 3$ wide) is scaled by 10
  (20 / 30 points) -> $3,000 collateral per condor, versus $300 for SPY.
- Slippage tiers are in dollars per leg + % of premium and are larger than the
  SPY tiers because SPXW quotes are wider in dollars (see SLIPS).
- Same caveat as everywhere: trade bars, no bid/ask.

Usage:
    python -m backtest.spx_condor --progress
    python -m backtest.spx_condor --start 2025-01-01 --end 2025-06-30 --progress
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import statistics as st
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest
from alpaca.data.timeframe import TimeFrame

import config
from backtest import engine as bt
from strikes import build_occ_symbol

ET = config.ET
ROOT = "SPXW"
STEP = 5
ENTRY_TIMES = (dt.time(12, 0), dt.time(13, 0), dt.time(13, 30))
DISTANCES = (15.0, 20.0, 30.0)
WIDTHS = (20.0, 30.0)
WINDOW_START = dt.time(11, 55)
WINDOW_END = dt.time(16, 0)
SLIPS = {                       # (dollars per leg, fraction of premium per leg)
    "base": (0.05, 0.01),
    "high": (0.10, 0.02),
    "stress": (0.20, 0.03),
}
ONE_MIN = dt.timedelta(minutes=1)
RATIO_GUESS = 10.03             # only used to choose which strikes to download


def _sell(p, slip):
    return max(0.0, p * (1.0 - slip[1]) - slip[0])


def _buy(p, slip):
    return max(0.0, p * (1.0 + slip[1]) + slip[0])


# ── Data ──────────────────────────────────────────────────────────────────────

def fetch_day(client, day: dt.date, lo: float, hi: float) -> dict[str, dict]:
    strikes = [lo + i * STEP for i in range(int(round((hi - lo) / STEP)) + 1)]
    symbols = []
    for k in strikes:
        symbols.append(build_occ_symbol(ROOT, day, "CALL", k))
        symbols.append(build_occ_symbol(ROOT, day, "PUT", k))
    start = dt.datetime.combine(day, WINDOW_START, tzinfo=ET)
    end = dt.datetime.combine(day, WINDOW_END, tzinfo=ET)
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 50):
        data = client.get_option_bars(OptionBarsRequest(
            symbol_or_symbols=symbols[i:i + 50], timeframe=TimeFrame.Minute,
            start=start, end=end)).data
        for sym, bars in data.items():
            out[sym] = {b.timestamp.astimezone(ET).isoformat(): float(b.close) for b in bars}
    return out


def load_day(client, day, lo, hi, use_cache=True) -> dict[str, dict[dt.datetime, float]]:
    raw = bt._cached(f"spxw_{day.isoformat()}_{lo:g}_{hi:g}",
                     lambda: fetch_day(client, day, lo, hi), use_cache)
    return {s: {dt.datetime.fromisoformat(t): c for t, c in v.items()} for s, v in raw.items()}


def px_at(bars: dict, sym: str, t: dt.datetime, tol: int = 2):
    d = bars.get(sym)
    if not d:
        return None
    for i in range(0, tol + 1):
        for cand in ((t,) if i == 0 else (t - i * ONE_MIN, t + i * ONE_MIN)):
            if cand in d:
                return d[cand]
    return None


def parity_spot(bars, day, t, guess, tol=1):
    """Median of K + C - P over the 3 strikes where C and P are closest."""
    base = round(guess / STEP) * STEP
    cands = []
    for k in [base + i * STEP for i in range(-8, 9)]:
        c = px_at(bars, build_occ_symbol(ROOT, day, "CALL", k), t, tol)
        p = px_at(bars, build_occ_symbol(ROOT, day, "PUT", k), t, tol)
        if c and p:
            cands.append((abs(c - p), k + c - p))
    if len(cands) < 2:
        return None
    cands.sort()
    return st.median(v for _, v in cands[:3])


def settle_value(sc, lc, sp, lp, s):
    """Cash value of the condor at the SPX close (what we owe)."""
    return (max(0.0, s - sc) - max(0.0, s - lc)) + (max(0.0, sp - s) - max(0.0, lp - s))


# ── Backtest ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2024, 2, 1))
    ap.add_argument("--end", type=dt.date.fromisoformat, default=dt.date(2026, 10, 30))
    ap.add_argument("--out-dir", type=Path, default=bt.RESULTS_DIR / "spx_condor")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--progress", action="store_true")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    use_cache = not args.no_cache

    stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    opt = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    daily, trading_days = bt.load_daily(stock, args.start, args.end, use_cache)
    days = [d for d in trading_days if args.start <= d <= args.end]
    _, bars_by_day = bt.load_minutes(stock, args.start, args.end, use_cache)
    print(f"{len(days)} sessions {days[0]} .. {days[-1]}", flush=True)

    rows = []          # one per (day, variant, slip)
    skipped = []
    for n, day in enumerate(days, 1):
        spy = bars_by_day.get(day, [])
        if not spy:
            skipped.append((day, "no SPY bars"))
            continue
        lo = math.floor(min(b.low for b in spy) * RATIO_GUESS / STEP) * STEP - 120
        hi = math.ceil(max(b.high for b in spy) * RATIO_GUESS / STEP) * STEP + 120
        try:
            bars = load_day(opt, day, lo, hi, use_cache)
        except Exception as exc:
            skipped.append((day, f"fetch failed: {exc}"))
            continue
        if not bars:
            skipped.append((day, "no option bars"))
            continue

        close_t = dt.datetime.combine(day, dt.time(15, 58), tzinfo=ET)
        spy_close = spy[-1].close
        s_close = (parity_spot(bars, day, close_t, spy_close * RATIO_GUESS, tol=2)
                   or parity_spot(bars, day, close_t - 5 * ONE_MIN, spy_close * RATIO_GUESS, tol=3))
        if s_close is None:
            skipped.append((day, "no SPX close estimate"))
            continue

        count = 0
        for et in ENTRY_TIMES:
            t = dt.datetime.combine(day, et, tzinfo=ET)
            spy_px = next((b.close for b in spy if b.t >= t), None)
            s0 = parity_spot(bars, day, t, (spy_px or spy_close) * RATIO_GUESS)
            if s0 is None:
                continue
            for dist in DISTANCES:
                for wid in WIDTHS:
                    sc = math.ceil((s0 + dist) / STEP) * STEP
                    lc = sc + wid
                    sp = math.floor((s0 - dist) / STEP) * STEP
                    lp = sp - wid
                    p = [px_at(bars, build_occ_symbol(ROOT, day, cp, k), t)
                         for cp, k in (("CALL", sc), ("CALL", lc), ("PUT", sp), ("PUT", lp))]
                    if any(x is None or x <= 0 for x in p):
                        continue
                    sv = settle_value(sc, lc, sp, lp, s_close)
                    for sname, slip in SLIPS.items():
                        credit = _sell(p[0], slip) - _buy(p[1], slip) + _sell(p[2], slip) - _buy(p[3], slip)
                        if credit <= 0 or credit > wid * 0.6:
                            continue
                        rows.append({
                            "date": day.isoformat(), "variant": f"{et.strftime('%H%M')}_d{dist:g}_w{wid:g}",
                            "slip": sname, "spot": round(s0, 1), "close": round(s_close, 1),
                            "credit": round(credit, 3), "pnl": round((credit - sv) * 100, 2),
                            "collateral": round((wid - credit) * 100, 0),
                        })
                    count += 1
        if args.progress:
            print(f"[{n:>4}/{len(days)}] {day}  SPX~{s_close:.0f}  condors priced: {count}", flush=True)

    with (args.out_dir / "spx_trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "variant", "slip", "spot", "close", "credit", "pnl", "collateral"])
        w.writeheader()
        w.writerows(rows)

    # summary
    by = {}
    for r in rows:
        by.setdefault((r["variant"], r["slip"]), []).append(r)
    out = []
    for (v, s), l in sorted(by.items()):
        x = [r["pnl"] for r in l]
        if len(x) < 30:
            continue
        m = st.mean(x)
        se = st.stdev(x) / math.sqrt(len(x))
        eq, peak, dd = 0.0, 0.0, 0.0
        for r in sorted(l, key=lambda r: r["date"]):
            eq += r["pnl"]
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        yr = {y: round(sum(r["pnl"] for r in l if r["date"].startswith(y))) for y in ("2024", "2025", "2026")}
        out.append({"variant": v, "slip": s, "n": len(x), "mean": round(m, 2), "t": round(m / se, 2),
                    "win_rate": round(sum(p > 0 for p in x) / len(x) * 100, 1), "total": round(sum(x)),
                    "avg_credit": round(st.mean(r["credit"] for r in l), 2),
                    "avg_collateral": round(st.mean(r["collateral"] for r in l)),
                    "max_dd": round(dd), "y2024": yr["2024"], "y2025": yr["2025"], "y2026": yr["2026"]})
    with (args.out_dir / "spx_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else ["none"])
        w.writeheader()
        w.writerows(out)
    print(f"Done. {len(rows)} priced condors, {len(skipped)} skipped sessions "
          f"-> {args.out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
