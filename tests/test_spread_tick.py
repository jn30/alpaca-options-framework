"""Minute loop of the spread bot (rules of 2026-11-16): entries, sizing, take-profit,
expiry close, the $100 day stop per book."""
import datetime as dt

import spread_bot as b
from test_spread_bot import LEGS, POS, WEEK, D, FakeTrading, O, _state


class Acct:
    def __init__(self, cash, obp):
        self.cash, self.options_buying_power, self.equity = str(cash), str(obp), str(cash)


def _acct(t, cash, obp=None):
    t.acct = Acct(cash, cash if obp is None else obp)
    t.get_account = lambda: t.acct
    return t


def at(day, hh, mm):
    return dt.datetime(2026, 11, day, hh, mm, tzinfo=b.ET)


# ── Cash ──────────────────────────────────────────────────────────────────────

def test_capital_and_free_with_open_comb():
    t = _acct(FakeTrading(), 5100, 3100)
    # credits 0.50*100*2 = 100 -> K = 5000 ; reserved 10*100*2 = 2000 -> free = 3100
    assert b.capital_and_free(t, _state()) == (5000.0, 3100.0)


def test_capital_and_free_capped_by_options_buying_power():
    t = _acct(FakeTrading(), 5000, 1200)
    assert b.capital_and_free(t, b.empty_state()) == (5000.0, 1200.0)


# ── Which book may enter ──────────────────────────────────────────────────────

def test_due_entries_monday_in_window_both():
    assert b.due_entries(at(16, 10, 5), b.empty_state(), WEEK) == ["comb", "nectar"]


def test_due_entries_outside_window_or_halted():
    assert b.due_entries(at(16, 9, 45), b.empty_state(), WEEK) == []
    assert b.due_entries(at(16, 15, 0), b.empty_state(), WEEK) == []
    assert b.due_entries(at(16, 10, 5), {**b.empty_state(), "halted": "assignment"}, WEEK) == []


def test_due_entries_comb_monday_only_nectar_monday_or_tuesday():
    assert b.due_entries(at(17, 10, 5), b.empty_state(), WEEK) == ["nectar"]
    assert b.due_entries(at(18, 10, 5), b.empty_state(), WEEK) == []


def test_due_entries_one_position_per_book_but_reentry_after_a_close():
    assert b.due_entries(at(16, 10, 5), _state(), WEEK) == ["nectar"]            # comb is open
    st = {**b.empty_state(), "entered": {"comb": "2026-11-16", "nectar": "2026-11-16"}}
    assert b.due_entries(at(16, 11, 0), st, WEEK) == ["comb", "nectar"]          # both flat again


def test_due_entries_skips_a_book_blocked_by_the_day_stop():
    st = b.empty_state()
    st["day"] = {"date": "2026-11-16", "realized": {}, "base": {}, "blocked": ["nectar"]}
    assert b.due_entries(at(16, 10, 5), st, WEEK) == ["comb"]


# ── Entries ───────────────────────────────────────────────────────────────────

def _plan(name, mid, width):
    limit = b.rules.open_limit(mid)
    return {"strategy": name, "expiry": "2026-11-20", "legs": LEGS, "width": width, "mid_credit": mid,
            "limit": limit, "natural_credit": mid, "spot": 672.4, "strikes": {},
            "loss": (width - limit) * 100.0}


PLANS = {"comb": _plan("comb", 0.80, 10.0), "nectar": _plan("nectar", 1.73, 5.0)}


