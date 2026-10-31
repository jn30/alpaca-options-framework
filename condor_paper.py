#!/usr/bin/env python3
"""
0DTE SPY iron condor — paper-trading probe (standalone, one condor per session).

Purpose
-------
The backtest (backtest/iron_condor.py) can't see bid/ask, so its edge hinges on
an assumed slippage. This script trades ONE condor per day on the Alpaca paper
account and logs the real fill versus the quote mid, so the slippage assumption
can be measured instead of guessed.

Default variant = best low-drawdown cell of the sweep:
    entry 12:00 ET, short strikes 2$ from spot, 3$ wide, held to expiry.

It is deliberately separate from main.py (the directional bot) and does not
touch its state, risk manager or log files.

Safety
------
- Dry-run by default: prints the order it WOULD send. Pass --submit to trade.
- Refuses to run unless config.PAPER is True.
- Paper only, 1 contract by default, max risk = (width - credit) * 100 * qty.

Usage
-----
    python condor_paper.py                    # dry-run, waits for 12:00 ET
    python condor_paper.py --now              # dry-run immediately
    python condor_paper.py --submit           # real paper orders
    python condor_paper.py --submit --exit close1555

Output: logs/condor_paper.csv (one row per session; slip_vs_mid is the number
that decides whether the strategy is viable — backtest assumed ~1 cent per leg).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import (
    OptionLatestQuoteRequest, StockBarsRequest, StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass, OrderClass, OrderSide, OrderStatus, PositionIntent, TimeInForce,
)
from alpaca.trading.requests import GetCalendarRequest, LimitOrderRequest, OptionLegRequest

import config
from notifier import notify
from strikes import build_occ_symbol

logger = logging.getLogger("condor_paper")
for _s in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ET = config.ET
CSV_PATH = os.path.join(config.LOG_DIR, "condor_paper.csv")
CSV_FIELDS = [
    "date", "underlying", "entry_time", "spot", "short_put", "long_put", "short_call",
    "long_call", "qty", "mid_credit", "natural_credit", "fill_credit",
    "slip_vs_mid", "slip_per_leg", "exit_mode", "exit_time", "close_cost",
    "spy_close", "theoretical_pnl", "realized_pnl", "pnl_per_contract", "status",
    "equity_before", "equity_after", "equity_change", "entry_quotes",
]
ACCOUNT_CSV = os.path.join(config.LOG_DIR, "account_daily.csv")
TERMINAL = (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED)


# ── Pure helpers (unit-testable) ──────────────────────────────────────────────

@dataclass
class Legs:
    short_call: float
    long_call: float
    short_put: float
    long_put: float


def condor_strikes(spot: float, distance: float, width: float, step: float) -> Legs:
    """Same construction as the backtest (backtest/iron_condor.py)."""
    sc = math.ceil((spot + distance) / step) * step
    sp = math.floor((spot - distance) / step) * step
    return Legs(sc, sc + width, sp, sp - width)


def settle_value(legs: Legs, spy_close: float) -> float:
    """Intrinsic value of the condor at expiry (what we'd owe to close)."""
    call = max(0.0, spy_close - legs.short_call) - max(0.0, spy_close - legs.long_call)
    put = max(0.0, legs.short_put - spy_close) - max(0.0, legs.long_put - spy_close)
    return call + put


@dataclass
class Profile:
    """Which underlying the condor trades. SPY is the backtested one; SPXW
    (SPX weeklies/0DTE) is the same structure scaled by ~10 (index = 10 x SPY)."""
    name: str
    root: str
    scale: float     # multiplies distance, width and credit thresholds
    step: float      # strike increment


def spy_profile() -> Profile:
    return Profile("SPY", config.UNDERLYING, 1.0, config.STRIKE_STEP)


def spxw_profile() -> Profile:
    return Profile("SPXW", "SPXW", float(config.CONDOR_SPX_SCALE), float(config.CONDOR_SPX_STEP))


def pick_profile(equity: float) -> Profile:
    mode = config.CONDOR_UNDERLYING.upper()
    if mode == "SPXW":
        return spxw_profile()
    if mode == "AUTO" and equity >= config.CONDOR_AUTO_MIN_EQUITY:
        return spxw_profile()
    return spy_profile()


def leg_symbols(day: dt.date, legs: Legs, root: Optional[str] = None) -> dict[str, str]:
    u = root or config.UNDERLYING
    return {
        "short_call": build_occ_symbol(u, day, "CALL", legs.short_call),
        "long_call": build_occ_symbol(u, day, "CALL", legs.long_call),
        "short_put": build_occ_symbol(u, day, "PUT", legs.short_put),
        "long_put": build_occ_symbol(u, day, "PUT", legs.long_put),
    }


def credit_from_quotes(q: dict[str, tuple[float, float]]) -> tuple[float, float]:
    """(mid_credit, natural_credit) from {leg: (bid, ask)}."""
    def mid(k): return (q[k][0] + q[k][1]) / 2.0
    mid_c = (mid("short_call") - mid("long_call")) + (mid("short_put") - mid("long_put"))
    nat_c = (q["short_call"][0] - q["long_call"][1]) + (q["short_put"][0] - q["long_put"][1])
    return round(mid_c, 4), round(nat_c, 4)


def condor_qty(buying_power: float, width: float, credit: float, fraction: float) -> int:
    """Max condors the buying power supports. Collateral per condor is the
    worst-case loss of one side: (width - credit) * 100."""
    per = (width - credit) * 100.0
    if per <= 0:
        return 0
    return int(buying_power * fraction // per)


def build_order(symbols: dict[str, str], qty: int, price: float, opening: bool) -> LimitOrderRequest:
    """
    Alpaca mleg price convention: limit_price > 0 is a net DEBIT, < 0 a net
    CREDIT. `price` here is the positive credit (opening) or debit (closing).
    """
    if opening:
        spec = [("short_call", OrderSide.SELL, PositionIntent.SELL_TO_OPEN),
                ("long_call", OrderSide.BUY, PositionIntent.BUY_TO_OPEN),
                ("short_put", OrderSide.SELL, PositionIntent.SELL_TO_OPEN),
                ("long_put", OrderSide.BUY, PositionIntent.BUY_TO_OPEN)]
        limit = -abs(price)
    else:
        spec = [("short_call", OrderSide.BUY, PositionIntent.BUY_TO_CLOSE),
                ("long_call", OrderSide.SELL, PositionIntent.SELL_TO_CLOSE),
                ("short_put", OrderSide.BUY, PositionIntent.BUY_TO_CLOSE),
                ("long_put", OrderSide.SELL, PositionIntent.SELL_TO_CLOSE)]
        limit = abs(price)
    legs = [OptionLegRequest(symbol=symbols[k], ratio_qty=1, side=s, position_intent=i)
            for k, s, i in spec]
    return LimitOrderRequest(
        qty=qty, order_class=OrderClass.MLEG, time_in_force=TimeInForce.DAY,
        limit_price=round(limit, 2), legs=legs,
    )


# ── Market data ───────────────────────────────────────────────────────────────

def spy_last(stock: StockHistoricalDataClient) -> float:
    r = stock.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=config.UNDERLYING, feed=DataFeed.IEX))
    return float(r[config.UNDERLYING].price)


