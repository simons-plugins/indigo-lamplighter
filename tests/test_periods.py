"""Unit tests for time expressions and period windows (PRD R11; section 5.5).

The acceptance promises in tests/test_promises_periods.py are the contract;
these are the arithmetic underneath it -- offset parsing, the midnight rule,
inclusive/exclusive edges -- pinned where they are cheap to read.
"""

import datetime as dt

import pytest
from helpers import FixedSun

from lamplighter.periods import (
    ConfigError,
    Period,
    TimeExpr,
    active_period,
    check_overlaps,
    next_boundary,
    parse_time_expr,
    period_window,
    resolve,
)

TODAY = dt.date(2026, 9, 4)
SUN = FixedSun(sunrise=dt.time(6, 30), sunset=dt.time(19, 45))


def band(name, start, end, mode="on_and_off", **kwargs):
    return Period(
        name=name,
        start=parse_time_expr(start),
        end=parse_time_expr(end),
        mode=mode,
        **kwargs,
    )


# ------------------------------------------------------- parse_time_expr


@pytest.mark.parametrize(
    "text,expected",
    [
        ("00:00", TimeExpr("clock", minutes=0, text="00:00")),
        ("06:30", TimeExpr("clock", minutes=390, text="06:30")),
        ("23:59", TimeExpr("clock", minutes=1439, text="23:59")),
        ("sunrise", TimeExpr("sunrise", text="sunrise")),
        ("sunset", TimeExpr("sunset", text="sunset")),
        ("sunset-30m", TimeExpr("sunset", offset_minutes=-30, text="sunset-30m")),
        ("sunset+45m", TimeExpr("sunset", offset_minutes=45, text="sunset+45m")),
        ("sunset-1h", TimeExpr("sunset", offset_minutes=-60, text="sunset-1h")),
        ("sunrise+1h30m", TimeExpr("sunrise", offset_minutes=90, text="sunrise+1h30m")),
        ("sunset-2h15m", TimeExpr("sunset", offset_minutes=-135, text="sunset-2h15m")),
        ("sunset-120m", TimeExpr("sunset", offset_minutes=-120, text="sunset-120m")),
    ],
)
def test_every_documented_form_parses_to_its_meaning(text, expected):
    assert parse_time_expr(text) == expected


@pytest.mark.parametrize(
    "text", ["", "7:30", "24:00", "noon", "sunset+30", "sunset-1h5", "SUNSET-30m", None, 630]
)
def test_a_thing_that_is_not_a_time_expression_names_itself(text):
    with pytest.raises(ValueError) as caught:
        parse_time_expr(text)
    assert repr(text) in str(caught.value)


# ---------------------------------------------------------------- resolve


def test_a_clock_expression_resolves_to_that_wall_time():
    assert resolve(parse_time_expr("19:00"), TODAY, SUN) == dt.datetime(2026, 9, 4, 19, 0)


def test_a_sun_offset_is_arithmetic_on_the_servers_answer():
    assert resolve(parse_time_expr("sunset"), TODAY, SUN) == dt.datetime(2026, 9, 4, 19, 45)
    assert resolve(parse_time_expr("sunset-1h"), TODAY, SUN) == dt.datetime(2026, 9, 4, 18, 45)
    assert resolve(parse_time_expr("sunset+45m"), TODAY, SUN) == dt.datetime(2026, 9, 4, 20, 30)
    assert resolve(parse_time_expr("sunrise+1h30m"), TODAY, SUN) == dt.datetime(2026, 9, 4, 8, 0)


def test_an_offset_may_cross_midnight_on_its_own():
    """A big enough offset lands on the next day, and says so as a date."""
    assert resolve(parse_time_expr("sunset+5h"), TODAY, SUN) == dt.datetime(2026, 9, 5, 0, 45)


def test_resolved_times_are_naive_local_datetimes():
    """The IOM hands back naive local datetimes and everything here compares
    against them; one aware value would raise on the first comparison."""
    for text in ("19:00", "sunset", "sunrise-30m"):
        assert resolve(parse_time_expr(text), TODAY, SUN).tzinfo is None


def test_the_arithmetic_is_wall_clock_across_a_dst_change():
    """Naive datetimes mean a band is what it says on the clock face.

    2026-03-29 is a 23-hour day in this timezone. Lamplighter does not model
    that: "22:00 to 06:00" is 8 clock hours on every date, which is what a
    config author writing period ladders means and what Indigo's own
    sunrise/sunset datetimes are expressed in.
    """
    spring_forward = dt.date(2026, 3, 29)
    start, end = period_window(band("Overnight", "22:00", "06:00"), spring_forward, SUN)
    assert start == dt.datetime(2026, 3, 29, 22, 0)
    assert end - start == dt.timedelta(hours=8)


# ----------------------------------------------------------- period_window


def test_a_plain_window_is_the_same_day():
    start, end = period_window(band("Evening", "19:00", "22:30"), TODAY, SUN)
    assert (start, end) == (dt.datetime(2026, 9, 4, 19, 0), dt.datetime(2026, 9, 4, 22, 30))


