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