def estimate_index_spot(opt: OptionHistoricalDataClient, day: dt.date, root: str,
                        guess: float, step: float) -> float:
    """SPX has no stock-API price, so recover the spot from put-call parity:
    at the strike where call and put mids are closest, S ~ K + C - P."""
    base = round(guess / step) * step
    strikes = [base + i * step for i in range(-4, 5)]
    syms = {}
    for k in strikes:
        syms[("C", k)] = build_occ_symbol(root, day, "CALL", k)
        syms[("P", k)] = build_occ_symbol(root, day, "PUT", k)
    raw = opt.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=list(syms.values())))
    best = None
    for k in strikes:
        c, p = raw.get(syms[("C", k)]), raw.get(syms[("P", k)])
        if not (c and p and c.bid_price and c.ask_price and p.bid_price and p.ask_price):
            continue
        cm, pm = (c.bid_price + c.ask_price) / 2, (p.bid_price + p.ask_price) / 2
        if best is None or abs(cm - pm) < best[0]:
            best = (abs(cm - pm), k + cm - pm)
    if best is None:
        raise RuntimeError(f"cannot estimate {root} spot from quotes (guess {guess})")
    return best[1]


def leg_quotes(opt: OptionHistoricalDataClient, symbols: dict[str, str],
               allow_zero_bid: bool = False) -> dict[str, tuple[float, float]]:
    """{leg: (bid, ask)}. allow_zero_bid=True for closing: near the close the
    far-OTM long wings routinely quote 0.00 bid, which must not abort the exit."""
    raw = opt.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=list(symbols.values())))
    out = {}
    for leg, sym in symbols.items():
        q = raw.get(sym)
        bid = float(q.bid_price or 0) if q is not None else 0.0
        ask = float(q.ask_price or 0) if q is not None else 0.0
        if q is None or ask <= 0 or ask < bid or (bid <= 0 and not allow_zero_bid):
            raise RuntimeError(f"no usable quote for {leg} {sym}: {q}")
        out[leg] = (bid, ask)
    return out


