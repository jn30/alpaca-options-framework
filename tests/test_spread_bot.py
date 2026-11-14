import datetime as dt

import spread_bot as b

D = dt.date
WEEK = {D(2026, 11, 16), D(2026, 11, 17), D(2026, 11, 18), D(2026, 11, 19), D(2026, 11, 20),
        D(2026, 11, 23), D(2026, 11, 24), D(2026, 11, 25), D(2026, 11, 27)}


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "STATE_PATH", str(tmp_path / "s.json"))
    st = b.load_state()
    assert st["nectar"] is None and st["comb"] is None and st["skipped"] == {} and st["halted"] is None
    st["halted"] = "assignment"
    b.save_state(st)
    assert b.load_state()["halted"] == "assignment"


def test_guard_refuses_wrong_strategy(monkeypatch):
    monkeypatch.setattr(b.config, "STRATEGY", "trend")
    assert b.guard() == 2


def test_guard_refuses_live(monkeypatch):
    monkeypatch.setattr(b.config, "STRATEGY", "spreads")
    monkeypatch.setattr(b.config, "PAPER", False)
    assert b.guard() == 2


def test_guard_passes_on_paper_spreads(monkeypatch):
    monkeypatch.setattr(b.config, "STRATEGY", "spreads")
    monkeypatch.setattr(b.config, "PAPER", True)
    assert b.guard() is None


LEGS = [{"symbol": "SPY261127P00650000", "short": True},
        {"symbol": "SPY261127P00640000", "short": False}]


def test_credit_of_put_spread():
    q = {"SPY261127P00650000": (0.90, 1.00), "SPY261127P00640000": (0.40, 0.50)}
    assert b.credit_of(LEGS, q) == (0.50, 0.40)


def test_build_order_signs_and_intents():
    o = b.build_order(LEGS, 3, 0.50, opening=True)
    assert o.limit_price == -0.50 and o.qty == 3 and len(o.legs) == 2
    assert o.legs[0].side.value == "sell" and o.legs[1].side.value == "buy"
    c = b.build_order(LEGS, 3, 0.25, opening=False)
    assert c.limit_price == 0.25 and c.legs[0].side.value == "buy"


def _patch_market(monkeypatch, quotes):
    monkeypatch.setattr(b, "notify", lambda *_: None)
    monkeypatch.setattr(b, "spy_last", lambda s: 672.40)
    monkeypatch.setattr(b, "trading_days", lambda t, d: WEEK)
    monkeypatch.setattr(b, "leg_quotes", quotes)


def test_plan_nectar_builds_condor_from_spot(monkeypatch):
    _patch_market(monkeypatch, lambda opt, syms, **k: {
        "short_put": (1.50, 1.60), "long_put": (0.30, 0.40),
        "short_call": (0.05, 0.07), "long_call": (0.01, 0.03)})
    p = b.plan_nectar(None, None, None, D(2026, 11, 16))
    assert p["expiry"] == "2026-11-19" and p["width"] == 5.0
    assert p["mid_credit"] == 1.24
    assert [(l["symbol"], l["short"]) for l in p["legs"]] == [
        ("SPY261119P00670000", True), ("SPY261119P00665000", False),
        ("SPY261119C00693000", True), ("SPY261119C00696000", False)]
    # worst accepted credit = mid - 3 cents -> loss per contract (5 - 1.21) * 100
    assert round(p["loss"], 2) == 379.0


def test_plan_nectar_falls_back_to_friday_when_thursday_is_not_quotable(monkeypatch):
    def quotes(opt, syms, **k):
        if "261119" in syms["short_put"]:
            raise RuntimeError("no usable quote")
        return {"short_put": (1.50, 1.60), "long_put": (0.30, 0.40),
                "short_call": (0.05, 0.07), "long_call": (0.01, 0.03)}
    _patch_market(monkeypatch, quotes)
    assert b.plan_nectar(None, None, None, D(2026, 11, 16))["expiry"] == "2026-11-20"


