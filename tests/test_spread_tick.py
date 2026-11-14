"""Daily loop of the spread bot: which entries are due, sizing wiring, expiry exit."""
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


def test_capital_and_free_with_open_comb():
    t = _acct(FakeTrading(), 5100, 3100)
    # credits 0.50*100*2 = 100 -> K = 5000 ; reserved 10*100*2 = 2000 -> free = 3100
    assert b.capital_and_free(t, _state()) == (5000.0, 3100.0)


def test_capital_and_free_capped_by_options_buying_power():
    t = _acct(FakeTrading(), 5000, 1200)
    assert b.capital_and_free(t, b.empty_state()) == (5000.0, 1200.0)


def at(day, hh, mm):
    return dt.datetime(2026, 11, day, hh, mm, tzinfo=b.ET)


def test_due_entries_monday_in_window_both():
    assert b.due_entries(at(16, 10, 5), b.empty_state(), WEEK) == ["comb", "nectar"]


def test_due_entries_outside_window_or_halted():
    assert b.due_entries(at(16, 9, 45), b.empty_state(), WEEK) == []
    assert b.due_entries(at(16, 15, 0), b.empty_state(), WEEK) == []
    assert b.due_entries(at(16, 10, 5), {**b.empty_state(), "halted": "assignment"}, WEEK) == []


def test_due_entries_tuesday_only_nectar_and_only_without_entry_this_week():
    assert b.due_entries(at(17, 10, 5), b.empty_state(), WEEK) == ["nectar"]
    st = {**b.empty_state(), "entered": {"nectar": "2026-11-16"}}
    assert b.due_entries(at(17, 10, 5), st, WEEK) == []
    assert b.due_entries(at(18, 10, 5), b.empty_state(), WEEK) == []       # Wednesday


def test_due_entries_monday_holiday_skips_comb_for_the_week():
    days = WEEK - {D(2026, 11, 16)}
    assert b.due_entries(at(17, 10, 5), b.empty_state(), days) == ["nectar"]


def test_due_entries_respects_open_position_skip_and_retry_time():
    assert b.due_entries(at(23, 10, 5), _state(), WEEK) == ["nectar"]       # comb still open
    st = {**b.empty_state(), "skipped": {"comb": "2026-11-16"},
          "next_try": {"nectar": at(16, 10, 20).isoformat()}}
    assert b.due_entries(at(16, 10, 5), st, WEEK) == []
    assert b.due_entries(at(16, 10, 20), st, WEEK) == ["nectar"]


def test_due_entries_no_second_entry_after_same_week_buyback():
    st = {**b.empty_state(), "entered": {"comb": "2026-11-16", "nectar": "2026-11-16"}}
    assert b.due_entries(at(16, 11, 0), st, WEEK) == []
    assert b.due_entries(at(23, 10, 5), st, WEEK) == ["comb", "nectar"]     # next Monday


def _plan(name, loss, width, credit):
    return {"strategy": name, "expiry": "2026-11-27", "legs": LEGS, "width": width,
            "mid_credit": credit, "natural_credit": credit, "spot": 672.4, "strikes": {}, "loss": loss}


def _tick_env(monkeypatch, cash, plans):
    t = _acct(FakeTrading(), cash)
    entered = []
    monkeypatch.setattr(b, "notify", lambda *_: None)
    monkeypatch.setattr(b, "save_state", lambda st: None)
    monkeypatch.setattr(b.config, "SPREAD_PRIORITY", None, raising=False)   # 7/93 split unless a test sets it
    monkeypatch.setattr(b, "session", lambda trading, day: (dt.time(9, 30), dt.time(16, 0), WEEK))
    monkeypatch.setattr(b, "plan_nectar", lambda *a: plans.get("nectar"))
    monkeypatch.setattr(b, "plan_comb", lambda *a: plans.get("comb"))
    monkeypatch.setattr(b, "ensure_buyback", lambda *a, **k: None)

    def fake_enter(trading, plan, qty, submit):
        entered.append((plan["strategy"], qty))
        t.acct.cash = str(float(t.acct.cash) + plan["mid_credit"] * 100 * qty)
        return {**POS, "strategy": plan["strategy"], "qty": qty, "width": plan["width"],
                "credit": plan["mid_credit"], "legs": [{"symbol": plan["strategy"], "short": True}]}
    monkeypatch.setattr(b, "enter", fake_enter)
    return t, entered


BOTH = {"nectar": _plan("nectar", 340.0, 5.0, 1.60), "comb": _plan("comb", 950.0, 10.0, 0.50)}


def test_tick_monday_allocates_original_ratio_and_enters_comb_first(monkeypatch):
    t, entered = _tick_env(monkeypatch, 10500, BOTH)
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    assert entered == [("comb", 10), ("nectar", 2)]
    assert st["comb"]["qty"] == 10 and st["nectar"]["qty"] == 2
    assert st["entered"] == {"comb": "2026-11-16", "nectar": "2026-11-16"}


def test_tick_small_account_gives_everything_to_nectar_and_skips_comb(monkeypatch):
    t, entered = _tick_env(monkeypatch, 871, BOTH)
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    assert entered == [("nectar", 2)]
    assert st["comb"] is None and st["skipped"]["comb"] == "2026-11-16"


