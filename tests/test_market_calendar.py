"""Tests for etf_tracker.market_calendar.

Holiday lists for 2025-2027 are the official NYSE Group calendars
(nyse.com/markets/hours-calendars and the ICE press releases of 2024-11-08
and 2025-12-23).
"""

from datetime import date, datetime

import pytest

from etf_tracker import market_calendar as mc
from etf_tracker.market_calendar import (
    FRIDAY,
    MONDAY,
    SATURDAY,
    SUNDAY,
    THURSDAY,
    TUESDAY,
    add_trading_days,
    easter_sunday,
    is_trading_day,
    last_trading_day_of_month,
    next_trading_day,
    nth_weekday_of_month,
    nyse_holiday_names,
    nyse_holidays,
    prev_trading_day,
    third_friday,
    thursday_before_second_friday,
    trading_days_between,
)

# --------------------------------------------------------------------------
# Official NYSE holiday calendars
# --------------------------------------------------------------------------

OFFICIAL_HOLIDAYS = {
    2025: {
        date(2025, 1, 1),  # New Year's Day (Wed)
        date(2025, 1, 20),  # MLK Day
        date(2025, 2, 17),  # Presidents' Day
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 26),  # Memorial Day
        date(2025, 6, 19),  # Juneteenth (Thu)
        date(2025, 7, 4),  # Independence Day (Fri)
        date(2025, 9, 1),  # Labor Day
        date(2025, 11, 27),  # Thanksgiving
        date(2025, 12, 25),  # Christmas (Thu)
    },
    2026: {
        date(2026, 1, 1),  # New Year's Day (Thu)
        date(2026, 1, 19),  # MLK Day
        date(2026, 2, 16),  # Presidents' Day
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth (Fri)
        date(2026, 7, 3),  # Independence Day observed (Jul 4 is a Saturday)
        date(2026, 9, 7),  # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas (Fri)
    },
    2027: {
        date(2027, 1, 1),  # New Year's Day (Fri)
        date(2027, 1, 18),  # MLK Day
        date(2027, 2, 15),  # Presidents' Day
        date(2027, 3, 26),  # Good Friday
        date(2027, 5, 31),  # Memorial Day
        date(2027, 6, 18),  # Juneteenth observed (Jun 19 is a Saturday)
        date(2027, 7, 5),  # Independence Day observed (Jul 4 is a Sunday)
        date(2027, 9, 6),  # Labor Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 12, 24),  # Christmas observed (Dec 25 is a Saturday)
    },
}


@pytest.mark.parametrize("year", sorted(OFFICIAL_HOLIDAYS))
def test_official_holiday_lists(year):
    assert nyse_holidays(year) == OFFICIAL_HOLIDAYS[year]


@pytest.mark.parametrize("year", sorted(OFFICIAL_HOLIDAYS))
def test_official_holidays_are_not_trading_days(year):
    for holiday in OFFICIAL_HOLIDAYS[year]:
        assert not is_trading_day(holiday), holiday


def test_holiday_names_match_dates():
    names = nyse_holiday_names(2026)
    assert set(names) == OFFICIAL_HOLIDAYS[2026]
    assert names[date(2026, 4, 3)] == "Good Friday"
    assert names[date(2026, 7, 3)] == "Independence Day"
    assert names[date(2026, 6, 19)] == "Juneteenth"


def test_nyse_holidays_returns_fresh_set():
    first = nyse_holidays(2026)
    first.add(date(2026, 1, 2))
    assert date(2026, 1, 2) not in nyse_holidays(2026)


# --------------------------------------------------------------------------
# Weekend observance rules
# --------------------------------------------------------------------------


def test_new_years_on_saturday_is_not_observed():
    # 2028-01-01 is a Saturday: the NYSE is open on Friday 2027-12-31 and the
    # 2028 calendar has only nine holidays.
    assert date(2028, 1, 1).weekday() == SATURDAY
    assert is_trading_day(date(2027, 12, 31))
    assert date(2027, 12, 31) not in nyse_holidays(2027)
    assert date(2027, 12, 31) not in nyse_holidays(2028)
    assert date(2028, 1, 1) not in nyse_holidays(2028)
    assert len(nyse_holidays(2028)) == 9
    # Same configuration in 2022: the exchange traded on 2021-12-31.
    assert is_trading_day(date(2021, 12, 31))


