"""Periods and levels (PRD R11, R12; sections 5.5, 5.6).

Periods are sunset/sunrise-relative when asked, may cross midnight, and must
not overlap at any minute of the year. Levels are per device per period, and
"leave" and "off" are separate settings rather than a boolean paired with a
zone-wide mode that only works in one combination.
"""

import datetime as dt
import logging

import pytest
from helpers import FixedSun, make_zone

from lamplighter import compare
from lamplighter.config import ConfigError, load_config
from lamplighter.periods import IndigoSun, active_period, parse_time_expr, resolve
from lamplighter.zone import ZoneState

TODAY = dt.date(2026, 9, 4)
SUN = FixedSun(sunrise=dt.time(6, 30), sunset=dt.time(19, 45))

# 20:00 on TODAY: after the fixed sun's 19:45 sunset, inside every evening
# band below, and far from any boundary that could move underneath a test.
EVENING = dt.datetime(2026, 9, 4, 20, 0, 0)
LUX = {"device": 302, "dark_below": 2200, "hysteresis": 300}


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


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


def live_zone(periods, sun=SUN, today=TODAY, **fields):
    """The same one-zone config as zone_with(), but as a running Zone.

    The planner promises below are about what a zone WANTS, so they need the
    object that decides it. Two lights, because "leave" and "off" are only
    distinguishable when something else in the room is being written.
    """
    fields.setdefault("lights", [201, 202])
    return make_zone(
        periods,
        sun=sun,
        today=today,
        logger=logging.getLogger("test.promises.periods"),
        **fields,
    )


def occupied(zone, now=EVENING, lux=1800):
    """Drive `zone` to OCCUPIED and assert it got there."""
    zone.ingest_lux(lux, now)
    zone.ingest_presence(101, True, now)
    zone.evaluate(now, "setup")
    assert zone.state is ZoneState.OCCUPIED
    return zone


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


def test_a_time_covered_by_no_period_leaves_the_zone_off_duty():
    """A gap between periods is legal and means OFF-DUTY: desired is `leave`
    for every device (section 5.3).

    Kills: fall back to the nearest or the last period, which turns a
    deliberate gap into a silently extended band.

    Mutation applied: periods.active_period's closing `return None` ->
    `return periods[-1] if periods else None`.
    """
    zone = live_zone(
        [
            band("Dusk", "18:00", "20:00", levels={"201": 60, "202": 30}),
            band("Night", "21:00", "23:00", levels={"201": 10, "202": 10}),
        ]
    )
    inside, gap, after = (
        dt.datetime(2026, 9, 4, 19, 0),
        dt.datetime(2026, 9, 4, 20, 30),
        dt.datetime(2026, 9, 4, 21, 0),
    )

    # Inside the first band, with the room occupied: on duty and driven.
    zone.ingest_presence(101, True, inside)
    assert zone.evaluate(inside, "presence edge").to_state is ZoneState.OCCUPIED
    assert zone.desired_levels(inside) == {201: 60, 202: 30}

    # In the gap, with presence still active and the room still dark, so
    # nothing except the gap can be what takes the zone off duty.
    zone.ingest_presence(101, True, gap)
    assert zone.active_period(gap) is None
    assert zone.evaluate(gap, "period boundary").to_state is ZoneState.OFF_DUTY
    assert zone.desired_levels(gap) == {201: "leave", 202: "leave"}

    # And out the far side: the gap really is a gap between two live bands.
    zone.ingest_presence(101, True, after)
    assert zone.evaluate(after, "period boundary").to_state is ZoneState.OCCUPIED
    assert zone.desired_levels(after) == {201: 10, 202: 10}


def test_off_only_never_turns_a_light_on():
    """In `off_only` the zone turns lights off when the room empties and never
    turns one on, whatever presence does (R11).

    Kills: implement the mode as a plan-time filter that still writes the
    period's levels in OCCUPIED. One hard-off band must express what the fork
    needed two periods to say.

    Mutation applied: Zone.desired_levels's OCCUPIED guard
    `if period.mode == "off_only":` -> `if False:`.
    """
    hard_off = [band("Night", "18:00", "23:00", mode="off_only", levels={"201": 60, "202": "off"})]

    # Occupied: nothing is turned on, and nothing is turned off either -- a
    # light somebody switched on while they are standing there is theirs.
    zone = occupied(live_zone(hard_off))
    assert zone.desired_levels(EVENING) == {201: "leave", 202: "leave"}

    # Empty: everything with a level goes off. That is the whole mode.
    assert zone.evaluate(EVENING + dt.timedelta(seconds=300), "hold expiry").to_state is (
        ZoneState.VACANT
    )
    assert zone.desired_levels(EVENING) == {201: "off", 202: "off"}

    # The same band written as on_and_off DOES turn the room on, so the
    # assertion above is about the mode and not about the levels.
    on_and_off = live_zone(
        [band("Night", "18:00", "23:00", levels={"201": 60, "202": "off"})]
    )
    assert occupied(on_and_off).desired_levels(EVENING) == {201: 60, 202: "off"}


