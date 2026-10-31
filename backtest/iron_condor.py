#!/usr/bin/env python3
"""
0DTE iron-condor feasibility study on real Alpaca option history.

Why this exists
---------------
The live bot is a directional 0DTE momentum strategy. The question here is
whether a *neutral* 0DTE iron condor on SPY could be a sensible complement or
replacement. The study is deliberately independent of the live bot's entries
so it can answer "does the structure itself have an edge?".

Design
------
Each session, at a fixed ET entry time, sell an OTM call spread and an OTM
put spread around spot:

    short call = ceil((spot + distance) / step) * step
    long  call = short call + width
    short put  = floor((spot - distance) / step) * step
    long  put  = short put - width

One condor per session per variant, one contract. Entry credit is the sum of
both spread credits, marked from the option 1-minute bars at the entry minute
with an adverse per-leg slippage haircut.

Exit modes
----------
- expiry      : hold to the close, settle at intrinsic (SPY 16:00 close)
- eod_1525    : buy the condor back at 15:25 ET (live bot's time stop)
- eod_1555    : buy back at 15:55 ET (any eod_HHMM works via --exits)
- tp50_stop100: take profit at 50% of credit, stop at 2x credit, else 15:25
- tp50_expiry : take profit at 50% of credit, otherwise hold to expiry

Filters
-------
- any     : always enter at the fixed time
- neutral : only enter when the live MomentumEngine is neutral at that minute

Data limitations (same as the directional replay)
-------------------------------------------------
- Alpaca historical options provide trade bars, not historical NBBO quotes.
  Fills are modelled from bar prices plus slippage, i.e. a conservative
  trade-price proxy, not a tick-exact fill.
- Intrabar path is unknown. For TP/stop, the high/low of every leg is used to
  build a worst-case and best-case cost envelope; if both the TP and the stop
  can be touched in the same minute, the stop is assumed first.
- Expiry is settled cash-style at intrinsic from the SPY close. Assignment,
  pin risk and early exercise are not modelled.
- Commissions/regulatory fees are ignored (Alpaca options commissions are $0,
  but small regulatory fees exist on sells).

Usage
-----
    python backtest/iron_condor.py --start 2026-09-01 --end 2026-10-30
    python backtest/iron_condor.py                      # full available range
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from momentum import Bar, MomentumEngine
from strikes import build_occ_symbol
from backtest import engine as bt
from backtest.run_suite import build_periods

ET = bt.ET
ONE_MIN = dt.timedelta(minutes=1)
RTH_CLOSE = dt.time(16, 0)
DEFAULT_START = dt.date(2024, 2, 1)
DEFAULT_END = dt.date(2026, 10, 30)

ENTRY_TIMES = (dt.time(9, 45), dt.time(10, 30), dt.time(12, 0))
DISTANCES = (2.0, 3.0, 5.0)
WIDTHS = (1.0, 2.0, 3.0)
EXITS = ("expiry", "eod_1525", "eod_1555", "tp50_stop100", "tp50_expiry")
FILTERS = ("any", "neutral")
SLIPS = {
    # name: (dollars per leg, fraction of premium per leg)
    "base": (0.01, 0.01),
    "high": (0.02, 0.02),
    "stress": (0.03, 0.03),
}
TP_FRAC = 0.50       # buy back at 50% of collected credit
STOP_MULT = 2.0      # buy back at 200% of collected credit


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CondorSpec:
    entry_time: dt.time
    distance: float
    width: float
    exit_mode: str
    filter_mode: str
    slip_name: str

    @property
    def name(self) -> str:
        return (f"{self.entry_time.strftime('%H%M')}_d{self.distance:g}"
                f"_w{self.width:g}_{self.exit_mode}_{self.filter_mode}"
                f"_{self.slip_name}")


@dataclass
class CondorEntry:
    bar_t: dt.datetime
    spot: float
    short_call: float
    long_call: float
    short_put: float
    long_put: float
    sc_sym: str
    lc_sym: str
    sp_sym: str
    lp_sym: str
    # Raw leg prices used to initialise the mark-to-market walk.
    p_sc: float
    p_lc: float
    p_sp: float
    p_lp: float
    state: str


@dataclass
class CondorTrade:
    day: dt.date
    variant: str
    entry_time: dt.datetime
    spot: float
    short_call: float
    long_call: float
    short_put: float
    long_put: float
    credit: float
    max_risk: float
    exit_time: dt.datetime
    exit_reason: str
    exit_cost: float
    pnl: float
    prev_atr: float = 0.0
    state: str = "neutral"


# ── Pricing helpers ───────────────────────────────────────────────────────────

def _sell(p: float, slip: tuple[float, float]) -> float:
    dollars, pct = slip
    return max(0.0, p * (1.0 - pct) - dollars)


def _buy(p: float, slip: tuple[float, float]) -> float:
    dollars, pct = slip
    return max(0.0, p * (1.0 + pct) + dollars)


def entry_credit(sc: float, lc: float, sp: float, lp: float,
                 slip: tuple[float, float]) -> float:
    """Net credit received when selling the four legs."""
    return _sell(sc, slip) - _buy(lc, slip) + _sell(sp, slip) - _buy(lp, slip)


def close_cost(sc: float, lc: float, sp: float, lp: float,
               slip: tuple[float, float]) -> float:
    """Net debit paid to close the four legs (adverse per-leg slippage)."""
    return _buy(sc, slip) - _sell(lc, slip) + _buy(sp, slip) - _sell(lp, slip)


def _bar_at(opt_map: dict[str, dict[dt.datetime, bt.OptBar]], symbol: str,
            t: dt.datetime, tol_min: int = 2) -> Optional[bt.OptBar]:
    bars = opt_map.get(symbol)
    if not bars:
        return None
    bar = bars.get(t)
    if bar is not None:
        return bar
    for i in range(1, tol_min + 1):
        for cand in (t - i * ONE_MIN, t + i * ONE_MIN):
            bar = bars.get(cand)
            if bar is not None:
                return bar
    return None


def _condor_strikes(spy_bars: list[Bar], step: float,
                    extent: float) -> tuple[list[float], float, float]:
    low = min(b.low for b in spy_bars)
    high = max(b.high for b in spy_bars)
    pad = 3.0 * step
    lo = math.floor(low / step) * step - extent - pad
    hi = math.ceil(high / step) * step + extent + pad
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 2) for i in range(n + 1)], lo, hi


def load_condor_options(option_client, day: dt.date, spy_bars: list[Bar],
                        use_cache: bool = True) -> dict[str, list[dict]]:
    extent = max(DISTANCES) + max(WIDTHS)
    strikes, lo, hi = _condor_strikes(spy_bars, config.STRIKE_STEP, extent)
    key = f"condor_options_{day.isoformat()}_{lo:g}_{hi:g}"
    return bt._cached(
        key,
        lambda: bt._fetch_option_day(option_client, day, strikes),
        use_cache,
    )


# ── Entry construction ────────────────────────────────────────────────────────

def _build_entries(
    day: dt.date,
    spy_bars: list[Bar],
    opt_map: dict[str, dict[dt.datetime, bt.OptBar]],
    momentum: MomentumEngine,
) -> dict[tuple[dt.time, float, float], Optional[CondorEntry]]:
    """Price every (entry time, distance, width) condor once per session."""
    step = config.STRIKE_STEP
    states: dict[dt.datetime, str] = {}
    for bar in spy_bars:
        states[bar.t] = momentum.on_bar(bar).direction

    entries: dict[tuple[dt.time, float, float], Optional[CondorEntry]] = {}

    for entry_time in ENTRY_TIMES:
        bar = next((b for b in spy_bars
                    if b.t.time() >= entry_time and b.t.time() < RTH_CLOSE), None)
        if bar is None:
            for distance in DISTANCES:
                for width in WIDTHS:
                    entries[(entry_time, distance, width)] = None
            continue

        spot = bar.close
        for distance in DISTANCES:
            for width in WIDTHS:
                short_call = math.ceil((spot + distance) / step) * step
                long_call = short_call + width
                short_put = math.floor((spot - distance) / step) * step
                long_put = short_put - width

                syms = {
                    "sc": build_occ_symbol(config.UNDERLYING, day, "CALL", short_call),
                    "lc": build_occ_symbol(config.UNDERLYING, day, "CALL", long_call),
                    "sp": build_occ_symbol(config.UNDERLYING, day, "PUT", short_put),
                    "lp": build_occ_symbol(config.UNDERLYING, day, "PUT", long_put),
                }
                bars = {k: _bar_at(opt_map, s, bar.t) for k, s in syms.items()}
                if any(b is None for b in bars.values()):
                    entries[(entry_time, distance, width)] = None
                    continue
                if any(b.close <= 0 for b in bars.values()):
                    entries[(entry_time, distance, width)] = None
                    continue

                entries[(entry_time, distance, width)] = CondorEntry(
                    bar_t=bar.t, spot=spot,
                    short_call=short_call, long_call=long_call,
                    short_put=short_put, long_put=long_put,
                    sc_sym=syms["sc"], lc_sym=syms["lc"],
                    sp_sym=syms["sp"], lp_sym=syms["lp"],
                    p_sc=bars["sc"].close, p_lc=bars["lc"].close,
                    p_sp=bars["sp"].close, p_lp=bars["lp"].close,
                    state=states.get(bar.t, "neutral"),
                )
    return entries


# ── Exit simulation ───────────────────────────────────────────────────────────

def _leg_closes_at(
    opt_map: dict[str, dict[dt.datetime, bt.OptBar]],
    entry: CondorEntry,
    t: dt.datetime,
    last: dict[str, float],
) -> Optional[tuple[float, float, float, float]]:
    syms = (entry.sc_sym, entry.lc_sym, entry.sp_sym, entry.lp_sym)
    for sym in syms:
        bar = opt_map.get(sym, {}).get(t)
        if bar is not None:
            last[sym] = bar.close
    if all(s in last for s in syms):
        return tuple(last[s] for s in syms)  # type: ignore[return-value]
    return None


def _settle_pnl(entry: CondorEntry, credit: float, spot_close: float,
                width: float) -> float:
    call_loss = min(max(spot_close - entry.short_call, 0.0), width)
    put_loss = min(max(entry.short_put - spot_close, 0.0), width)
    return (credit - call_loss - put_loss) * 100.0


def _simulate_exit(
    day: dt.date,
    spy_bars: list[Bar],
    opt_map: dict[str, dict[dt.datetime, bt.OptBar]],
    entry: CondorEntry,
    slip: tuple[float, float],
    exit_mode: str,
    variant: str,
    prev_atr: float = 0.0,
) -> Optional[CondorTrade]:
    credit = entry_credit(entry.p_sc, entry.p_lc, entry.p_sp, entry.p_lp, slip)
    if credit <= 0.02:
        return None
    width = round(entry.long_call - entry.short_call, 2)
    if width <= 0:
        return None
    max_risk = max(0.01, width - credit)

    start = next((i for i, b in enumerate(spy_bars) if b.t >= entry.bar_t), None)
    if start is None:
        return None

    def make_trade(exit_time: dt.datetime, reason: str, cost: float) -> CondorTrade:
        return CondorTrade(
            day=day, variant=variant, entry_time=entry.bar_t, spot=entry.spot,
            short_call=entry.short_call, long_call=entry.long_call,
            short_put=entry.short_put, long_put=entry.long_put,
            credit=round(credit, 4), max_risk=round(max_risk, 4),
            exit_time=exit_time, exit_reason=reason,
            exit_cost=round(cost, 4), pnl=round((credit - cost) * 100.0, 2),
            prev_atr=round(prev_atr, 4), state=entry.state,
        )

    last = {entry.sc_sym: entry.p_sc, entry.lc_sym: entry.p_lc,
            entry.sp_sym: entry.p_sp, entry.lp_sym: entry.p_lp}

    # ── Hold to expiry ───────────────────────────────────────────────────────
    if exit_mode == "expiry":
        last_bar = next((b for b in reversed(spy_bars) if b.t.time() < RTH_CLOSE),
                        spy_bars[-1])
        pnl = _settle_pnl(entry, credit, last_bar.close, width)
        return CondorTrade(
            day=day, variant=variant, entry_time=entry.bar_t, spot=entry.spot,
            short_call=entry.short_call, long_call=entry.long_call,
            short_put=entry.short_put, long_put=entry.long_put,
            credit=round(credit, 4), max_risk=round(max_risk, 4),
            exit_time=last_bar.t, exit_reason="expiry",
            exit_cost=round(credit - pnl / 100.0, 4), pnl=round(pnl, 2),
            prev_atr=round(prev_atr, 4), state=entry.state,
        )

    # ── Conditional close: "safe_HHMM_CCC" ───────────────────────────────────
    # At HHMM, buy the condor back only if SPY is within CCC cents of a short
    # strike (or through it) — the cases that would end in assignment for a
    # physically settled SPY condor. Otherwise let it expire (OTM legs expire
    # worthless at no cost; settle at intrinsic in case it moves in late).
    if exit_mode.startswith("safe_"):
        _, hhmm, cents = exit_mode.split("_")
        check_t = dt.time(int(hhmm[:2]), int(hhmm[2:]))
        buffer = int(cents) / 100.0
        bar = next((b for b in spy_bars[start:]
                    if b.t.time() >= check_t and b.t.time() < RTH_CLOSE), None)
        if bar is None:
            return None
        if bar.close >= entry.short_call - buffer or bar.close <= entry.short_put + buffer:
            legs = _leg_closes_at(opt_map, entry, bar.t, last)
            if legs is None:
                return None
            return make_trade(bar.t, "eod", close_cost(*legs, slip))
        last_bar = next((b for b in reversed(spy_bars) if b.t.time() < RTH_CLOSE),
                        spy_bars[-1])
        pnl = _settle_pnl(entry, credit, last_bar.close, width)
        return make_trade(last_bar.t, "expiry", credit - pnl / 100.0)

    # ── Time-based close ─────────────────────────────────────────────────────
    # Any "eod_HHMM" (e.g. eod_1525, eod_1545) buys the condor back at that minute.
    if exit_mode.startswith("eod_"):
        close_time = dt.time(int(exit_mode[4:6]), int(exit_mode[6:8]))
        bar = next((b for b in spy_bars[start:]
                    if b.t.time() >= close_time and b.t.time() < RTH_CLOSE), None)
        if bar is None:
            return None
        legs = _leg_closes_at(opt_map, entry, bar.t, last)
        if legs is None:
            return None
        cost = close_cost(*legs, slip)
        return make_trade(bar.t, "eod", cost)

    # ── TP / stop walk ───────────────────────────────────────────────────────
    tp_level = credit * TP_FRAC
    stop_level = credit * STOP_MULT
    tp_idx = None
    stop_idx = None
    close_bar = None

    for i in range(start, len(spy_bars)):
        bar = spy_bars[i]
        if bar.t.time() >= dt.time(15, 25):
            close_bar = bar
            break

        # Update last known closes and build a per-leg high/low envelope.
        ranges: dict[str, tuple[float, float]] = {}
        missing = False
        for sym in (entry.sc_sym, entry.lc_sym, entry.sp_sym, entry.lp_sym):
            ob = opt_map.get(sym, {}).get(bar.t)
            if ob is not None:
                last[sym] = ob.close
                ranges[sym] = (ob.low, ob.high)
            elif sym in last:
                ranges[sym] = (last[sym], last[sym])
            else:
                missing = True
                break
        if missing:
            continue

        sc_lo, sc_hi = ranges[entry.sc_sym]
        lc_lo, lc_hi = ranges[entry.lc_sym]
        sp_lo, sp_hi = ranges[entry.sp_sym]
        lp_lo, lp_hi = ranges[entry.lp_sym]

        best_cost = sc_lo - lc_hi + sp_lo - lp_hi
        worst_cost = sc_hi - lc_lo + sp_hi - lp_lo

        # Stop has precedence when both levels are inside the same minute.
        if worst_cost >= stop_level:
            stop_idx = i
            break
        if best_cost <= tp_level:
            tp_idx = i
            break

    if stop_idx is not None and (tp_idx is None or stop_idx <= tp_idx):
        # Stop first (conservative if both are touched in the same minute).
        stop_bar = spy_bars[stop_idx]
        legs = _leg_closes_at(opt_map, entry, stop_bar.t, last)
        if legs is None:
            return None
        cost = max(stop_level, close_cost(*legs, slip))
        return make_trade(stop_bar.t, "stop", cost)

    if tp_idx is not None:
        tp_bar = spy_bars[tp_idx]
        return make_trade(tp_bar.t, "tp", tp_level)

    if close_bar is None:
        close_bar = next((b for b in reversed(spy_bars) if b.t.time() < RTH_CLOSE), None)
    if close_bar is None:
        return None

    if exit_mode == "tp50_expiry":
        pnl = _settle_pnl(entry, credit, close_bar.close, width)
        return CondorTrade(
            day=day, variant=variant, entry_time=entry.bar_t, spot=entry.spot,
            short_call=entry.short_call, long_call=entry.long_call,
            short_put=entry.short_put, long_put=entry.long_put,
            credit=round(credit, 4), max_risk=round(max_risk, 4),
            exit_time=close_bar.t, exit_reason="expiry",
            exit_cost=round(credit - pnl / 100.0, 4), pnl=round(pnl, 2),
            prev_atr=round(prev_atr, 4), state=entry.state,
        )

    legs = _leg_closes_at(opt_map, entry, close_bar.t, last)
    if legs is None:
        return None
    cost = close_cost(*legs, slip)
    return make_trade(close_bar.t, "eod", cost)


# ── Reporting ─────────────────────────────────────────────────────────────────

def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst


def summarize(days: list[dt.date], trades: list[CondorTrade]) -> dict:
    by_day: dict[dt.date, list[CondorTrade]] = {d: [] for d in days}
    for t in trades:
        by_day.setdefault(t.day, []).append(t)

    equity = 0.0
    curve = []
    for day in days:
        equity += sum(t.pnl for t in by_day.get(day, []))
        curve.append(equity)

    total = sum(t.pnl for t in trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    pf = gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0)
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0
    avg_credit = sum(t.credit for t in trades) / len(trades) if trades else 0.0
    avg_risk = sum(t.max_risk for t in trades) / len(trades) if trades else 0.0
    breakeven = (abs(avg_loss) / (avg_win + abs(avg_loss)) * 100
                 if avg_win + abs(avg_loss) > 0 else 0.0)

    reasons: dict[str, dict] = {}
    for t in trades:
        s = reasons.setdefault(t.exit_reason, {"n": 0, "pnl": 0.0})
        s["n"] += 1
        s["pnl"] += t.pnl

    return {
        "sessions": len(days),
        "trades": len(trades),
        "coverage_pct": round(len(trades) / len(days) * 100, 1) if days else 0.0,
        "net_pnl": round(total, 2),
        "pnl_per_day": round(total / len(days), 2) if days else 0.0,
        "win_rate_pct": round(win_rate, 2),
        "breakeven_wr_pct": round(breakeven, 2),
        "profit_factor": None if math.isinf(pf) else round(pf, 3),
        "expectancy": round(total / len(trades), 2) if trades else 0.0,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_credit": round(avg_credit, 3),
        "avg_max_risk": round(avg_risk, 3),
        "return_on_risk_pct": round(avg_credit / avg_risk * 100, 2) if avg_risk else 0.0,
        "max_drawdown": round(_max_drawdown(curve), 2),
        "tp_n": reasons.get("tp", {}).get("n", 0),
        "tp_pnl": round(reasons.get("tp", {}).get("pnl", 0.0), 2),
        "stop_n": reasons.get("stop", {}).get("n", 0),
        "stop_pnl": round(reasons.get("stop", {}).get("pnl", 0.0), 2),
        "eod_n": reasons.get("eod", {}).get("n", 0),
        "eod_pnl": round(reasons.get("eod", {}).get("pnl", 0.0), 2),
        "expiry_n": reasons.get("expiry", {}).get("n", 0),
        "expiry_pnl": round(reasons.get("expiry", {}).get("pnl", 0.0), 2),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="0DTE iron-condor feasibility study on Alpaca history."
    )
    parser.add_argument("--start", type=dt.date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--out-dir", type=Path, default=bt.RESULTS_DIR)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--entry-times", default=None,
                        help="comma list of HH:MM entry times (default: 09:45,10:30,12:00)")
    parser.add_argument("--exits", default=None,
                        help="comma list of exit modes to run (default: all)")
    parser.add_argument("--filters", default=None, help="comma list: any,neutral")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    global ENTRY_TIMES, EXITS, FILTERS
    if args.entry_times:
        ENTRY_TIMES = tuple(dt.time.fromisoformat(x) for x in args.entry_times.split(","))
    if args.exits:
        EXITS = tuple(args.exits.split(","))
    if args.filters:
        FILTERS = tuple(args.filters.split(","))
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    opt = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)

    print(f"Loading SPY bars {args.start} .. {args.end} ...", flush=True)
    daily, trading_days = bt.load_daily(stock, args.start, args.end, not args.no_cache)
    days = [d for d in trading_days if args.start <= d <= args.end]
    all_bars, bars_by_day = bt.load_minutes(stock, args.start, args.end, not args.no_cache)
    print(f"  {len(days)} sessions, {len(all_bars)} minute bars", flush=True)

    specs = [
        CondorSpec(t, d, w, ex, fl, sl)
        for t in ENTRY_TIMES
        for d in DISTANCES
        for w in WIDTHS
        for ex in EXITS
        for fl in FILTERS
        for sl in SLIPS
    ]
    print(f"Running {len(specs)} condor variants ...", flush=True)

    trades_by_variant: dict[str, list[CondorTrade]] = {s.name: [] for s in specs}
    skipped: list[tuple[dt.date, str]] = []
    prior_bars: list[Bar] = []

    for n, day in enumerate(days, 1):
        spy_bars = bars_by_day.get(day, [])
        if not spy_bars:
            skipped.append((day, "no SPY bars"))
            continue
        try:
            raw = load_condor_options(opt, day, spy_bars, not args.no_cache)
        except Exception as exc:
            skipped.append((day, f"option fetch failed: {exc}"))
            if args.progress:
                print(f"[{n:>4}/{len(days)}] {day}  skipped — {exc}", flush=True)
            continue
        if not raw:
            skipped.append((day, "no option bars"))
            if args.progress:
                print(f"[{n:>4}/{len(days)}] {day}  skipped — no option bars", flush=True)
            continue

        opt_map = bt._to_option_map(raw)
        prev_atr = bt._atr_baseline(daily, day)
        momentum = MomentumEngine()
        if prior_bars:
            momentum.preseed(prior_bars[-30:])
        entries = _build_entries(day, spy_bars, opt_map, momentum)

        # Cache exit outcomes per (entry, slip, mode) — the heavy part.
        exit_cache: dict[tuple, Optional[CondorTrade]] = {}
        day_count = 0
        for spec in specs:
            entry = entries.get((spec.entry_time, spec.distance, spec.width))
            if entry is None:
                continue
            if spec.filter_mode == "neutral" and entry.state != "neutral":
                continue
            key = (spec.entry_time, spec.distance, spec.width,
                   spec.slip_name, spec.exit_mode)
            if key not in exit_cache:
                exit_cache[key] = _simulate_exit(
                    day, spy_bars, opt_map, entry, SLIPS[spec.slip_name],
                    spec.exit_mode, spec.name, prev_atr,
                )
            trade = exit_cache[key]
            if trade is None:
                continue
            # Each spec owns its trade object; the variant label differs.
            trades_by_variant[spec.name].append(CondorTrade(
                **{**trade.__dict__, "variant": spec.name}
            ))
            day_count += 1

        prior_bars.extend(spy_bars)
        if args.progress:
            print(f"[{n:>4}/{len(days)}] {day}  ok  {day_count} condors", flush=True)

    if skipped:
        print(f"Skipped {len(skipped)} sessions (first: {skipped[0]})", flush=True)

    periods = build_periods(days)
    print(f"Aggregating {len(specs)} variants x {len(periods)} periods ...", flush=True)

    rows = []
    for spec in specs:
        all_trades = trades_by_variant[spec.name]
        for period in periods:
            p_days = [d for d in days if period["start"] <= d <= period["end"]]
            p_trades = [t for t in all_trades
                        if period["start"] <= t.day <= period["end"]]
            row = {
                "strategy": "iron_condor",
                "variant": spec.name,
                "period": period["name"],
                "start": period["start"].isoformat(),
                "end": period["end"].isoformat(),
                **summarize(p_days, p_trades),
            }
            rows.append(row)

    summary_path = out_dir / "condor_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {summary_path} ({len(rows)} rows)")

    daily_path = out_dir / "condor_daily.csv"
    with daily_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["variant", "date", "trades", "pnl", "equity"])
        for spec in specs:
            by_day: dict[dt.date, list[CondorTrade]] = {}
            for t in trades_by_variant[spec.name]:
                by_day.setdefault(t.day, []).append(t)
            equity = 0.0
            for day in days:
                day_trades = by_day.get(day, [])
                pnl = sum(t.pnl for t in day_trades)
                equity += pnl
                if day_trades:
                    writer.writerow([spec.name, day.isoformat(), len(day_trades),
                                     f"{pnl:.2f}", f"{equity:.2f}"])
    print(f"Wrote {daily_path}")

    trades_path = out_dir / "condor_trades.csv"
    with trades_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "variant", "date", "entry_time", "spot",
            "short_call", "long_call", "short_put", "long_put",
            "credit", "max_risk", "exit_time", "exit_reason", "exit_cost", "pnl",
            "prev_atr", "momentum",
        ])
        for spec in specs:
            for t in trades_by_variant[spec.name]:
                writer.writerow([
                    spec.name, t.day.isoformat(), t.entry_time.isoformat(),
                    f"{t.spot:.2f}", f"{t.short_call:.2f}", f"{t.long_call:.2f}",
                    f"{t.short_put:.2f}", f"{t.long_put:.2f}",
                    f"{t.credit:.4f}", f"{t.max_risk:.4f}",
                    t.exit_time.isoformat(), t.exit_reason,
                    f"{t.exit_cost:.4f}", f"{t.pnl:.2f}",
                    f"{t.prev_atr:.4f}", t.state,
                ])
    print(f"Wrote {trades_path}")

    meta = {
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "sessions": len(days),
        "entry_times": [t.strftime("%H:%M") for t in ENTRY_TIMES],
        "distances": list(DISTANCES),
        "widths": list(WIDTHS),
        "exits": list(EXITS),
        "filters": list(FILTERS),
        "slips": {k: list(v) for k, v in SLIPS.items()},
        "tp_frac": TP_FRAC,
        "stop_mult": STOP_MULT,
        "strike_step": config.STRIKE_STEP,
        "skipped_sessions": [{"day": d.isoformat(), "reason": r} for d, r in skipped],
    }
    with (out_dir / "condor_meta.json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print("Wrote condor_meta.json")

    # Compact console leaderboard for the full period.
    full = [r for r in rows if r["period"] == "full"]
    ranked = sorted(full, key=lambda r: r["net_pnl"], reverse=True)
    print(f"\nTop 15 condor variants over the full range "
          f"({days[0]} .. {days[-1]}):")
    print(f"{'variant':<38} {'trades':>6} {'net':>10} {'WR':>6} {'BE':>6} "
          f"{'PF':>5} {'credit':>7} {'ROR':>6}")
    for r in ranked[:15]:
        print(f"{r['variant']:<38} {r['trades']:>6} {r['net_pnl']:>+10.2f} "
              f"{r['win_rate_pct']:>5.1f}% {r['breakeven_wr_pct']:>5.1f}% "
              f"{(r['profit_factor'] if r['profit_factor'] is not None else 0):>5.2f} "
              f"{r['avg_credit']:>7.2f} {r['return_on_risk_pct']:>5.1f}%")
    print("\nBottom 5:")
    for r in ranked[-5:]:
        print(f"{r['variant']:<38} {r['trades']:>6} {r['net_pnl']:>+10.2f} "
              f"{r['win_rate_pct']:>5.1f}% {r['breakeven_wr_pct']:>5.1f}% "
              f"{(r['profit_factor'] if r['profit_factor'] is not None else 0):>5.2f} "
              f"{r['avg_credit']:>7.2f} {r['return_on_risk_pct']:>5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
