#!/usr/bin/env python3
"""
Spread bot: trades two SPY rule sets on the Alpaca PAPER account and sizes
them from the account's cash.

    Nectar  iron condor. Monday (else Tuesday). Short put $2.50 under SPY with a
            $5 wing, short call 3% over SPY with a $3 wing. Expires Thursday
            (Friday for a Tuesday entry). Bought back at 75% of the credit.
    Comb    put credit spread. Monday. Short put nearest 15 delta, long put $10
            below, expiring the next Friday at least 7 days out. Bought back at
            50% of the credit.

Both: no stop-loss; closed 30 minutes before the bell on expiry day if the
buy-back never filled; one position per strategy at a time.

Sizing: the worst-case loss of all open positions together may use the whole
cash balance (no margin). Cash is split 7% Nectar / 93% Comb; a strategy that
cannot afford one contract hands its share to the other (spread_rules.allocate).

The rules are NOT backtested. Related tests in this repo (0DTE condor, 30-45 DTE
put spreads) were negative.

    python spread_bot.py                 # loop forever (orders if SPREAD_SUBMIT)
    python spread_bot.py --now           # one pass right now, dry-run
    python spread_bot.py --now --submit  # one pass right now, real paper orders
    python spread_bot.py --status        # state file + account
    python spread_bot.py --clear-halt    # resume entries after a manual check

Spec: docs/superpowers/specs/2026-11-14-spread-bot-design.md
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import socket
import sys

import time
from typing import Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    ContractType, OrderClass, OrderSide, OrderStatus, PositionIntent, TimeInForce,
)
from alpaca.trading.requests import GetCalendarRequest, LimitOrderRequest, OptionLegRequest

import config
import spread_rules as rules
from condor_paper import TERMINAL, _poll, leg_quotes, spy_last
from notifier import notify
from strikes import build_occ_symbol

logger = logging.getLogger("spread_bot")
for _s in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ET = config.ET
LOCK_PORT = 47321          # same port as trend_bot.py: the two must never run together
_LOCK = None
STATE_PATH = os.path.join(config.LOG_DIR, "spread_state.json")
CSV_PATH = os.path.join(config.LOG_DIR, "spread_trades.csv")
STRATEGIES = ("nectar", "comb")


# ── State ─────────────────────────────────────────────────────────────────────

def empty_state() -> dict:
    """nectar/comb: the open position or None. skipped: {strategy: date it gave
    up for the day}. entered: {strategy: date of its last entry}. next_try:
    {strategy: time before which it does not retry}. halted: reason or None."""
    return {"nectar": None, "comb": None, "skipped": {}, "halted": None,
            "entered": {}, "next_try": {}, "warned": {}}


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return empty_state()
    return {**empty_state(), **st}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


# ── Start-up guards ───────────────────────────────────────────────────────────

def guard():
    """Exit code 2 (with a message) if this bot must not run, else None."""
    if config.STRATEGY != "spreads":
        print(f"config.STRATEGY is '{config.STRATEGY}', not 'spreads' — refusing to run (one strategy "
              f"per account). Set STRATEGY = \"spreads\" or BOT_STRATEGY=spreads.", file=sys.stderr)
        return 2
    if config.PAPER is not True:
        print("Refusing: config.PAPER is not True. The spread bot is paper-only.", file=sys.stderr)
        return 2
    return None


def take_lock() -> bool:
    global _LOCK                       # keep the socket open for the process lifetime
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _LOCK.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return False
    return True


# ── Orders ────────────────────────────────────────────────────────────────────

def build_order(legs: list[dict], qty: int, price: float, opening: bool) -> LimitOrderRequest:
    """Multi-leg limit order. Alpaca mleg convention: limit_price < 0 is a net
    CREDIT, > 0 a net DEBIT. `price` is the positive credit (opening) or debit
    (closing)."""
    reqs = []
    for leg in legs:
        if opening:
            side, intent = ((OrderSide.SELL, PositionIntent.SELL_TO_OPEN) if leg["short"]
                            else (OrderSide.BUY, PositionIntent.BUY_TO_OPEN))
        else:
            side, intent = ((OrderSide.BUY, PositionIntent.BUY_TO_CLOSE) if leg["short"]
                            else (OrderSide.SELL, PositionIntent.SELL_TO_CLOSE))
        reqs.append(OptionLegRequest(symbol=leg["symbol"], ratio_qty=1, side=side, position_intent=intent))
    limit = -abs(price) if opening else abs(price)
    return LimitOrderRequest(qty=qty, order_class=OrderClass.MLEG, time_in_force=TimeInForce.DAY,
                             limit_price=round(limit, 2), legs=reqs)


def work(trading, legs: list[dict], qty: int, start_price: float, opening: bool, max_steps: int,
         step: float = 0.01, step_wait: int = 20, cap: Optional[float] = None):
    """Start at `start_price` and concede `step` every `step_wait` seconds, up
    to `max_steps` times: the credit goes down when opening, the debit up when
    closing (never above `cap`). Returns the filled Order or None. Never leaves
    a live order behind (same logic as condor_paper.work_order)."""
    last_price = None
    for n in range(max_steps + 1):
        price = round(start_price - step * n if opening else start_price + step * n, 2)
        if cap is not None and not opening:
            price = min(price, round(cap, 2))
        if price <= 0:
            if opening:
                return None
            price = 0.01
        if price == last_price:
            continue
        last_price = price
        order = trading.submit_order(build_order(legs, qty, price, opening))
        logger.info("%s submitted: qty=%d price=%.2f (step %d) id=%s",
                    "OPEN" if opening else "CLOSE", qty, price, n, order.id)
        order = _poll(trading, str(order.id), step_wait)
        if order.status != OrderStatus.FILLED:
            try:
                trading.cancel_order_by_id(str(order.id))
            except Exception as exc:
                logger.warning("cancel failed (maybe already done): %s", exc)
            time.sleep(1)
            order = trading.get_order_by_id(str(order.id))   # catch a late fill
        if order.status == OrderStatus.FILLED:
            return order
    return None


def credit_of(legs: list[dict], quotes: dict[str, tuple[float, float]]) -> tuple[float, float]:
    """(mid_credit, natural_credit) of the position from {symbol: (bid, ask)}."""
    mid = nat = 0.0
    for leg in legs:
        bid, ask = quotes[leg["symbol"]]
        if leg["short"]:
            mid += (bid + ask) / 2.0
            nat += bid
        else:
            mid -= (bid + ask) / 2.0
            nat -= ask
    return round(mid, 4), round(nat, 4)


# ── Entry planning ────────────────────────────────────────────────────────────

SKIP_REASON: dict[str, str] = {}      # last reason a plan_* returned None, for the Discord message


def _no_plan(strategy: str, reason: str) -> None:
    SKIP_REASON[strategy] = reason
    logger.warning("%s: no entry — %s", strategy, reason)
    return None


def trading_days(trading, day: dt.date) -> set[dt.date]:
    cal = trading.get_calendar(GetCalendarRequest(start=day - dt.timedelta(days=7),
                                                  end=day + dt.timedelta(days=21)))
    return {c.date for c in cal}


def put_deltas(opt, expiry: dt.date, spot: float) -> dict[float, float]:
    """{strike: delta} of the OTM SPY puts of `expiry` that carry a delta."""
    chain = opt.get_option_chain(OptionChainRequest(
        underlying_symbol=config.UNDERLYING, expiration_date=expiry, type=ContractType.PUT,
        strike_price_gte=round(spot * 0.85), strike_price_lte=round(spot)))
    out = {}
    for sym, snap in chain.items():
        if snap.greeks is not None and snap.greeks.delta is not None:
            out[int(sym[-8:]) / 1000.0] = float(snap.greeks.delta)
    return out


def _finish_plan(strategy: str, opt, expiry: dt.date, named: dict[str, tuple[str, float, bool]],
                 width: float, spot: float) -> Optional[dict]:
    """Quote the legs and turn them into a plan. `named` = {leg name: (right, strike, short)}.
    Raises RuntimeError if a leg has no usable quote."""
    symbols = {name: build_occ_symbol(config.UNDERLYING, expiry, right, strike)
               for name, (right, strike, _) in named.items()}
    q = leg_quotes(opt, symbols)
    legs = [{"symbol": symbols[name], "short": short} for name, (_, _, short) in named.items()]
    mid, nat = credit_of(legs, {symbols[name]: q[name] for name in named})
    if mid <= 0:
        return _no_plan(strategy, f"mid credit {mid:.2f} is not positive")
    if mid > config.SPREAD_MAX_CREDIT_FRAC * width:
        return _no_plan(strategy, f"mid credit {mid:.2f} exceeds {config.SPREAD_MAX_CREDIT_FRAC:.0%} "
                                  f"of the {width:.0f}$ wing (bad quotes?)")
    floor_credit = round(mid, 2) - config.SPREAD_MAX_CONCEDE * 0.01    # worst credit the entry accepts
    if floor_credit <= 0:
        return _no_plan(strategy, f"mid credit {mid:.2f} too small to work an order")
    return {"strategy": strategy, "expiry": expiry.isoformat(), "legs": legs, "width": width,
            "mid_credit": round(mid, 2), "natural_credit": round(nat, 2), "spot": round(spot, 2),
            "strikes": {name: strike for name, (_, strike, _) in named.items()},
            "loss": (width - floor_credit) * 100.0}


def plan_nectar(stock, opt, trading, day: dt.date) -> Optional[dict]:
    try:
        spot = spy_last(stock)
        days = trading_days(trading, day)
    except Exception as exc:
        return _no_plan("nectar", f"market data failed: {exc}")
    k = rules.nectar_strikes(spot)
    named = {"short_put": ("PUT", k["short_put"], True), "long_put": ("PUT", k["long_put"], False),
             "short_call": ("CALL", k["short_call"], True), "long_call": ("CALL", k["long_call"], False)}
    expiry = rules.nectar_expiry(day, days)
    candidates = [expiry]
    friday = expiry + dt.timedelta(days=1)
    if expiry.weekday() == 3 and friday in days:     # Thursday strikes not listed -> Friday
        candidates.append(friday)
    err = None
    for exp in candidates:
        try:
            return _finish_plan("nectar", opt, exp, named, rules.NECTAR_PUT_WIDTH, spot)
        except Exception as exc:
            err = exc
            logger.warning("nectar: %s not quotable: %s", exp, exc)
    return _no_plan("nectar", f"no usable quotes: {err}")


def plan_comb(stock, opt, trading, day: dt.date) -> Optional[dict]:
    try:
        spot = spy_last(stock)
        expiry = rules.comb_expiry(day, trading_days(trading, day))
        deltas = put_deltas(opt, expiry, spot)
    except Exception as exc:
        return _no_plan("comb", f"market data failed: {exc}")
    short = rules.pick_delta_strike(deltas, config.SPREAD_COMB_DELTA)
    if short is None:
        return _no_plan("comb", f"Alpaca returned no put deltas for {expiry}")
    width = float(config.SPREAD_COMB_WIDTH)
    named = {"short_put": ("PUT", short, True), "long_put": ("PUT", short - width, False)}
    try:
        plan = _finish_plan("comb", opt, expiry, named, width, spot)
    except Exception as exc:
        return _no_plan("comb", f"no usable quotes: {exc}")
    if plan is not None:
        plan["delta"] = deltas[short]
    return plan


def enter(trading, plan: dict, qty: int, submit: bool) -> Optional[dict]:
    """Work the opening order. Returns the position record, or None (dry-run / no fill)."""
    name = plan["strategy"]
    logger.info("%s: %d x %s exp %s, mid credit %.2f, worst-case loss/contract %.0f$",
                name, qty, plan["strikes"], plan["expiry"], plan["mid_credit"], plan["loss"])
    if not submit:
        logger.info("%s: dry-run, no order sent", name)
        return None
    equity = float(trading.get_account().equity)
    order = work(trading, plan["legs"], qty, plan["mid_credit"], True, config.SPREAD_MAX_CONCEDE)
    if order is None:
        SKIP_REASON[name] = (f"kein Fill zwischen {plan['mid_credit']:.2f} und "
                             f"{plan['mid_credit'] - config.SPREAD_MAX_CONCEDE * 0.01:.2f}")
        return None
    credit = abs(float(order.filled_avg_price))
    return {"strategy": name, "entry_date": dt.datetime.now(ET).date().isoformat(),
            "expiry": plan["expiry"], "legs": plan["legs"], "qty": qty, "width": plan["width"],
            "mid_credit": plan["mid_credit"], "credit": credit,
            "buyback": rules.buyback_price(credit, config.SPREAD_BUYBACK[name]),
            "tp_order_id": None, "equity_before": equity}


# ── Exits ─────────────────────────────────────────────────────────────────────

CSV_FIELDS = ["strategy", "entry_date", "expiry", "qty", "width", "mid_credit", "credit", "buyback",
              "exit_date", "exit_reason", "exit_debit", "pnl", "equity_before", "equity_after"]
EXIT_TEXT = {"buyback": "Rückkauf-Ziel erreicht", "expiry_close": "am Verfallstag geschlossen",
             "expired": "verfallen bzw. vom Broker geschlossen", "gone": "Position nicht mehr im Konto"}


def record_exit(pos: dict, reason: str, debit: Optional[float], trading) -> None:
    """One CSV row + a Discord message for a finished position. `debit` is the
    price paid to close, None if unknown (closed while the bot was away)."""
    try:
        equity_after = float(trading.get_account().equity)
    except Exception:
        equity_after = None
    pnl = None if debit is None else round((pos["credit"] - debit) * 100.0 * pos["qty"], 2)
    row = {k: pos.get(k, "") for k in CSV_FIELDS}
    row.update(exit_date=dt.datetime.now(ET).date().isoformat(), exit_reason=reason,
               exit_debit="" if debit is None else f"{debit:.2f}",
               pnl="" if pnl is None else f"{pnl:.2f}",
               equity_after="" if equity_after is None else f"{equity_after:.2f}")
    os.makedirs(os.path.dirname(CSV_PATH) or ".", exist_ok=True)
    new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    logger.info("%s closed (%s): debit=%s pnl=%s", pos["strategy"], reason, row["exit_debit"], row["pnl"])
    result = "P&L unbekannt" if pnl is None else f"P&L **{pnl:+.2f} $**"
    notify(f"📕 **{pos['strategy'].upper()}** {pos['qty']}x beendet — {EXIT_TEXT.get(reason, reason)}. "
           f"Credit {pos['credit']:.2f}, Rückkauf {row['exit_debit'] or '?'}, {result}")


def _halt(state: dict, reason: str, text: str) -> None:
    if state["halted"] != reason:
        logger.error("HALT (%s): %s", reason, text)
        notify(f"🚨 **SPREAD-Bot angehalten ({reason}):** {text} Keine neuen Einstiege, bis du "
               f"`python spread_bot.py --clear-halt` ausführst.")
    state["halted"] = reason


def reconcile(trading, state: dict, today: dt.date) -> dict:
    """Bring the state file in line with the account before anything else.
    - all legs of a position present: keep it.
    - SPY shares in the account (early assignment): halt; a position whose
      legs are not intact is marked "stuck" and left for a human.
    - no leg present: it was closed while we were not looking — record it.
    - some legs present or wrong size: halt, mark it stuck.
    A stuck position gets no automatic orders; intact ones keep their exits."""
    held = {p.symbol: float(p.qty) for p in trading.get_all_positions()}
    assigned = config.UNDERLYING in held
    if assigned:
        _halt(state, "assignment", f"{held[config.UNDERLYING]:.0f} SPY-Aktien im Konto (vorzeitige "
                                   f"Ausübung eines Short Puts?). Bitte Konto prüfen.")
    for name in STRATEGIES:
        pos = state[name]
        if pos is None:
            continue
        want = {leg["symbol"]: (-pos["qty"] if leg["short"] else pos["qty"]) for leg in pos["legs"]}
        have = {s: held.get(s, 0.0) for s in want}
        pos["stuck"] = have != want
        if not pos["stuck"]:
            continue
        if assigned:
            continue
        if any(have.values()):
            _halt(state, "legs_mismatch", f"{name}: erwartet {want}, im Konto {have}.")
            continue
        reason, debit = "buyback", None
        if pos.get("tp_order_id"):
            try:
                order = trading.get_order_by_id(pos["tp_order_id"])
                if order.status == OrderStatus.FILLED:
                    debit = abs(float(order.filled_avg_price))
                else:
                    reason = "gone"
            except Exception as exc:
                logger.warning("%s: buy-back order not readable: %s", name, exc)
        if debit is None and today > dt.date.fromisoformat(pos["expiry"]):
            reason = "expired"
        record_exit(pos, reason, debit, trading)
        state[name] = None
    return state


def ensure_buyback(trading, pos: dict, submit: bool) -> None:
    """Keep one closing limit order at the buy-back price working. Option
    orders are day orders, so this places a fresh one every trading day."""
    if not submit or pos["buyback"] < 0.01:
        return
    if pos.get("tp_order_id"):
        order = trading.get_order_by_id(pos["tp_order_id"])
        if order.status == OrderStatus.FILLED or order.status not in TERMINAL:
            return                      # filled (reconcile books it) or still working
    order = trading.submit_order(build_order(pos["legs"], pos["qty"], pos["buyback"], opening=False))
    pos["tp_order_id"] = str(order.id)
    logger.info("%s: buy-back order at %.2f placed (id=%s)", pos["strategy"], pos["buyback"], order.id)


def close_debits(legs: list[dict], quotes: dict[str, tuple[float, float]]) -> tuple[float, float]:
    """(mid_debit, natural_debit) to buy the position back from {symbol: (bid, ask)}.
    Natural = pay the ask on the shorts, take the bid on the longs."""
    mid = nat = 0.0
    for leg in legs:
        bid, ask = quotes[leg["symbol"]]
        if leg["short"]:
            mid += (bid + ask) / 2.0
            nat += ask
        else:
            mid -= (bid + ask) / 2.0
            nat -= bid
    return round(mid, 4), round(nat, 4)


def close_position(opt, trading, pos: dict):
    """Expiry-day exit: cancel the buy-back order, then buy the position back.
    Pass 1 works up from the mid in 1c steps, pass 2 from the natural debit in
    5c steps, capped at the wing width (the most it can be worth). Returns the
    filled Order (possibly the buy-back order itself) or None."""
    if pos.get("tp_order_id"):
        try:
            trading.cancel_order_by_id(pos["tp_order_id"])
        except Exception as exc:
            logger.warning("%s: cancel of buy-back order failed (maybe done): %s", pos["strategy"], exc)
        time.sleep(1)
        order = trading.get_order_by_id(pos["tp_order_id"])
        if order.status == OrderStatus.FILLED:
            return order
    symbols = {leg["symbol"]: leg["symbol"] for leg in pos["legs"]}
    width = pos["width"]
    for attempt, (natural, step, steps, wait) in enumerate([(False, 0.01, 10, 20), (True, 0.05, 12, 10)], 1):
        try:
            mid, nat = close_debits(pos["legs"], leg_quotes(opt, symbols, allow_zero_bid=True))
            start = round(min(max(nat if natural else mid, 0.01), width), 2)
        except Exception as exc:
            logger.warning("close quotes failed (%s) — working from width/2", exc)
            start = round(width / 2, 2)
        logger.info("%s CLOSE pass %d: start debit %.2f, step %.2f x %d",
                    pos["strategy"], attempt, start, step, steps)
        filled = work(trading, pos["legs"], pos["qty"], start, False, steps,
                      step=step, step_wait=wait, cap=width)
        if filled is not None:
            return filled
    return None


# ── Daily loop ────────────────────────────────────────────────────────────────

RETRY = dt.timedelta(minutes=15)      # wait this long after bad quotes / no fill


def _hhmm(text: str) -> dt.time:
    return dt.time(int(text[:2]), int(text[3:5]))


def session(trading, day: dt.date):
    """(open time, close time, trading days around `day`) in ET, or None if
    the market is closed on `day`."""
    cal = trading.get_calendar(GetCalendarRequest(start=day - dt.timedelta(days=7),
                                                  end=day + dt.timedelta(days=21)))
    for c in cal:
        if c.date == day:
            return c.open.time(), c.close.time(), {x.date for x in cal}
    return None


def capital_and_free(trading, state: dict) -> tuple[float, float]:
    """(capital, free cash). Capital = cash without the credits of the open
    positions (the base the 7/93 split is taken from). Free = cash not already
    reserved as collateral (full wing width) of open positions, and never more
    than Alpaca's options buying power. Margin and securities do not count."""
    acct = trading.get_account()
    cash = float(acct.cash)
    open_pos = [state[n] for n in STRATEGIES if state[n] is not None]
    credits = sum(p["credit"] * 100.0 * p["qty"] for p in open_pos)
    reserved = sum(p["width"] * 100.0 * p["qty"] for p in open_pos)
    return round(cash - credits, 2), round(min(cash - reserved, float(acct.options_buying_power)), 2)