def test_an_end_before_the_start_runs_into_the_next_day():
    start, end = period_window(band("Overnight", "22:30", "07:00"), TODAY, SUN)
    assert (start, end) == (dt.datetime(2026, 9, 4, 22, 30), dt.datetime(2026, 9, 5, 7, 0))


def test_to_midnight_means_the_end_of_the_day_not_the_start():
    """The schema's words: '00:00' as 'to' is midnight at the END."""
    start, end = period_window(band("Dusk", "sunset-30m", "00:00"), TODAY, SUN)
    assert start == dt.datetime(2026, 9, 4, 19, 15)
    assert end == dt.datetime(2026, 9, 5, 0, 0)


def test_midnight_to_midnight_is_a_whole_day():
    start, end = period_window(band("All day", "00:00", "00:00"), TODAY, SUN)
    assert (start, end) == (dt.datetime(2026, 9, 4, 0, 0), dt.datetime(2026, 9, 5, 0, 0))


# ----------------------------------------------------------- active_period


def test_from_is_inclusive_and_to_is_exclusive():
    """So one band may end at 19:00 and the next begin at 19:00."""
    early = band("Early", "17:00", "19:00")
    late = band("Late", "19:00", "22:00")
    periods = [early, late]

    assert active_period(periods, dt.datetime(2026, 9, 4, 17, 0), SUN) is early
    assert active_period(periods, dt.datetime(2026, 9, 4, 18, 59), SUN) is early
    assert active_period(periods, dt.datetime(2026, 9, 4, 19, 0), SUN) is late
    assert active_period(periods, dt.datetime(2026, 9, 4, 21, 59), SUN) is late
    assert active_period(periods, dt.datetime(2026, 9, 4, 22, 0), SUN) is None


def test_a_gap_between_periods_has_no_active_period():
    """Kills: fall back to the nearest or the last period, which turns a
    deliberate gap into a silently extended band.

    The desired-levels half of this promise (OFF-DUTY with cause `no_period`
    means `leave` for every device) belongs to the planner and lives in
    tests/test_promises_periods.py.
    """
    periods = [band("Morning", "06:00", "09:00"), band("Evening", "19:00", "22:00")]
    assert active_period(periods, dt.datetime(2026, 9, 4, 13, 0), SUN) is None
    assert active_period(periods, dt.datetime(2026, 9, 4, 9, 0), SUN) is None


def test_a_zone_with_no_periods_at_all_is_never_active():
    assert active_period([], dt.datetime(2026, 9, 4, 20, 0), SUN) is None


# ----------------------------------------------------------- next_boundary


def test_the_next_boundary_is_the_next_edge_of_any_period():
    periods = [band("Evening", "19:00", "22:30"), band("Overnight", "22:30", "07:00")]
    at = dt.datetime(2026, 9, 4, 20, 0)
    assert next_boundary(periods, at, SUN) == dt.datetime(2026, 9, 4, 22, 30)


def test_the_next_boundary_after_the_last_edge_of_today_is_tomorrows():
    periods = [band("Evening", "19:00", "22:30")]
    at = dt.datetime(2026, 9, 4, 23, 0)
    assert next_boundary(periods, at, SUN) == dt.datetime(2026, 9, 5, 19, 0)


def test_a_boundary_exactly_now_is_not_the_next_one():
    """Strictly after, or the worker wakes up in a loop on the same edge."""
    periods = [band("Evening", "19:00", "22:30")]
    assert next_boundary(periods, dt.datetime(2026, 9, 4, 19, 0), SUN) == dt.datetime(
        2026, 9, 4, 22, 30
    )


def test_a_zone_with_no_periods_has_no_next_boundary():
    assert next_boundary([], dt.datetime(2026, 9, 4, 20, 0), SUN) is None


# ---------------------------------------------------------- check_overlaps


def test_bands_that_touch_do_not_overlap():
    periods = [band("Early", "17:00", "19:00"), band("Late", "19:00", "22:00")]
    check_overlaps(periods, SUN, [TODAY])  # must not raise


def test_a_wrapping_band_is_compared_against_the_next_days_instances():
    """"22:00 to 06:00" and "00:00 to 06:00" never overlap on one day's
    arithmetic, and collide every single night."""
    periods = [band("Overnight", "22:00", "06:00"), band("Small hours", "00:00", "06:00")]
    with pytest.raises(ConfigError) as caught:
        check_overlaps(periods, SUN, [TODAY])
    assert "Overnight" in str(caught.value) and "Small hours" in str(caught.value)


def test_the_overlap_error_carries_the_path_it_was_given():
    periods = [band("A", "18:00", "20:00"), band("B", "19:00", "21:00")]
    with pytest.raises(ConfigError) as caught:
        check_overlaps(periods, SUN, [TODAY], path="zones/3/periods")
    assert caught.value.path == "zones/3/periods"
    assert "zones/3/periods" in str(caught.value)


def test_the_overlap_error_names_the_first_shared_minute():
    periods = [band("A", "18:00", "20:00"), band("B", "19:00", "21:00")]
    with pytest.raises(ConfigError) as caught:
        check_overlaps(periods, SUN, [TODAY])
    assert "2026-09-04 19:00" in str(caught.value)


def test_one_period_alone_can_never_overlap():
    check_overlaps([band("All day", "00:00", "00:00")], SUN, [TODAY])