def test_new_years_on_sunday_is_observed_monday():
    assert date(2023, 1, 1).weekday() == SUNDAY
    assert date(2023, 1, 2) in nyse_holidays(2023)
    assert not is_trading_day(date(2023, 1, 2))
    assert date(2017, 1, 2) in nyse_holidays(2017)


@pytest.mark.parametrize(
    "holiday, observed",
    [
        (date(2020, 7, 4), date(2020, 7, 3)),  # Saturday -> Friday
        (date(2021, 7, 4), date(2021, 7, 5)),  # Sunday -> Monday
        (date(2021, 12, 25), date(2021, 12, 24)),  # Saturday -> Friday
        (date(2022, 12, 25), date(2022, 12, 26)),  # Sunday -> Monday
        (date(2022, 6, 19), date(2022, 6, 20)),  # Sunday -> Monday
        (date(2027, 6, 19), date(2027, 6, 18)),  # Saturday -> Friday
    ],
)
def test_fixed_date_weekend_observance(holiday, observed):
    assert observed in nyse_holidays(holiday.year)
    assert holiday not in nyse_holidays(holiday.year)
    assert not is_trading_day(observed)


def test_juneteenth_only_from_2022():
    # Juneteenth became a federal holiday in June 2021 but the NYSE stayed
    # open on Friday 2021-06-18; it first closed for it in 2022.
    assert is_trading_day(date(2021, 6, 18))
    assert date(2021, 6, 18) not in nyse_holidays(2021)
    assert date(2022, 6, 20) in nyse_holidays(2022)
    assert len(nyse_holidays(2021)) == 9
    assert len(nyse_holidays(2023)) == 10


@pytest.mark.parametrize("year", range(1998, 2061))
def test_holiday_set_invariants(year):
    holidays = nyse_holidays(year)
    for d in holidays:
        assert d.year == year
        assert d.weekday() < SATURDAY, d
    expected = 10 if year >= 2022 else 9
    if date(year, 1, 1).weekday() == SATURDAY:
        expected -= 1
    assert len(holidays) == expected


# --------------------------------------------------------------------------
# Easter / Good Friday
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "year, easter",
    [
        (2000, date(2000, 4, 23)),
        (2008, date(2008, 3, 23)),  # earliest in living memory
        (2011, date(2011, 4, 24)),
        (2016, date(2016, 3, 27)),
        (2019, date(2019, 4, 21)),
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
        (2028, date(2028, 4, 16)),
        (2038, date(2038, 4, 25)),  # latest possible Easter
    ],
)
def test_easter_sunday(year, easter):
    assert easter_sunday(year) == easter
    assert easter_sunday(year).weekday() == SUNDAY


@pytest.mark.parametrize(
    "good_friday",
    [date(2008, 3, 21), date(2024, 3, 29), date(2025, 4, 18), date(2026, 4, 3), date(2027, 3, 26)],
)
def test_good_friday_closed(good_friday):
    assert good_friday.weekday() == FRIDAY
    assert good_friday in nyse_holidays(good_friday.year)
    assert not is_trading_day(good_friday)


# --------------------------------------------------------------------------
# Special closures / opens and early closes
# --------------------------------------------------------------------------


def test_carter_day_of_mourning_closure():
    assert date(2025, 1, 9) in mc.SPECIAL_CLOSURES
    assert not is_trading_day(date(2025, 1, 9))
    assert is_trading_day(date(2025, 1, 8))
    assert is_trading_day(date(2025, 1, 10))
    # Not part of the published annual holiday calendar ...
    assert date(2025, 1, 9) not in nyse_holidays(2025)
    # ... but reported when asked for actual closures.
    assert date(2025, 1, 9) in nyse_holidays(2025, include_special=True)
    assert len(nyse_holidays(2025, include_special=True)) == 11