def due_entries(now: dt.datetime, state: dict, days: set[dt.date]) -> list[str]:
    """Strategies that should try to enter right now, Comb first.
    Comb: Mondays. Nectar: Monday, or Tuesday if it has no entry yet this week
    (Monday was a holiday, had no fill or no budget). One entry per strategy
    and week; never while its position is open."""
    today = now.date()
    if state["halted"] or today not in days:
        return []
    if not (_hhmm(config.SPREAD_ENTRY_TIME) <= now.time() < _hhmm(config.SPREAD_ENTRY_END)):
        return []
    monday = today - dt.timedelta(days=today.weekday())
    out = []
    for name in ("comb", "nectar"):
        if state[name] is not None:
            continue
        if today.weekday() > (0 if name == "comb" else 1):
            continue
        last = state["entered"].get(name)
        if last and dt.date.fromisoformat(last) >= monday:
            continue
        if state["skipped"].get(name) == today.isoformat():
            continue
        retry = state["next_try"].get(name)
        if retry and now < dt.datetime.fromisoformat(retry):
            continue
        out.append(name)
    return out


def _give_up(state: dict, name: str, today: dt.date, text: str) -> None:
    state["skipped"][name] = today.isoformat()
    logger.info("%s: skipped today — %s", name, text)
    notify(f"⏭️ **{name.upper()}** setzt heute aus: {text}")


