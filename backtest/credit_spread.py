#!/usr/bin/env python3
"""
30-45 DTE SPY credit spreads / iron condors on real Alpaca option history.

Why this exists
---------------
Every 0DTE variant (directional, SPY condor, SPX condor) failed: no robust edge,
and Alpaca paper buys 0DTE short legs back at ~15:45 ET, so "hold to expiry" is
not realizable. The variance risk premium is best documented at 30-45 DTE, where
the 15:45 close does not matter. This tests that idea on the data Alpaca has
(option bars from 2024-02).

PRE-REGISTERED (fixed before the first run, do not tune afterwards)
-------------------------------------------------------------------
Primary variant: put credit spread, short leg ~16 delta, 3$ wide, one entry per
week (first trading day, at the daily close), expiry the Friday nearest to 38
calendar days out, exit at 50% of the credit or at the first close with <= 21
days to expiry, high slippage.
GO only if ALL hold for the primary variant:
  1. mean P&L per trade > 0 with t >= 2 (t on monthly sums: overlapping weekly
     trades are not independent)
  2. positive total in each calendar year 2024, 2025 and 2026
  (3. max drawdown < 500 $ was dropped before the first full run: the user
      only cares about the end result, not the path. Shown for information.)
Other variants (iron condor, 30 delta, no take-profit) are for context only;
picking the best of them is NOT a result.

Model and limitations
---------------------
- Daily option trade bars (close = last trade of the day, not NBBO). Legs with
  no trade on a day keep their last close; a mark is never below intrinsic.
- Short strike chosen by delta from each strike's own implied vol (Black-Scholes,
  r=4%, q=1.3%), so the put skew is respected.
- Slippage per leg per side: base 0.01 $, high 0.03 $.
- Exits are checked on daily closes only (no intraday TP/stop path).
- Cash settlement is never needed: every exit happens >= 21 DTE.

Usage
-----
    python backtest/credit_spread.py                         # full range
    python backtest/credit_spread.py --start 2024-02-05 --end 2024-04-30
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import math
import statistics as st
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest
from alpaca.data.timeframe import TimeFrame
from strikes import build_occ_symbol
from backtest import engine as bt

ET = bt.ET
DEFAULT_START = dt.date(2024, 2, 5)
DEFAULT_END = dt.date(2026, 10, 30)
RESULTS = Path(__file__).resolve().parent / "results" / "credit_spread"

R, Q = 0.04, 0.013
TARGET_DTE = 38
TIME_EXIT_DTE = 21
TP_FRAC = 0.50
WIDTH = 3.0
DELTAS = (0.16, 0.30)
STRUCTURES = ("pcs", "ccs", "ic", "regime")   # regime = pcs/ccs/ic by trend rule (see regime())
EXITS = ("tp50_21dte", "21dte")
SLIPS = {"base": 0.01, "high": 0.03}
PRIMARY = "pcs_d16_tp50_21dte_high"
# Second pre-registered test (2026-11-04, decided before its first run): the
# regime switch -- up trend -> put spread, down trend -> call spread, flat -> condor.
PRIMARY_REGIME = "regime_d16_tp50_21dte_high"
MIN_PRICE = 0.05


# ── Black-Scholes ─────────────────────────────────────────────────────────────

def _n(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(s: float, k: float, t: float, vol: float, call: bool) -> float:
    if t <= 0 or vol <= 0:
        return max(0.0, (s - k) if call else (k - s))
    d1 = (math.log(s / k) + (R - Q + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if call:
        return s * math.exp(-Q * t) * _n(d1) - k * math.exp(-R * t) * _n(d2)
    return k * math.exp(-R * t) * _n(-d2) - s * math.exp(-Q * t) * _n(-d1)


def implied_vol(price: float, s: float, k: float, t: float, call: bool) -> Optional[float]:
    lo, hi = 0.01, 3.0
    if not (bs_price(s, k, t, lo, call) < price < bs_price(s, k, t, hi, call)):
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(s, k, t, mid, call) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bs_delta(s: float, k: float, t: float, vol: float, call: bool) -> float:
    d1 = (math.log(s / k) + (R - Q + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return math.exp(-Q * t) * (_n(d1) if call else _n(d1) - 1.0)


# ── Data ──────────────────────────────────────────────────────────────────────

def fetch_daily_option_bars(client, symbols: list[str], start: dt.date,
                            end: dt.date) -> dict[str, dict[str, float]]:
    """{symbol: {iso date: close}} from daily trade bars."""
    out: dict[str, dict[str, float]] = {}
    end = min(end, dt.date.today() - dt.timedelta(days=1))   # recent data needs an OPRA agreement
    for i in range(0, len(symbols), 50):
        req = OptionBarsRequest(
            symbol_or_symbols=symbols[i:i + 50], timeframe=TimeFrame.Day,
            start=dt.datetime.combine(start, dt.time(0, 0), tzinfo=ET),
            end=dt.datetime.combine(end, dt.time(23, 59), tzinfo=ET))
        for sym, bars in client.get_option_bars(req).data.items():
            out[sym] = {b.timestamp.astimezone(ET).date().isoformat(): float(b.close) for b in bars}
    return out


def fetch_minute_option_bars(client, symbols: list[str], start: dt.date,
                             end: dt.date) -> dict[str, dict[str, list[tuple[int, float]]]]:
    """{symbol: {iso date: [(minute of day, close), ...]}} for RTH minute bars."""
    out: dict[str, dict[str, list[tuple[int, float]]]] = {}
    end = min(end, dt.date.today() - dt.timedelta(days=1))
    if end < start:
        return out
    req = OptionBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
        start=dt.datetime.combine(start, dt.time(9, 30), tzinfo=ET),
        end=dt.datetime.combine(end, dt.time(16, 0), tzinfo=ET))
    for sym, bars in client.get_option_bars(req).data.items():
        per: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for b in bars:
            t = b.timestamp.astimezone(ET)
            m = t.hour * 60 + t.minute
            if 570 <= m < 960:
                per[t.date().isoformat()].append((m, float(b.close)))
        out[sym] = dict(per)
    return out


SYNC_WINDOW = 5      # minutes: all legs must print within this window to form a mark


def synced_mark(minutes: dict[str, dict[str, list[tuple[int, float]]]], syms: list[str],
                iso: str) -> Optional[dict[str, float]]:
    """Latest moment of the day at which every leg printed within SYNC_WINDOW
    minutes; returns each leg's last price in that window, else None."""
    per = []
    for s in syms:
        bars = minutes.get(s, {}).get(iso)
        if not bars:
            return None
        per.append(sorted(bars))
    for m, _ in sorted({x for bars in per for x in bars}, reverse=True):
        px = {}
        for s, bars in zip(syms, per):
            hit = [p for mm, p in bars if m - SYNC_WINDOW <= mm <= m]
            if not hit:
                break
            px[s] = hit[-1]
        if len(px) == len(syms):
            return px
    return None