def _env(monkeypatch, cash, plans=PLANS, marks=None, fill=None):
    """Fake market. marks: {book: mid debit}. fill: price a close fills at (None = no fill)."""
    t = _acct(FakeTrading(), cash)
    t.cancelled = []
    t.cancel_order_by_id = lambda oid: t.cancelled.append(oid)
    log = {"entered": [], "closed": [], "exits": []}
    monkeypatch.setattr(b, "notify", lambda *_: None)
    monkeypatch.setattr(b, "save_state", lambda st: None)
    monkeypatch.setattr(b, "session", lambda trading, day: (dt.time(9, 30), dt.time(16, 0), WEEK))
    monkeypatch.setattr(b, "reconcile", lambda trading, state, today: state)
    monkeypatch.setattr(b, "plan_nectar", lambda *a, **k: plans.get("nectar"))
    monkeypatch.setattr(b, "plan_comb", lambda *a, **k: plans.get("comb"))
    monkeypatch.setattr(b, "mark_of", lambda opt, pos: (marks or {})[pos["strategy"]])

    def fake_close(trading, pos, limit):
        log["closed"].append((pos["strategy"], limit))
        return None if fill is None else O("c", b.OrderStatus.FILLED, str(fill))
    monkeypatch.setattr(b, "close_once", fake_close)
    monkeypatch.setattr(b, "record_exit",
                        lambda pos, reason, debit, trading: log["exits"].append((pos["strategy"], reason, debit)))

    def fake_enter(trading, plan, qty, submit):
        log["entered"].append((plan["strategy"], qty))
        if not submit:
            return None
        t.acct.cash = str(float(t.acct.cash) + plan["limit"] * 100 * qty)
        return {**POS, "strategy": plan["strategy"], "qty": qty, "width": plan["width"],
                "credit": plan["limit"], "entry_date": "2026-11-16", "legs": LEGS}
    monkeypatch.setattr(b, "enter", fake_enter)
    return t, log


def test_tick_monday_opens_comb_10_then_nectar_inside_1000(monkeypatch):
    t, log = _env(monkeypatch, 30000)
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    # comb: (10 - 0.72) * 1000 = 9,280 fits cash and 40% of 25,000 -> 10
    # nectar: (5 - 1.56) * 100 = 344 per contract, 1,000 allows 2
    assert log["entered"] == [("comb", 10), ("nectar", 2)]
    assert st["comb"]["qty"] == 10 and st["nectar"]["qty"] == 2


def test_tick_small_cash_skips_comb_and_sizes_nectar_from_cash(monkeypatch):
    t, log = _env(monkeypatch, 500)
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    assert log["entered"] == [("nectar", 1)] and st["comb"] is None


def test_tick_comb_needs_room_in_its_book(monkeypatch):
    monkeypatch.setattr(b.config, "SPREAD_BOOKS", {"nectar": 5000.0, "comb": 20000.0})
    t, log = _env(monkeypatch, 30000)
    b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    assert log["entered"] == [("nectar", 2)]                   # 9,280 > 40% of 20,000


def test_tick_nectar_alongside_open_comb_uses_only_free_cash(monkeypatch):
    t, log = _env(monkeypatch, 10400, marks={"comb": 0.60})
    st = {**b.empty_state(), "comb": {**POS, "qty": 10, "credit": 0.50}}    # reserves 10,000
    b.tick(at(16, 10, 5), None, None, t, st, True)
    assert log["entered"] == [("nectar", 1)]                   # 400 free // 344


def test_tick_dry_run_sends_nothing_and_does_not_retry_all_day(monkeypatch):
    t, log = _env(monkeypatch, 30000, marks={"comb": 0.10})
    st = b.tick(at(16, 10, 5), None, None, t, _state(), False)
    assert log["closed"] == [] and st["skipped"] == {"nectar": "2026-11-16"}


def test_tick_does_nothing_when_market_is_closed(monkeypatch):
    t, log = _env(monkeypatch, 30000)
    monkeypatch.setattr(b, "session", lambda trading, day: None)
    assert b.tick(at(15, 10, 5), None, None, t, b.empty_state(), True) == b.empty_state()
    monkeypatch.setattr(b, "session", lambda trading, day: (dt.time(9, 30), dt.time(16, 0), WEEK))
    b.tick(at(16, 16, 5), None, None, t, b.empty_state(), True)
    assert log["entered"] == []


# ── Exits ─────────────────────────────────────────────────────────────────────

NECTAR = {**POS, "strategy": "nectar", "expiry": "2026-11-19", "qty": 2, "width": 5.0,
          "credit": 1.71, "entry_date": "2026-11-16"}


def _with(**books):
    return {**b.empty_state(), **books}


def test_nectar_take_at_75_percent_pays_up_to_15_over_mid(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 1.20}, fill=1.30)
    st = b.tick(at(17, 11, 0), None, None, t, _with(nectar=dict(NECTAR)), True)
    assert log["closed"] == [("nectar", 1.38)] and log["exits"] == [("nectar", "buyback", 1.30)]
    assert st["nectar"] is None


def test_nectar_holds_above_the_take(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 1.30})
    st = b.tick(at(17, 11, 0), None, None, t, _with(nectar=dict(NECTAR)), True)
    assert log["closed"] == [] and st["nectar"] is not None