def test_historical_special_closures():
    for d in (date(2001, 9, 11), date(2001, 9, 14), date(2012, 10, 29), date(2018, 12, 5)):
        assert not is_trading_day(d), d
    assert is_trading_day(date(2001, 9, 17))
    assert is_trading_day(date(2012, 10, 31))


@pytest.mark.parametrize(
    "early_close",
    [
        date(2025, 7, 3),
        date(2025, 11, 28),
        date(2025, 12, 24),
        date(2026, 11, 27),
        date(2026, 12, 24),
        date(2027, 11, 26),
    ],
)
def test_early_close_days_are_trading_days(early_close):
    assert is_trading_day(early_close)


def test_special_opens_override_weekend_and_holiday(monkeypatch):
    saturday = date(2026, 6, 6)
    good_friday = date(2026, 4, 3)
    assert not is_trading_day(saturday)
    monkeypatch.setattr(mc, "SPECIAL_OPENS", {saturday, good_friday})
    assert is_trading_day(saturday)
    assert is_trading_day(good_friday)
    assert good_friday not in nyse_holidays(2026, include_special=True)
    assert good_friday in nyse_holidays(2026)


def test_special_closures_win_over_special_opens(monkeypatch):
    d = date(2026, 3, 11)
    monkeypatch.setattr(mc, "SPECIAL_OPENS", {d})
    monkeypatch.setattr(mc, "SPECIAL_CLOSURES", {d})
    assert not is_trading_day(d)


def test_runtime_added_closure_is_respected(monkeypatch):
    d = date(2026, 3, 11)
    assert is_trading_day(d)
    monkeypatch.setattr(mc, "SPECIAL_CLOSURES", set(mc.SPECIAL_CLOSURES) | {d})
    assert not is_trading_day(d)
    assert next_trading_day(date(2026, 3, 10)) == date(2026, 3, 12)


def test_datetime_inputs_are_normalised():
    assert not is_trading_day(datetime(2025, 1, 9, 10, 30))
    assert not is_trading_day(datetime(2026, 4, 3))
    assert is_trading_day(datetime(2026, 4, 2, 9, 30))
    assert next_trading_day(datetime(2026, 4, 2, 16)) == date(2026, 4, 6)
    assert trading_days_between(datetime(2026, 4, 1), datetime(2026, 4, 6)) == 2


def test_non_date_input_rejected():
    with pytest.raises(TypeError):
        is_trading_day("2026-04-02")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# is_trading_day basics
# --------------------------------------------------------------------------


def test_weekends_closed_weekdays_open():
    monday = date(2026, 3, 9)
    for offset in range(5):
        assert is_trading_day(date.fromordinal(monday.toordinal() + offset))
    assert not is_trading_day(date(2026, 3, 14))  # Saturday
    assert not is_trading_day(date(2026, 3, 15))  # Sunday


# --------------------------------------------------------------------------
# next / prev trading day
# --------------------------------------------------------------------------


def test_next_trading_day_include_semantics():
    wed = date(2026, 11, 25)
    assert next_trading_day(wed, include=True) == wed
    assert next_trading_day(wed) == date(2026, 11, 27)  # skips Thanksgiving
    assert next_trading_day(date(2026, 11, 26), include=True) == date(2026, 11, 27)


def test_next_trading_day_over_christmas_weekend():
    # Thu 2026-12-24 (early close) -> Fri 25 holiday, weekend -> Mon 28.
    assert next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)
    assert next_trading_day(date(2026, 12, 25), include=True) == date(2026, 12, 28)


def test_prev_trading_day_include_semantics():
    mlk = date(2026, 1, 19)
    assert prev_trading_day(mlk, include=True) == date(2026, 1, 16)
    assert prev_trading_day(mlk) == date(2026, 1, 16)
    assert prev_trading_day(date(2026, 1, 16), include=True) == date(2026, 1, 16)
    assert prev_trading_day(date(2026, 1, 16)) == date(2026, 1, 15)


