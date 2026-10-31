#!/usr/bin/env python3
"""
Replay backtester for the live 0DTE SPY strategy.

What it does
------------
Replays real Alpaca history through the *same* entry function the live bot
uses (`signals.check_entry`) and mirrors the live exit rules from `main.py`
(TP / stop / peak trail / SPY stop / time stop), including the session gates
from `risk.RiskManager` (daily loss, profit target, win streak, cooldown).

Data used
---------
- SPY 1-minute bars (IEX feed, same feed the live bot streams).
- Option 1-minute bars (Alpaca historical options) for the 0DTE contracts
  the bot's strike window would cover.

Important limitations
---------------------
- Historical options give trade bars, not bid/ask quotes (Alpaca only serves
  the *latest* option quote; there is no historical NBBO endpoint). Entries
  and exits are therefore filled at the bar price plus a configurable
  slippage haircut (`--entry-slip`, `--exit-slip`), not at the real NBBO.
- Because the live bot marks exits on the quote mid, and quote mids can spike
  beyond the last traded price (especially on fast 0DTE moves), this replay
  tends to *understate* winning exits. Treat the result as a conservative,
  trade-price-based lower bound, not as a reproduction of the paper P&L.
- Within a 1-minute bar the path is unknown. If both the stop and the take
  profit are touched, the conservative assumption is stop-first (override
  with `--optimistic-intrabar`).
- The live bot buys whichever subscribed strike quotes first; with 1-min data
  this replay deterministically picks the nearest OTM strike. Use
  `--strike-steps N` to test the effect of picking a strike N steps further
  out (the live bot often lands ~1 step further OTM).
- Proxy delta is disabled in the live config, so it is not modelled.
- This is a strategy backtest, not a tick-perfect simulation of every
  Alpaca order-handling edge case.

Usage
-----
    python backtest/engine.py                     # last 30 trading days
    python backtest/engine.py --days 60
    python backtest/engine.py --start 2026-09-01 --end 2026-10-30
    python backtest/engine.py --days 10 --entry-slip 0 --exit-slip 0
    python backtest/run_suite.py                   # full period/variant sweep
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import logging
import math
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Make the repo root importable when this file is run from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config
from momentum import Bar, MomentumEngine
from risk import RiskManager
from signals import ProxyDeltaTracker, Quote, check_entry
from strikes import build_occ_symbol

ET = config.ET
RTH_OPEN = dt.time(9, 30)
RTH_CLOSE = dt.time(16, 0)
# The live StockDataStream also delivers premarket bars, and main.on_spy_bar
# feeds every bar into the momentum engine (only option subscriptions wait for
# 09:30). IEX minute history starts around 08:00 ET, so replay from 04:00 to be
# safe. This matters: VWAP/EMA/streak state at 09:45 includes premarket bars.
DATA_OPEN = dt.time(4, 0)
TIME_STOP = dt.time.fromisoformat(config.TIME_STOP)
ONE_MIN = dt.timedelta(minutes=1)
CACHE_DIR = Path(__file__).resolve().parent / "cache"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class OptBar:
    t: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class OpenPos:
    symbol: str
    side: str
    strike: float
    qty: int
    entry_price: float
    entry_time: dt.datetime
    entry_spy_price: float
    entry_atr5: float
    peak_mid: float
    last_mid: float


@dataclass
class Trade:
    day: dt.date
    symbol: str
    side: str
    strike: float
    qty: int
    entry_time: dt.datetime
    entry_price: float
    exit_time: dt.datetime
    exit_price: float
    reason: str
    pnl: float
    entry_spy: float
    entry_atr5: float


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cached(key: str, fetch, use_cache: bool = True):
    path = CACHE_DIR / f"{key}.pkl"
    if use_cache and path.exists():
        try:
            with path.open("rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    value = fetch()
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump(value, f)
    return value


# ── Alpaca history ────────────────────────────────────────────────────────────

def _fetch_daily(client: StockHistoricalDataClient, start: dt.date, end: dt.date) -> list[dict]:
    req = StockBarsRequest(
        symbol_or_symbols=config.UNDERLYING,
        timeframe=TimeFrame.Day,
        start=dt.datetime.combine(start, dt.time(0, 0), tzinfo=ET),
        end=dt.datetime.combine(end, dt.time(23, 59), tzinfo=ET),
        feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(req).data.get(config.UNDERLYING, [])
    return [
        {
            "d": b.timestamp.astimezone(ET).date().isoformat(),
            "o": float(b.open),
            "h": float(b.high),
            "l": float(b.low),
            "c": float(b.close),
            "v": float(b.volume or 0),
        }
        for b in bars
    ]


def _fetch_minute(client: StockHistoricalDataClient, start: dt.date, end: dt.date) -> list[dict]:
    req = StockBarsRequest(
        symbol_or_symbols=config.UNDERLYING,
        timeframe=TimeFrame.Minute,
        start=dt.datetime.combine(start, DATA_OPEN, tzinfo=ET),
        end=dt.datetime.combine(end, RTH_CLOSE, tzinfo=ET),
        feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(req).data.get(config.UNDERLYING, [])
    out = []
    for b in bars:
        t = b.timestamp.astimezone(ET)
        if DATA_OPEN <= t.time() < RTH_CLOSE:
            out.append({
                "t": t.isoformat(),
                "o": float(b.open),
                "h": float(b.high),
                "l": float(b.low),
                "c": float(b.close),
                "v": float(b.volume or 0),
            })
    return out


def _fetch_option_day(
    client: OptionHistoricalDataClient,
    day: dt.date,
    strikes: list[float],
) -> dict[str, list[dict]]:
    symbols: list[str] = []
    for strike in strikes:
        symbols.append(build_occ_symbol(config.UNDERLYING, day, "CALL", strike))
        symbols.append(build_occ_symbol(config.UNDERLYING, day, "PUT", strike))

    start = dt.datetime.combine(day, RTH_OPEN, tzinfo=ET)
    end = dt.datetime.combine(day, RTH_CLOSE, tzinfo=ET)
    result: dict[str, list[dict]] = {}

    # Keep requests around 50 symbols to stay well inside Alpaca's limits.
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        req = OptionBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
        )
        data = client.get_option_bars(req).data
        for symbol, bars in data.items():
            rows = []
            for b in bars:
                t = b.timestamp.astimezone(ET)
                if RTH_OPEN <= t.time() < RTH_CLOSE:
                    rows.append({
                        "t": t.isoformat(),
                        "o": float(b.open),
                        "h": float(b.high),
                        "l": float(b.low),
                        "c": float(b.close),
                        "v": float(b.volume or 0),
                    })
            if rows:
                result[symbol] = rows
    return result


# ── Simulation ────────────────────────────────────────────────────────────────

def _atr_baseline(daily: list[dict], day: dt.date) -> float:
    """Live bot's 5-day ATR: mean high-low over the 5 completed sessions
    before `day` (falls back to 3.0 like strikes._atr_5day)."""
    prior = [r for r in daily if dt.date.fromisoformat(r["d"]) < day][-5:]
    if not prior:
        return 3.0
    return sum(r["h"] - r["l"] for r in prior) / len(prior)


def _strike_window(spy_bars: list[Bar], step: float) -> list[float]:
    """Every strike the bot's subscription window could reach that day.
    Activation zone is only ±0.3% of spot, so a few points around the daily
    range is more than enough."""
    low = min(b.low for b in spy_bars)
    high = max(b.high for b in spy_bars)
    lo = math.floor(low / step) * step - 6 * step
    hi = math.ceil(high / step) * step + 6 * step
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 2) for i in range(n + 1)]


def _to_option_map(raw: dict[str, list[dict]]) -> dict[str, dict[dt.datetime, OptBar]]:
    out: dict[str, dict[dt.datetime, OptBar]] = {}
    for symbol, rows in raw.items():
        out[symbol] = {
            dt.datetime.fromisoformat(r["t"]): OptBar(
                t=dt.datetime.fromisoformat(r["t"]),
                open=r["o"], high=r["h"], low=r["l"], close=r["c"], volume=r["v"],
            )
            for r in rows
        }
    return out


def _exit_fill(price: float, slip: float) -> float:
    return max(0.01, round(price * (1.0 - slip), 2))


def _check_exit(
    pos: OpenPos,
    bar: Bar,
    opt_bar: Optional[OptBar],
    eval_time: dt.datetime,
    args,
) -> Optional[tuple[float, str, dt.datetime]]:
    """Mirror main._evaluate_exit + _evaluate_spy_stop using 1-min OHLC.

    Returns (exit_price_before_slippage, reason, exit_time) or None.
    """
    # Time stop wins — the live watcher closes everything at 15:25 ET.
    if eval_time.time() >= args.time_stop:
        if opt_bar is not None:
            px = opt_bar.close
        else:
            px = pos.last_mid
        return px, "time_stop", eval_time

    tp_price = pos.entry_price * config.TP_MULT
    stop_price = pos.entry_price * config.STOP_MULT
    exit_px = None
    reason = None

    if opt_bar is not None:
        # Highest mid seen while the bar traded; the live bot sees every tick.
        peak_before = pos.peak_mid
        new_peak = max(peak_before, opt_bar.high)
        trail_armed = new_peak >= pos.entry_price * config.PEAK_TRAIL_ACTIVATE
        trail_stop = new_peak * config.PEAK_TRAIL_PCT
        # Trail level that was already resting at the start of this bar.
        prior_trail = (peak_before * config.PEAK_TRAIL_PCT
                       if peak_before >= pos.entry_price * config.PEAK_TRAIL_ACTIVATE
                       else None)

        stop_hit = opt_bar.low <= stop_price
        tp_hit = opt_bar.high >= tp_price
        trail_hit = trail_armed and opt_bar.low <= trail_stop

        def stop_price_fill() -> float:
            # Gap below the stop fills at the open, not the stop level.
            return min(stop_price, opt_bar.open)

        def tp_price_fill() -> float:
            # Gap above the TP fills at the open, better than the TP level.
            return max(tp_price, opt_bar.open)

        def trail_price_fill() -> float:
            # If the trail was already armed before this bar and the bar opens
            # through it, that is a real gap and fills at the open. Otherwise
            # the peak is set *inside* this bar (we already use its high), so a
            # later dip fills at the trail level -- not at the bar's first
            # trade, which would fabricate a huge giveback.
            if prior_trail is not None and opt_bar.open <= prior_trail:
                return opt_bar.open
            return trail_stop

        if args.optimistic_intrabar and tp_hit:
            exit_px, reason = tp_price_fill(), "tp"
        elif stop_hit:
            exit_px, reason = stop_price_fill(), "stop"
        elif tp_hit:
            exit_px, reason = tp_price_fill(), "tp"
        elif trail_hit:
            exit_px, reason = trail_price_fill(), "peak_trail"

        pos.peak_mid = max(pos.peak_mid, opt_bar.high)
        pos.last_mid = opt_bar.close

        if reason is not None:
            return exit_px, reason, eval_time

    # SPY-level stop is evaluated on the SPY bar close, using the current
    # option price (or the last known one if this minute had no option trade).
    buf = max(args.spy_stop_floor, args.spy_stop_mult * pos.entry_atr5)
    if pos.side == "call" and bar.close < pos.entry_spy_price - buf:
        return (opt_bar.close if opt_bar else pos.last_mid), "spy_stop", eval_time
    if pos.side == "put" and bar.close > pos.entry_spy_price + buf:
        return (opt_bar.close if opt_bar else pos.last_mid), "spy_stop", eval_time

    return None


def simulate_day(
    day: dt.date,
    spy_bars: list[Bar],
    opt_map: dict[str, dict[dt.datetime, OptBar]],
    daily: list[dict],
    seed_bars: list[Bar],
    args,
    strikes: Optional[list[float]] = None,
) -> list[Trade]:
    if not spy_bars:
        return []

    risk = RiskManager()
    risk.reset_day()
    momentum = MomentumEngine()
    if seed_bars:
        momentum.preseed(seed_bars[-30:])

    if strikes is None:
        strikes = _strike_window(spy_bars, config.STRIKE_STEP)
    dummy_tracker = ProxyDeltaTracker()   # proxy delta is disabled in config
    trades: list[Trade] = []
    pos: Optional[OpenPos] = None

    for bar in spy_bars:
        state = momentum.on_bar(bar)
        risk.tick_bar()
        eval_time = bar.t + ONE_MIN
        closed_this_bar = False

        # ── Manage an open position ──────────────────────────────────────────
        if pos is not None:
            opt_bar = opt_map.get(pos.symbol, {}).get(bar.t)
            exit_info = _check_exit(pos, bar, opt_bar, eval_time, args)

            if exit_info is not None:
                raw_px, reason, exit_time = exit_info
                exit_px = _exit_fill(raw_px, args.exit_slip)
                pnl = (exit_px - pos.entry_price) * pos.qty * 100.0
                trades.append(Trade(
                    day=day,
                    symbol=pos.symbol,
                    side=pos.side,
                    strike=pos.strike,
                    qty=pos.qty,
                    entry_time=pos.entry_time,
                    entry_price=pos.entry_price,
                    exit_time=exit_time,
                    exit_price=exit_px,
                    reason=reason,
                    pnl=pnl,
                    entry_spy=pos.entry_spy_price,
                    entry_atr5=pos.entry_atr5,
                ))
                pos = None
                closed_this_bar = True
                risk.record_trade(pnl)

        # ── Look for a new entry ─────────────────────────────────────────────
        if pos is None and not closed_this_bar and risk.can_trade():
            if state.direction not in ("bull", "bear"):
                continue
            side = "call" if state.direction == "bull" else "put"

            candidates = []
            if side == "call":
                start = bisect.bisect_right(strikes, bar.close)
                end = bisect.bisect_right(
                    strikes, bar.close * (1.0 + config.ACTIVATION_PCT))
            else:
                start = bisect.bisect_left(
                    strikes, bar.close * (1.0 - config.ACTIVATION_PCT))
                end = bisect.bisect_left(strikes, bar.close)

            for strike in strikes[start:end]:
                # Activation zone only; early filter keeps the loop cheap.
                if abs(strike - bar.close) / bar.close > config.ACTIVATION_PCT:
                    continue

                symbol = build_occ_symbol(config.UNDERLYING, day, side, strike)
                opt_bar = opt_map.get(symbol, {}).get(bar.t)
                if opt_bar is None or opt_bar.close <= 0:
                    continue

                quote = Quote(symbol=symbol, bid=opt_bar.close,
                              ask=opt_bar.close, timestamp=bar.t)
                if not check_entry(
                    side=side,
                    strike=strike,
                    option_quote=quote,
                    momentum=state,
                    proxy_tracker=dummy_tracker,
                    spy_price=bar.close,
                    trades_today=risk.trades_today,
                    has_open_pos=False,
                    atr5=state.atr5,
                    now=eval_time,
                ):
                    continue

                candidates.append((abs(strike - bar.close), strike, symbol, opt_bar.close))

            if not candidates:
                continue

            # The live bot takes whichever eligible quote ticks first. With
            # 1-min data, use the strike nearest to spot by default; the first
            # tick often lands ~1 step further OTM (see --strike-steps).
            candidates.sort(key=lambda c: (c[0], c[1]))
            step_idx = min(max(args.strike_steps, 0), len(candidates) - 1)
            _, strike, symbol, mid = candidates[step_idx]

            qty = risk.size_trade(mid)
            if qty <= 0:
                continue

            # Entry limit is mid × 1.02; slippage is applied inside that cap.
            limit_px = round(mid * 1.02, 2)
            fill = min(limit_px, round(mid * (1.0 + args.entry_slip), 2))
            fill = max(0.01, fill)

            pos = OpenPos(
                symbol=symbol,
                side=side,
                strike=strike,
                qty=qty,
                entry_price=fill,
                entry_time=eval_time,
                entry_spy_price=bar.close,
                entry_atr5=state.atr5,
                peak_mid=mid,
                last_mid=mid,
            )

    # Safety net: the live time stop should already have closed everything.
    if pos is not None:
        last_bar = spy_bars[-1]
        exit_px = _exit_fill(pos.last_mid, args.exit_slip)
        pnl = (exit_px - pos.entry_price) * pos.qty * 100.0
        trades.append(Trade(
            day=day,
            symbol=pos.symbol,
            side=pos.side,
            strike=pos.strike,
            qty=pos.qty,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=last_bar.t + ONE_MIN,
            exit_price=exit_px,
            reason="time_stop",
            pnl=pnl,
            entry_spy=pos.entry_spy_price,
            entry_atr5=pos.entry_atr5,
        ))
        risk.record_trade(pnl)

    return trades


# ── Reporting ─────────────────────────────────────────────────────────────────

def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst


def _report(days: list[dt.date], trades: list[Trade], args, print_table: bool = True) -> dict:
    by_day: dict[dt.date, list[Trade]] = {d: [] for d in days}
    for t in trades:
        by_day.setdefault(t.day, []).append(t)

    equity = 0.0
    curve = []
    if print_table:
        print()
        print(f"{'Date':<12} {'Trades':>6} {'Wins':>5} {'P&L':>11} {'Equity':>11}  Exit reasons")
        print("-" * 92)
    for day in days:
        day_trades = by_day.get(day, [])
        day_pnl = sum(t.pnl for t in day_trades)
        equity += day_pnl
        curve.append(equity)
        wins = sum(1 for t in day_trades if t.pnl > 0)
        if print_table:
            if day_trades:
                reasons = ", ".join(f"{r}:{sum(1 for t in day_trades if t.reason == r)}"
                                    for r in sorted({t.reason for t in day_trades}))
            else:
                reasons = "—"
            print(f"{day.isoformat():<12} {len(day_trades):>6} {wins:>5} "
                  f"{day_pnl:>+11.2f} {equity:>11.2f}  {reasons}")

    total = sum(t.pnl for t in trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    pf = gross_win / gross_loss if gross_loss else float("inf")
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    avg = total / len(trades) if trades else 0.0
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0

    reason_stats: dict[str, dict] = {}
    for t in trades:
        s = reason_stats.setdefault(t.reason, {"n": 0, "pnl": 0.0, "wins": 0})
        s["n"] += 1
        s["pnl"] += t.pnl
        s["wins"] += 1 if t.pnl > 0 else 0

    if print_table:
        print("-" * 92)
        print(f"Sessions:          {len(days)}")
        print(f"Trades:            {len(trades)}")
        print(f"Net P&L:           ${total:+,.2f}")
        print(f"Win rate:          {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"Profit factor:     {pf:.2f}")
        print(f"Expectancy/trade:  ${avg:+,.2f}")
        print(f"Avg win / loss:    ${avg_win:+,.2f} / ${avg_loss:+,.2f}")
        print(f"Max drawdown:      ${_max_drawdown(curve):,.2f}")
        if curve:
            best = max(by_day, key=lambda d: sum(t.pnl for t in by_day[d]))
            worst = min(by_day, key=lambda d: sum(t.pnl for t in by_day[d]))
            print(f"Best day:          {best.isoformat()}  ${sum(t.pnl for t in by_day[best]):+,.2f}")
            print(f"Worst day:         {worst.isoformat()}  ${sum(t.pnl for t in by_day[worst]):+,.2f}")
        print()
        print(f"{'Exit reason':<14} {'Trades':>7} {'Wins':>6} {'P&L':>12}")
        print("-" * 44)
        for reason in sorted(reason_stats, key=lambda r: -reason_stats[r]["n"]):
            s = reason_stats[reason]
            print(f"{reason:<14} {s['n']:>7} {s['wins']:>6} {s['pnl']:>+12.2f}")

    return {
        "sessions": len(days),
        "trades": len(trades),
        "net_pnl": round(total, 2),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": None if math.isinf(pf) else round(pf, 3),
        "expectancy": round(avg, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown": round(_max_drawdown(curve), 2),
        "exit_reasons": {
            r: {"trades": s["n"], "wins": s["wins"], "pnl": round(s["pnl"], 2)}
            for r, s in reason_stats.items()
        },
    }


def _write_trades(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "symbol", "side", "strike", "qty",
            "entry_time", "entry_price", "exit_time", "exit_price",
            "reason", "realized_pnl", "entry_spy", "entry_atr5",
        ])
        for t in trades:
            writer.writerow([
                t.day.isoformat(), t.symbol, t.side, t.strike, t.qty,
                t.entry_time.isoformat(), f"{t.entry_price:.4f}",
                t.exit_time.isoformat(), f"{t.exit_price:.4f}",
                t.reason, f"{t.pnl:.2f}", f"{t.entry_spy:.4f}",
                f"{t.entry_atr5:.4f}",
            ])


# ── Orchestration ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay-backtest the live 0DTE SPY strategy on Alpaca history."
    )
    parser.add_argument("--days", type=int, default=30,
                        help="number of trading sessions (default 30)")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=None,
                        help="first session (YYYY-MM-DD); overrides --days")
    parser.add_argument("--end", type=dt.date.fromisoformat, default=None,
                        help="last session (YYYY-MM-DD); default = today")
    parser.add_argument("--entry-slip", type=float, default=0.01,
                        help="entry slippage as fraction of bar price (default 0.01)")
    parser.add_argument("--exit-slip", type=float, default=0.01,
                        help="exit slippage as fraction of trigger/bar price (default 0.01)")
    parser.add_argument("--optimistic-intrabar", action="store_true",
                        help="if TP and stop are both touched in one bar, assume TP first")
    parser.add_argument("--strike-steps", type=int, default=0,
                        help="pick the Nth eligible OTM strike instead of the nearest "
                             "(0 = nearest). Live picks whichever quote ticks first, "
                             "which is often 1 step further out.")
    parser.add_argument("--spy-stop-floor", type=float, default=config.SPY_STOP_FLOOR,
                        help="minimum SPY-stop buffer in dollars (default from config)")
    parser.add_argument("--spy-stop-mult", type=float, default=config.SPY_STOP_ATR_MULT,
                        help="SPY-stop buffer as multiple of 1-min ATR5 (default from config)")
    parser.add_argument("--time-stop", type=dt.time.fromisoformat,
                        default=dt.time.fromisoformat(config.TIME_STOP),
                        help="time stop in ET (default from config)")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "backtest_trades.csv",
                        help="trade CSV output path")
    parser.add_argument("--summary-json", type=Path, default=None,
                        help="optional JSON summary output path")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore and do not write the cache directory")
    parser.add_argument("--verbose", action="store_true",
                        help="show entry/risk logs from the strategy modules")
    return parser


def default_args(**overrides):
    """A fully populated args namespace without touching sys.argv."""
    args = build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def load_daily(stock_client, first: dt.date, last: dt.date, use_cache: bool = True):
    daily_start = first - dt.timedelta(days=400)
    daily = _cached(
        f"daily_{daily_start.isoformat()}_{last.isoformat()}",
        lambda: _fetch_daily(stock_client, daily_start, last),
        use_cache,
    )
    trading_days = sorted({dt.date.fromisoformat(r["d"]) for r in daily
                           if dt.date.fromisoformat(r["d"]) <= last})
    return daily, trading_days


def load_minutes(stock_client, first: dt.date, last: dt.date, use_cache: bool = True):
    minute_start = first - dt.timedelta(days=10)
    minute_raw = _cached(
        f"minute_v2_{minute_start.isoformat()}_{last.isoformat()}",
        lambda: _fetch_minute(stock_client, minute_start, last),
        use_cache,
    )
    all_bars = [
        Bar(
            t=dt.datetime.fromisoformat(r["t"]),
            open=r["o"], high=r["h"], low=r["l"], close=r["c"], volume=r["v"],
        )
        for r in minute_raw
    ]
    all_bars.sort(key=lambda b: b.t)
    bars_by_day: dict[dt.date, list[Bar]] = {}
    for b in all_bars:
        bars_by_day.setdefault(b.t.date(), []).append(b)
    return all_bars, bars_by_day


def load_day_options(option_client, day: dt.date, spy_bars: list[Bar],
                     use_cache: bool = True) -> dict[str, list[dict]]:
    strikes = _strike_window(spy_bars, config.STRIKE_STEP)
    lo, hi = strikes[0], strikes[-1]
    return _cached(
        f"options_{day.isoformat()}_{lo:g}_{hi:g}",
        lambda: _fetch_option_day(option_client, day, strikes),
        use_cache,
    )


@dataclass
class Variant:
    name: str
    args: argparse.Namespace
    overrides: dict


class _ConfigOverride:
    def __init__(self, overrides: dict):
        self.overrides = overrides
        self.old: dict = {}

    def __enter__(self):
        self.old = {k: getattr(config, k) for k in self.overrides}
        for k, v in self.overrides.items():
            setattr(config, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(config, k, v)
        return False


def run_variants(
    days: list[dt.date],
    daily: list[dict],
    all_bars: list[Bar],
    bars_by_day: dict[dt.date, list[Bar]],
    option_client,
    variants: list[Variant],
    use_cache: bool = True,
    progress: bool = False,
) -> dict[str, dict]:
    """Replay every variant over the same data, loading each day only once."""
    results = {v.name: {"trades": [], "skipped": []} for v in variants}
    prior_bars: list[Bar] = []

    for n, day in enumerate(days, 1):
        spy_bars = bars_by_day.get(day, [])
        if not spy_bars:
            for v in variants:
                results[v.name]["skipped"].append((day, "no SPY bars"))
            continue

        try:
            raw = load_day_options(option_client, day, spy_bars, use_cache)
        except Exception as exc:
            for v in variants:
                results[v.name]["skipped"].append((day, f"option fetch failed: {exc}"))
            if progress:
                print(f"[{n:>4}/{len(days)}] {day}  skipped — fetch failed: {exc}",
                      flush=True)
            continue
        if not raw:
            for v in variants:
                results[v.name]["skipped"].append((day, "no option bars"))
            if progress:
                print(f"[{n:>4}/{len(days)}] {day}  skipped — no option bars", flush=True)
            continue

        opt_map = _to_option_map(raw)
        seed_bars = prior_bars[-30:]
        strikes = _strike_window(spy_bars, config.STRIKE_STEP)

        day_pnls = []
        for variant in variants:
            with _ConfigOverride(variant.overrides):
                day_trades = simulate_day(
                    day, spy_bars, opt_map, daily, seed_bars, variant.args, strikes
                )
            results[variant.name]["trades"].extend(day_trades)
            if progress:
                day_pnls.append(f"{variant.name}:{sum(t.pnl for t in day_trades):+.0f}")

        prior_bars.extend(spy_bars)

        if progress:
            print(f"[{n:>4}/{len(days)}] {day}  ok  " + "  ".join(day_pnls), flush=True)

    return results


def _summary_payload(summary: dict, first: dt.date, last: dt.date, args,
                     skipped: list) -> dict:
    return {
        **summary,
        "start": first.isoformat(),
        "end": last.isoformat(),
        "entry_slip": args.entry_slip,
        "exit_slip": args.exit_slip,
        "intrabar": "tp_first" if args.optimistic_intrabar else "stop_first",
        "strike_steps": args.strike_steps,
        "spy_stop_floor": args.spy_stop_floor,
        "spy_stop_mult": args.spy_stop_mult,
        "time_stop": args.time_stop.strftime("%H:%M"),
        "skipped_sessions": [{"day": d.isoformat(), "reason": r} for d, r in skipped],
    }


def main() -> int:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not config.ALPACA_API_KEY or not config.ALPACA_API_SECRET:
        print("Set ALPACA_API_KEY / ALPACA_API_SECRET in config.py or the environment.",
              file=sys.stderr)
        return 2

    stock_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    option_client = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)

    end = args.end or dt.date.today()
    probe_start = args.start or (end - dt.timedelta(days=max(args.days * 2 + 30, 90)))
    use_cache = not args.no_cache

    try:
        daily, trading_days = load_daily(stock_client, probe_start, end, use_cache)
    except Exception as exc:
        print(f"Could not fetch SPY daily bars: {exc}", file=sys.stderr)
        return 2

    if args.start:
        days = [d for d in trading_days if d >= args.start]
    else:
        days = trading_days[-args.days:]
    if not days:
        print("No trading days found in the requested range.", file=sys.stderr)
        return 2

    first, last = days[0], days[-1]
    try:
        all_bars, bars_by_day = load_minutes(stock_client, first, last, use_cache)
    except Exception as exc:
        print(f"Could not fetch SPY minute bars: {exc}", file=sys.stderr)
        return 2

    print(f"Backtest: {first.isoformat()} .. {last.isoformat()} "
          f"({len(days)} sessions)")
    print(f"Entry slippage {args.entry_slip:.2%}, exit slippage {args.exit_slip:.2%}, "
          f"intrabar={'TP-first' if args.optimistic_intrabar else 'stop-first'}, "
          f"strike-steps={args.strike_steps}")
    print(f"Cache: {'disabled' if not use_cache else CACHE_DIR}")

    variant = Variant("cli", args, {})
    results = run_variants(days, daily, all_bars, bars_by_day, option_client,
                           [variant], use_cache, progress=True)
    all_trades = results["cli"]["trades"]
    skipped = results["cli"]["skipped"]

    if skipped:
        print("\nSkipped sessions:")
        for day, reason in skipped:
            print(f"  {day}: {reason}")

    if not all_trades and len(skipped) == len(days):
        print("\nNo usable data — nothing to simulate.", file=sys.stderr)
        return 2

    summary = _report(days, all_trades, args)
    _write_trades(args.out, all_trades)
    print(f"\nTrades written to {args.out}")

    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        with args.summary_json.open("w", encoding="utf-8") as f:
            json.dump(_summary_payload(summary, first, last, args, skipped), f, indent=2)
        print(f"Summary written to {args.summary_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