def test_plan_skips_on_unusable_quote(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no usable quote")
    _patch_market(monkeypatch, boom)
    assert b.plan_nectar(None, None, None, D(2026, 11, 16)) is None


def test_plan_skips_when_credit_too_rich(monkeypatch):
    # mid credit 3.24 on a 5$ wing = 65% > 60% -> bad quotes
    _patch_market(monkeypatch, lambda opt, syms, **k: {
        "short_put": (3.40, 3.50), "long_put": (0.20, 0.30),
        "short_call": (0.05, 0.07), "long_call": (0.01, 0.03)})
    assert b.plan_nectar(None, None, None, D(2026, 11, 16)) is None


def test_plan_comb_picks_15_delta_put_and_10_wide_wing(monkeypatch):
    _patch_market(monkeypatch, lambda opt, syms, **k: {
        "short_put": (0.90, 1.00), "long_put": (0.40, 0.50)})
    monkeypatch.setattr(b, "put_deltas", lambda opt, expiry, spot: {648.0: -0.13, 650.0: -0.16, 655.0: -0.22})
    p = b.plan_comb(None, None, None, D(2026, 11, 16))
    assert p["expiry"] == "2026-11-27" and p["width"] == 10.0 and p["mid_credit"] == 0.50
    assert [(l["symbol"], l["short"]) for l in p["legs"]] == [
        ("SPY261127P00650000", True), ("SPY261127P00640000", False)]


def test_plan_comb_skips_without_deltas(monkeypatch):
    _patch_market(monkeypatch, lambda *a, **k: {})
    monkeypatch.setattr(b, "put_deltas", lambda opt, expiry, spot: {})
    assert b.plan_comb(None, None, None, D(2026, 11, 16)) is None


class FakeTrading:
    def __init__(self, positions=(), orders=None):
        self._p, self._o = list(positions), dict(orders or {})
        self.submitted = []

    def get_all_positions(self):
        return self._p

    def get_order_by_id(self, oid):
        return self._o[oid]

    def submit_order(self, req):
        self.submitted.append(req)
        return O("new-1", b.OrderStatus.NEW)


class P:  # position stub
    def __init__(self, symbol, qty):
        self.symbol, self.qty = symbol, str(qty)


class O:  # order stub
    def __init__(self, oid, status, price=None):
        self.id, self.status, self.filled_avg_price = oid, status, price


POS = {"strategy": "comb", "entry_date": "2026-11-16", "expiry": "2026-11-27", "legs": LEGS,
       "qty": 2, "width": 10.0, "mid_credit": 0.52, "credit": 0.50, "buyback": 0.25,
       "tp_order_id": None, "equity_before": 5000.0}
TODAY = D(2026, 11, 18)


def _state(**pos):
    return {**b.empty_state(), "comb": {**POS, **pos}}


def _exits(monkeypatch):
    seen = []
    monkeypatch.setattr(b, "notify", lambda *_: None)
    monkeypatch.setattr(b, "record_exit",
                        lambda pos, reason, debit, trading: seen.append((reason, debit)))
    return seen


def test_reconcile_keeps_position_that_is_still_there(monkeypatch):
    seen = _exits(monkeypatch)
    t = FakeTrading([P(LEGS[0]["symbol"], -2), P(LEGS[1]["symbol"], 2)])
    out = b.reconcile(t, _state(), TODAY)
    assert out["comb"] is not None and out["halted"] is None and seen == []


def test_reconcile_marks_vanished_position_as_closed(monkeypatch):
    seen = _exits(monkeypatch)
    out = b.reconcile(FakeTrading([]), _state(), TODAY)
    assert out["comb"] is None and seen == [("buyback", None)]


def test_reconcile_takes_debit_from_filled_buyback_order(monkeypatch):
    seen = _exits(monkeypatch)
    t = FakeTrading([], {"tp-1": O("tp-1", b.OrderStatus.FILLED, "0.24")})
    out = b.reconcile(t, _state(tp_order_id="tp-1"), TODAY)
    assert out["comb"] is None and seen == [("buyback", 0.24)]


def test_reconcile_vanished_after_expiry_is_expired(monkeypatch):
    seen = _exits(monkeypatch)
    out = b.reconcile(FakeTrading([]), _state(), D(2026, 11, 30))
    assert out["comb"] is None and seen == [("expired", None)]


def test_reconcile_halts_on_spy_shares(monkeypatch):
    seen = _exits(monkeypatch)
    out = b.reconcile(FakeTrading([P("SPY", 200)]), _state(), TODAY)
    assert out["halted"] == "assignment" and out["comb"] is not None and seen == []


def test_reconcile_halts_on_partial_legs(monkeypatch):
    seen = _exits(monkeypatch)
    out = b.reconcile(FakeTrading([P(LEGS[1]["symbol"], 2)]), _state(), TODAY)   # short leg missing
    assert out["halted"] == "legs_mismatch" and out["comb"] is not None and seen == []


def test_ensure_buyback_places_closing_limit_once():
    t = FakeTrading()
    pos = dict(POS)
    b.ensure_buyback(t, pos, submit=True)
    assert pos["tp_order_id"] == "new-1" and len(t.submitted) == 1
    assert t.submitted[0].limit_price == 0.25 and t.submitted[0].legs[0].side.value == "buy"
    t._o["new-1"] = O("new-1", b.OrderStatus.NEW)
    b.ensure_buyback(t, pos, submit=True)            # still working -> no second order
    assert len(t.submitted) == 1


def test_ensure_buyback_replaces_expired_day_order():
    t = FakeTrading([], {"old": O("old", b.OrderStatus.EXPIRED)})
    pos = {**POS, "tp_order_id": "old"}
    b.ensure_buyback(t, pos, submit=True)
    assert pos["tp_order_id"] == "new-1"


def test_ensure_buyback_does_not_resend_a_rejected_order_the_same_day(monkeypatch):
    monkeypatch.setattr(b, "notify", lambda *_: None)
    t = FakeTrading()
    pos = dict(POS)
    b.ensure_buyback(t, pos, submit=True, today=TODAY)
    t._o["new-1"] = O("new-1", b.OrderStatus.REJECTED)
    b.ensure_buyback(t, pos, submit=True, today=TODAY)
    b.ensure_buyback(t, pos, submit=True, today=TODAY)
    assert len(t.submitted) == 1
    b.ensure_buyback(t, pos, submit=True, today=TODAY + dt.timedelta(days=1))   # next day: fresh order
    assert len(t.submitted) == 2


def test_ensure_buyback_dry_run_sends_nothing():
    t = FakeTrading()
    b.ensure_buyback(t, dict(POS), submit=False)
    assert t.submitted == []


def test_record_exit_writes_csv_row_with_pnl(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "notify", lambda *_: None)
    monkeypatch.setattr(b, "CSV_PATH", str(tmp_path / "t.csv"))
    t = FakeTrading()
    t.get_account = lambda: type("A", (), {"equity": "5050"})()
    b.record_exit(dict(POS), "buyback", 0.25, t)
    import csv
    row = list(csv.DictReader(open(tmp_path / "t.csv", encoding="utf-8")))[0]
    assert row["strategy"] == "comb" and row["exit_reason"] == "buyback"
    assert float(row["pnl"]) == 50.0 and float(row["equity_after"]) == 5050.0


def test_close_debits_mid_and_natural():
    q = {"SPY261127P00650000": (0.90, 1.00), "SPY261127P00640000": (0.40, 0.50)}
    # mid: 0.95 - 0.45 ; natural: pay the short's ask 1.00, get the long's bid 0.40
    assert b.close_debits(LEGS, q) == (0.50, 0.60)