def test_prev_trading_day_over_carter_closure():
    # Fri 2025-01-10 <- Thu 09 closed <- Wed 08.
    assert prev_trading_day(date(2025, 1, 10)) == date(2025, 1, 8)
    assert prev_trading_day(date(2025, 1, 9), include=True) == date(2025, 1, 8)


def test_next_prev_cross_year_boundary():
    # New Year's 2028 not observed: Fri 2027-12-31 is open.
    assert next_trading_day(date(2027, 12, 30)) == date(2027, 12, 31)
    assert next_trading_day(date(2027, 12, 31)) == date(2028, 1, 3)
    # New Year's 2026 is Thursday: Wed 2025-12-31 -> Fri 2026-01-02.
    assert next_trading_day(date(2025, 12, 31)) == date(2026, 1, 2)
    assert prev_trading_day(date(2026, 1, 2)) == date(2025, 12, 31)


# --------------------------------------------------------------------------
# Month helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "year, month, expected",
    [
        (2026, 11, date(2026, 11, 30)),  # Monday, ordinary month end
        (2026, 5, date(2026, 5, 29)),  # May 31 is a Sunday
        (2027, 5, date(2027, 5, 28)),  # May 31 is Memorial Day
        (2026, 12, date(2026, 12, 31)),  # Thursday
        (2027, 12, date(2027, 12, 31)),  # open: New Year's 2028 not observed
        (2021, 12, date(2021, 12, 31)),  # same configuration in 2021
        (2025, 8, date(2025, 8, 29)),  # Aug 31 is a Sunday
        (2026, 2, date(2026, 2, 27)),  # Feb 28 is a Saturday
    ],
)
def test_last_trading_day_of_month(year, month, expected):
    assert last_trading_day_of_month(year, month) == expected


def test_nth_weekday_of_month_from_start():
    # March 2026: the 1st is a Sunday.
    assert nth_weekday_of_month(2026, 3, SUNDAY, 1) == date(2026, 3, 1)
    assert nth_weekday_of_month(2026, 3, MONDAY, 1) == date(2026, 3, 2)
    assert nth_weekday_of_month(2026, 3, SATURDAY, 1) == date(2026, 3, 7)
    assert nth_weekday_of_month(2026, 3, FRIDAY, 3) == date(2026, 3, 20)
    assert nth_weekday_of_month(2026, 3, TUESDAY, 5) == date(2026, 3, 31)
    # Month whose first day *is* the requested weekday: first occurrence is day 1.
    assert nth_weekday_of_month(2026, 5, FRIDAY, 1) == date(2026, 5, 1)
    assert nth_weekday_of_month(2026, 5, FRIDAY, 3) == date(2026, 5, 15)


def test_nth_weekday_of_month_from_end():
    assert nth_weekday_of_month(2026, 5, MONDAY, -1) == date(2026, 5, 25)
    assert nth_weekday_of_month(2027, 5, MONDAY, -1) == date(2027, 5, 31)
    assert nth_weekday_of_month(2026, 3, TUESDAY, -1) == date(2026, 3, 31)
    assert nth_weekday_of_month(2026, 3, TUESDAY, -5) == date(2026, 3, 3)


def test_nth_weekday_of_month_errors():
    with pytest.raises(ValueError):
        nth_weekday_of_month(2026, 3, FRIDAY, 5)  # March 2026 has only 4 Fridays
    with pytest.raises(ValueError):
        nth_weekday_of_month(2026, 3, TUESDAY, -6)
    with pytest.raises(ValueError):
        nth_weekday_of_month(2026, 3, FRIDAY, 0)
    with pytest.raises(ValueError):
        nth_weekday_of_month(2026, 3, 7, 1)
    with pytest.raises(ValueError):
        nth_weekday_of_month(2026, 13, FRIDAY, 1)


@pytest.mark.parametrize(
    "year, month, expected",
    [
        (2026, 12, date(2026, 12, 18)),
        (2026, 9, date(2026, 9, 18)),
        (2027, 3, date(2027, 3, 19)),
        (2026, 5, date(2026, 5, 15)),  # 1st is a Friday
        (2026, 8, date(2026, 8, 21)),  # 1st is a Saturday
        (2026, 3, date(2026, 3, 20)),
        (2008, 3, date(2008, 3, 21)),  # coincides with Good Friday
    ],
)
def test_third_friday(year, month, expected):
    assert third_friday(year, month) == expected
    assert expected.weekday() == FRIDAY