def pick_expiry(entry: dt.date) -> dt.date:
    target = entry + dt.timedelta(days=TARGET_DTE)
    fridays = [target + dt.timedelta(days=(4 - target.weekday()) % 7 - k) for k in (0, 7)]
    return min(fridays, key=lambda d: abs((d - target).days))


def chain_on(client, day: dt.date, expiry: dt.date, spot: float, use_cache: bool):
    """Entry-day closes for puts 82-100% and calls 100-115% of spot ($1 strikes)."""
    puts = list(range(math.floor(spot * 0.82), math.ceil(spot) + 1))
    calls = list(range(math.floor(spot), math.ceil(spot * 1.15) + 1))
    key = f"cs_chain_{day}_{expiry}_{puts[0]}_{calls[-1]}"

    def fetch():
        syms = ([build_occ_symbol("SPY", expiry, "PUT", k) for k in puts]
                + [build_occ_symbol("SPY", expiry, "CALL", k) for k in calls])
        return fetch_daily_option_bars(client, syms, day, day)
    raw = bt._cached(key, fetch, use_cache)
    d = day.isoformat()
    put_px = {k: raw.get(build_occ_symbol("SPY", expiry, "PUT", k), {}).get(d) for k in puts}
    call_px = {k: raw.get(build_occ_symbol("SPY", expiry, "CALL", k), {}).get(d) for k in calls}
    return ({k: v for k, v in put_px.items() if v}, {k: v for k, v in call_px.items() if v})


