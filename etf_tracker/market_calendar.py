"""US equity (NYSE) trading calendar and the date helpers index rules need.

Standard library only.  All functions take and return ``datetime.date``
objects (a ``datetime.datetime`` is accepted and reduced to its date).

Scope and accuracy
------------------
* A *trading day* is a weekday that is not an NYSE full-day holiday and not
  listed in :data:`SPECIAL_CLOSURES` (or that is listed in
  :data:`SPECIAL_OPENS`).
* **Early-close days count as trading days.**  The 1:00 p.m. ET closes (the
  day after Thanksgiving, Christmas Eve, and July 3 in some years) are full
  trading sessions for every purpose in this module.
* The rule set reproduces the modern NYSE holiday schedule: Martin Luther
  King Jr. Day has been observed since 1998 and Juneteenth since 2022.  Dates
  before 1998 are not modelled (the exchange had a different schedule) and
  the module makes no attempt to be correct there.
* Ad-hoc closures (national days of mourning, weather, 9/11) cannot be
  derived from rules.  Known ones are listed in :data:`SPECIAL_CLOSURES`;
  future announcements must be added there.

Weekend observance
------------------
Fixed-date holidays (New Year's Day, Juneteenth, Independence Day,
Christmas) that fall on a Saturday are observed on the preceding Friday and
on a Sunday on the following Monday, with one NYSE-specific exception: when
New Year's Day falls on a Saturday it is **not** observed at all, so the
exchange is open on Friday, December 31 of the preceding year (e.g. 2021-12-31
and 2027-12-31).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

__all__ = [
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
    "MLK_FIRST_YEAR",
    "JUNETEENTH_FIRST_YEAR",
    "SPECIAL_CLOSURES",
    "SPECIAL_OPENS",
    "easter_sunday",
    "nyse_holidays",
    "nyse_holiday_names",
    "is_trading_day",
    "next_trading_day",
    "prev_trading_day",
    "last_trading_day_of_month",
    "nth_weekday_of_month",
    "third_friday",
    "thursday_before_second_friday",
    "trading_days_between",
    "add_trading_days",
]

# ``datetime.date.weekday()`` numbering: Monday == 0 ... Sunday == 6.
MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)

_ONE_DAY = timedelta(days=1)
_ONE_WEEK = timedelta(days=7)

#: First year the NYSE closed for Martin Luther King Jr. Day.
MLK_FIRST_YEAR = 1998
#: First year the NYSE closed for Juneteenth National Independence Day.
JUNETEENTH_FIRST_YEAR = 2022

#: Full-day closures that cannot be derived from the holiday rules.  The set
#: is deliberately mutable so a caller can register a newly announced closure
#: at runtime (``SPECIAL_CLOSURES.add(date(...))``).  Entries take precedence
#: over everything else, including :data:`SPECIAL_OPENS`.
SPECIAL_CLOSURES: set[date] = {
    # September 11 attacks: the exchange did not open until September 17.
    date(2001, 9, 11),
    date(2001, 9, 12),
    date(2001, 9, 13),
    date(2001, 9, 14),
    # National day of mourning for President Ronald Reagan.
    date(2004, 6, 11),
    # National day of mourning for President Gerald Ford.
    date(2007, 1, 2),
    # Hurricane Sandy.
    date(2012, 10, 29),
    date(2012, 10, 30),
    # National day of mourning for President George H. W. Bush.
    date(2018, 12, 5),
    # National day of mourning for President Jimmy Carter.
    date(2025, 1, 9),
}

#: Dates on which the exchange was open although the rules say it is closed
#: (a weekend or a rule-derived holiday).  Empty in the modern era; kept as an
#: explicit override hook.  A date present in both override sets is treated
#: as closed.
SPECIAL_OPENS: set[date] = set()


# --------------------------------------------------------------------------
# Generic date helpers
# --------------------------------------------------------------------------


def _as_date(d: date) -> date:
    """Reduce a ``datetime`` to its ``date``; pass a plain ``date`` through.

    ``date(2025, 1, 9) == datetime(2025, 1, 9)`` is ``False`` in Python, so
    set membership tests would silently fail for ``datetime`` inputs without
    this normalisation.
    """
    if isinstance(d, datetime):
        return d.date()
    if not isinstance(d, date):
        raise TypeError(f"expected datetime.date, got {type(d).__name__}")
    return d


def _last_day_of_month(year: int, month: int) -> date:
    """Return the last calendar day of ``month``/``year``."""
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - _ONE_DAY


def easter_sunday(year: int) -> date:
    """Return Gregorian Easter Sunday for ``year``.

    Uses the "Anonymous Gregorian algorithm" (Meeus/Jones/Butcher), valid for
    every year of the Gregorian calendar.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - canonical variable name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return the ``n``-th occurrence of ``weekday`` in ``month``/``year``.

    ``weekday`` uses ``date.weekday()`` numbering (Monday == 0).  Positive
    ``n`` counts from the start of the month (``n == 1`` is the first
    occurrence); negative ``n`` counts from the end (``n == -1`` is the last
    occurrence).  Raises ``ValueError`` when ``n == 0``, when ``weekday`` is
    outside ``0..6`` or when the month has no such occurrence (e.g. a fifth
    Friday in a month that only has four).

    The result is a calendar date; it is *not* adjusted for holidays.
    """
    if not 0 <= weekday <= 6:
        raise ValueError(f"weekday must be in 0..6 (Monday == 0), got {weekday}")
    if n == 0:
        raise ValueError("n must be non-zero (1 = first, -1 = last)")
    if n > 0:
        first = date(year, month, 1)
        result = first + timedelta(days=(weekday - first.weekday()) % 7)
        result += _ONE_WEEK * (n - 1)
    else:
        last = _last_day_of_month(year, month)
        result = last - timedelta(days=(last.weekday() - weekday) % 7)
        result -= _ONE_WEEK * (-n - 1)
    # Compare year *and* month: a jump of ~52 weeks lands in the same month
    # of a neighbouring year, which a month-only check would wave through.
    if (result.year, result.month) != (year, month):
        raise ValueError(
            f"{year}-{month:02d} has no occurrence number {n} of weekday {weekday}"
        )
    return result


def third_friday(year: int, month: int) -> date:
    """Return the third Friday of ``month``/``year`` (a calendar date).

    This is the conventional effective date of quarterly index rebalances
    (changes take effect after the close).  It is **not** adjusted when the
    day is a holiday -- e.g. 2008-03-21 was both the third Friday of March and
    Good Friday, and the rebalance moved to Thursday 2008-03-20.  Callers that
    need the actual session should wrap the result in
    ``prev_trading_day(..., include=True)``.
    """
    return nth_weekday_of_month(year, month, FRIDAY, 3)


def thursday_before_second_friday(year: int, month: int) -> date:
    """Return the Thursday immediately before the second Friday of the month.

    Index providers anchor the reference date for rebalance data (share
    counts, float factors, reference prices) to the week of the second Friday
    of the rebalance month; the exact weekday differs by index family and has
    been changed by S&P Dow Jones Indices more than once (Tuesday, Wednesday
    and Thursday before the second Friday all appear in its announcements).
    This helper computes the Thursday variant; callers must confirm the anchor
    against the current methodology of the index they model.

    The result is a calendar date between the 7th and the 13th of the month;
    none of the rule-based NYSE holidays can ever fall on it, but an ad-hoc
    closure can (2025-01-09, the Carter day of mourning, was one).  Use
    ``prev_trading_day(..., include=True)`` to land on the actual session.
    """
    return nth_weekday_of_month(year, month, FRIDAY, 2) - _ONE_DAY


# --------------------------------------------------------------------------
# NYSE holidays
# --------------------------------------------------------------------------


def _observed(holiday: date) -> date:
    """Shift a fixed-date holiday that falls on a weekend to its observed day.

    Saturday -> preceding Friday, Sunday -> following Monday.  The caller is
    responsible for the New Year's Day exception (see module docstring).
    """
    wd = holiday.weekday()
    if wd == SATURDAY:
        return holiday - _ONE_DAY
    if wd == SUNDAY:
        return holiday + _ONE_DAY
    return holiday


@lru_cache(maxsize=None)
def _holiday_table(year: int) -> tuple[tuple[date, str], ...]:
    """Return the rule-derived NYSE full-day holidays of ``year`` with names.

    The result is sorted by date and never contains a date outside ``year``:
    a Saturday New Year's Day is simply dropped rather than observed on
    December 31 of the previous year.
    """
    holidays: list[tuple[date, str]] = []

    new_year = date(year, 1, 1)
    if new_year.weekday() != SATURDAY:
        holidays.append((_observed(new_year), "New Year's Day"))

    if year >= MLK_FIRST_YEAR:
        holidays.append(
            (nth_weekday_of_month(year, 1, MONDAY, 3), "Martin Luther King Jr. Day")
        )
    holidays.append((nth_weekday_of_month(year, 2, MONDAY, 3), "Presidents' Day"))
    holidays.append((easter_sunday(year) - timedelta(days=2), "Good Friday"))
    holidays.append((nth_weekday_of_month(year, 5, MONDAY, -1), "Memorial Day"))
    if year >= JUNETEENTH_FIRST_YEAR:
        holidays.append((_observed(date(year, 6, 19)), "Juneteenth"))
    holidays.append((_observed(date(year, 7, 4)), "Independence Day"))
    holidays.append((nth_weekday_of_month(year, 9, MONDAY, 1), "Labor Day"))
    holidays.append((nth_weekday_of_month(year, 11, THURSDAY, 4), "Thanksgiving Day"))
    holidays.append((_observed(date(year, 12, 25)), "Christmas Day"))

    holidays.sort()
    return tuple(holidays)


@lru_cache(maxsize=None)
def _holiday_set(year: int) -> frozenset[date]:
    """Cached frozenset view of :func:`_holiday_table` for fast membership."""
    return frozenset(d for d, _ in _holiday_table(year))


def nyse_holidays(year: int, include_special: bool = False) -> set[date]:
    """Return the NYSE full-day holidays (observed dates) of ``year``.

    Only rule-derived holidays are returned by default, which matches the
    exchange's published annual calendar.  With ``include_special=True`` the
    dates from :data:`SPECIAL_CLOSURES` that fall in ``year`` are added and
    any :data:`SPECIAL_OPENS` dates removed, giving the set of weekdays on
    which the exchange was actually closed.  Weekends are never included.
    A fresh ``set`` is returned on every call; mutating it has no effect.
    """
    holidays = set(_holiday_set(year))
    if include_special:
        # A weekend date registered as a closure is a no-op for trading and is
        # not reported: this view only ever holds weekdays.
        holidays |= {
            d for d in SPECIAL_CLOSURES if d.year == year and d.weekday() < SATURDAY
        }
        holidays -= {d for d in SPECIAL_OPENS if d.year == year}
    return holidays


def nyse_holiday_names(year: int) -> dict[date, str]:
    """Return ``{observed_date: holiday_name}`` for the rule-derived holidays."""
    return dict(_holiday_table(year))


# --------------------------------------------------------------------------
# Trading-day arithmetic
# --------------------------------------------------------------------------


def is_trading_day(d: date) -> bool:
    """Return ``True`` if the NYSE holds a (full or early-close) session on ``d``.

    Precedence: :data:`SPECIAL_CLOSURES` (closed) > :data:`SPECIAL_OPENS`
    (open) > weekend (closed) > rule-derived holiday (closed) > open.
    """
    d = _as_date(d)
    if d in SPECIAL_CLOSURES:
        return False
    if d in SPECIAL_OPENS:
        return True
    if d.weekday() >= SATURDAY:
        return False
    return d not in _holiday_set(d.year)


def next_trading_day(d: date, include: bool = False) -> date:
    """Return the first trading day after ``d``.

    With ``include=True`` the search starts *at* ``d``, so a trading day is
    returned unchanged (useful for "on or after" semantics).
    """
    d = _as_date(d)
    if not include:
        d += _ONE_DAY
    while not is_trading_day(d):
        d += _ONE_DAY
    return d


def prev_trading_day(d: date, include: bool = False) -> date:
    """Return the last trading day before ``d``.

    With ``include=True`` the search starts *at* ``d``, so a trading day is
    returned unchanged (useful for "on or before" semantics).
    """
    d = _as_date(d)
    if not include:
        d -= _ONE_DAY
    while not is_trading_day(d):
        d -= _ONE_DAY
    return d


def last_trading_day_of_month(year: int, month: int) -> date:
    """Return the last trading day of ``month``/``year``."""
    return prev_trading_day(_last_day_of_month(year, month), include=True)


def trading_days_between(a: date, b: date) -> int:
    """Count trading days strictly after ``a`` up to and including ``b``.

    Returns 0 when ``a == b`` and a negative number when ``b < a`` (the
    function is antisymmetric: ``trading_days_between(a, b) ==
    -trading_days_between(b, a)``).  Neither endpoint needs to be a trading
    day; a non-trading ``a`` simply contributes nothing.
    """
    a = _as_date(a)
    b = _as_date(b)
    if b < a:
        return -trading_days_between(b, a)
    count = 0
    d = a + _ONE_DAY
    while d <= b:
        if is_trading_day(d):
            count += 1
        d += _ONE_DAY
    return count


def add_trading_days(d: date, n: int) -> date:
    """Return the date ``n`` trading days away from ``d``.

    Positive ``n`` moves forward, negative ``n`` backward; ``n == 0`` returns
    ``d`` unchanged even when ``d`` is not a trading day.  Counting starts
    from ``d`` exclusive, so from a Saturday ``add_trading_days(d, 1)`` is the
    following Monday and ``add_trading_days(d, -1)`` the preceding Friday.
    For a trading day ``d``, ``trading_days_between(d, add_trading_days(d, n))
    == n`` for every ``n``.
    """
    d = _as_date(d)
    step = _ONE_DAY if n > 0 else -_ONE_DAY
    remaining = abs(n)
    while remaining:
        d += step
        if is_trading_day(d):
            remaining -= 1
    return d