def test_third_friday_on_good_friday_rolls_back_with_prev_trading_day():
    tf = third_friday(2008, 3)
    assert not is_trading_day(tf)
    assert prev_trading_day(tf, include=True) == date(2008, 3, 20)


@pytest.mark.parametrize(
    "year, month, expected",
    [
        (2026, 12, date(2026, 12, 10)),
        (2026, 5, date(2026, 5, 7)),  # 1st is a Friday: Fridays 1, 8 -> Thu 7
        (2026, 8, date(2026, 8, 13)),  # 1st is a Saturday: Fridays 7, 14 -> Thu 13
        (2026, 9, date(2026, 9, 10)),
        (2027, 3, date(2027, 3, 11)),
        (2026, 10, date(2026, 10, 8)),  # 1st is a Thursday
    ],
)
def test_thursday_before_second_friday(year, month, expected):
    result = thursday_before_second_friday(year, month)
    assert result == expected
    assert result.weekday() == THURSDAY
    assert 7 <= result.day <= 13
    assert is_trading_day(result)


# --------------------------------------------------------------------------
# trading_days_between / add_trading_days
# --------------------------------------------------------------------------


def test_trading_days_between_basic():
    d = date(2026, 3, 11)
    assert trading_days_between(d, d) == 0
    assert trading_days_between(d, date(2026, 3, 12)) == 1
    assert trading_days_between(date(2026, 3, 13), date(2026, 3, 16)) == 1  # Fri -> Mon
    assert trading_days_between(date(2026, 3, 9), date(2026, 3, 13)) == 4  # Mon -> Fri
    assert trading_days_between(date(2026, 3, 9), date(2026, 3, 15)) == 4  # Mon -> Sun


def test_trading_days_between_excludes_start_includes_end():
    # Start excluded: Thu -> Fri over no holiday is exactly 1, not 2.
    assert trading_days_between(date(2026, 3, 12), date(2026, 3, 13)) == 1
    # Non-trading start contributes nothing: Saturday -> Monday is 1.
    assert trading_days_between(date(2026, 3, 14), date(2026, 3, 16)) == 1
    # Non-trading end counts nothing for itself: Friday -> Saturday is 0.
    assert trading_days_between(date(2026, 3, 13), date(2026, 3, 14)) == 0


def test_trading_days_between_skips_holidays_and_closures():
    # Wed 2026-11-25 -> Mon 2026-11-30: Fri 27 and Mon 30 (Thu 26 is Thanksgiving).
    assert trading_days_between(date(2026, 11, 25), date(2026, 11, 30)) == 2
    # Wed 2025-01-08 -> Mon 2025-01-13: Fri 10 and Mon 13 (Thu 9 Carter closure).
    assert trading_days_between(date(2025, 1, 8), date(2025, 1, 13)) == 2
    # Thu 2027-12-30 -> Tue 2028-01-04: Fri 31 (open), Mon 3, Tue 4.
    assert trading_days_between(date(2027, 12, 30), date(2028, 1, 4)) == 3


def test_trading_days_between_negative_and_antisymmetric():
    a, b = date(2026, 11, 25), date(2026, 11, 30)
    assert trading_days_between(b, a) == -2
    for x, y in [(a, b), (date(2026, 3, 14), date(2026, 3, 16)), (date(2025, 12, 31), date(2026, 1, 5))]:
        assert trading_days_between(x, y) == -trading_days_between(y, x)


def test_trading_days_in_2026():
    # 365 days, 261 weekdays, 10 holidays -> 251 sessions.
    assert trading_days_between(date(2025, 12, 31), date(2026, 12, 31)) == 251