def choose_short(px: dict[int, float], spot: float, t: float, call: bool,
                 target: float) -> Optional[int]:
    best, best_err = None, 1e9
    for k, p in px.items():
        if p < MIN_PRICE or (call and k <= spot) or (not call and k >= spot):
            continue
        vol = implied_vol(p, spot, k, t, call)
        if vol is None:
            continue
        err = abs(abs(bs_delta(spot, k, t, vol, call)) - target)
        if err < best_err:
            best, best_err = k, err
    return best if best_err < 0.06 else None


# ── Simulation ────────────────────────────────────────────────────────────────

@dataclass
class Spread:
    call: bool
    short_k: int
    long_k: int
    short_sym: str
    long_sym: str


@dataclass
class TradeRow:
    variant: str
    entry: str
    expiry: str
    spot: float
    legs: str
    credit: float
    max_risk: float
    exit: str
    exit_reason: str
    exit_cost: float
    pnl: float
    regime: str = ""


def regime(closes: list[tuple[str, float]], iso: str) -> str:
    """PRE-REGISTERED trend rule at the entry close: up = close > SMA50 and
    SMA20 > SMA50; down = close < SMA50 and SMA20 < SMA50; else flat."""
    i = next(k for k, (d, _) in enumerate(closes) if d == iso)
    if i < 49:
        return "flat"
    c = [x for _, x in closes[i - 49:i + 1]]
    sma50, sma20, last = sum(c) / 50, sum(c[-20:]) / 20, c[-1]
    if last > sma50 and sma20 > sma50:
        return "up"
    if last < sma50 and sma20 < sma50:
        return "down"
    return "flat"


def mark(sp: Spread, px: dict[str, float], spot: float) -> float:
    """Value of one vertical from synchronous leg prices, kept inside its
    no-arbitrage bounds [spread intrinsic, width]."""
    intrinsic = (min(WIDTH, max(0.0, spot - sp.short_k)) if sp.call
                 else min(WIDTH, max(0.0, sp.short_k - spot)))
    return max(intrinsic, min(WIDTH, px[sp.short_sym] - px[sp.long_sym]))


def simulate(spreads: list[Spread], minutes: dict, days: list[dt.date],
             spot_by_day: dict[str, float], entry: dt.date, expiry: dt.date,
             exit_mode: str, slip: float) -> Optional[tuple[float, float, str, str]]:
    """Returns (credit, exit_cost, exit_date, reason) in $ per share. All legs
    are marked at one synchronous moment per day (see synced_mark)."""
    syms = [s for sp in spreads for s in (sp.short_sym, sp.long_sym)]
    e = entry.isoformat()
    px = synced_mark(minutes, syms, e)
    if px is None:
        return None
    legs = len(syms)
    credit = sum(mark(sp, px, spot_by_day[e]) for sp in spreads) - slip * legs
    if credit <= 0.05:
        return None
    last_cost, time_exit_due = None, False
    for d in days:
        if d <= entry or d > expiry:
            continue
        iso = d.isoformat()
        spot = spot_by_day[iso]
        due = (expiry - d).days <= TIME_EXIT_DTE
        px = synced_mark(minutes, syms, iso)
        if px is None:
            if due:
                time_exit_due = True     # close at the next day with a synchronous mark
            continue
        cost = sum(mark(sp, px, spot) for sp in spreads) + slip * legs
        last_cost = (cost, iso)
        if exit_mode == "tp50_21dte" and cost <= credit * TP_FRAC:
            return credit, cost, iso, "tp50"
        if due or time_exit_due:
            return credit, cost, iso, "21dte"
    if last_cost is not None and (expiry - dt.date.fromisoformat(last_cost[1])).days <= TIME_EXIT_DTE:
        return credit, last_cost[0], last_cost[1], "21dte_late"
    return None


def variant_name(struct: str, delta: float, exit_mode: str, slip_name: str) -> str:
    return f"{struct}_d{int(round(delta * 100))}_{exit_mode}_{slip_name}"


