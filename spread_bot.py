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
    return {"nectar": None, "comb": None, "skipped": {}, "halted": None}


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
    if args.status:
        print(json.dumps(load_state(), indent=1))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