def test_add_trading_days_forward():
    assert add_trading_days(date(2026, 3, 11), 0) == date(2026, 3, 11)
    assert add_trading_days(date(2026, 3, 13), 1) == date(2026, 3, 16)  # Fri -> Mon
    assert add_trading_days(date(2026, 11, 25), 1) == date(2026, 11, 27)  # over Thanksgiving
    assert add_trading_days(date(2026, 11, 25), 2) == date(2026, 11, 30)
    assert add_trading_days(date(2027, 12, 30), 3) == date(2028, 1, 4)


def test_add_trading_days_backward():
    assert add_trading_days(date(2026, 3, 16), -1) == date(2026, 3, 13)  # Mon -> Fri
    assert add_trading_days(date(2026, 11, 30), -2) == date(2026, 11, 25)
    assert add_trading_days(date(2025, 1, 10), -1) == date(2025, 1, 8)  # over Carter closure
    assert add_trading_days(date(2026, 1, 2), -1) == date(2025, 12, 31)


def test_add_trading_days_from_non_trading_day():
    saturday = date(2026, 3, 14)
    assert add_trading_days(saturday, 0) == saturday
    assert add_trading_days(saturday, 1) == date(2026, 3, 16)
    assert add_trading_days(saturday, -1) == date(2026, 3, 13)


@pytest.mark.parametrize("n", [-30, -7, -1, 0, 1, 5, 22, 65, 260])
def test_add_and_between_are_consistent(n):
    start = date(2026, 3, 11)  # a trading day
    end = add_trading_days(start, n)
    assert is_trading_day(end) or n == 0
    assert trading_days_between(start, end) == n


def test_sequential_trading_days_have_gap_one():
    d = date(2025, 12, 15)
    for _ in range(40):
        nxt = next_trading_day(d)
        assert trading_days_between(d, nxt) == 1
        assert add_trading_days(d, 1) == nxt
        assert prev_trading_day(nxt) == d
        d = nxt


# --------------------------------------------------------------------------
# Adversarial review: official 2028 calendar, Good Friday 2024-2030, month-start
# edge cases, validation gaps
# --------------------------------------------------------------------------

# nyse.com/markets/hours-calendars, fetched 2026-10-09.  2028 has no New
# Year's Day holiday ("Because the holiday falls on Saturday, January 1, 2028,
# no New Year's Day holiday is observed").
OFFICIAL_HOLIDAYS_2028 = {
    date(2028, 1, 17),  # MLK Day
    date(2028, 2, 21),  # Washington's Birthday
    date(2028, 4, 14),  # Good Friday
    date(2028, 5, 29),  # Memorial Day
    date(2028, 6, 19),  # Juneteenth (Mon)
    date(2028, 7, 4),  # Independence Day (Tue)
    date(2028, 9, 4),  # Labor Day
    date(2028, 11, 23),  # Thanksgiving
    date(2028, 12, 25),  # Christmas (Mon)
}

# Early closes published on the same page.
OFFICIAL_EARLY_CLOSES_2028 = [date(2028, 7, 3), date(2028, 11, 24)]


def test_official_holiday_list_2028():
    assert nyse_holidays(2028) == OFFICIAL_HOLIDAYS_2028
    for d in OFFICIAL_HOLIDAYS_2028:
        assert not is_trading_day(d), d
    for d in OFFICIAL_EARLY_CLOSES_2028:
        assert is_trading_day(d), d


@pytest.mark.parametrize(
    "year, good_friday",
    [
        (2024, date(2024, 3, 29)),
        (2025, date(2025, 4, 18)),
        (2026, date(2026, 4, 3)),
        (2027, date(2027, 3, 26)),
        (2028, date(2028, 4, 14)),
        (2029, date(2029, 3, 30)),
        (2030, date(2030, 4, 19)),
    ],
)
def test_good_friday_2024_to_2030(year, good_friday):
    assert easter_sunday(year) - good_friday == __import__("datetime").timedelta(days=2)
    assert good_friday.weekday() == FRIDAY
    assert nyse_holiday_names(year)[good_friday] == "Good Friday"
    assert not is_trading_day(good_friday)
    # The Thursday before and the Monday after are ordinary sessions.
    assert is_trading_day(good_friday - __import__("datetime").timedelta(days=1))
    assert next_trading_day(good_friday) == good_friday + __import__("datetime").timedelta(days=3)