def run(start: dt.date, end: dt.date, use_cache: bool) -> list[TradeRow]:
    stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    opt = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    last_needed = end + dt.timedelta(days=TARGET_DTE + 10)
    daily = bt._cached(f"cs_spy_daily_{start}_{last_needed}",
                       lambda: bt._fetch_daily(stock, start, last_needed), use_cache)
    spot_by_day = {b["d"]: b["c"] for b in daily}
    days = [dt.date.fromisoformat(b["d"]) for b in daily]
    warm = start - dt.timedelta(days=110)          # history for the 50-day SMA
    hist = bt._cached(f"cs_spy_daily_{warm}_{last_needed}",
                      lambda: bt._fetch_daily(stock, warm, last_needed), use_cache)
    closes = [(b["d"], b["c"]) for b in hist]

    entries, seen = [], set()
    for d in days:
        wk = d.isocalendar()[:2]
        if d <= end and wk not in seen:
            seen.add(wk)
            entries.append(d)

    trades: list[TradeRow] = []
    for n, entry in enumerate(entries, 1):
        expiry = pick_expiry(entry)
        spot = spot_by_day[entry.isoformat()]
        t = (expiry - entry).days / 365.0
        puts, calls = chain_on(opt, entry, expiry, spot, use_cache)
        if not puts:     # holiday Friday etc. -> try Thursday
            expiry -= dt.timedelta(days=1)
            puts, calls = chain_on(opt, entry, expiry, spot, use_cache)
        print(f"[{n}/{len(entries)}] {entry} exp {expiry} spot {spot:.2f}  "
              f"puts {len(puts)} calls {len(calls)}", flush=True)

        legs_by_delta: dict[float, dict[str, Spread]] = {}
        for delta in DELTAS:
            got: dict[str, Spread] = {}
            for call, px in ((False, puts), (True, calls)):
                k = choose_short(px, spot, t, call, delta)
                if k is None:
                    continue
                lk = k + int(WIDTH) if call else k - int(WIDTH)
                if lk not in px:
                    continue
                r = "CALL" if call else "PUT"
                got["call" if call else "put"] = Spread(call, k, lk, build_occ_symbol("SPY", expiry, r, k),
                                                        build_occ_symbol("SPY", expiry, r, lk))
            legs_by_delta[delta] = got

        syms = sorted({s for g in legs_by_delta.values() for sp in g.values()
                       for s in (sp.short_sym, sp.long_sym)})
        if not syms:
            continue
        # Minute bars only up to the latest possible time exit (~21 DTE + a few days).
        stop = expiry - dt.timedelta(days=TIME_EXIT_DTE - 7)
        series = bt._cached(f"cs_min_{entry}_{expiry}_{hashlib.md5(','.join(syms).encode()).hexdigest()[:10]}",
                            lambda: fetch_minute_option_bars(opt, syms, entry, stop), use_cache)

        reg = regime(closes, entry.isoformat())
        for struct in STRUCTURES:
            pick = {"up": "pcs", "down": "ccs", "flat": "ic"}[reg] if struct == "regime" else struct
            for delta in DELTAS:
                g = legs_by_delta[delta]
                spreads = {"pcs": [g.get("put")], "ccs": [g.get("call")],
                           "ic": [g.get("put"), g.get("call")]}[pick]
                if not spreads or any(s is None for s in spreads):
                    continue
                for exit_mode in EXITS:
                    for slip_name, slip in SLIPS.items():
                        res = simulate(spreads, series, days, spot_by_day, entry, expiry, exit_mode, slip)
                        if res is None:
                            continue
                        credit, cost, xd, why = res
                        trades.append(TradeRow(
                            variant=variant_name(struct, delta, exit_mode, slip_name),
                            entry=entry.isoformat(), expiry=expiry.isoformat(), spot=round(spot, 2),
                            legs="/".join(f"{'C' if sp.call else 'P'}{sp.short_k}-{sp.long_k}" for sp in spreads),
                            credit=round(credit, 4), max_risk=round((WIDTH - credit) * 100, 2),
                            exit=xd, exit_reason=why, exit_cost=round(cost, 4),
                            pnl=round((credit - cost) * 100, 2), regime=reg))
    return trades


# ── Report ────────────────────────────────────────────────────────────────────

