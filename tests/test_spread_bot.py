import datetime as dt

import spread_bot as b

D = dt.date
WEEK = {D(2026, 11, 16), D(2026, 11, 17), D(2026, 11, 18), D(2026, 11, 19), D(2026, 11, 20),
        D(2026, 11, 23), D(2026, 11, 24), D(2026, 11, 25), D(2026, 11, 27)}


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "STATE_PATH", str(tmp_path / "s.json"))
    assert b.load_state() == {"nectar": None, "comb": None, "skipped": {}, "halted": None}
    st = b.load_state()
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
