"""Pure parts of backtest/spread_strategies.py on synthetic minute data (no network)."""
import datetime as dt

from backtest import spread_strategies as s

D = dt.date
MON, TUE, THU = D(2026, 3, 2), D(2026, 3, 3), D(2026, 3, 5)
PUT = s.Vertical(short_sym="SP", long_sym="LP", short_k=670.0, width=5.0, call=False)


def flat(price, start=570, end=960):
    return [(m, price) for m in range(start, end)]


def spots(days, price=672.0):
    return {d.isoformat(): {m: price for m in range(570, 960)} for d in days}


def test_mark_is_kept_inside_no_arbitrage_bounds():
    assert s.mark(PUT, {"SP": 1.50, "LP": 0.30}, 672.0) == 1.20
    assert s.mark(PUT, {"SP": 0.10, "LP": 0.30}, 672.0) == 0.0          # never negative
    assert s.mark(PUT, {"SP": 1.00, "LP": 0.30}, 668.0) == 2.0          # at least intrinsic
    assert s.mark(PUT, {"SP": 9.00, "LP": 0.30}, 660.0) == 5.0          # at most the width


def test_take_profit_fills_at_the_buyback_price_when_market_reaches_it():
    days = [MON, TUE, THU]
    minutes = {"SP": {MON.isoformat(): flat(1.50), TUE.isoformat(): flat(1.50, 570, 700) + flat(0.90, 700)},
               "LP": {MON.isoformat(): flat(0.30), TUE.isoformat(): flat(0.30)}}
    r = s.simulate_trade([PUT], set(), minutes, spots(days), days, MON, THU, tp_frac=0.75, slip=0.01)
    # credit 1.20 - 2 legs * 0.01 = 1.18 ; buy-back floor(1.18 * 0.75) = 0.88
    # Tuesday 11:40: mark 0.60 + 0.02 <= 0.88 -> filled at the limit
    assert r["credit"] == 1.18 and r["cost"] == 0.88
    assert (r["exit"], r["exit_min"], r["reason"]) == (TUE.isoformat(), 700, "tp")


def test_position_is_closed_30_minutes_before_the_bell_on_expiry_day():
    days = [MON, TUE, THU]
    px = {d.isoformat(): flat(1.50) for d in days}
    minutes = {"SP": px, "LP": {d.isoformat(): flat(0.30) for d in days}}
    r = s.simulate_trade([PUT], set(), minutes, spots(days), days, MON, THU, tp_frac=0.75, slip=0.01)
    assert (r["exit"], r["exit_min"], r["reason"]) == (THU.isoformat(), 930, "expiry_close")
    assert r["cost"] == 1.22                                             # mark 1.20 + slippage


def test_expiry_close_never_costs_less_than_intrinsic():
    days = [MON, THU]
    sp = spots(days)
    sp[THU.isoformat()] = {m: 666.0 for m in range(570, 960)}            # 4$ in the money
    minutes = {"SP": {MON.isoformat(): flat(1.50), THU.isoformat(): flat(1.50)},
               "LP": {MON.isoformat(): flat(0.30), THU.isoformat(): flat(0.30)}}
    r = s.simulate_trade([PUT], set(), minutes, sp, days, MON, THU, tp_frac=0.75, slip=0.01)
    assert r["cost"] == 4.02 and r["reason"] == "expiry_close"


def test_no_trade_without_synchronous_prints_in_the_entry_window():
    days = [MON, THU]
    minutes = {"SP": {MON.isoformat(): flat(1.50)}, "LP": {MON.isoformat(): flat(0.30, 570, 580)}}
    assert s.simulate_trade([PUT], set(), minutes, spots(days), days, MON, THU, 0.75, 0.01) is None