def test_sessions_per_year_match_published_counts():
    # 2024: 252 sessions.  2025: 251 scheduled minus the Carter closure = 250.
    # 2026/2027: 251.  2028: leap year starting on a Saturday -> 260 weekdays,
    # 9 holidays -> 251.
    for year, sessions in [(2024, 252), (2025, 250), (2026, 251), (2027, 251), (2028, 251)]:
        assert trading_days_between(date(year - 1, 12, 31), date(year, 12, 31)) == sessions, year


def test_mlk_day_first_observed_1998():
    # Third Monday of January 1997 was an ordinary session; 1998 was the first closure.
    assert is_trading_day(date(1997, 1, 20))
    assert date(1997, 1, 20) not in nyse_holidays(1997)
    assert date(1998, 1, 19) in nyse_holidays(1998)
    assert not is_trading_day(date(1998, 1, 19))


@pytest.mark.parametrize("year", [2000, 2005, 2011, 2022, 2028, 2033])
def test_every_saturday_new_years_day_leaves_dec_31_open(year):
    assert date(year, 1, 1).weekday() == SATURDAY
    dec31 = date(year - 1, 12, 31)
    assert dec31.weekday() == FRIDAY
    assert is_trading_day(dec31)
    assert last_trading_day_of_month(year - 1, 12) == dec31
    assert "New Year's Day" not in nyse_holiday_names(year).values()


def test_other_saturday_holidays_are_still_observed_on_friday():
    # The month-end exception applies only to New Year's Day; a Saturday
    # Christmas / Juneteenth / Independence Day is observed on the Friday.
    assert nyse_holiday_names(2027)[date(2027, 12, 24)] == "Christmas Day"
    assert nyse_holiday_names(2027)[date(2027, 6, 18)] == "Juneteenth"
    assert nyse_holiday_names(2026)[date(2026, 7, 3)] == "Independence Day"
    assert not is_trading_day(date(2027, 12, 24))


def _all_fri_or_sat_starting_months(first_year, last_year):
    for year in range(first_year, last_year + 1):
        for month in range(1, 13):
            if date(year, month, 1).weekday() in (FRIDAY, SATURDAY):
                yield year, month


@pytest.mark.parametrize("year, month", list(_all_fri_or_sat_starting_months(2024, 2030)))
def test_month_starting_friday_or_saturday(year, month):
    first = date(year, month, 1)
    if first.weekday() == FRIDAY:
        # Fridays fall on 1, 8, 15, 22, 29.
        assert nth_weekday_of_month(year, month, FRIDAY, 1) == first
        assert third_friday(year, month) == date(year, month, 15)
        assert thursday_before_second_friday(year, month) == date(year, month, 7)
        if mc._last_day_of_month(year, month).day >= 29:
            assert nth_weekday_of_month(year, month, FRIDAY, 5) == date(year, month, 29)
        else:  # February 2030 starts on a Friday and has no fifth one
            with pytest.raises(ValueError):
                nth_weekday_of_month(year, month, FRIDAY, 5)
    else:
        # Fridays fall on 7, 14, 21, 28: a Saturday start pushes everything a week.
        assert nth_weekday_of_month(year, month, FRIDAY, 1) == date(year, month, 7)
        assert third_friday(year, month) == date(year, month, 21)
        assert thursday_before_second_friday(year, month) == date(year, month, 13)
        with pytest.raises(ValueError):
            nth_weekday_of_month(year, month, FRIDAY, 5)
    assert thursday_before_second_friday(year, month).weekday() == THURSDAY


@pytest.mark.parametrize("year", range(2024, 2031))
@pytest.mark.parametrize("month", range(1, 13))
def test_nth_weekday_matches_brute_force(year, month):
    days = [date(year, month, d) for d in range(1, 32) if d <= mc._last_day_of_month(year, month).day]
    for weekday in range(7):
        occurrences = [d for d in days if d.weekday() == weekday]
        for i, d in enumerate(occurrences):
            assert nth_weekday_of_month(year, month, weekday, i + 1) == d
            assert nth_weekday_of_month(year, month, weekday, i - len(occurrences)) == d
        with pytest.raises(ValueError):
            nth_weekday_of_month(year, month, weekday, len(occurrences) + 1)
        with pytest.raises(ValueError):
            nth_weekday_of_month(year, month, weekday, -len(occurrences) - 1)