def _postpone(state: dict, name: str, now: dt.datetime, text: str) -> None:
    state["next_try"][name] = (now + RETRY).isoformat()
    logger.info("%s: retry in %s — %s", name, RETRY, text)
    if state["warned"].get(name) != now.date().isoformat():
        state["warned"][name] = now.date().isoformat()
        notify(f"⏳ **{name.upper()}** noch nicht eingestiegen ({text}). Neuer Versuch alle 15 Minuten "
               f"bis {config.SPREAD_ENTRY_END} ET.")


def tick(now: dt.datetime, stock, opt, trading, state: dict, submit: bool) -> dict:
    """One pass: reconcile, manage exits, then entries. Returns the new state."""
    today = now.date()
    ses = session(trading, today)
    if ses is None:
        return state
    open_t, close_t, days = ses
    if not (open_t <= now.time() < close_t):
        return state
    state = reconcile(trading, state, today)

    # 1. Exits: buy-back order every day, forced close on expiry day.
    for name in STRATEGIES:
        pos = state[name]
        if pos is None or pos.get("stuck"):
            continue
        expiring = today >= dt.date.fromisoformat(pos["expiry"])
        if expiring and now.time() >= rules.expiry_close_time(close_t):
            if not submit:
                logger.info("%s: dry-run, would close the expiring position now", name)
                continue
            order = close_position(opt, trading, pos)
            if order is None:
                notify(f"🚨 **{name.upper()}** ließ sich am Verfallstag nicht schließen — neuer Versuch läuft. "
                       f"Bitte Konto prüfen, sonst droht Ausübung (SPY wird physisch geliefert)!")
                continue
            reason = "buyback" if str(order.id) == pos.get("tp_order_id") else "expiry_close"
            record_exit(pos, reason, abs(float(order.filled_avg_price)), trading)
            state[name] = None
            save_state(state)
        else:
            ensure_buyback(trading, pos, submit)

    # 2. Entries.
    due = due_entries(now, state, days)
    if not due:
        return state
    end = dt.datetime.combine(today, _hhmm(config.SPREAD_ENTRY_END), tzinfo=now.tzinfo)
    last_chance = now + RETRY >= end
    plans = {n: (plan_comb if n == "comb" else plan_nectar)(stock, opt, trading, today) for n in due}
    missing = [n for n in due if plans[n] is None]
    if missing and not last_chance:
        # Both wait, so that a strategy with bad quotes right now does not lose its share.
        for n in due:
            _postpone(state, n, now, SKIP_REASON.get(n, "keine Kurse") if n in missing
                      else f"wartet auf {', '.join(missing)}")
        return state
    for n in missing:
        _give_up(state, n, today, SKIP_REASON.get(n, "keine brauchbaren Kurse"))
    ready = [n for n in due if plans[n] is not None]
    if not ready:
        return state

    capital, free = capital_and_free(trading, state)
    shares = config.SPREAD_SHARES
    if any(state[n] is not None for n in STRATEGIES):       # the other strategy is open
        qty = {n: rules.qty_alongside(capital, free, shares[n], plans[n]["loss"]) for n in ready}
    else:
        qty = rules.allocate(capital, {n: plans[n]["loss"] for n in ready}, shares)
    logger.info("capital %.2f, free cash %.2f -> contracts %s", capital, free, qty)

    for name in ready:
        plan = plans[name]
        _, free = capital_and_free(trading, state)           # cash may have moved with the last fill
        q = min(qty.get(name, 0), int(max(free, 0.0) // plan["loss"]))
        if q <= 0:
            _give_up(state, name, today, f"Budget reicht nicht für einen Kontrakt (Maximalverlust "
                                         f"{plan['loss']:.0f} $ pro Kontrakt, frei {free:.0f} $).")
            continue
        pos = enter(trading, plan, q, submit)
        if pos is None:
            if not submit:
                state["skipped"][name] = today.isoformat()   # dry-run: plan once, not every 30 s
            elif last_chance:
                _give_up(state, name, today, SKIP_REASON.get(name, "kein Fill"))
            else:
                _postpone(state, name, now, SKIP_REASON.get(name, "kein Fill"))
            continue
        state[name] = pos
        state["entered"][name] = today.isoformat()
        save_state(state)
        notify(f"📗 **{name.upper()}** eröffnet: {q}x, Verfall {pos['expiry']}, Credit {pos['credit']:.2f} "
               f"(Mid {pos['mid_credit']:.2f}), Rückkauf bei {pos['buyback']:.2f}, "
               f"Maximalverlust {(pos['width'] - pos['credit']) * 100 * q:.0f} $")
        ensure_buyback(trading, pos, submit)
    return state


def preview(stock, opt, trading, state: dict) -> None:
    """Dry-run for --now: what the bot would trade at the next entry day, with
    today's quotes and cash. Sends nothing and changes no state."""
    today = dt.datetime.now(ET).date()
    days = trading_days(trading, today)
    day = today
    while day not in days or day.weekday() > 1:
        day += dt.timedelta(days=1)
    capital, free = capital_and_free(trading, state)
    print(f"Entry day {day} ({day:%A}) | capital {capital:.2f} $ | free cash {free:.2f} $")
    plans = {}
    for name in (("comb", "nectar") if day.weekday() == 0 else ("nectar",)):
        if state[name] is not None:
            print(f"  {name}: position open ({state[name]['qty']}x, exp {state[name]['expiry']}) — no entry")
            continue
        plan = (plan_comb if name == "comb" else plan_nectar)(stock, opt, trading, day)
        if plan is None:
            print(f"  {name}: no entry — {SKIP_REASON.get(name)}")
            continue
        plans[name] = plan
    if any(state[n] is not None for n in STRATEGIES):
        qty = {n: rules.qty_alongside(capital, free, config.SPREAD_SHARES[n], p["loss"]) for n, p in plans.items()}
    else:
        qty = rules.allocate(capital, {n: p["loss"] for n, p in plans.items()}, config.SPREAD_SHARES)
    for name, p in plans.items():
        q = min(qty.get(name, 0), int(max(free, 0.0) // p["loss"]))
        print(f"  {name}: SPY {p['spot']:.2f}, strikes {p['strikes']}, exp {p['expiry']}, "
              f"mid credit {p['mid_credit']:.2f} (natural {p['natural_credit']:.2f}), "
              f"max loss/contract {p['loss']:.0f} $ -> {q} contract(s)")
    print("Dry-run: no order sent.")


# ── Entry point ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--now", action="store_true", help="one pass right now, then exit (dry-run unless --submit)")
    ap.add_argument("--submit", action="store_true", help="with --now: send real PAPER orders")
    ap.add_argument("--status", action="store_true", help="print the state file and the account, then exit")
    ap.add_argument("--clear-halt", action="store_true", help="clear a halt (assignment / leg mismatch)")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    rc = guard()
    if rc is not None:
        return rc
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(os.path.join(config.LOG_DIR, "spread_bot.log"),
                                                      encoding="utf-8")])
    if args.clear_halt:
        st = load_state()
        logger.info("halt cleared (was: %s)", st["halted"])
        st["halted"] = None
        save_state(st)
        return 0
    stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    opt = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET, paper=True)
    if args.status:
        acct = trading.get_account()
        print(json.dumps(load_state(), indent=1))
        print(f"cash {float(acct.cash):.2f} | equity {float(acct.equity):.2f} | "
              f"options buying power {float(acct.options_buying_power):.2f}")
        for p in trading.get_all_positions():
            print(f"  {p.symbol}: {p.qty}")
        return 0
    if args.now:
        if not args.submit:
            preview(stock, opt, trading, load_state())
            return 0
        save_state(tick(dt.datetime.now(ET), stock, opt, trading, load_state(), True))
        return 0

    if not take_lock():
        print(f"Another bot is already running (port {LOCK_PORT} taken) — not starting a second one.",
              file=sys.stderr)
        return 2
    submit = bool(config.SPREAD_SUBMIT)
    logger.info("Spread bot loop: Nectar + Comb on %s, entry %s-%s ET%s.", config.UNDERLYING,
                config.SPREAD_ENTRY_TIME, config.SPREAD_ENTRY_END, "" if submit else " (DRY)")
    last_alarm = None
    while True:
        try:
            save_state(tick(dt.datetime.now(ET), stock, opt, trading, load_state(), submit))
        except Exception as exc:
            logger.exception("tick failed")
            if last_alarm is None or time.time() - last_alarm > 1800:     # at most one alarm per 30 min
                notify(f"🚨 **SPREAD-Bot Fehler:** `{type(exc).__name__}: {str(exc)[:300]}`")
                last_alarm = time.time()
            time.sleep(120)
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