def test_far_legs_may_be_stale_or_missing():
    days = [MON, THU]
    call = s.Vertical("SC", "LC", 693.0, 3.0, True)
    minutes = {"SP": {d.isoformat(): flat(1.50) for d in days},
               "LP": {d.isoformat(): flat(0.30) for d in days},
               "SC": {MON.isoformat(): [(575, 0.06)]}}                   # one early print, long call never
    r = s.simulate_trade([PUT, call], {"SC", "LC"}, minutes, spots(days), days, MON, THU, 0.75, 0.01)
    assert r["credit"] == round(1.20 + 0.06 - 4 * 0.01, 2)


def T(strategy, entry, exit_, credit, cost, width):
    return {"strategy": strategy, "entry": entry, "exit": exit_, "credit": credit, "cost": cost, "width": width}


def test_account_sim_sizes_from_cash_and_compounds():
    trades = [T("nectar", "2026-03-02", "2026-03-03", 1.60, 1.20, 5.0),   # +40 per contract
              T("nectar", "2026-03-09", "2026-03-12", 1.60, 5.00, 5.0)]   # -340 per contract
    out = s.account_sim(trades, 871.0)
    # week 1: 871 // 340 = 2 contracts -> +80 ; week 2: 951 // 340 = 2 contracts -> -680
    assert out["end"] == 271.0 and out["trades"] == 2 and out["low"] == 271.0


def test_account_sim_keeps_comb_open_across_the_next_monday():
    trades = [T("comb", "2026-03-02", "2026-03-13", 0.50, 0.25, 10.0),
              T("comb", "2026-03-09", "2026-03-20", 0.50, 0.25, 10.0),    # first one still open
              T("nectar", "2026-03-09", "2026-03-12", 1.60, 1.20, 5.0)]
    out = s.account_sim(trades, 10500.0)
    # 2 Mar: comb only -> spill-over: 10500 // 950 = 11 contracts (+275 on 13 Mar)
    # 9 Mar: comb open -> no second comb; nectar share 735 but free cash 10500 - 10450 = 50 -> 0
    assert out["trades"] == 1 and out["end"] == 10775.0


def _two_day_spike(spike_price, spot_tue=672.0):
    days = [MON, TUE, THU]
    minutes = {"SP": {MON.isoformat(): flat(1.50), TUE.isoformat(): flat(1.50, 570, 700) + flat(spike_price, 700),
                      THU.isoformat(): flat(1.50)},
               "LP": {d.isoformat(): flat(0.30) for d in days}}
    sp = spots(days)
    sp[TUE.isoformat()] = {m: (672.0 if m < 700 else spot_tue) for m in range(570, 960)}
    return days, minutes, sp


def test_stop_loss_closes_when_the_position_costs_a_multiple_of_the_credit():
    days, minutes, sp = _two_day_spike(3.00)
    r = s.simulate_trade([PUT], set(), minutes, sp, days, MON, THU, 0.75, 0.01, stop_mult=2.0)
    # credit 1.18 -> stop level 2.36 ; Tuesday 11:40 mark 2.70 >= 2.36 -> pay mark + slippage
    assert (r["exit"], r["exit_min"], r["reason"]) == (TUE.isoformat(), 700, "stop")
    assert r["cost"] == 2.72


def test_stop_loss_does_not_fire_below_its_level():
    days, minutes, sp = _two_day_spike(2.50)                  # mark 2.20 < 2.36
    r = s.simulate_trade([PUT], set(), minutes, sp, days, MON, THU, 0.75, 0.01, stop_mult=2.0)
    assert r["reason"] == "expiry_close"


def test_touch_stop_closes_when_spy_reaches_the_short_strike():
    days, minutes, sp = _two_day_spike(2.00, spot_tue=670.0)  # SPY at the 670 short put
    r = s.simulate_trade([PUT], set(), minutes, sp, days, MON, THU, 0.75, 0.01, stop_touch=True)
    assert (r["exit"], r["exit_min"], r["reason"]) == (TUE.isoformat(), 700, "stop")
    assert r["cost"] == 1.72
    r = s.simulate_trade([PUT], set(), minutes, sp, days, MON, THU, 0.75, 0.01)
    assert r["reason"] == "expiry_close"                      # without the stop it is held