def parse_exit(mode: str) -> Optional[dt.time]:
    """'expiry' -> None (hold); 'closeHHMM' (e.g. close1545) -> buy-back time."""
    if mode == "expiry":
        return None
    if mode.startswith("close") and len(mode) == 9 and mode[5:].isdigit():
        t = dt.time(int(mode[5:7]), int(mode[7:9]))
        if dt.time(13, 0) <= t <= dt.time(15, 58):
            return t
    raise argparse.ArgumentTypeError(f"exit must be 'expiry' or 'closeHHMM' (13:00-15:58), got {mode!r}")


def exit_mode(mode: str) -> str:
    parse_exit(mode)
    return mode


# ── Order execution ───────────────────────────────────────────────────────────

def _poll(trading: TradingClient, order_id: str, seconds: int):
    """Poll until filled/terminal or timeout. Returns the latest Order."""
    end = time.time() + seconds
    order = trading.get_order_by_id(order_id)
    while time.time() < end:
        if order.status == OrderStatus.FILLED or order.status in TERMINAL:
            break
        time.sleep(2)
        order = trading.get_order_by_id(order_id)
    return order


def work_order(trading: TradingClient, symbols: dict[str, str], qty: int,
               start_price: float, opening: bool, max_steps: int,
               step_wait: int = 20, step: float = 0.01, cap: Optional[float] = None):
    """
    Start at `start_price` and concede `step` per `step_wait` seconds for up to
    `max_steps` steps. Credit goes DOWN when opening, debit goes UP when closing
    (never above `cap`). Returns the filled Order or None. Never leaves a live
    order behind: each step cancels, then re-checks for a late fill.
    """
    last_price = None
    for n in range(max_steps + 1):
        price = round(start_price - step * n if opening else start_price + step * n, 2)
        if cap is not None and not opening:
            price = min(price, round(cap, 2))
        if opening and price <= 0:
            return None
        if price == last_price:      # capped — no point re-sending the same price
            continue
        last_price = price
        order = trading.submit_order(build_order(symbols, qty, price, opening))
        logger.info("%s submitted: price=%.2f (step %d) id=%s",
                    "OPEN" if opening else "CLOSE", price, n, order.id)
        order = _poll(trading, str(order.id), step_wait)
        if order.status != OrderStatus.FILLED:
            try:
                trading.cancel_order_by_id(str(order.id))
            except Exception as exc:
                logger.warning("cancel failed (maybe already done): %s", exc)
            time.sleep(1)
            order = trading.get_order_by_id(str(order.id))  # catch a late fill
        if order.status == OrderStatus.FILLED:
            return order
    return None


# ── Logging ───────────────────────────────────────────────────────────────────