def test_comb_take_at_half_the_credit(monkeypatch):
    t, log = _env(monkeypatch, 5000, plans={}, marks={"comb": 0.25}, fill=0.27)
    st = b.tick(at(17, 11, 0), None, None, t, _state(), True)
    assert log["closed"] == [("comb", 0.28)] and st["comb"] is None


def test_expiry_day_close_30_minutes_before_the_bell(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 1.50}, fill=1.60)
    st = b.tick(at(19, 15, 29), None, None, t, _with(nectar=dict(NECTAR)), True)
    assert log["closed"] == [] and st["nectar"] is not None
    st = b.tick(at(19, 15, 30), None, None, t, _with(nectar=dict(NECTAR)), True)
    assert log["closed"] == [("nectar", 1.72)] and log["exits"] == [("nectar", "expiry_close", 1.60)]


def test_unfilled_close_keeps_the_position_and_tries_again(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 1.20}, fill=None)
    st = b.tick(at(17, 11, 0), None, None, t, _with(nectar=dict(NECTAR)), True)
    assert log["closed"] == [("nectar", 1.38)] and log["exits"] == [] and st["nectar"] is not None


def test_old_resting_buyback_order_is_cancelled(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 1.40})
    t._o["tp-1"] = O("tp-1", b.OrderStatus.CANCELED)
    st = b.tick(at(17, 11, 0), None, None, t, _with(nectar={**NECTAR, "tp_order_id": "tp-1"}), True)
    assert t.cancelled == ["tp-1"] and st["nectar"]["tp_order_id"] is None


def test_old_resting_buyback_order_that_filled_is_booked(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 1.40})
    t._o["tp-1"] = O("tp-1", b.OrderStatus.FILLED, "1.28")
    st = b.tick(at(17, 11, 0), None, None, t, _with(nectar={**NECTAR, "tp_order_id": "tp-1"}), True)
    assert log["exits"] == [("nectar", "buyback", 1.28)] and st["nectar"] is None


# ── $100 day stop per book ────────────────────────────────────────────────────

def test_day_stop_from_the_intraday_mark_blocks_the_book(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 1.40})
    st = b.tick(at(17, 10, 0), None, None, t, _with(nectar=dict(NECTAR)), True)
    assert st["day"]["base"]["nectar"] == 62.0 and st["day"]["blocked"] == []   # (1.71-1.40)*200
    monkeypatch.setattr(b, "mark_of", lambda opt, pos: 2.05)                   # unreal -68 -> day -130
    st = b.tick(at(17, 10, 1), None, None, t, st, True)
    assert st["day"]["blocked"] == ["nectar"]


def test_a_position_opened_today_counts_from_zero(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 2.25})
    st = b.tick(at(16, 11, 0), None, None, t, _with(nectar=dict(NECTAR)), True)   # opened today
    assert st["day"]["base"]["nectar"] == 0.0 and st["day"]["blocked"] == ["nectar"]   # -108


def test_closes_still_go_out_when_the_book_is_blocked(monkeypatch):
    t, log = _env(monkeypatch, 1000, plans={}, marks={"nectar": 1.20}, fill=1.30)
    st = _with(nectar=dict(NECTAR))
    st["day"] = {"date": "2026-11-17", "realized": {}, "base": {}, "blocked": ["nectar"]}
    b.tick(at(17, 11, 0), None, None, t, st, True)
    assert log["exits"] == [("nectar", "buyback", 1.30)]


def test_a_losing_close_counts_against_the_day(monkeypatch):
    st = b.empty_state()
    st["day"] = {"date": "2026-11-17", "realized": {}, "base": {"nectar": 62.0}, "blocked": []}
    monkeypatch.setattr(b, "notify", lambda *_: None)
    b.book_close(st, "nectar", -40.0)                     # day: -40 - 62 = -102
    assert st["day"]["realized"]["nectar"] == -102.0 and st["day"]["blocked"] == ["nectar"]


def test_the_day_stop_resets_next_session(monkeypatch):
    t, log = _env(monkeypatch, 30000)
    st = b.empty_state()
    st["day"] = {"date": "2026-11-16", "realized": {"nectar": -150.0}, "base": {}, "blocked": ["nectar"]}
    st = b.tick(at(17, 10, 5), None, None, t, st, True)
    assert st["day"]["date"] == "2026-11-17" and log["entered"] == [("nectar", 2)]
