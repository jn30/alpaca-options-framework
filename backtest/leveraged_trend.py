#!/usr/bin/env python3
"""
Leveraged S&P 500 with a 200-day trend filter, on decades of daily data.

Why this exists
---------------
Every option-premium strategy we tested lost after costs (0DTE directional,
0DTE SPY/SPX condors, 30-45 DTE spreads/condors, regime switching), while
simply holding SPY gained ~55% over the same window. The one return source
visible in the data is the equity premium itself, so this tests HARVESTING IT
WITH LEVERAGE instead of selling premium against it -- over 1988-2026 (incl.
1990, 2000-02, 2008, 2020, 2022), not just the 2024-26 bull market.

PRE-REGISTERED (fixed before the first run, do not tune afterwards)
-------------------------------------------------------------------
Primary: 2x daily-leveraged S&P 500 total return (like SSO) while the S&P 500
close is above its 200-day SMA, else T-bills. Signal at close t, position from
close t+1 (one day lag). Rule from Gayed & Bilello (2016), not fitted here.
GO only if the primary
  1. has a higher CAGR than S&P 500 buy & hold over 1988-2026, and
  2. beats buy & hold in at least 2 of 3 sub-periods:
     1988-2005, 2006-2015, 2016-2026.
Other variants (1x/2x/3x with and without filter) are context only.

Cost model (per year unless noted)
----------------------------------
- leveraged ETF fee 0.90% (SSO/UPRO ~0.9%)
- financing of the borrowed part: (L-1) x (T-bill + 0.40%)
- cash leg earns the 13-week T-bill (^IRX), like SGOV/BIL
- 0.10% per switch (spread + slippage)
Leverage resets daily, so volatility decay is in the simulation.

Usage
-----
    python backtest/leveraged_trend.py
    python backtest/leveraged_trend.py --price-only   # ^GSPC from 1970, no dividends
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from pathlib import Path

import requests

CACHE = Path(__file__).resolve().parent / "cache"
RESULTS = Path(__file__).resolve().parent / "results" / "leveraged_trend"
FEE = 0.009
FIN_SPREAD = 0.004
SWITCH_COST = 0.001
SMA_N = 200
PERIODS = [("1988-2005", "1988-01-01", "2005-12-31"), ("2006-2015", "2006-01-01", "2015-12-31"),
           ("2016-2026", "2016-01-01", "2026-12-31")]
PRIMARY = "2x_sma200"


def yahoo(symbol: str, refresh: bool) -> dict[str, float]:
    """{iso date: close} from Yahoo's chart API, cached as JSON."""
    path = CACHE / f"yahoo_{symbol.strip('^')}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}"
           f"?period1=0&period2=9999999999&interval=1d")
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    out = {}
    for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            out[dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()] = float(c)
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))
    return out


def simulate(dates, ret, rf, signal, lev: float, use_filter: bool) -> list[float]:
    """Daily equity curve (start 1.0). signal[i] = invested on day i (already lagged)."""
    eq, cur, prev_in = [1.0], 1.0, None
    for i in range(1, len(dates)):
        invested = signal[i] if use_filter else True
        r_f = rf[i]
        if invested:
            r = lev * ret[i] - (lev - 1) * (r_f + FIN_SPREAD / 252) - FEE / 252 * (lev > 1)
        else:
            r = r_f
        if prev_in is not None and invested != prev_in:
            r -= SWITCH_COST
        prev_in = invested
        cur *= 1 + r
        cur = max(cur, 0.0)
        eq.append(cur)
    return eq


def stats(dates, eq, a: str, b: str) -> dict:
    idx = [i for i, d in enumerate(dates) if a <= d <= b]
    if len(idx) < 2:
        return {}
    i0, i1 = idx[0], idx[-1]
    years = (dt.date.fromisoformat(dates[i1]) - dt.date.fromisoformat(dates[i0])).days / 365.25
    if eq[i0] <= 0:
        return {"cagr": -100.0, "mdd": -100.0, "mult": 0.0}
    mult = eq[i1] / eq[i0]
    peak, mdd = eq[i0], 0.0
    for i in range(i0, i1 + 1):
        peak = max(peak, eq[i])
        mdd = min(mdd, eq[i] / peak - 1 if peak > 0 else -1)
    cagr = (mult ** (1 / years) - 1) * 100 if mult > 0 else -100.0
    return {"cagr": round(cagr, 2), "mdd": round(mdd * 100, 1), "mult": round(mult, 2)}


