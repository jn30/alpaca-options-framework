import datetime as dt

import spread_rules as r

D = dt.date
WEEK = {D(2026, 11, 16), D(2026, 11, 17), D(2026, 11, 18), D(2026, 11, 19), D(2026, 11, 20),
        D(2026, 11, 23), D(2026, 11, 24), D(2026, 11, 25), D(2026, 11, 27)}   # 26th = Thanksgiving


def test_nectar_strikes_basic():
    assert r.nectar_strikes(672.40) == {"short_put": 670, "long_put": 665,
                                        "short_call": 693, "long_call": 696}


def test_nectar_short_put_must_be_below_spot_and_call_above():
    s = r.nectar_strikes(2.60)
    assert s["short_put"] < 2.60 and s["short_call"] > 2.60


def test_nectar_expiry_monday_is_thursday_tuesday_is_friday():
    assert r.nectar_expiry(D(2026, 11, 16), WEEK) == D(2026, 11, 19)
    assert r.nectar_expiry(D(2026, 11, 17), WEEK) == D(2026, 11, 20)


def test_nectar_expiry_holiday_thursday_moves_to_friday():
    assert r.nectar_expiry(D(2026, 11, 23), WEEK) == D(2026, 11, 27)


def test_nectar_entry_days():
    assert r.nectar_entry_ok(D(2026, 11, 16), WEEK)
    assert not r.nectar_entry_ok(D(2026, 11, 17), WEEK)            # Monday traded
    assert r.nectar_entry_ok(D(2026, 11, 17), WEEK - {D(2026, 11, 16)})   # Monday holiday
    assert not r.nectar_entry_ok(D(2026, 11, 18), WEEK)


def test_comb_expiry_is_next_friday_with_min_7_dte():
    assert r.comb_expiry(D(2026, 11, 16), WEEK) == D(2026, 11, 27)   # 11 days
    assert r.comb_expiry(D(2026, 11, 20), WEEK) == D(2026, 11, 27)   # exactly 7 days


def test_comb_expiry_holiday_friday_uses_last_trading_day_before():
    days = WEEK - {D(2026, 11, 27)}
    assert r.comb_expiry(D(2026, 11, 16), days) == D(2026, 11, 25)


def test_pick_delta_strike():
    assert r.pick_delta_strike({660: -0.11, 662: -0.14, 663: -0.17, 665: -0.22}) == 662
    assert r.pick_delta_strike({}) is None


def test_buyback_price_floors_to_cent():
    assert r.buyback_price(1.60, 0.75) == 1.20
    assert r.buyback_price(0.50, 0.50) == 0.25
    assert r.buyback_price(0.37, 0.50) == 0.18


def test_expiry_close_time_normal_and_half_day():
    assert r.expiry_close_time(dt.time(16, 0)) == dt.time(15, 30)
    assert r.expiry_close_time(dt.time(13, 0)) == dt.time(12, 30)


SH = {"nectar": 0.07, "comb": 0.93}


def test_contracts():
    assert r.contracts(871, 5, 1.60) == 2
    assert r.contracts(339, 5, 1.60) == 0
    assert r.contracts(5000, 5, 0) == 0        # no credit
    assert r.contracts(5000, 5, 5.2) == 0      # credit >= width
    assert r.contracts(-10, 5, 1.6) == 0


def test_allocate_original_ratio():
    assert r.allocate(10500, {"nectar": 340, "comb": 950}, SH) == {"nectar": 2, "comb": 10}


def test_allocate_spillover_to_other_strategy():
    assert r.allocate(5000, {"nectar": 340, "comb": 950}, SH) == {"nectar": 1, "comb": 4}
    assert r.allocate(2000, {"nectar": 340, "comb": 950}, SH) == {"nectar": 0, "comb": 2}


def test_allocate_both_zero_tries_comb_then_nectar_with_everything():
    assert r.allocate(871, {"nectar": 340, "comb": 950}, SH) == {"nectar": 2, "comb": 0}
    assert r.allocate(1000, {"nectar": 340, "comb": 950}, SH) == {"nectar": 0, "comb": 1}


def test_allocate_one_strategy_unavailable():
    assert r.allocate(871, {"nectar": 340}, SH) == {"nectar": 2, "comb": 0}


def test_allocate_never_exceeds_capital():
    q = r.allocate(5000, {"nectar": 340, "comb": 950}, SH)
    assert q["nectar"] * 340 + q["comb"] * 950 <= 5000


def test_qty_alongside_capped_by_free_cash():
    assert r.qty_alongside(10000, 500, 0.07, 340) == 1     # share 700 -> 2, free cash allows 1
    assert r.qty_alongside(10000, 5000, 0.07, 340) == 2
    assert r.qty_alongside(10000, 100, 0.07, 340) == 0


