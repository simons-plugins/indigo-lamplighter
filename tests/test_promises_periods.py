"""Periods and levels (PRD R11, R12; sections 5.5, 5.6).

Periods are sunset/sunrise-relative when asked, may cross midnight, and must
not overlap at any minute of the year. Levels are per device per period, and
"leave" and "off" are separate settings rather than a boolean paired with a
zone-wide mode that only works in one combination.
"""

import datetime as dt
import logging

import pytest
from helpers import FixedSun

from lamplighter.config import ConfigError, load_config
from lamplighter.periods import IndigoSun, active_period, parse_time_expr, resolve

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)

TODAY = dt.date(2026, 9, 4)
SUN = FixedSun(sunrise=dt.time(6, 30), sunset=dt.time(19, 45))


def zone_with(periods, sun=SUN, today=TODAY, **zone):
    """Load a one-zone config and hand back the zone, periods included.

    Built through load_config rather than by constructing Period objects, so
    these promises cover the path a real config actually takes.
    """
    document = {
        "version": 1,
        "zones": [
            {
                "name": "Study",
                "presence_devices": [101],
                "hold_seconds": 300,
                "lux": None,
                "lights": [201],
                "periods": periods,
                **zone,
            }
        ],
    }
    return load_config(document, sun, today).zones[0]


def band(name, start, end, mode="on_and_off", levels=None, **extra):
    return {
        "name": name,
        "from": start,
        "to": end,
        "mode": mode,
        "levels": levels or {"201": 60},
        **extra,
    }


def test_a_sunset_relative_boundary_resolves_against_the_indigo_server(monkeypatch):
    """"sunset-30m" resolves through indigo.server.calculateSunset for the
    date being resolved, not against a fixed clock time.

    Kills: approximate dusk with a hard-coded band -- the Kitchen's 16:00-19:00
    fudge, which is an hour wrong in June.
    """
    import indigo

    midsummer, midwinter = dt.date(2026, 6, 21), dt.date(2026, 12, 21)
    server_sunsets = {midsummer: dt.time(21, 21), midwinter: dt.time(15, 53)}
    asked = []

    def calculate_sunset(date):
        asked.append(date)
        return dt.datetime.combine(date, server_sunsets[date])

    monkeypatch.setattr(indigo.server, "calculateSunset", calculate_sunset)
    sun = IndigoSun(logging.getLogger("test.sun"))
    dusk = parse_time_expr("sunset-30m")

    # Five and a half hours apart: no fixed band can sit in both places.
    assert resolve(dusk, midsummer, sun) == dt.datetime(2026, 6, 21, 20, 51)
    assert resolve(dusk, midwinter, sun) == dt.datetime(2026, 12, 21, 15, 23)

    # Making "never asked" fatal: a hard-coded band would answer without ever
    # consulting the server, and would answer for the wrong date if it did.
    assert asked == [midsummer, midwinter]

    zone = zone_with([band("Dusk", "sunset-30m", "23:00")], sun=sun, today=midwinter)
    assert active_period(zone.periods, dt.datetime(2026, 12, 21, 16, 0), sun) is zone.periods[0]
    assert active_period(zone.periods, dt.datetime(2026, 12, 21, 15, 0), sun) is None


def test_a_sun_boundary_that_cannot_be_resolved_warns_and_falls_back(monkeypatch, caplog):
    """If the sunrise/sunset call fails, the period falls back to a fixed time
    and says so (PRD section 9, R15).

    Kills: swallow the failure and treat the period as absent, which leaves
    the zone silently OFF-DUTY all evening.
    """
    import indigo

    def unavailable(date):
        raise RuntimeError("server not responding")

    monkeypatch.setattr(indigo.server, "calculateSunset", unavailable)
    # Loaded against a working sun, so every warning below comes from the
    # resolution under test rather than from the loader's overlap sampling.
    dusk = zone_with([band("Dusk", "sunset-30m", "23:00")]).periods[0]
    sun = IndigoSun(logging.getLogger("test.sun.fallback"))

    with caplog.at_level(logging.WARNING, logger="test.sun.fallback"):
        # Fallback sunset is 18:00, so the band runs 17:30 to 23:00 and the
        # zone is on duty at 18:00 -- not absent, not silently off duty.
        assert active_period([dusk], dt.datetime(2026, 9, 4, 18, 0), sun) is dusk
        assert active_period([dusk], dt.datetime(2026, 9, 4, 17, 0), sun) is None

        assert caplog.records, "falling back without saying so is the silent half"
        message = caplog.records[0].getMessage()
        assert "sunset" in message and "18:00" in message
        assert "server not responding" in message, "the message must carry the cause"

        # Once per day per boundary: asking again about a date already warned
        # about is silent, and a fresh date warns afresh rather than latching.
        warned_dates = len(caplog.records)
        sun.sunset(dt.date(2026, 9, 4))
        assert len(caplog.records) == warned_dates
        sun.sunset(dt.date(2026, 9, 6))
        assert len(caplog.records) == warned_dates + 1