def write_row(row: dict) -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def account_snapshot(trading, tag: str) -> None:
    """Append the account's true state to logs/account_daily.csv. The 'morning'
    snapshot shows what happened overnight (assignment/exercise of the previous
    day's condor), which no in-session number can see. Never raises."""
    try:
        acct = trading.get_account()
        positions = trading.get_all_positions()
        pos = ";".join(f"{p.symbol}:{p.qty}" for p in positions)
        os.makedirs(config.LOG_DIR, exist_ok=True)
        new = not os.path.exists(ACCOUNT_CSV)
        with open(ACCOUNT_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "time_et", "tag", "equity", "last_equity", "cash",
                            "options_buying_power", "positions"])
            now = dt.datetime.now(ET)
            w.writerow([now.date().isoformat(), now.strftime("%H:%M:%S"), tag, acct.equity,
                        acct.last_equity, acct.cash, acct.options_buying_power, pos])
        if tag == "morning" and positions:
            notify(f"⚠️ **Positionen über Nacht im Konto:** {pos}  (Equity ${float(acct.equity):.2f})")
    except Exception as exc:
        logger.warning("account snapshot (%s) failed: %s", tag, exc)


def log_skip(day: dt.date, reason: str, msg: str, persist: bool = True, **extra) -> int:
    """A session without a trade still gets a CSV row, so the report can count
    skips, and is marked done so a restart can't enter late the same day."""
    row = {"date": day.isoformat(), "status": f"skip:{reason}", **extra}
    if not persist:      # manual --now / dry-run test: log only, don't block the real bot
        logger.info("skip (%s): %s", reason, msg)
        return 0
    write_row(row)
    save_state({"date": day.isoformat(), "done": True, "row": row})
    notify(f"⚪ **CONDOR kein Trade** {day} – {msg}")
    return 0


def wait_until(t: dt.time) -> None:
    while True:
        now = dt.datetime.now(ET)
        target = dt.datetime.combine(now.date(), t, tzinfo=ET)
        left = (target - now).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))


# ── Main ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="0DTE SPY iron condor paper probe.")
    p.add_argument("--entry-time", type=dt.time.fromisoformat,
                   default=dt.time.fromisoformat(config.CONDOR_ENTRY_TIME))
    p.add_argument("--distance", type=float, default=config.CONDOR_DISTANCE,
                   help="short strike distance from spot ($)")
    p.add_argument("--width", type=float, default=config.CONDOR_WIDTH, help="spread width ($)")
    p.add_argument("--qty", type=int, default=None,
                   help="fixed contracts; overrides config.CONDOR_SIZING")
    p.add_argument("--exit", type=exit_mode, default=exit_mode(config.CONDOR_EXIT),
                   help="expiry = hold & log theoretical settlement; closeHHMM (e.g. close1545) "
                        "= buy the condor back at that ET time")
    p.add_argument("--max-concede", type=int, default=config.CONDOR_MAX_CONCEDE,
                   help="max cents conceded from mid per leg-set while working the order")
    p.add_argument("--min-credit", type=float, default=config.CONDOR_MIN_CREDIT)
    p.add_argument("--submit", action="store_true", default=config.CONDOR_SUBMIT,
                   help="actually send paper orders (default: config.CONDOR_SUBMIT)")
    p.add_argument("--now", action="store_true", help="skip waiting for --entry-time")
    p.add_argument("--loop", action="store_true", default=config.CONDOR_LOOP,
                   help="keep running day after day (default: config.CONDOR_LOOP)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if config.STRATEGY != "condor":
        print(f"config.STRATEGY is '{config.STRATEGY}', not 'condor' — refusing to run so the "
              f"two strategies never share an account. Set STRATEGY = \"condor\" in config.py "
              f"or run with BOT_STRATEGY=condor.", file=sys.stderr)
        return 2
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(os.path.join(config.LOG_DIR, "condor_bot.log"),
                                      encoding="utf-8")])

    if args.submit and not config.PAPER:
        print("Refusing to submit: config.PAPER is False. This probe is paper-only.", file=sys.stderr)
        return 2

    stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    opt = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET, paper=config.PAPER)

    if args.now or not args.loop:
        return run_session(args, stock, opt, trading)

    logger.info("Loop mode: running one condor per trading day until you close this window.")
    while True:
        try:
            run_session(args, stock, opt, trading)
        except Exception as exc:
            logger.exception("Session failed — check the paper account for open legs. "
                             "Trying again tomorrow.")
            notify(f"🚨 **CONDOR-Bot Fehler:** `{type(exc).__name__}: {str(exc)[:300]}` – "
                   f"Konto auf offene Legs prüfen. Nächster Versuch morgen.")
        # Sleep until 06:00 ET the next calendar day, then let run_session decide
        # (non-trading day -> returns immediately, loop sleeps another day).
        now = dt.datetime.now(ET)
        nxt = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(6, 0), tzinfo=ET)
        logger.info("Next check at %s ET.", nxt.strftime("%Y-%m-%d %H:%M"))
        while dt.datetime.now(ET) < nxt:
            time.sleep(60)


