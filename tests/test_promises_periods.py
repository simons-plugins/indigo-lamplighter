"""Periods and levels (PRD R11, R12; sections 5.5, 5.6).

Periods are sunset/sunrise-relative when asked, may cross midnight, and must
not overlap at any minute of the year. Levels are per device per period, and
"leave" and "off" are separate settings rather than a boolean paired with a
zone-wide mode that only works in one combination.
"""

import pytest

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)


@promise
def test_a_sunset_relative_boundary_resolves_against_the_indigo_server():
    """"sunset-30m" resolves through indigo.server.calculateSunset for the
    date being resolved, not against a fixed clock time.

    Kills: approximate dusk with a hard-coded band -- the Kitchen's 16:00-19:00
    fudge, which is an hour wrong in June.
    """
    raise NotImplementedError


@promise
def test_a_sun_boundary_that_cannot_be_resolved_warns_and_falls_back():
    """If the sunrise/sunset call fails, the period falls back to a fixed time
    and says so (PRD section 9, R15).

    Kills: swallow the failure and treat the period as absent, which leaves
    the zone silently OFF-DUTY all evening.
    """
    raise NotImplementedError


@promise
def test_a_period_that_crosses_midnight_covers_both_sides():
    """"22:30" to "07:00" is active at 23:00 and at 01:00 (R11).

    Kills: compare `from <= now < to` numerically, which makes a wrapping
    period active never rather than twice.
    """
    raise NotImplementedError


@promise
def test_overlapping_periods_are_rejected_naming_the_pair():
    """Two periods that overlap at any minute of the year are a validation
    error naming both, not first-match-wins (R11).

    Kills: resolve overlaps by ordering. The check must resolve today's and
    tomorrow's instances, because a sun-relative boundary can move a period
    into another one only at certain times of year.
    """
    raise NotImplementedError


@promise
def test_a_time_covered_by_no_period_leaves_the_zone_off_duty():
    """A gap between periods is legal and means OFF-DUTY: desired is `leave`
    for every device (section 5.3).

    Kills: fall back to the nearest or last period, which turns a deliberate
    gap into a silently extended band.
    """
    raise NotImplementedError


@promise
def test_off_only_never_turns_a_light_on():
    """In `off_only` the zone turns lights off when the room empties and never
    turns one on, whatever presence does (R11).

    Kills: implement the mode as a plan-time filter that still writes the
    period's levels in OCCUPIED. One hard-off band must express what the fork
    needed two periods to say.
    """
    raise NotImplementedError


@promise
def test_leave_means_the_device_is_never_written_in_that_period():
    """A device set to `leave` is not written in any state -- not on, not off,
    not at the reconcile tick (R12).

    Kills: treat `leave` as "no target" and then let the reconcile pass drive
    it to off because it has no desired level.
    """
    raise NotImplementedError


@promise
def test_a_light_absent_from_levels_is_treated_as_leave():
    """A light in the zone's `lights` with no entry in this period's `levels`
    behaves exactly as if it were set to `leave`.

    Kills: default a missing level to off, which makes every unlisted light a
    light the zone turns off.
    """
    raise NotImplementedError


@promise
def test_off_forces_the_light_off_in_vacant_and_off_duty():
    """`off` is a force-off, distinct from `leave` (R12).

    Kills: collapse `off` and `leave` into one falsy value -- the fork's
    device_period_map false paired with off_lights_behavior, which only did
    the right thing in one combination.
    """
    raise NotImplementedError


@promise
def test_limit_caps_every_device_including_lux_adjusted_ones():
    """A period's `limit` caps every device's level, after any lux adjustment.

    Kills: apply the cap only inside the adjust-by-lux branch -- the fork's
    limit_brightness bug, where the cap silently did nothing for any device
    with an explicit level.
    """
    raise NotImplementedError


@promise
def test_a_period_override_block_replaces_the_zones_timing_while_active():
    """A period's `override` block replaces the zone's `duration_minutes` and
    `extend_minutes` while that period is active (PRD section 11, decision 4).

    Kills: merge the two blocks field by field, so a period that names only a
    longer duration silently keeps the zone's extension and the timing depends
    on which fields were written.
    """
    raise NotImplementedError