def test_leave_means_the_device_is_never_written_in_that_period():
    """A device set to `leave` is not written in any state -- not on, not off,
    not at the reconcile tick (R12).

    Kills: treat `leave` as "no target" and then let the reconcile pass drive
    it to off because it has no desired level.

    The reconcile pass writes only what desired_levels() asks for, so the
    guarantee is proved here across all four states; the writer's half of it
    is pinned by tests/test_promises_reconcile.py, still a stub at M1.

    Mutation applied: Zone._all_off's `if period.levels.get(light, LEAVE) !=
    LEAVE:` -> `if True:`.
    """
    zone = live_zone([band("Evening", "18:00", "23:00", levels={"201": 60, "202": "leave"})])

    # OFF-DUTY, before anything has happened.
    assert zone.state is ZoneState.OFF_DUTY
    assert zone.desired_levels(EVENING)[202] == "leave"

    # OCCUPIED: the other light is driven, this one is not.
    occupied(zone)
    assert zone.desired_levels(EVENING) == {201: 60, 202: "leave"}

    # OVERRIDDEN.
    zone.start_override(201, EVENING)
    zone.evaluate(EVENING, "override started")
    assert zone.state is ZoneState.OVERRIDDEN
    assert zone.desired_levels(EVENING)[202] == "leave"
    zone.end_override("test", EVENING)

    # VACANT -- the state that turns lights off, and the one the mutation
    # gets wrong: 201 goes off and 202 is still not touched.
    empty = EVENING + dt.timedelta(seconds=300)
    assert zone.evaluate(empty, "hold expiry").to_state is ZoneState.VACANT
    assert zone.desired_levels(empty) == {201: "off", 202: "leave"}


def test_a_light_absent_from_levels_is_treated_as_leave():
    """A light in the zone's `lights` with no entry in this period's `levels`
    behaves exactly as if it were set to `leave`.

    Kills: default a missing level to off, which makes every unlisted light a
    light the zone turns off.

    Mutation applied: both of Zone's `period.levels.get(light, LEAVE)` ->
    `period.levels.get(light, OFF)`.
    """
    absent = live_zone([band("Evening", "18:00", "23:00", levels={"201": 60})])
    written = live_zone(
        [band("Evening", "18:00", "23:00", levels={"201": 60, "202": "leave"})]
    )

    for state, now in (("occupied", EVENING), ("vacant", EVENING + dt.timedelta(seconds=300))):
        occupied(absent)
        occupied(written)
        if state == "vacant":
            absent.evaluate(now, "hold expiry")
            written.evaluate(now, "hold expiry")
        assert absent.state is written.state
        assert absent.desired_levels(now) == written.desired_levels(now), state
        assert absent.desired_levels(now)[202] == "leave"


def test_off_forces_the_light_off_in_vacant_and_off_duty():
    """`off` is a force-off, distinct from `leave` (R12).

    Kills: collapse `off` and `leave` into one falsy value -- the fork's
    device_period_map false paired with off_lights_behavior, which only did
    the right thing in one combination.

    OFF-DUTY here means OFF-DUTY *because the room is bright*, which section
    5.3 (amended 2026-09-04) makes identical to VACANT. The other two causes
    write nothing at all and are pinned separately in tests/test_zone.py; the
    mutation below is the one that erases the distinction between them.

    Mutations applied, one at a time: Zone._all_off's `if
    period.levels.get(light, LEAVE) != LEAVE:` -> `if period.levels.get(light,
    LEAVE) not in (LEAVE, OFF):` (off and leave collapsed), and
    Zone.desired_levels's `if self._off_duty == OFF_DUTY_BRIGHT:` ->
    `if self._off_duty == OFF_DUTY_NO_PERIOD:` (bright treated like
    no_period, so a room that brightens keeps its lights on).
    """
    zone = live_zone(
        [band("Evening", "18:00", "23:00", levels={"201": "off", "202": "leave"})],
        lux=dict(LUX),
    )

    # OCCUPIED: `off` is held off, `leave` is untouched. Two settings, two
    # different answers, in the state where a falsy collapse looks the same.
    occupied(zone)
    assert zone.desired_levels(EVENING) == {201: "off", 202: "leave"}

    # VACANT: `off` is still off and `leave` is still not written.
    empty = EVENING + dt.timedelta(seconds=300)
    assert zone.evaluate(empty, "hold expiry").to_state is ZoneState.VACANT
    assert zone.desired_levels(empty) == {201: "off", 202: "leave"}

    # OFF-DUTY because the room is bright: the same answer as VACANT, and
    # the `off`/`leave` distinction has to survive the change of state.
    bright = live_zone(
        [band("Evening", "18:00", "23:00", levels={"201": "off", "202": "leave"})],
        lux=dict(LUX),
    )
    occupied(bright)
    bright.ingest_lux(30000, EVENING)  # broad daylight
    assert bright.evaluate(EVENING, "lux edge").to_state is ZoneState.OFF_DUTY
    assert bright.off_duty_cause == "bright"
    assert bright.desired_levels(EVENING) == {201: "off", 202: "leave"}

    # A light with a LEVEL, not just a force-off, goes off too when the room
    # brightens -- that is what makes `bright` VACANT's plan rather than a
    # special case for force-off lights.
    levelled = live_zone(
        [band("Evening", "18:00", "23:00", levels={"201": 60, "202": "leave"})],
        lux=dict(LUX),
    )
    occupied(levelled)
    levelled.ingest_lux(30000, EVENING)
    levelled.evaluate(EVENING, "lux edge")
    assert levelled.desired_levels(EVENING) == {201: "off", 202: "leave"}

    # An off_only band answers the same way, because the cause decides the
    # plan and the mode no longer needs its own OFF-DUTY exception.
    garden = live_zone(
        [band("Night", "18:00", "23:00", mode="off_only", levels={"201": "off", "202": "leave"})],
        lux=dict(LUX),
    )
    occupied(garden)
    garden.ingest_lux(30000, EVENING)
    garden.evaluate(EVENING, "lux edge")
    assert garden.off_duty_cause == "bright"
    assert garden.desired_levels(EVENING) == {201: "off", 202: "leave"}