def main() -> int:
    global FIN_SPREAD
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--price-only", action="store_true", help="use ^GSPC from 1970 (no dividends)")
    ap.add_argument("--index", default="^GSPC", help="price index for signal+returns with --price-only "
                                                     "(e.g. ^NDX); default ^GSPC")
    ap.add_argument("--fin-spread", type=float, default=FIN_SPREAD,
                    help="financing spread over T-bills; 0.0078 matches real SSO/UPRO 2009-2026")
    ap.add_argument("--sweep", action="store_true", help="CAGR by leverage 1-4x, with/without filter")
    ap.add_argument("--refresh", action="store_true", help="re-download data")
    args = ap.parse_args()
    FIN_SPREAD = args.fin_spread

    px = yahoo(args.index, args.refresh)                   # signal on the price index
    tr = px if (args.price_only or args.index != "^GSPC") else yahoo("^SP500TR", args.refresh)
    irx = yahoo("^IRX", args.refresh)
    dates = sorted(d for d in tr if d in px)
    rf_last, rf = 0.0, []
    for d in dates:                      # ^IRX is an annual % yield -> daily rate
        if d in irx:
            rf_last = irx[d] / 100 / 252
        rf.append(rf_last)
    ret = [0.0] + [tr[dates[i]] / tr[dates[i - 1]] - 1 for i in range(1, len(dates))]

    # signal: close > SMA200 at day i-1 -> invested on day i (one-day lag)
    closes = [px[d] for d in dates]
    above = [False] * len(dates)
    run = 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= SMA_N:
            run -= closes[i - SMA_N]
        if i >= SMA_N - 1:
            above[i] = c > run / SMA_N
    signal = [False] + above[:-1]
    first = SMA_N                                           # start once the SMA exists
    dates, ret, rf, signal = dates[first:], ret[first:], rf[first:], signal[first:]

    if args.sweep:
        a, b = dates[0], dates[-1]
        print(f"Leverage sweep {args.index} {a}..{b}, fin spread {FIN_SPREAD:.2%}: "
              f"CAGR% (maxDD%) per sub-period")
        heads = [("FULL", a, b)] + PERIODS
        print(f"{'':<14}" + "".join(f"{p:>18}" for p, _, _ in heads))
        for f in (False, True):
            for lev in (1, 1.5, 2, 2.5, 3, 3.5, 4):
                eq = simulate(dates, ret, rf, signal, lev, f)
                cells = [stats(dates, eq, pa, pb) for _, pa, pb in heads]
                print(f"{lev:>4g}x {'sma200' if f else 'hold  '}  " +
                      "".join(f"{c.get('cagr', 0):>9.1f} ({c.get('mdd', 0):>5.0f})" for c in cells))
        return 0

    variants = {f"{l:g}x{'_sma200' if f else ''}": (l, f) for l in (1, 2, 3) for f in (False, True)}
    curves = {name: simulate(dates, ret, rf, signal, l, f) for name, (l, f) in variants.items()}
    a, b = dates[0], dates[-1]
    periods = [("FULL", a, b)] + PERIODS
    print(f"Data {a} .. {b}  ({'price only' if args.price_only else 'total return'}), "
          f"invested {100 * sum(signal) / len(signal):.0f}% of days\n")
    print(f"{'variant':<11}" + "".join(f"{p:>22}" for p, _, _ in periods))
    print(f"{'':<11}" + "".join(f"{'CAGR%  maxDD%   x':>22}" for _ in periods))
    table = {}
    for name in curves:
        row = [stats(dates, curves[name], pa, pb) for _, pa, pb in periods]
        table[name] = row
        print(f"{name:<11}" + "".join(f"{s.get('cagr', 0):>8.1f}{s.get('mdd', 0):>8.1f}{s.get('mult', 0):>6.1f}"
                                      for s in row))

    bh, pr = table["1x"], table[PRIMARY]
    c1 = pr[0]["cagr"] > bh[0]["cagr"]
    wins = sum(pr[k]["cagr"] > bh[k]["cagr"] for k in range(1, 4))
    print(f"\nPRIMARY {PRIMARY}: {'GO' if c1 and wins >= 2 else 'NO-GO'}")
    print(f"  [{'PASS' if c1 else 'FAIL'}] full-period CAGR {pr[0]['cagr']:.2f}% vs buy & hold {bh[0]['cagr']:.2f}%")
    print(f"  [{'PASS' if wins >= 2 else 'FAIL'}] beats buy & hold in {wins}/3 sub-periods (need >= 2)")
    print(f"  [info] max drawdown {pr[0]['mdd']:.1f}% vs buy & hold {bh[0]['mdd']:.1f}%")

    RESULTS.mkdir(parents=True, exist_ok=True)
    tag = "price" if args.price_only else "tr"
    with open(RESULTS / f"equity_{tag}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "invested"] + list(curves))
        for i, d in enumerate(dates):
            w.writerow([d, int(signal[i])] + [f"{curves[n][i]:.6f}" for n in curves])
    print(f"\nWritten: {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