def test_a_period_that_crosses_midnight_covers_both_sides():
    """"22:30" to "07:00" is active at 23:00 and at 01:00 (R11).

    Kills: compare `from <= now < to` numerically, which makes a wrapping
    period active never rather than twice.
    """
    zone = zone_with([band("Overnight", "22:30", "07:00")])
    overnight = zone.periods[0]

    assert active_period(zone.periods, dt.datetime(2026, 9, 4, 23, 0), SUN) is overnight
    assert active_period(zone.periods, dt.datetime(2026, 9, 5, 1, 0), SUN) is overnight
    assert active_period(zone.periods, dt.datetime(2026, 9, 5, 6, 59), SUN) is overnight

    # The edges stay where the schema says: from inclusive, to exclusive.
    assert active_period(zone.periods, dt.datetime(2026, 9, 4, 22, 30), SUN) is overnight
    assert active_period(zone.periods, dt.datetime(2026, 9, 4, 22, 29), SUN) is None
    assert active_period(zone.periods, dt.datetime(2026, 9, 5, 7, 0), SUN) is None
    assert active_period(zone.periods, dt.datetime(2026, 9, 4, 12, 0), SUN) is None


def test_overlapping_periods_are_rejected_naming_the_pair():
    """Two periods that overlap at any minute of the year are a validation
    error naming both, not first-match-wins (R11).

    Kills: resolve overlaps by ordering. The check must resolve today's and
    tomorrow's instances, because a sun-relative boundary can move a period
    into another one only at certain times of year.
    """
    with pytest.raises(ConfigError) as caught:
        zone_with([band("Early", "18:00", "20:00"), band("Late", "19:00", "21:00")])
    assert "Early" in str(caught.value) and "Late" in str(caught.value)
    assert caught.value.path == "zones/0/periods"
    assert "19:00" in str(caught.value), "and the first minute they share"

    # The seasonal half. This pair is clean today and every day either side
    # of it, and collides in December, when sunset has walked nearly four
    # hours earlier. A check that samples only today and tomorrow accepts it
    # and the zone runs all winter with one of its two bands dead.
    def seasonal_sunset(date):
        return {6: dt.time(21, 30), 12: dt.time(16, 0)}.get(date.month, dt.time(19, 45))

    seasonal = FixedSun(sunset=seasonal_sunset)
    winter_collision = [band("Evening", "19:00", "20:00"), band("Dusk", "sunset+30m", "23:00")]

    # Proof that the pair really is clean today, so the rejection below can
    # only come from having looked at another time of year.
    zone = zone_with([winter_collision[0]], sun=seasonal)
    assert active_period(zone.periods, dt.datetime(2026, 9, 4, 20, 30), seasonal) is None

    with pytest.raises(ConfigError) as caught:
        zone_with(winter_collision, sun=seasonal)
    assert "Evening" in str(caught.value) and "Dusk" in str(caught.value)
    assert "-12-" in str(caught.value), "the offending date is December, not today"


@promise
def test_a_time_covered_by_no_period_leaves_the_zone_off_duty():
    """A gap between periods is legal and means OFF-DUTY: desired is `leave`
    for every device (section 5.3).

    Kills: fall back to the nearest or the last period, which turns a
    deliberate gap into a silently extended band.
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