STATE_PATH = os.path.join(config.LOG_DIR, "condor_state.json")


def save_state(state: dict) -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def load_state() -> Optional[dict]:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def spy_session_close(stock: StockHistoricalDataClient, day: dt.date) -> float:
    """Regular-session close (daily bar), not a post-market print."""
    req = StockBarsRequest(
        symbol_or_symbols=config.UNDERLYING, timeframe=TimeFrame.Day, feed=DataFeed.IEX,
        start=dt.datetime.combine(day, dt.time(0, 0), tzinfo=ET),
        end=dt.datetime.combine(day, dt.time(23, 59), tzinfo=ET))
    bars = stock.get_stock_bars(req).data.get(config.UNDERLYING, [])
    return float(bars[-1].close) if bars else spy_last(stock)


def has_spy_exposure(trading: TradingClient) -> bool:
    """Any open SPY/SPX option position, SPY stock (e.g. from an assignment) or
    open option order (e.g. left by a crash)."""
    try:
        for p in trading.get_all_positions():
            sym = str(p.symbol)
            if sym.startswith(("SPY", "SPX")):
                if len(sym) <= 5:
                    notify(f"🚨 **{sym}-Aktienposition im Konto** ({p.qty} Stück) – vermutlich "
                           f"Zuteilung aus einem Condor. Kein neuer Trade, bitte manuell schließen.")
                return True
        return any(o.asset_class == AssetClass.US_OPTION for o in trading.get_orders())
    except Exception as exc:
        logger.warning("exposure check failed (%s) — assuming none", exc)
        return False


def shadow_spxw(opt, day: dt.date, spy_px: float, args) -> None:
    """Record what the SPXW twin of today's condor would have looked like
    (spot, credit, spreads) WITHOUT trading it. Builds the SPX data set while
    the account is too small for SPX. Never raises."""
    try:
        p = spxw_profile()
        spot = estimate_index_spot(opt, day, p.root, spy_px * p.scale, p.step)
        legs = condor_strikes(spot, args.distance * p.scale, args.width * p.scale, p.step)
        q = leg_quotes(opt, leg_symbols(day, legs, p.root))
        mid_c, nat_c = credit_from_quotes(q)
        spreads = [q[k][1] - q[k][0] for k in q]
        path = os.path.join(config.LOG_DIR, "spx_shadow.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "time", "spy", "spx_est", "short_put", "long_put",
                            "short_call", "long_call", "mid_credit", "natural_credit",
                            "avg_leg_spread", "width", "collateral_per_condor"])
            w.writerow([day.isoformat(), dt.datetime.now(ET).strftime("%H:%M:%S"),
                        f"{spy_px:.2f}", f"{spot:.2f}", legs.short_put, legs.long_put,
                        legs.short_call, legs.long_call, f"{mid_c:.2f}", f"{nat_c:.2f}",
                        f"{sum(spreads) / 4:.2f}", args.width * p.scale,
                        f"{(args.width * p.scale - mid_c) * 100:.0f}"])
    except Exception as exc:
        logger.warning("SPXW shadow record failed (ignored): %s", exc)


def close_condor(opt, trading, symbols: dict[str, str], qty: int, width: float):
    """Buy the condor back. Pass 1 works up from the mid in 1c steps; if that
    fails, pass 2 re-quotes and works up from the natural debit in 5c steps
    (capped at the width = max value). Returns the filled Order or None."""
    def debit(q, side):   # side 0 = mid, 1 = natural (pay ask on shorts, take bid on longs)
        def px(k, short):
            b, a = q[k]
            return (b + a) / 2 if side == 0 else (a if short else b)
        return (px("short_call", True) - px("long_call", False)
                + px("short_put", True) - px("long_put", False))

    for attempt, (side, step, steps, wait) in enumerate([(0, 0.01, 10, 20), (1, 0.05, 12, 10)], 1):
        try:
            q = leg_quotes(opt, symbols, allow_zero_bid=True)
            start = round(min(max(debit(q, side), 0.01), width), 2)
        except Exception as exc:
            logger.warning("close quotes failed (%s) — working from width/2", exc)
            start = round(width / 2, 2)
        logger.info("CLOSE pass %d: start debit %.2f, step %.2f x %d", attempt, start, step, steps)
        filled = work_order(trading, symbols, qty, start, False, max_steps=steps,
                            step_wait=wait, step=step, cap=width)
        if filled is not None:
            return filled
    return None


