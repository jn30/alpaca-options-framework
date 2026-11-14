#!/usr/bin/env python3
"""
Backtest of the two rule sets spread_bot.py trades (Nectar, Comb) on real
Alpaca option history, with the bot's own rules (spread_rules.py).

PRE-REGISTERED (fixed before the first run, do not tune afterwards)
-------------------------------------------------------------------
Primary variants: "nectar_high" and "comb_high" — each rule set exactly as the
bot trades it, one contract, high slippage (0.03 $ per leg per side).
A strategy is GO only if BOTH hold for its primary variant:
  1. mean P&L per trade > 0 with t >= 2 (t on monthly sums)
  2. positive total in each calendar year 2024, 2025 and 2026
The base-slippage variants and the account simulation are context only.

Rules (as in spread_rules.py / spread_bot.py)
---------------------------------------------
Nectar: Monday 10:00 ET (Tuesday if Monday is a holiday). Short put =
  round(SPY - 2.50), long put 5 below; short call = round(SPY * 1.03), long
  call 3 above. Expiry Thursday (Friday for a Tuesday entry). Buy-back limit
  at 75% of the credit.
Comb: Monday 10:00 ET. Short put nearest 15 delta, long put 10 below, expiry
  the next Friday at least 7 days out. Buy-back limit at 50% of the credit.
Both: no stop; closed 30 minutes before the bell on expiry day.

Model and limitations
---------------------
- Option minute TRADE bars (not NBBO). A spread is marked only at minutes
  where both of its legs printed within SYNC_WINDOW minutes (legs printing at
  different times fake take-profits — see credit_spread.py). Nectar's far-OTM
  call legs trade rarely and are worth cents: they use their last print (0 if
  none), which moves results by pennies.
- A mark is kept inside its no-arbitrage bounds [intrinsic, width].
- Slippage per leg per side: base 0.01 $, high 0.03 $. The entry credit is the
  mark minus slippage. The buy-back is a resting limit: it fills AT the limit
  price once mark + slippage reaches it (no price improvement). The expiry-day
  close pays mark + slippage.
- Comb's short strike: delta from each put's own implied vol at 10:00
  (Black-Scholes, r=4%, q=1.3%); Alpaca has no historical greeks.
- The bot's Tuesday retry after a Monday without fill, and early assignment,
  are not modelled. Entry is the first synchronous print 10:00-15:00.
- Account simulation: contracts from cash with the bot's 7/93 allocation
  (spread_rules.allocate), compounding, starting at 815 $ and 10,500 $.

Usage
-----
    python backtest/spread_strategies.py
    python backtest/spread_strategies.py --start 2026-01-05 --end 2026-03-27
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import math
import statistics as st
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import spread_rules as rules

DEFAULT_START = dt.date(2024, 2, 1)
DEFAULT_END = dt.date(2026, 10, 30)
RESULTS = Path(__file__).resolve().parent / "results" / "spread_strategies"

SYNC_WINDOW = 5                      # minutes within which both legs of a spread must print
ENTRY_MIN, ENTRY_END = 600, 900      # 10:00 - 15:00 ET, minute of day
CLOSE_BEFORE_BELL = 30
SLIPS = {"base": 0.01, "high": 0.03}
TP_FRAC = {"nectar": 0.75, "comb": 0.50, "comb5": 0.50}
SHARES = {"nectar": 0.07, "comb": 0.93}
COMB_DELTA, COMB_WIDTH = 0.15, 10.0
START_CASH = (815.0, 10500.0)
# Stop-loss study (2026-11-14, variants fixed before their first run). "none" is the
# bot as it trades. PRIMARY of the study: "x2" = close when the position costs
# 2x the credit, high slippage, same GO criteria. x1.5 / x3 / touch (SPY reaches
# a short strike) are context only; picking the best of them is NOT a result.
STOPS = {"none": {}, "x1.5": {"stop_mult": 1.5}, "x2": {"stop_mult": 2.0}, "x3": {"stop_mult": 3.0},
         "touch": {"stop_touch": True}}


# ── Pure model ────────────────────────────────────────────────────────────────

@dataclass
class Vertical:
    short_sym: str
    long_sym: str
    short_k: float
    width: float
    call: bool


def mark(v: Vertical, px: dict[str, float], spot: float) -> float:
    """Value of one vertical, kept inside [intrinsic, width]."""
    itm = (spot - v.short_k) if v.call else (v.short_k - spot)
    intrinsic = min(v.width, max(0.0, itm))
    return round(max(intrinsic, min(v.width, px[v.short_sym] - px[v.long_sym])), 4)


def _last_print(bars: list[tuple[int, float]], m: int, window: Optional[int]) -> Optional[float]:
    """Last price at or before minute `m` (within `window` minutes if given)."""
    i = bisect.bisect_right(bars, (m, float("inf"))) - 1
    if i < 0 or (window is not None and bars[i][0] < m - window):
        return None
    return bars[i][1]


def _prices(verticals, far: set[str], minutes: dict, iso: str, m: int, carry: dict) -> Optional[dict]:
    """Leg prices at minute m, or None if a near leg has no fresh print."""
    px = {}
    for v in verticals:
        for sym in (v.short_sym, v.long_sym):
            bars = minutes.get(sym, {}).get(iso, [])
            if sym in far:
                p = _last_print(bars, m, None)
                px[sym] = carry.get(sym, 0.0) if p is None else p
            else:
                p = _last_print(bars, m, SYNC_WINDOW)
                if p is None:
                    return None
                px[sym] = p
    return px


def _spot(spot: dict, iso: str, m: int) -> Optional[float]:
    day = spot.get(iso, {})
    for k in range(m, m - 30, -1):
        if k in day:
            return day[k]
    return None


def simulate_trade(verticals: list[Vertical], far: set[str], minutes: dict, spot: dict,
                   days: list[dt.date], entry: dt.date, expiry: dt.date,
                   tp_frac: float, slip: float, stop_mult: Optional[float] = None,
                   stop_touch: bool = False) -> Optional[dict]:
    """One position from entry to exit, in $ per share. `minutes` =
    {symbol: {iso: [(minute, price)]}} sorted; `spot` = {iso: {minute: SPY}}.
    stop_mult: close once the position's mark reaches this multiple of the
    credit. stop_touch: close once SPY reaches a short strike. A stop pays the
    mark at that moment plus slippage (so a gap through the level costs more)."""
    legs = 2 * len(verticals)
    carry: dict[str, float] = {}

    def value(iso: str, m: int) -> Optional[float]:
        s = _spot(spot, iso, m)
        px = _prices(verticals, far, minutes, iso, m, carry)
        if s is None or px is None:
            return None
        return sum(mark(v, px, s) for v in verticals)

    e = entry.isoformat()
    credit = entry_min = None
    for m in range(ENTRY_MIN, ENTRY_END):
        val = value(e, m)
        if val is not None:
            credit, entry_min = round(val - slip * legs, 2), m
            break
    if credit is None or credit <= 0.05:
        return None
    buyback = rules.buyback_price(credit, tp_frac)
    base = {"entry": e, "entry_min": entry_min, "credit": credit, "buyback": buyback}

    for d in days:
        if d < entry or d > expiry:
            continue
        iso = d.isoformat()
        last_min = max(spot.get(iso, {}), default=959)
        close_min = min(930, last_min - CLOSE_BEFORE_BELL + 1) if d == expiry else None
        first = entry_min + 1 if d == entry else 570
        last_val = None
        for m in range(first, 960 if close_min is None else close_min + 15):
            if close_min is not None and m >= close_min:
                val = value(iso, m)
                if val is None and m < close_min + 14:
                    continue                      # wait a few minutes for a synchronous print
                if val is None:                   # none came: last mark of the day, else intrinsic
                    s = _spot(spot, iso, m) or 0.0
                    val = last_val if last_val is not None else 0.0
                    val = max(val, sum(mark(v, {v.short_sym: 0.0, v.long_sym: 0.0}, s) for v in verticals))
                return {**base, "cost": round(val + slip * legs, 2), "exit": iso,
                        "exit_min": close_min, "reason": "expiry_close"}
            val = value(iso, m)
            if val is None:
                continue
            last_val = val
            if val + slip * legs <= buyback:
                return {**base, "cost": buyback, "exit": iso, "exit_min": m, "reason": "tp"}
            stopped = stop_mult is not None and val >= credit * stop_mult
            if stop_touch and not stopped:
                s_now = _spot(spot, iso, m)
                stopped = s_now is not None and any(
                    (s_now >= v.short_k) if v.call else (s_now <= v.short_k) for v in verticals)
            if stopped:
                return {**base, "cost": round(val + slip * legs, 2), "exit": iso, "exit_min": m,
                        "reason": "stop"}
        for v in verticals:                       # carry the far legs' last print into the next day
            for sym in (v.short_sym, v.long_sym):
                if sym in far:
                    p = _last_print(minutes.get(sym, {}).get(iso, []), 959, None)
                    if p is not None:
                        carry[sym] = p
    return None


def account_sim(trades: list[dict], start_cash: float) -> dict:
    """Compounding account with the bot's sizing: contracts from cash, 7/93
    split with spill-over, one position per strategy. `trades` = one-contract
    results with strategy/entry/exit/credit/cost/width."""
    cash, low, open_pos, n = start_cash, start_cash, [], 0
    by_day: dict[str, dict[str, dict]] = defaultdict(dict)
    for t in trades:
        by_day[t["entry"]][t["strategy"]] = t
    for day in sorted(by_day):
        for p in [p for p in open_pos if p["exit"] < day]:
            cash += p["pnl"]
            open_pos.remove(p)
        low = min(low, cash)
        busy = {p["strategy"] for p in open_pos}
        cand = {k: t for k, t in by_day[day].items() if k not in busy}
        loss = {k: (t["width"] - t["credit"]) * 100.0 for k, t in cand.items()}
        reserved = sum(p["risk"] for p in open_pos)
        if open_pos:
            qty = {k: rules.qty_alongside(cash, cash - reserved, SHARES[k], loss[k]) for k in cand}
        else:
            qty = rules.allocate(cash, loss, SHARES)
        for k in ("comb", "nectar"):
            q = qty.get(k, 0)
            if k not in cand or q <= 0:
                continue
            q = min(q, int((cash - reserved) // loss[k]))
            if q <= 0:
                continue
            t = cand[k]
            open_pos.append({"strategy": k, "exit": t["exit"], "risk": loss[k] * q,
                             "pnl": (t["credit"] - t["cost"]) * 100.0 * q})
            reserved += loss[k] * q
            n += 1
    for p in open_pos:
        cash += p["pnl"]
    low = min(low, cash)
    return {"start": start_cash, "end": round(cash, 2), "low": round(low, 2), "trades": n}


# ── Data (Alpaca) ─────────────────────────────────────────────────────────────

def _load(start: dt.date, end: dt.date, use_cache: bool):
    import config
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from backtest import engine as bt
    stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    opt = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    _, bars_by_day = bt.load_minutes(stock, start, end, use_cache)
    spot = {}
    for d, bars in bars_by_day.items():
        day = {b.t.hour * 60 + b.t.minute: b.close for b in bars if 570 <= b.t.hour * 60 + b.t.minute < 960}
        if day:
            spot[d.isoformat()] = day
    return opt, spot


def _minute_bars(opt, symbols: list[str], start: dt.datetime, end: dt.datetime) -> dict:
    """{symbol: {iso: [(minute, close)]}} of RTH option trade bars."""
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame
    import config
    out: dict[str, dict[str, list]] = {}
    for i in range(0, len(symbols), 100):
        req = OptionBarsRequest(symbol_or_symbols=symbols[i:i + 100], timeframe=TimeFrame.Minute,
                                start=start, end=end)
        for sym, bars in opt.get_option_bars(req).data.items():
            per = defaultdict(list)
            for b in bars:
                t = b.timestamp.astimezone(config.ET)
                m = t.hour * 60 + t.minute
                if 570 <= m < 960:
                    per[t.date().isoformat()].append((m, float(b.close)))
            out[sym] = {k: sorted(v) for k, v in per.items()}
    return out


def _legs_minutes(opt, tag: str, symbols: list[str], entry: dt.date, expiry: dt.date, use_cache: bool) -> dict:
    import config
    from backtest import engine as bt
    key = f"ss_{tag}_{entry}_{expiry}_{'_'.join(s[-9:] for s in symbols)}"
    return bt._cached(key, lambda: _minute_bars(
        opt, symbols, dt.datetime.combine(entry, dt.time(9, 30), tzinfo=config.ET),
        dt.datetime.combine(expiry, dt.time(16, 0), tzinfo=config.ET)), use_cache)


def _comb_short_strike(opt, entry: dt.date, expiry: dt.date, s0: float, use_cache: bool) -> Optional[int]:
    """Put nearest 15 delta at 10:00, from prints 09:45-10:15 and their own implied vol."""
    import config
    from backtest import credit_spread as cs
    from backtest import engine as bt
    from strikes import build_occ_symbol
    ks = list(range(math.floor(s0 * 0.88), math.floor(s0) + 1))
    syms = {build_occ_symbol("SPY", expiry, "PUT", k): k for k in ks}
    raw = bt._cached(f"ss_chain_{entry}_{expiry}_{ks[0]}_{ks[-1]}", lambda: _minute_bars(
        opt, list(syms), dt.datetime.combine(entry, dt.time(9, 45), tzinfo=config.ET),
        dt.datetime.combine(entry, dt.time(10, 15), tzinfo=config.ET)), use_cache)
    px = {}
    for sym, k in syms.items():
        p = _last_print(raw.get(sym, {}).get(entry.isoformat(), []), ENTRY_MIN + 15, 30)
        if p is not None:
            px[k] = p
    t = ((expiry - entry).days + 0.25) / 365.0          # 10:00 -> 16:00 of expiry day
    return cs.choose_short(px, s0, t, False, COMB_DELTA)


def run(start: dt.date, end: dt.date, use_cache: bool = True) -> list[dict]:
    from strikes import build_occ_symbol
    opt, spot = _load(start, end, use_cache)
    days = sorted(dt.date.fromisoformat(d) for d in spot)
    day_set = set(days)
    trades: list[dict] = []
    for entry in days:
        if entry < start or not rules.nectar_entry_ok(entry, day_set):
            continue
        s0 = _spot(spot, entry.isoformat(), ENTRY_MIN)
        if s0 is None:
            continue
        specs = []
        # Nectar
        exp = rules.nectar_expiry(entry, day_set)
        if exp <= end and exp > entry:
            k = rules.nectar_strikes(s0)
            sym = {n: build_occ_symbol("SPY", exp, "CALL" if "call" in n else "PUT", k[n]) for n in k}
            specs.append(("nectar", exp, [Vertical(sym["short_put"], sym["long_put"], k["short_put"], 5.0, False),
                                          Vertical(sym["short_call"], sym["long_call"], k["short_call"], 3.0, True)],
                          {sym["short_call"], sym["long_call"]}, 5.0, k))
        # Comb (Mondays only)
        exp = rules.comb_expiry(entry, day_set)
        if entry.weekday() == 0 and exp <= end:
            short = _comb_short_strike(opt, entry, exp, s0, use_cache)
            if short is not None:
                k = {"short_put": float(short), "long_put": float(short) - COMB_WIDTH}
                sym = {n: build_occ_symbol("SPY", exp, "PUT", k[n]) for n in k}
                specs.append(("comb", exp, [Vertical(sym["short_put"], sym["long_put"], k["short_put"],
                                                     COMB_WIDTH, False)], set(), COMB_WIDTH, k))
                # Context variant (2026-11-14): same short put, 5$ wing, so one contract fits a ~800$ account.
                k5 = {"short_put": float(short), "long_put": float(short) - 5.0}
                sym5 = {n: build_occ_symbol("SPY", exp, "PUT", k5[n]) for n in k5}
                specs.append(("comb5", exp, [Vertical(sym5["short_put"], sym5["long_put"], k5["short_put"],
                                                      5.0, False)], set(), 5.0, k5))
            else:
                print(f"{entry} comb: no 15-delta put found", flush=True)
        for name, exp, verts, far, width, k in specs:
            syms = [s for v in verts for s in (v.short_sym, v.long_sym)]
            minutes = _legs_minutes(opt, name, syms, entry, exp, use_cache)
            for slip_name, slip in SLIPS.items():
                for stop_name, stop in STOPS.items():
                    r = simulate_trade(verts, far, minutes, spot, days, entry, exp, TP_FRAC[name], slip, **stop)
                    if r is None:
                        if slip_name == "high" and stop_name == "none":
                            print(f"{entry} {name}: no trade (no synchronous prints / credit too small)",
                                  flush=True)
                        continue
                    variant = f"{name}_{slip_name}" if stop_name == "none" else f"{name}_{stop_name}_{slip_name}"
                    trades.append({"variant": variant, "strategy": name, "slip": slip_name, "stop": stop_name,
                                   "expiry": exp.isoformat(), "spot": round(s0, 2), "width": width,
                                   "short_put": k["short_put"],
                                   "pnl": round((r["credit"] - r["cost"]) * 100.0, 2), **r})
        print(f"{entry} done ({len(trades)} trade rows)", flush=True)
    return trades


# ── Report ────────────────────────────────────────────────────────────────────

def summarize(trades: list[dict]) -> list[dict]:
    by_v: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_v[t["variant"]].append(t)
    rows = []
    for v, ts in sorted(by_v.items()):
        pnl = [t["pnl"] for t in ts]
        monthly, yearly = defaultdict(float), defaultdict(float)
        for t in ts:
            monthly[t["entry"][:7]] += t["pnl"]
            yearly[t["entry"][:4]] += t["pnl"]
        m = list(monthly.values())
        t_stat = st.mean(m) / (st.stdev(m) / math.sqrt(len(m))) if len(m) > 2 and st.stdev(m) > 0 else 0.0
        eq = peak = mdd = 0.0
        for t in sorted(ts, key=lambda x: x["exit"]):
            eq += t["pnl"]
            peak = max(peak, eq)
            mdd = min(mdd, eq - peak)
        wins = [p for p in pnl if p > 0]
        losses = [p for p in pnl if p <= 0]
        rows.append({
            "variant": v, "trades": len(ts), "win_pct": round(100 * len(wins) / len(pnl), 1),
            "tp_pct": round(100 * sum(t["reason"] == "tp" for t in ts) / len(ts), 1),
            "stop_pct": round(100 * sum(t["reason"] == "stop" for t in ts) / len(ts), 1),
            "mean": round(st.mean(pnl), 2), "total": round(sum(pnl), 2), "t_monthly": round(t_stat, 2),
            "y2024": round(yearly.get("2024", 0), 2), "y2025": round(yearly.get("2025", 0), 2),
            "y2026": round(yearly.get("2026", 0), 2), "max_dd": round(mdd, 2),
            "avg_credit": round(st.mean(t["credit"] for t in ts), 2),
            "avg_win": round(st.mean(wins), 2) if wins else 0.0,
            "avg_loss": round(st.mean(losses), 2) if losses else 0.0, "worst": round(min(pnl), 2),
        })
    return rows


def verdict(rows: list[dict], variant: str) -> str:
    p = next((r for r in rows if r["variant"] == variant), None)
    if p is None:
        return f"PRIMARY {variant}: no trades."
    ok1 = p["mean"] > 0 and p["t_monthly"] >= 2
    ok2 = min(p["y2024"], p["y2025"], p["y2026"]) > 0
    return (f"PRIMARY {variant}: {'GO' if ok1 and ok2 else 'NO-GO'}\n"
            f"  [{'PASS' if ok1 else 'FAIL'}] mean {p['mean']:+.2f} $/trade, t {p['t_monthly']:+.2f} (need >0, t>=2)\n"
            f"  [{'PASS' if ok2 else 'FAIL'}] years 2024 {p['y2024']:+.0f} / 2025 {p['y2025']:+.0f} / "
            f"2026 {p['y2026']:+.0f} (all > 0)\n"
            f"  [info] max drawdown {p['max_dd']:.0f} $, worst trade {p['worst']:.0f} $ (per contract)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=dt.date.fromisoformat, default=DEFAULT_START)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=DEFAULT_END)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", default=str(RESULTS))
    ap.add_argument("--sync", type=int, default=5,
                    help="minutes within which both legs of a spread must print (robustness check)")
    args = ap.parse_args()
    globals()["SYNC_WINDOW"] = args.sync
    trades = run(args.start, args.end, not args.no_cache)
    if not trades:
        print("no trades")
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0]))
        w.writeheader()
        w.writerows(trades)
    rows = summarize(trades)
    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print()
    for r in rows:
        print(r)
    print()
    for v in ("nectar_high", "comb_high", "nectar_x2_high", "comb_x2_high"):
        print(verdict(rows, v))
    print("\nAccount simulation (bot sizing, compounding):")
    for slip in SLIPS:
        for stop in STOPS:
            sel = [t for t in trades if t["slip"] == slip and t["stop"] == stop and t["strategy"] in SHARES]
            for cash in START_CASH:
                print(f"  slip {slip}, stop {stop}: {account_sim(sel, cash)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