@pytest.mark.parametrize(
    "n",
    [54, -57, 53 * 4, -53 * 4],  # multiples of ~52 weeks wrap into the same month of another year
)
def test_nth_weekday_rejects_year_wrap(n):
    with pytest.raises(ValueError):
        nth_weekday_of_month(2026, 5, FRIDAY, n)
    with pytest.raises(ValueError):
        nth_weekday_of_month(2026, 3, TUESDAY, n)


def test_thursday_before_second_friday_can_hit_ad_hoc_closure():
    # January 2025: Fridays 3 and 10 -> Thursday 9 == Carter day of mourning.
    ref = thursday_before_second_friday(2025, 1)
    assert ref == date(2025, 1, 9)
    assert not is_trading_day(ref)
    assert prev_trading_day(ref, include=True) == date(2025, 1, 8)


def test_thursday_before_second_friday_is_never_a_rule_holiday():
    for year in range(1998, 2061):
        for month in range(1, 13):
            ref = thursday_before_second_friday(year, month)
            assert 7 <= ref.day <= 13
            assert ref not in nyse_holidays(year), ref


def _naive_trading_days_between(a, b):
    sign = 1
    if b < a:
        a, b = b, a
        sign = -1
    count = 0
    d = a + __import__("datetime").timedelta(days=1)
    while d <= b:
        count += is_trading_day(d)
        d += __import__("datetime").timedelta(days=1)
    return sign * count


@pytest.mark.parametrize(
    "a, b",
    [
        (date(2026, 12, 24), date(2026, 12, 28)),  # early close, holiday, weekend
        (date(2026, 12, 28), date(2026, 12, 24)),
        (date(2025, 1, 8), date(2025, 1, 10)),  # Carter closure
        (date(2025, 1, 10), date(2025, 1, 8)),
        (date(2027, 12, 31), date(2028, 1, 3)),  # Fri 31 open, Mon 3 open
        (date(2028, 1, 3), date(2027, 12, 31)),
        (date(2026, 4, 2), date(2026, 4, 6)),  # Good Friday
        (date(2026, 4, 3), date(2026, 4, 3)),  # holiday to itself
        (date(2026, 4, 3), date(2026, 4, 6)),  # holiday start, Monday end
        (date(2026, 4, 2), date(2026, 4, 3)),  # Thursday to Good Friday: 0
    ],
)
def test_trading_days_between_matches_naive_enumeration(a, b):
    assert trading_days_between(a, b) == _naive_trading_days_between(a, b)


def test_trading_days_between_endpoint_semantics_explicit():
    # (a, b]: start exclusive, end inclusive.
    assert trading_days_between(date(2026, 4, 2), date(2026, 4, 3)) == 0  # end is Good Friday
    assert trading_days_between(date(2026, 4, 3), date(2026, 4, 6)) == 1  # start is Good Friday
    assert trading_days_between(date(2026, 4, 6), date(2026, 4, 3)) == -1
    assert trading_days_between(date(2026, 4, 2), date(2026, 4, 6)) == 1
    assert trading_days_between(date(2026, 4, 6), date(2026, 4, 2)) == -1


def test_include_special_never_reports_weekend_closures(monkeypatch):
    # Registering a weekend date as a closure is meaningless for trading but
    # must not leak into the "weekdays the exchange was closed" view.
    saturday = date(2026, 6, 6)
    monkeypatch.setattr(mc, "SPECIAL_CLOSURES", set(mc.SPECIAL_CLOSURES) | {saturday})
    assert not is_trading_day(saturday)
    assert saturday not in nyse_holidays(2026, include_special=True)
    for d in nyse_holidays(2026, include_special=True):
        assert d.weekday() < SATURDAY