def test_allocate_priority_gives_the_first_strategy_all_cash_and_the_rest_to_the_next():
    loss = {"nectar": 340, "comb": 950}
    order = ("comb", "nectar")
    assert r.allocate_priority(10500, loss, order) == {"comb": 11, "nectar": 0}      # 50 left
    assert r.allocate_priority(5500, loss, order) == {"comb": 5, "nectar": 2}        # 750 left
    assert r.allocate_priority(871, loss, order) == {"comb": 0, "nectar": 2}         # comb unaffordable
    assert r.allocate_priority(871, {"nectar": 340}, order) == {"comb": 0, "nectar": 2}
    assert r.allocate_priority(100, loss, order) == {"comb": 0, "nectar": 0}


# ── Rules of 2026-11-16 (user spec "Step 5") ──────────────────────────────────

def test_nectar_expiries_three_days_out_then_later_sessions_of_the_same_week():
    assert r.nectar_expiries(D(2026, 11, 16), WEEK) == [D(2026, 11, 19), D(2026, 11, 20)]
    assert r.nectar_expiries(D(2026, 11, 17), WEEK) == [D(2026, 11, 20)]
    assert r.nectar_expiries(D(2026, 11, 23), WEEK) == [D(2026, 11, 27)]          # Thanksgiving Thursday


def test_nectar_expiries_never_cross_the_weekend():
    assert r.nectar_expiries(D(2026, 11, 17), WEEK - {D(2026, 11, 20)}) == []
    assert r.nectar_expiries(D(2026, 11, 16), WEEK - {D(2026, 11, 19), D(2026, 11, 20)}) == []


def test_comb_expiry_is_the_friday_nearest_7_days_within_3_to_11():
    assert r.comb_expiry_nearest(D(2026, 11, 16), WEEK) == D(2026, 11, 20)        # 4 days beats 11
    assert r.comb_expiry_nearest(D(2026, 11, 23), WEEK) == D(2026, 11, 27)


def test_comb_expiry_holiday_friday_moves_to_thursday_if_still_3_days_out():
    assert r.comb_expiry_nearest(D(2026, 11, 16), WEEK - {D(2026, 11, 20)}) == D(2026, 11, 19)
    days = WEEK - {D(2026, 11, 19), D(2026, 11, 20)} | {D(2026, 11, 26)}           # Wed would be 2 days
    assert r.comb_expiry_nearest(D(2026, 11, 16), days) == D(2026, 11, 27)


def test_bs_put_delta_matches_the_textbook_value():
    assert round(r.bs_put_delta(100, 100, 1.0, 0.20, rate=0.0, div=0.0), 4) == -0.4602


def test_comb_short_strike_is_the_listed_otm_put_nearest_15_delta_at_18_vol():
    strikes = [float(k) for k in range(650, 700)] + [701.0, 705.0]
    t = 4.25 / 365
    k = r.comb_short_strike(strikes, 700.0, t, delta=0.15, vol=0.18)
    best = min((s for s in strikes if s < 700), key=lambda s: abs(abs(r.bs_put_delta(700, s, t, 0.18)) - 0.15))
    assert k == best and 680 < k < 695
    assert r.comb_short_strike([705.0, 710.0], 700.0, t) is None                     # no OTM put listed


def test_open_and_close_limits():
    assert r.open_limit(1.73) == 1.56            # 90% of the mid credit
    assert r.open_limit(0.50) == 0.45
    assert r.close_limit(1.20, 5.0) == 1.38      # up to 15% over the mid debit, never more
    assert r.close_limit(0.03, 5.0) == 0.03
    assert r.close_limit(0.0, 5.0) == 0.01
    assert r.close_limit(4.80, 5.0) == 5.0       # never above the width


def test_take_reached_on_the_mid_debit():
    assert r.take_reached(1.28, 1.71, 0.75) and not r.take_reached(1.29, 1.71, 0.75)
    assert r.take_reached(0.25, 0.50, 0.50) and not r.take_reached(0.26, 0.50, 0.50)


def test_nectar_qty_inside_1000_dollars_inside_cash_cap_10():
    assert r.nectar_qty(1.56, 1138.0) == 2       # loss 344/contract: 1000 allows 2, cash 3
    assert r.nectar_qty(1.56, 100000.0) == 2
    assert r.nectar_qty(1.56, 500.0) == 1
    assert r.nectar_qty(4.50, 100000.0) == 10    # 1000 // 50 = 20 -> cap
    assert r.nectar_qty(5.20, 100000.0) == 0     # credit above the wing = bad quote
    assert r.nectar_qty(0.0, 100000.0) == 0


def test_comb_qty_is_10_or_nothing():
    assert r.comb_qty(0.80, 30000.0, 25000.0) == 10      # 9,200 fits cash and 40% of 25,000
    assert r.comb_qty(0.80, 9000.0, 25000.0) == 0        # not in cash
    assert r.comb_qty(0.50, 30000.0, 20000.0) == 0       # 9,500 > 40% of 20,000
    assert r.comb_qty(0.0, 30000.0, 25000.0) == 0
