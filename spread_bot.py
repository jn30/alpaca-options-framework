#!/usr/bin/env python3
"""
Spread bot: trades two SPY books on the Alpaca PAPER account.

    Nectar  (~5,000 $ book) one iron condor. Monday or Tuesday. Short put
            round(SPY - 2.50) with a $5 wing, short call round(SPY x 1.03) with a
            $3 wing, expiry 3 calendar days out (next listed session through
            Friday, never over the weekend). Size: (5 - credit) x 100 x qty
            inside 1,000 $ and inside the cash, max 10. Take at 75% of the credit.
    Comb    (~25,000 $ book) one bull put spread. Monday 10:00-15:00 ET. Short
            put = listed strike nearest 15 delta at 18% implied vol, long put
            $10 below, expiry the Friday nearest 7 days (3-11 days). 10
            contracts when (10 - credit) x 100 x 10 fits the cash and is at most
            40% of the book. Take at 50% of the credit.

Both: checked once a minute while the US cash session is open. One position
per book. Every open and close is one multi-leg limit order: open at 90% of
the mid credit, close paying at most 15% over the mid debit; no fill -> cancel
and try again next minute. No roll, no stop-loss; held to expiry day if the
take never comes, then closed 30 minutes before the bell. A book that is down
100 $ or more on the day opens nothing new until the next session; its closes
still go out. Every order is refused unless the client points at PAPER_HOST.

The rules are NOT backtested in this form (backtest/spread_strategies.py tested
the 2026-11-14 version: both NO-GO).

    python spread_bot.py                 # loop forever (orders if SPREAD_SUBMIT)
    python spread_bot.py --now           # dry-run: what it would open at the next entry day
    python spread_bot.py --now --submit  # one pass right now, real paper orders
    python spread_bot.py --status        # state, marks and $ left to the take, account
    python spread_bot.py --clear-halt    # resume entries after a manual check
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
PAPER_HOST = "https://paper-api.alpaca.markets"     # hard-coded; tests/test_spread_bot.py pins it
ORDER_WAIT = 45                                       # seconds an order may work before it is cancelled


# ── State ─────────────────────────────────────────────────────────────────────

def empty_state() -> dict:
    """nectar/comb: the open position or None. skipped: {strategy: date it gave
    up for the day}. entered: {strategy: date of its last entry}. next_try:
    {strategy: time before which it does not retry}. halted: reason or None."""
    return {"nectar": None, "comb": None, "skipped": {}, "halted": None,
            "entered": {}, "next_try": {}, "warned": {},
            "day": {"date": None, "realized": {}, "base": {}, "blocked": []}}


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


# ── Paper host ────────────────────────────────────────────────────────────────

class LiveHostRefused(RuntimeError):
    pass


def host_of(trading) -> str:
    url = getattr(trading, "_base_url", "")
    return str(getattr(url, "value", url)).rstrip("/")


def submit(trading, req):
    """The only way an order leaves this bot: refused unless the client points
    at the paper host."""
    host = host_of(trading)
    if host != PAPER_HOST:
        raise LiveHostRefused(f"refusing to send an order to {host!r}: only {PAPER_HOST} is allowed")
    return trading.submit_order(req)


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
        order = submit(trading, build_order(legs, qty, price, opening))
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


def put_strikes(opt, expiry: dt.date, spot: float) -> list[float]:
    """Listed SPY put strikes of `expiry` between 85% of spot and spot."""
    chain = opt.get_option_chain(OptionChainRequest(
        underlying_symbol=config.UNDERLYING, expiration_date=expiry, type=ContractType.PUT,
        strike_price_gte=round(spot * 0.85), strike_price_lte=round(spot)))
    return sorted(int(sym[-8:]) / 1000.0 for sym in chain)


def years_to(expiry: dt.date, now: dt.datetime) -> float:
    """Calendar time from `now` to 16:00 ET on `expiry`, in years."""
    end = dt.datetime.combine(expiry, dt.time(16, 0), tzinfo=ET)
    return max((end - now).total_seconds(), 0.0) / (365.0 * 86400.0)


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
    limit = rules.open_limit(mid, config.SPREAD_OPEN_FRAC)          # the credit the order asks for
    if limit <= 0:
        return _no_plan(strategy, f"mid credit {mid:.2f} too small to work an order")
    return {"strategy": strategy, "expiry": expiry.isoformat(), "legs": legs, "width": width,
            "mid_credit": round(mid, 2), "limit": limit, "natural_credit": round(nat, 2),
            "spot": round(spot, 2), "strikes": {name: strike for name, (_, strike, _) in named.items()},
            "loss": (width - limit) * 100.0}


def plan_nectar(stock, opt, trading, day: dt.date) -> Optional[dict]:
    try:
        spot = spy_last(stock)
        days = trading_days(trading, day)
    except Exception as exc:
        return _no_plan("nectar", f"market data failed: {exc}")
    k = rules.nectar_strikes(spot)
    named = {"short_put": ("PUT", k["short_put"], True), "long_put": ("PUT", k["long_put"], False),
             "short_call": ("CALL", k["short_call"], True), "long_call": ("CALL", k["long_call"], False)}
    candidates = rules.nectar_expiries(day, days)    # 3 days out, then later sessions through Friday
    if not candidates:
        return _no_plan("nectar", "no expiry left this week (never over the weekend)")
    err = None
    for exp in candidates:
        try:
            return _finish_plan("nectar", opt, exp, named, rules.NECTAR_PUT_WIDTH, spot)
        except Exception as exc:
            err = exc
            logger.warning("nectar: %s not quotable: %s", exp, exc)
    return _no_plan("nectar", f"no usable quotes: {err}")


def plan_comb(stock, opt, trading, day: dt.date, now: Optional[dt.datetime] = None) -> Optional[dict]:
    now = now or dt.datetime.now(ET)
    try:
        spot = spy_last(stock)
        expiry = rules.comb_expiry_nearest(day, trading_days(trading, day))
        if expiry is None:
            return _no_plan("comb", "no Friday expiry 3-11 days out")
        strikes = put_strikes(opt, expiry, spot)
    except Exception as exc:
        return _no_plan("comb", f"market data failed: {exc}")
    short = rules.comb_short_strike(strikes, spot, years_to(expiry, now),
                                    config.SPREAD_COMB_DELTA, config.SPREAD_COMB_IV)
    if short is None:
        return _no_plan("comb", f"no listed OTM put strikes for {expiry}")
    width = float(config.SPREAD_COMB_WIDTH)
    named = {"short_put": ("PUT", short, True), "long_put": ("PUT", short - width, False)}
    try:
        plan = _finish_plan("comb", opt, expiry, named, width, spot)
    except Exception as exc:
        return _no_plan("comb", f"no usable quotes: {exc}")
    if plan is not None:
        plan["delta"] = round(rules.bs_put_delta(spot, short, years_to(expiry, now), config.SPREAD_COMB_IV), 3)
    return plan


def enter(trading, plan: dict, qty: int, submit: bool) -> Optional[dict]:
    """Work the opening order. Returns the position record, or None (dry-run / no fill)."""
    name = plan["strategy"]
    logger.info("%s: %d x %s exp %s, mid credit %.2f, limit %.2f, worst-case loss/contract %.0f$",
                name, qty, plan["strikes"], plan["expiry"], plan["mid_credit"], plan["limit"], plan["loss"])
    if not submit:
        logger.info("%s: dry-run, no order sent", name)
        return None
    equity = float(trading.get_account().equity)
    order = work(trading, plan["legs"], qty, plan["limit"], True, 0, step_wait=ORDER_WAIT)
    if order is None:
        SKIP_REASON[name] = f"kein Fill zu {plan['limit']:.2f} (90 % von Mid {plan['mid_credit']:.2f})"
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


def legs_text(legs: list[dict], qty: int) -> str:
    """'-2× 742P / +2× 737P / ... (Verfall 19.11.)' from OCC symbols (root + YYMMDD + C/P + strike*1000)."""
    parts = []
    for leg in legs:
        sym = leg["symbol"]
        strike = int(sym[-8:]) / 1000.0
        parts.append(f"{'-' if leg['short'] else '+'}{qty}× {strike:g}{sym[-9]}")
    exp = dt.datetime.strptime(legs[0]["symbol"][-15:-9], "%y%m%d").date()
    return " / ".join(parts) + f" (Verfall {exp:%d.%m.})"


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
           f"Credit {pos['credit']:.2f}, Rückkauf {row['exit_debit'] or '?'}, {result}\n"
           f"Legs: {legs_text(pos['legs'], pos['qty'])}")


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


def drop_resting_order(trading, pos: dict):
    """Positions opened by the 2026-11-14 version carry a resting buy-back
    order. Cancel it (the take is now checked on the mark every minute).
    Returns the Order if it had filled, else None."""
    oid = pos.get("tp_order_id")
    if not oid:
        return None
    try:
        trading.cancel_order_by_id(oid)
    except Exception as exc:
        logger.info("%s: resting order %s not cancelled (%s)", pos["strategy"], oid, exc)
    order = trading.get_order_by_id(oid)
    if order.status == OrderStatus.FILLED:
        return order
    if order.status not in TERMINAL:
        return None                     # cancel still pending: look again next minute
    pos["tp_order_id"] = None
    logger.info("%s: resting buy-back order %s cancelled", pos["strategy"], oid)
    return None


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


def mark_of(opt, pos: dict) -> float:
    """Mid debit to buy the position back now."""
    symbols = {leg["symbol"]: leg["symbol"] for leg in pos["legs"]}
    mid, _ = close_debits(pos["legs"], leg_quotes(opt, symbols, allow_zero_bid=True))
    return max(mid, 0.0)


def close_once(trading, pos: dict, limit: float):
    """One closing limit order at `limit`; cancelled if it has not filled
    after ORDER_WAIT seconds. Returns the filled Order or None."""
    return work(trading, pos["legs"], pos["qty"], limit, False, 0, step_wait=ORDER_WAIT, cap=pos["width"])


# ── Daily loop ────────────────────────────────────────────────────────────────

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


def roll_day(state: dict, today: dt.date) -> None:
    """Start a fresh day record (realized P&L, mark baselines, blocked books)."""
    if state["day"].get("date") != today.isoformat():
        state["day"] = {"date": today.isoformat(), "realized": {}, "base": {}, "blocked": []}


def day_pnl(state: dict, name: str) -> float:
    day = state["day"]
    pos = state[name]
    open_part = (pos.get("unreal", 0.0) - day["base"].get(name, 0.0)) if pos is not None else 0.0
    return round(day["realized"].get(name, 0.0) + open_part, 2)


def _check_day_stop(state: dict, name: str) -> None:
    pnl = day_pnl(state, name)
    if pnl <= -config.SPREAD_DAY_STOP and name not in state["day"]["blocked"]:
        state["day"]["blocked"].append(name)
        logger.warning("%s: day P&L %.2f <= -%.0f -> no new entries until the next session",
                       name, pnl, config.SPREAD_DAY_STOP)
        notify(f"🛑 **{name.upper()}** liegt heute {pnl:+.2f} $ im Minus — keine neuen Einstiege bis zur "
               f"nächsten Sitzung. Schließungen laufen weiter.")


def note_mark(state: dict, name: str, pos: dict, mid_debit: float, today: dt.date) -> None:
    """Record the position's unrealized P&L; the first mark of the day is the
    baseline (0 for a position opened today, so entry costs count)."""
    pos["mark"] = round(mid_debit, 4)
    pos["unreal"] = round((pos["credit"] - mid_debit) * 100.0 * pos["qty"], 2)
    base = 0.0 if pos.get("entry_date") == today.isoformat() else pos["unreal"]
    state["day"]["base"].setdefault(name, base)
    _check_day_stop(state, name)


def book_close(state: dict, name: str, pnl: Optional[float]) -> None:
    """Move a closed position's result into today's realized P&L of its book."""
    pos = state[name]
    if pnl is None:
        pnl = (pos or {}).get("unreal", 0.0)
    base = state["day"]["base"].pop(name, 0.0)
    state["day"]["realized"][name] = round(state["day"]["realized"].get(name, 0.0) + pnl - base, 2)
    if pos is not None:
        pos["unreal"] = 0.0
    _check_day_stop(state, name)


def due_entries(now: dt.datetime, state: dict, days: set[dt.date]) -> list[str]:
    """Books that may try to open right now, Comb first. Comb: Monday. Nectar:
    Monday or Tuesday. Never while the book holds a position, never when the
    book hit the day stop, only inside SPREAD_ENTRY_TIME-SPREAD_ENTRY_END."""
    today = now.date()
    if state["halted"] or today not in days:
        return []
    if not (_hhmm(config.SPREAD_ENTRY_TIME) <= now.time() < _hhmm(config.SPREAD_ENTRY_END)):
        return []
    blocked = state["day"]["blocked"] if state["day"].get("date") == today.isoformat() else []
    out = []
    for name in ("comb", "nectar"):
        if name not in config.SPREAD_ENABLED or state[name] is not None or name in blocked:
            continue
        if today.weekday() > (0 if name == "comb" else 1):
            continue
        if state["skipped"].get(name) == today.isoformat():
            continue
        out.append(name)
    return out


def _once(state: dict, name: str, key: str, today: dt.date, text: str) -> None:
    """Log every time, but send a Discord message only once per day and reason."""
    logger.info("%s: %s", name, text)
    tag = f"{today.isoformat()}:{key}"
    if state["warned"].get(name) != tag:
        state["warned"][name] = tag
        notify(f"⏭️ **{name.upper()}**: {text}")


def qty_for(name: str, plan: dict, free: float) -> int:
    if name == "comb":
        return rules.comb_qty(plan["limit"], free, config.SPREAD_BOOKS["comb"], plan["width"],
                              config.SPREAD_COMB_QTY, config.SPREAD_COMB_BOOK_FRAC)
    return rules.nectar_qty(plan["limit"], free, plan["width"],
                            config.SPREAD_NECTAR_MAX_LOSS, config.SPREAD_NECTAR_MAX_QTY)


def _closed(state: dict, name: str, pos: dict, reason: str, debit: float, trading) -> None:
    record_exit(pos, reason, debit, trading)
    book_close(state, name, round((pos["credit"] - debit) * 100.0 * pos["qty"], 2))
    state[name] = None
    save_state(state)


def tick(now: dt.datetime, stock, opt, trading, state: dict, submit: bool) -> dict:
    """One minute: reconcile, manage open positions, then entries."""
    today = now.date()
    ses = session(trading, today)
    if ses is None:
        return state
    open_t, close_t, days = ses
    if not (open_t <= now.time() < close_t):
        return state
    roll_day(state, today)
    state = reconcile(trading, state, today)

    # 1. Open positions: take-profit on the mark, close on expiry day.
    for name in STRATEGIES:
        pos = state[name]
        if pos is None or pos.get("stuck"):
            continue
        if submit:
            filled = drop_resting_order(trading, pos)
            if filled is not None:
                _closed(state, name, pos, "buyback", abs(float(filled.filled_avg_price)), trading)
                continue
        try:
            mid = mark_of(opt, pos)
        except Exception as exc:
            logger.warning("%s: no mark this minute (%s)", name, exc)
            continue
        note_mark(state, name, pos, mid, today)
        expiring = (today >= dt.date.fromisoformat(pos["expiry"])
                    and now.time() >= rules.expiry_close_time(close_t))
        take = rules.take_reached(mid, pos["credit"], config.SPREAD_BUYBACK[name])
        if not (expiring or take):
            continue
        reason = "buyback" if take else "expiry_close"
        limit = rules.close_limit(mid, pos["width"], config.SPREAD_CLOSE_OVER)
        if not submit:
            logger.info("%s: dry-run, would close (%s) at %.2f (mid %.2f)", name, reason, limit, mid)
            continue
        order = close_once(trading, pos, limit)
        if order is None:
            if expiring:
                _once(state, name, "close", today, f"Schließen am Verfallstag zu {limit:.2f} nicht gefüllt "
                                                   f"(Mid {mid:.2f}) — neuer Versuch jede Minute.")
            continue
        _closed(state, name, pos, reason, abs(float(order.filled_avg_price)), trading)

    # 2. Entries.
    for name in due_entries(now, state, days):
        plan = (plan_comb if name == "comb" else plan_nectar)(stock, opt, trading, today)
        if plan is None:
            _once(state, name, "plan", today, f"kein Einstieg — {SKIP_REASON.get(name, 'keine Kurse')}")
            continue
        _, free = capital_and_free(trading, state)
        q = qty_for(name, plan, free)
        if q <= 0:
            _once(state, name, "budget", today,
                  f"Budget reicht nicht (Maximalverlust {plan['loss']:.0f} $ pro Kontrakt, frei {free:.0f} $).")
            continue
        pos = enter(trading, plan, q, submit)
        if pos is None:
            if not submit:
                state["skipped"][name] = today.isoformat()   # dry-run: plan once, not every minute
            else:
                _once(state, name, "fill", today,
                      SKIP_REASON.get(name, "kein Fill") + " — neuer Versuch jede Minute.")
            continue
        state[name] = pos
        state["entered"][name] = today.isoformat()
        state["day"]["base"][name] = 0.0
        save_state(state)
        notify(f"📗 **{name.upper()}** eröffnet: {q}x, Verfall {pos['expiry']}, Credit {pos['credit']:.2f} "
               f"(Mid {pos['mid_credit']:.2f}), Ziel {pos['buyback']:.2f}, "
               f"Maximalverlust {(pos['width'] - pos['credit']) * 100 * q:.0f} $\n"
               f"Legs: {legs_text(pos['legs'], q)}")
    return state


def preview(stock, opt, trading, state: dict) -> None:
    """Dry-run for --now: what the bot would trade at the next entry day, with
    today's quotes and cash. Sends nothing and changes no state."""
    today = dt.datetime.now(ET).date()
    days = trading_days(trading, today)
    day = today
    last_weekday = 1 if "nectar" in config.SPREAD_ENABLED else 0
    while day not in days or day.weekday() > last_weekday:
        day += dt.timedelta(days=1)
    capital, free = capital_and_free(trading, state)
    print(f"Entry day {day} ({day:%A}) | capital {capital:.2f} $ | free cash {free:.2f} $")
    for name in (("comb", "nectar") if day.weekday() == 0 else ("nectar",)):
        if name not in config.SPREAD_ENABLED:
            print(f"  {name}: switched off (config.SPREAD_ENABLED)")
            continue
        if state[name] is not None:
            print(f"  {name}: position open ({state[name]['qty']}x, exp {state[name]['expiry']}) — no entry")
            continue
        plan = (plan_comb if name == "comb" else plan_nectar)(stock, opt, trading, day)
        if plan is None:
            print(f"  {name}: no entry — {SKIP_REASON.get(name)}")
            continue
        q = qty_for(name, plan, free)
        print(f"  {name}: SPY {plan['spot']:.2f}, strikes {plan['strikes']}, exp {plan['expiry']}, "
              f"mid credit {plan['mid_credit']:.2f} -> limit {plan['limit']:.2f}, "
              f"max loss/contract {plan['loss']:.0f} $ -> {q} contract(s)")
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
        st = load_state()
        print(json.dumps(st, indent=1))
        for name in STRATEGIES:
            pos = st[name]
            if pos is None:
                continue
            try:
                mid = mark_of(opt, pos)
            except Exception as exc:
                print(f"{name}: no mark ({exc})")
                continue
            take = pos["credit"] * config.SPREAD_BUYBACK[name]
            print(f"{name}: {pos['qty']}x exp {pos['expiry']} | credit {pos['credit']:.2f} | mark {mid:.2f} | "
                  f"take at {take:.2f} -> {(mid - take) * 100 * pos['qty']:.0f} $ to go")
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
            time.sleep(60)
        time.sleep(60 - dt.datetime.now(ET).second)          # once a minute, on the minute


if __name__ == "__main__":
    sys.exit(main())