def test_limit_caps_every_device_including_lux_adjusted_ones():
    """A period's `limit` caps every device's level, after any lux adjustment.

    Kills: apply the cap only inside the adjust-by-lux branch -- the fork's
    limit_brightness bug, where the cap silently did nothing for any device
    with an explicit level.

    The lux-adjusted half cannot be measured at v1 because adjust_by_lux is
    not implemented. What is asserted instead is that it cannot be silently
    skipped: the loader refuses the flag outright on a zone with a lux block
    (PRD section 5.6) rather than returning uncapped, unadjusted levels that
    look correct.

    Mutation applied: zone._capped's `if limit is None:` -> `if limit is None
    or True:`.
    """
    zone = occupied(
        live_zone(
            [band("Evening", "18:00", "23:00", levels={"201": 100, "202": "on"}, limit=60)]
        )
    )
    assert zone.desired_levels(EVENING) == {201: 60, 202: 60}

    # A level already under the cap is left where it is.
    under = occupied(
        live_zone([band("Evening", "18:00", "23:00", levels={"201": 20, "202": 30}, limit=60)])
    )
    assert under.desired_levels(EVENING) == {201: 20, 202: 30}

    # The flag the cap is meant to apply after is refused, not ignored: the
    # file does not load, so no zone ever runs with an unhonoured flag.
    with pytest.raises(ConfigError) as caught:
        live_zone(
            [
                band(
                    "Evening",
                    "18:00",
                    "23:00",
                    levels={"201": 100, "202": "on"},
                    limit=60,
                    adjust_by_lux=True,
                )
            ],
            lux=dict(LUX),
        )
    assert caught.value.path == "zones/0/periods/0/adjust_by_lux"
    assert "not implemented in this version" in str(caught.value)


def test_a_period_override_block_replaces_the_zones_timing_while_active():
    """A period's `override` block replaces the zone's `duration_minutes` and
    `extend_minutes` while that period is active (PRD section 11, decision 4).

    Kills: merge the two blocks field by field, so a period that names only a
    longer duration silently keeps the zone's extension and the timing depends
    on which fields were written.

    Mutation applied: Zone.override_timing's `timing = period.override` ->
    `return period.override.duration_minutes, timing.extend_minutes`.
    """
    dining = live_zone(
        [
            band(
                "Meal",
                "18:00",
                "21:00",
                levels={"201": 60, "202": 30},
                override={"duration_minutes": 120, "extend_minutes": 45},
            ),
            band("Late", "21:00", "23:00", levels={"201": 10, "202": 10}),
        ],
        override={"duration_minutes": 60, "extend_minutes": 5, "unlock_on_leave": False},
    )
    occupied(dining)

    # Both fields come from the period. A merge would keep the zone's 5.
    assert dining.override_timing(EVENING) == (120, 45)
    override = dining.start_override(201, EVENING)
    assert dining.evaluate(EVENING, "override started").to_state is ZoneState.OVERRIDDEN
    assert override.duration_minutes == 120
    assert override.extend_minutes == 45
    assert override.expires_at == EVENING + dt.timedelta(minutes=120)

    # Outside the band the zone's own timing is what applies, unmerged.
    assert dining.override_timing(dt.datetime(2026, 9, 4, 22, 0)) == (60, 5)

    # And the extension actually used at expiry is the period's 45, not the
    # zone's 5: the copy taken at creation is what runs.
    expiry = EVENING + dt.timedelta(minutes=120)
    dining.ingest_presence(101, True, expiry - dt.timedelta(minutes=1))
    assert dining.evaluate(expiry, "override expiry") is None
    assert dining.override.expires_at == expiry + dt.timedelta(minutes=45)
