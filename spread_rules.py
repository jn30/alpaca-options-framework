"""
Rules of the two SPY spread strategies traded by spread_bot.py, as pure
functions (no network, no Alpaca imports) so they can be unit-tested.

Nectar: iron condor, entered Monday (or Tuesday), expiring Thursday (Friday).
Comb:   15-delta put credit spread, entered Monday, expiring the next Friday
        that is at least 7 days out.

Spec: docs/superpowers/specs/2026-11-14-spread-bot-design.md
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Optional

NECTAR_PUT_DISTANCE = 2.50    # short put this many $ below spot
NECTAR_PUT_WIDTH = 5.0
NECTAR_CALL_PCT = 0.03        # short call this far above spot
NECTAR_CALL_WIDTH = 3.0
COMB_MIN_DTE = 7


# ── Strikes ───────────────────────────────────────────────────────────────────

def nectar_strikes(spot: float) -> dict[str, float]:
    sp = float(round(spot - NECTAR_PUT_DISTANCE))
    if sp >= spot:
        sp -= 1.0
    sc = float(round(spot * (1.0 + NECTAR_CALL_PCT)))
    if sc <= spot:
        sc += 1.0
    return {"short_put": sp, "long_put": sp - NECTAR_PUT_WIDTH,
            "short_call": sc, "long_call": sc + NECTAR_CALL_WIDTH}


def pick_delta_strike(deltas: dict[float, float], target: float = -0.15) -> Optional[float]:
    """Strike whose delta is closest to `target`; None if there are no deltas."""
    if not deltas:
        return None
    return min(deltas, key=lambda k: (abs(deltas[k] - target), k))


# ── Calendar ──────────────────────────────────────────────────────────────────

def _trading_day(day: dt.date, trading_days: set[dt.date]) -> dt.date:
    """`day` if the market is open; else the next trading day of the same
    Mon-Fri week; else the last trading day before it."""
    if day in trading_days:
        return day
    d = day
    while d.weekday() < 4:
        d += dt.timedelta(days=1)
        if d in trading_days:
            return d
    d = day
    for _ in range(10):
        d -= dt.timedelta(days=1)
        if d in trading_days:
            return d
    raise ValueError(f"no trading day near {day}")


def nectar_entry_ok(day: dt.date, trading_days: set[dt.date]) -> bool:
    """Monday, or Tuesday when that week's Monday is not a trading day."""
    if day not in trading_days:
        return False
    if day.weekday() == 0:
        return True
    return day.weekday() == 1 and (day - dt.timedelta(days=1)) not in trading_days


def nectar_expiry(entry: dt.date, trading_days: set[dt.date]) -> dt.date:
    """Thursday for a Monday entry, Friday for a Tuesday entry (always +3 days)."""
    return _trading_day(entry + dt.timedelta(days=3), trading_days)


def comb_expiry(entry: dt.date, trading_days: set[dt.date]) -> dt.date:
    """The next Friday at least COMB_MIN_DTE calendar days after `entry`."""
    d = entry + dt.timedelta(days=COMB_MIN_DTE)
    while d.weekday() != 4:
        d += dt.timedelta(days=1)
    return _trading_day(d, trading_days)


def expiry_close_time(session_close: dt.time) -> dt.time:
    """On expiry day the position is closed 30 minutes before the bell
    (15:30 ET normally, 12:30 on a half day) — ahead of Alpaca's own
    ~15:45 buy-back of expiring short legs."""
    t = dt.datetime.combine(dt.date(2000, 1, 3), session_close) - dt.timedelta(minutes=30)
    return t.time()


# ── Prices ────────────────────────────────────────────────────────────────────

def buyback_price(credit: float, frac: float) -> float:
    """Debit at which the position is bought back: `frac` of the credit,
    rounded down to the cent."""
    return math.floor(round(credit * frac * 100.0, 6)) / 100.0


# ── Sizing ────────────────────────────────────────────────────────────────────

def _fit(budget: float, loss_per_contract: float) -> int:
    if budget <= 0 or loss_per_contract <= 0:
        return 0
    return int(budget // loss_per_contract)


def contracts(budget: float, width: float, credit: float) -> int:
    """Contracts whose worst-case loss, (width - credit) * 100 each, fits the
    budget. A credit <= 0 or >= width is a bad quote: 0."""
    if credit <= 0 or credit >= width:
        return 0
    return _fit(budget, (width - credit) * 100.0)


def allocate(capital: float, loss: dict[str, float], shares: dict[str, float]) -> dict[str, int]:
    """Contracts per strategy when both are free to enter. `loss` is the
    worst-case loss per contract of each strategy that can enter today (a
    missing key = cannot enter). Each gets its share of the capital; a
    strategy that cannot afford one contract hands its share to the other.
    If neither can, Comb and then Nectar gets a try with everything."""
    qty = {k: _fit(capital * shares[k], loss[k]) if k in loss else 0 for k in shares}
    funded = [k for k in qty if qty[k] > 0]
    if len(funded) == len(qty):
        return qty
    if funded:
        qty[funded[0]] = _fit(capital, loss[funded[0]])
        return qty
    for k in ("comb", "nectar"):
        if k in loss and _fit(capital, loss[k]) > 0:
            qty[k] = _fit(capital, loss[k])
            break
    return qty


def qty_alongside(capital: float, free_cash: float, share: float, loss_per_contract: float) -> int:
    """Contracts for a strategy entering while the other one is open: its own
    share of the capital, but never more than the cash still free."""
    return _fit(min(capital * share, free_cash), loss_per_contract)