def finish_position(args, stock, opt, trading, st: dict) -> int:
    """Exit phase. Driven entirely by `st` so it can resume after a restart."""
    row, symbols, qty, fill_c = st["row"], st["symbols"], st["qty"], st["fill_c"]
    legs = Legs(**st["legs"])
    day = dt.date.fromisoformat(st["date"])

    close_at = parse_exit(st["exit"])
    if close_at is not None:
        wait_until(close_at)
        closed = close_condor(opt, trading, symbols, qty, legs.long_call - legs.short_call)
        if closed is None:
            logger.error("CLOSE NOT FILLED — manage the position manually!")
            row.update(status="close_failed")
            notify("🚨 **CONDOR CLOSE NICHT GEFILLT** – Position sofort manuell schließen, "
                   "sonst droht Zuteilung (SPY wird physisch geliefert)!")
        else:
            cost = abs(float(closed.filled_avg_price))
            row.update(exit_time=dt.datetime.now(ET).isoformat(timespec="seconds"),
                       close_cost=f"{cost:.4f}",
                       realized_pnl=f"{(fill_c - cost) * 100 * qty:.2f}",
                       pnl_per_contract=f"{(fill_c - cost) * 100:.2f}",
                       status="closed")
            pc = (fill_c - cost) * 100
            notify(f"{'✅' if pc >= 0 else '❌'} **CONDOR EXIT {close_at:%H:%M}** {day}  {qty}x  "
                   f"Rückkauf ${cost:.2f}  **${pc:+.2f}/Kontrakt**  (gesamt ${pc * qty:+.2f})")
    else:
        wait_until(dt.time(16, 5))
        spy_close = spy_session_close(stock, day)
        close = spy_close * st.get("ratio", 1.0)   # SPX: scaled from SPY at entry (no index feed)
        theo = (fill_c - settle_value(legs, close)) * 100 * qty
        row.update(spy_close=f"{close:.2f}", theoretical_pnl=f"{theo:.2f}",
                   pnl_per_contract=f"{theo / qty:.2f}", status="expired")
        notify(f"{'✅' if theo >= 0 else '❌'} **CONDOR VERFALL** {day}  {qty}x  SPY {close:.2f}  "
               f"**${theo / qty:+.2f}/Kontrakt**  (gesamt ${theo:+.2f}, rechnerisch)")
        logger.info("SPY close %.2f — theoretical settlement P&L $%.2f = $%.2f per contract "
                    "(check real assignment/exercise in the paper account)", close, theo, theo / qty)

    # Ground truth: the account's equity change over the trade (one strategy per
    # account, so this is the condor's real P&L incl. anything the broker did).
    try:
        eq_after = float(trading.get_account().equity)
        row["equity_after"] = f"{eq_after:.2f}"
        if row.get("equity_before"):
            row["equity_change"] = f"{eq_after - float(row['equity_before']):.2f}"
    except Exception as exc:
        logger.warning("could not read equity after exit: %s", exc)

    write_row(row)
    st["done"], st["row"] = True, row
    save_state(st)
    account_snapshot(trading, "exit")
    return 0


