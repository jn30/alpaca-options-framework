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


def allocate_priority(capital: float, loss: dict[str, float], order: tuple[str, ...]) -> dict[str, int]:
    """Contracts per strategy when they are served in `order`: the first takes
    as many contracts as the whole capital allows, the next sizes from what is
    left. A strategy missing from `loss` cannot enter today and gets 0."""
    qty, left = {}, capital
    for k in order:
        qty[k] = _fit(left, loss[k]) if k in loss else 0
        left -= qty[k] * loss.get(k, 0.0)
    return qty


def qty_alongside(capital: float, free_cash: float, share: float, loss_per_contract: float) -> int:
    """Contracts for a strategy entering while the other one is open: its own
    share of the capital, but never more than the cash still free."""
    return _fit(min(capital * share, free_cash), loss_per_contract)


# ── Rules of 2026-11-16 (user spec "Step 5"; the functions above stay for the backtest) ──

def nectar_expiries(entry: dt.date, trading_days: set[dt.date]) -> list[dt.date]:
    """Expiries to try for Nectar, in order: 3 calendar days out (Thursday from
    Monday, Friday from Tuesday), then each later session through Friday of
    the same week. Never past that Friday."""
    friday = entry + dt.timedelta(days=4 - entry.weekday())
    out, d = [], entry + dt.timedelta(days=3)
    while d <= friday:
        if d in trading_days:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def comb_expiry_nearest(entry: dt.date, trading_days: set[dt.date]) -> Optional[dt.date]:
    """The Friday nearest to 7 days after `entry`, 3 to 11 days out. A holiday
    Friday becomes that week's last session if it is still 3+ days out."""
    fridays = [entry + dt.timedelta(days=n) for n in range(3, 12)
               if (entry + dt.timedelta(days=n)).weekday() == 4]
    for f in sorted(fridays, key=lambda f: (abs((f - entry).days - 7), f)):
        try:
            d = _trading_day(f, trading_days)
        except ValueError:
            continue
        if 3 <= (d - entry).days <= 11:
            return d
    return None


def bs_put_delta(spot: float, strike: float, t: float, vol: float,
                 rate: float = 0.04, div: float = 0.013) -> float:
    """Black-Scholes delta of a European put (negative)."""
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return math.exp(-div * t) * (0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0))) - 1.0)


def comb_short_strike(strikes: list[float], spot: float, t: float,
                      delta: float = 0.15, vol: float = 0.18) -> Optional[float]:
    """Listed OTM put strike whose delta, at a flat `vol`, is nearest -`delta`."""
    otm = [k for k in strikes if k < spot]
    if not otm or t <= 0:
        return None
    return min(otm, key=lambda k: (abs(abs(bs_put_delta(spot, k, t, vol)) - delta), -k))


def open_limit(mid_credit: float, frac: float = 0.90) -> float:
    """Opening limit: `frac` of the mid credit, to the cent."""
    return round(mid_credit * frac + 1e-9, 2)


def close_limit(mid_debit: float, width: float, over: float = 0.15) -> float:
    """Closing limit: at most `over` above the mid debit (rounded down to the
    cent), at least 0.01, never above the width."""
    px = math.floor(round(max(mid_debit, 0.0) * (1.0 + over) * 100.0, 6)) / 100.0
    return round(min(max(px, 0.01), width), 2)


def take_reached(mid_debit: float, credit: float, frac: float) -> bool:
    """Take-profit: the mid cost to close is `frac` of the credit or less."""
    return mid_debit <= credit * frac + 1e-9


def nectar_qty(credit: float, free_cash: float, width: float = NECTAR_PUT_WIDTH,
               max_loss: float = 1000.0, max_qty: int = 10) -> int:
    """(width - credit) * 100 * qty stays inside `max_loss` and inside the cash; cap `max_qty`."""
    if credit <= 0 or credit >= width:
        return 0
    loss = (width - credit) * 100.0
    return max(0, min(max_qty, _fit(max_loss, loss), _fit(free_cash, loss)))


def comb_qty(credit: float, free_cash: float, book: float, width: float = 10.0,
             qty: int = 10, book_frac: float = 0.40) -> int:
    """`qty` contracts when (width - credit) * 100 * qty fits the cash and is at
    most `book_frac` of the book, else 0."""
    if credit <= 0 or credit >= width:
        return 0
    risk = (width - credit) * 100.0 * qty
    return qty if risk <= free_cash and risk <= book * book_frac else 0