def test_tick_never_reserves_more_than_cash(monkeypatch):
    t, entered = _tick_env(monkeypatch, 5000, BOTH)
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    risk = sum(st[n]["qty"] * BOTH[n]["loss"] for n in ("nectar", "comb") if st[n])
    assert entered == [("comb", 4), ("nectar", 1)] and risk <= 5000


def test_tick_nectar_alongside_open_comb_uses_own_share_capped_by_free_cash(monkeypatch):
    t, entered = _tick_env(monkeypatch, 10100, {"nectar": BOTH["nectar"]})
    monkeypatch.setattr(b, "reconcile", lambda trading, state, today: state)
    st = {**b.empty_state(), "comb": {**POS, "qty": 10, "credit": 0.50}}    # reserves 10,000
    out = b.tick(at(23, 10, 5), None, None, t, st, True)
    # K = 10100 - 500 = 9600 -> share 672 -> 1 contract ; free = 100 -> 0 contracts
    assert entered == [] and out["skipped"]["nectar"] == "2026-11-23"


def test_tick_postpones_both_when_one_plan_is_missing(monkeypatch):
    t, entered = _tick_env(monkeypatch, 10500, {"nectar": BOTH["nectar"]})
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    assert entered == [] and set(st["next_try"]) == {"nectar", "comb"}


def test_tick_last_chance_enters_the_one_with_a_plan(monkeypatch):
    t, entered = _tick_env(monkeypatch, 10500, {"nectar": BOTH["nectar"]})
    b.tick(at(16, 14, 50), None, None, t, b.empty_state(), True)
    assert [e[0] for e in entered] == ["nectar"]


def test_tick_closes_on_expiry_day_after_close_time(monkeypatch):
    t, entered = _tick_env(monkeypatch, 5000, {})
    seen = []
    monkeypatch.setattr(b, "reconcile", lambda trading, state, today: state)
    monkeypatch.setattr(b, "close_position", lambda opt, trading, pos: O("c", b.OrderStatus.FILLED, "0.10"))
    monkeypatch.setattr(b, "record_exit", lambda pos, reason, debit, trading: seen.append((reason, debit)))
    st = b.tick(at(27, 15, 29), None, None, t, _state(), True)
    assert st["comb"] is not None and seen == []
    st = b.tick(at(27, 15, 30), None, None, t, _state(), True)
    assert st["comb"] is None and seen == [("expiry_close", 0.10)]


def test_tick_does_nothing_when_market_is_closed(monkeypatch):
    t, entered = _tick_env(monkeypatch, 10500, BOTH)
    monkeypatch.setattr(b, "session", lambda trading, day: None)
    assert b.tick(at(15, 10, 5), None, None, t, b.empty_state(), True) == b.empty_state()
    monkeypatch.setattr(b, "session", lambda trading, day: (dt.time(9, 30), dt.time(16, 0), WEEK))
    b.tick(at(16, 16, 5), None, None, t, b.empty_state(), True)
    assert entered == []


def test_tick_dry_run_enters_nothing_and_does_not_retry_all_day(monkeypatch):
    t, entered = _tick_env(monkeypatch, 10500, BOTH)
    monkeypatch.setattr(b, "enter", lambda trading, plan, qty, submit: None)
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), False)
    assert st["comb"] is None and st["skipped"] == {"comb": "2026-11-16", "nectar": "2026-11-16"}


def test_tick_with_priority_gives_comb_all_cash_and_nectar_the_rest(monkeypatch):
    t, entered = _tick_env(monkeypatch, 5500, BOTH)
    monkeypatch.setattr(b.config, "SPREAD_PRIORITY", ("comb", "nectar"))
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    # comb 5500 // 950 = 5 (4750 at risk) ; cash after the fill 5750, 5000 reserved -> 750 free -> nectar 2
    assert entered == [("comb", 5), ("nectar", 2)]


def test_tick_with_priority_falls_back_to_nectar_when_comb_is_unaffordable(monkeypatch):
    t, entered = _tick_env(monkeypatch, 871, BOTH)
    monkeypatch.setattr(b.config, "SPREAD_PRIORITY", ("comb", "nectar"))
    st = b.tick(at(16, 10, 5), None, None, t, b.empty_state(), True)
    assert entered == [("nectar", 2)] and st["skipped"]["comb"] == "2026-11-16"


def test_tick_with_priority_nectar_alongside_open_comb_may_use_all_free_cash(monkeypatch):
    t, entered = _tick_env(monkeypatch, 11500, {"nectar": BOTH["nectar"]})
    monkeypatch.setattr(b.config, "SPREAD_PRIORITY", ("comb", "nectar"))
    monkeypatch.setattr(b, "reconcile", lambda trading, state, today: state)
    st = {**b.empty_state(), "comb": {**POS, "qty": 10, "credit": 0.50}}    # reserves 10,000
    b.tick(at(23, 10, 5), None, None, t, st, True)
    assert entered == [("nectar", 4)]                                        # 1500 free // 340


def test_due_entries_skips_a_disabled_strategy(monkeypatch):
    monkeypatch.setattr(b.config, "SPREAD_ENABLED", ("comb",))
    assert b.due_entries(at(16, 10, 5), b.empty_state(), WEEK) == ["comb"]
    assert b.due_entries(at(17, 10, 5), b.empty_state(), WEEK) == []        # Tuesday is Nectar-only