def run_session(args, stock, opt, trading) -> int:
    today = dt.datetime.now(ET).date()
    persist = bool(args.submit and not args.now)

    def skip(reason: str, msg: str, **extra) -> int:
        return log_skip(today, reason, msg, persist=persist, **extra)

    cal = [] if args.now else trading.get_calendar(GetCalendarRequest(start=today, end=today))
    if not args.now and not cal:
        print(f"{today} is not a trading day.")
        return 0

    # Restart safety: never open a second condor for the same day, and pick up
    # an open one where we left off (exit phase) if the process was restarted.
    st = None if args.now else load_state()
    if st and st.get("date") == today.isoformat():
        if st.get("done"):
            logger.info("Today's session is already complete.")
            return 0
        logger.info("Resuming today's open condor after restart.")
        return finish_position(args, stock, opt, trading, st)

    if not args.now:
        account_snapshot(trading, "morning")
        now = dt.datetime.now(ET)
        late = now - dt.datetime.combine(today, args.entry_time, tzinfo=ET)
        if late > dt.timedelta(minutes=10):
            print(f"Entry time {args.entry_time} already passed by {late} — not entering late today.")
            return skip("late_start", f"Bot erst {now:%H:%M} ET gestartet, "
                                                 f"Einstieg {args.entry_time} verpasst")
        logger.info("Waiting for entry time %s ET (leave this window open)...", args.entry_time)
        wait_until(args.entry_time)

    if args.submit and has_spy_exposure(trading):
        logger.error("Open SPY option positions/orders already on the account (crash "
                     "leftover or another strategy?) — NOT entering. Clean up manually.")
        skip("exposure", "offene SPY-Positionen/Orders im Konto – bitte manuell bereinigen")
        return 1

    acct = trading.get_account()
    prof = pick_profile(float(acct.equity))
    spy_px = spy_last(stock)
    if prof.name == "SPY":
        spot, ratio = spy_px, 1.0
    else:
        spot = estimate_index_spot(opt, today, prof.root, spy_px * prof.scale, prof.step)
        ratio = spot / spy_px
    distance, width = args.distance * prof.scale, args.width * prof.scale
    legs = condor_strikes(spot, distance, width, prof.step)
    symbols = leg_symbols(today, legs, prof.root)
    try:
        quotes = leg_quotes(opt, symbols)
    except RuntimeError as exc:
        logger.error("%s — skipping session", exc)
        return skip("no_quote", f"keine brauchbaren Quotes ({exc})"[:300],
                        underlying=prof.name, spot=f"{spot:.2f}")
    mid_c, nat_c = credit_from_quotes(quotes)
    skip_info = {"underlying": prof.name, "spot": f"{spot:.2f}", "mid_credit": f"{mid_c:.4f}",
                 "natural_credit": f"{nat_c:.4f}", "short_put": legs.short_put,
                 "long_put": legs.long_put, "short_call": legs.short_call,
                 "long_call": legs.long_call, "entry_quotes": json.dumps(quotes)}
    logger.info("Underlying %s (equity $%.0f, mode %s): spot %.2f, %g away, %g wide",
                prof.name, float(acct.equity), config.CONDOR_UNDERLYING, spot, distance, width)
    if prof.name == "SPY" and config.CONDOR_SPX_SHADOW:
        shadow_spxw(opt, today, spy_px, args)
    qty = args.qty
    if qty is None:
        if config.CONDOR_SIZING == "scaled":
            # 1 condor per CONDOR_EQUITY_PER_CONTRACT of equity (min 1), never more than
            # the buying power allows. Backtest: bigger sizes ruin a small account.
            want = max(1, int(float(acct.equity) // config.CONDOR_EQUITY_PER_CONTRACT))
            fits = condor_qty(float(acct.options_buying_power), width, mid_c, config.CONDOR_BP_FRACTION)
            qty = min(want, fits)
            logger.info("Sizing SCALED: equity $%.0f / $%d per contract -> %d, buying power fits %d -> %d",
                        float(acct.equity), config.CONDOR_EQUITY_PER_CONTRACT, want, fits, qty)
            if qty < 1:
                logger.warning("Buying power too low for one condor (needs ~$%.0f) — skipping",
                               (width - mid_c) * 100)
                return skip("buying_power", "zu wenig Kaufkraft für einen Condor",
                                **skip_info)
        elif config.CONDOR_SIZING == "max":
            bp = float(acct.options_buying_power)
            qty = condor_qty(bp, width, mid_c, config.CONDOR_BP_FRACTION)
            logger.info("Sizing MAX: options buying power $%.2f x %.0f%% -> %d condor(s)",
                        bp, config.CONDOR_BP_FRACTION * 100, qty)
            if qty < 1:
                logger.warning("Buying power too low for one condor (needs ~$%.0f) — skipping",
                               (width - mid_c) * 100)
                return skip("buying_power", "zu wenig Kaufkraft für einen Condor",
                                **skip_info)
        else:
            qty = config.CONDOR_QTY
    max_risk = (width - mid_c) * 100 * qty
    logger.info("spot=%.2f legs=%s", spot, legs)
    logger.info("mid credit=%.2f natural credit=%.2f max risk=$%.0f", mid_c, nat_c, max_risk)

    if mid_c > width * config.CONDOR_MAX_CREDIT_FRAC:
        logger.error("credit %.2f > %.0f%% of width %.1f — quotes look stale/wrong, skipping session",
                     mid_c, config.CONDOR_MAX_CREDIT_FRAC * 100, width)
        return skip("stale_quotes", f"Credit ${mid_c:.2f} unplausibel hoch "
                                               f"(Quotes veraltet?)", qty=qty, **skip_info)

    if mid_c < args.min_credit * prof.scale:
        logger.warning("credit %.2f < min %.2f — skipping session", mid_c, args.min_credit * prof.scale)
        return skip("low_credit", f"Credit ${mid_c:.2f} unter Minimum "
                                             f"${args.min_credit * prof.scale:.2f}", qty=qty, **skip_info)

    row = {
        "date": today.isoformat(), "underlying": prof.name, "spot": f"{spot:.2f}", "qty": qty,
        "short_put": legs.short_put, "long_put": legs.long_put,
        "short_call": legs.short_call, "long_call": legs.long_call,
        "mid_credit": f"{mid_c:.4f}", "natural_credit": f"{nat_c:.4f}",
        "exit_mode": args.exit, "equity_before": f"{float(acct.equity):.2f}",
        "entry_quotes": json.dumps(quotes),
    }

    if not args.submit:
        o = build_order(symbols, qty, mid_c, opening=True)
        print("\nDRY-RUN — would submit:\n", o.model_dump_json(indent=2, exclude_none=True))
        return 0

    filled = work_order(trading, symbols, qty, round(mid_c, 2), True, args.max_concede)
    if filled is None:
        logger.warning("entry not filled within %dc of mid — no trade today", args.max_concede)
        row.update(status="no_fill")
        notify(f"⚪ **CONDOR kein Fill** {today}  {qty}x  mid ${mid_c:.2f} – kein Trade heute")
        write_row(row)
        save_state({"date": today.isoformat(), "done": True, "row": row})
        return 0

    fill_c = abs(float(filled.filled_avg_price))
    slip = mid_c - fill_c
    row.update(
        entry_time=dt.datetime.now(ET).isoformat(timespec="seconds"),
        fill_credit=f"{fill_c:.4f}", slip_vs_mid=f"{slip:.4f}",
        slip_per_leg=f"{slip / 4:.4f}", status="open",
    )
    logger.info("FILLED credit=%.2f (mid %.2f, slippage %.2f = %.3f/leg; backtest assumed ~0.01/leg)",
                fill_c, mid_c, slip, slip / 4)

    notify(f"🟢 **CONDOR ENTRY** {today}  {qty}x  {prof.name} {spot:.2f}\n"
           f"`{legs.long_put:g}/{legs.short_put:g}P  {legs.short_call:g}/{legs.long_call:g}C`  "
           f"Kredit **${fill_c:.2f}** (mid ${mid_c:.2f}, Slippage {slip / 4 * 100:.1f}c/Leg)  "
           f"max. Risiko ${(width - fill_c) * 100 * qty:.0f}")
    st = {"date": today.isoformat(), "done": False, "row": row, "symbols": symbols,
          "qty": qty, "fill_c": fill_c, "exit": args.exit, "legs": legs.__dict__,
          "underlying": prof.name, "ratio": ratio}
    save_state(st)   # written BEFORE waiting, so a restart can resume the exit
    account_snapshot(trading, "entry")
    return finish_position(args, stock, opt, trading, st)


if __name__ == "__main__":
    raise SystemExit(main())