def summarize(trades: list[TradeRow]) -> list[dict]:
    by_v: dict[str, list[TradeRow]] = defaultdict(list)
    for tr in trades:
        by_v[tr.variant].append(tr)
    rows = []
    for v, ts in sorted(by_v.items()):
        pnl = [t.pnl for t in ts]
        monthly: dict[str, float] = defaultdict(float)
        yearly: dict[str, float] = defaultdict(float)
        for t in ts:
            monthly[t.entry[:7]] += t.pnl
            yearly[t.entry[:4]] += t.pnl
        m = list(monthly.values())
        t_stat = (st.mean(m) / (st.stdev(m) / math.sqrt(len(m)))) if len(m) > 2 and st.stdev(m) > 0 else 0.0
        eq, peak, mdd = 0.0, 0.0, 0.0
        for t in sorted(ts, key=lambda x: x.exit):
            eq += t.pnl
            peak = max(peak, eq)
            mdd = min(mdd, eq - peak)
        rows.append({
            "variant": v, "trades": len(ts), "win_pct": round(100 * sum(p > 0 for p in pnl) / len(pnl), 1),
            "mean": round(st.mean(pnl), 2), "total": round(sum(pnl), 2), "t_monthly": round(t_stat, 2),
            "y2024": round(yearly.get("2024", 0), 2), "y2025": round(yearly.get("2025", 0), 2),
            "y2026": round(yearly.get("2026", 0), 2), "max_dd": round(mdd, 2),
            "avg_credit": round(st.mean(t.credit for t in ts), 3),
            "worst": round(min(pnl), 2), "avg_risk": round(st.mean(t.max_risk for t in ts), 2),
        })
    return rows


def verdict(rows: list[dict], primary: str = PRIMARY) -> str:
    p = next((r for r in rows if r["variant"] == primary), None)
    if p is None:
        return f"PRIMARY {primary} has no trades."
    checks = [
        (p["mean"] > 0 and p["t_monthly"] >= 2, f"mean {p['mean']:+.2f} $/trade, t {p['t_monthly']:+.2f} (need >0, t>=2)"),
        (min(p["y2024"], p["y2025"], p["y2026"]) > 0,
         f"years 2024 {p['y2024']:+.0f} / 2025 {p['y2025']:+.0f} / 2026 {p['y2026']:+.0f} (all > 0)"),
    ]
    lines = [f"  [{'PASS' if ok else 'FAIL'}] {txt}" for ok, txt in checks]
    lines.append(f"  [info] max drawdown {p['max_dd']:.0f} $ (not a criterion)")
    go = all(ok for ok, _ in checks)
    return f"PRIMARY {primary}: {'GO' if go else 'NO-GO'}\n" + "\n".join(lines)


def regime_breakdown(trades: list[TradeRow], variant: str) -> str:
    out = [f"{variant} by regime:"]
    by: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.variant == variant:
            by[t.regime].append(t.pnl)
    for reg in ("up", "flat", "down"):
        p = by.get(reg, [])
        if p:
            out.append(f"  {reg:<5} n={len(p):>3}  win {100 * sum(x > 0 for x in p) / len(p):5.1f}%  "
                       f"mean {st.mean(p):+7.2f}  total {sum(p):+8.0f}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=dt.date.fromisoformat, default=DEFAULT_START)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=DEFAULT_END,
                    help="last ENTRY date (exits may run ~3 weeks later)")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    trades = run(args.start, args.end, not args.no_cache)
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "trades.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(trades[0])) if trades else ["variant"])
        w.writeheader()
        w.writerows(asdict(t) for t in trades)
    rows = summarize(trades)
    if rows:
        with open(RESULTS / "summary.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\n{'variant':<26}{'n':>5}{'win%':>7}{'mean':>8}{'total':>9}{'t':>7}"
              f"{'2024':>8}{'2025':>8}{'2026':>8}{'maxDD':>8}{'worst':>8}{'credit':>8}")
        for r in rows:
            print(f"{r['variant']:<26}{r['trades']:>5}{r['win_pct']:>7}{r['mean']:>8}{r['total']:>9}"
                  f"{r['t_monthly']:>7}{r['y2024']:>8.0f}{r['y2025']:>8.0f}{r['y2026']:>8.0f}"
                  f"{r['max_dd']:>8.0f}{r['worst']:>8.0f}{r['avg_credit']:>8}")
        print("\n" + verdict(rows))
        print("\n" + verdict(rows, PRIMARY_REGIME))
        print("\n" + regime_breakdown(trades, PRIMARY_REGIME))
        print(regime_breakdown(trades, PRIMARY_REGIME.replace("_high", "_base")))
    print(f"\nWritten: {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
