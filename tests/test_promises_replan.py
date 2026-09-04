"""Re-plan only on input edges (PRD R4, section 5.2).

A zone re-plans when an INPUT changes: presence on/off, presence last-seen
crossing the hold, lux crossing the hysteresis-widened threshold, a period
boundary, an override starting or ending. Never on a device update as such.

The fork re-planned on every event: an Occupatum zone device ticking its
countdown every 1.2 s produced hundreds of re-plans an hour, reverts within a
second of a manual change, and 10 s of callback lag.
"""

import datetime as dt
import logging

import pytest
from helpers import make_period, make_zone

from lamplighter import compare
from lamplighter.zone import ZoneState

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
LUX = {"device": 302, "dark_below": 2200, "hysteresis": 300}


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def at(**kwargs):
    return NOW + dt.timedelta(**kwargs)


def a_zone(periods=None, **fields):
    fields.setdefault("lights", [201, 202])
    fields.setdefault("presence_devices", [101, 102])
    fields.setdefault("lux", dict(LUX))
    return make_zone(
        periods or [make_period("Evening", "18:00", "23:00", levels={"201": 60, "202": 30})],
        logger=logging.getLogger("test.promises.replan"),
        **fields,
    )


def test_a_presence_device_re_reporting_on_does_not_replan():
    """A presence device that reports "on" again, unchanged, causes no
    re-plan -- the live bug.

    Kills: re-evaluate on any update of a presence device. This is the
    Occupatum countdown tick.

    Mutation applied: Zone.evaluate's `if new_state is previous: return None`
    -> `if False: return None`, which is the fork re-planning every event.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)
    assert zone.evaluate(NOW, "presence edge").to_state is ZoneState.OCCUPIED

    # The Occupatum device, ticking its countdown once a second for two
    # minutes. Every tick refreshes the hold and none of them re-plans.
    replans = 0
    for tick in range(1, 121):
        moment = at(seconds=tick)
        assert zone.ingest_presence(101, True, moment) is True, "the hold must move"
        if zone.evaluate(moment, "presence re-report") is not None:
            replans += 1

    assert replans == 0
    assert zone.state is ZoneState.OCCUPIED
    assert zone.presence.last_seen == at(seconds=120)


@promise
def test_a_display_string_or_timer_update_does_not_replan():
    """An update that touches only a display string, an uptime counter or a
    battery level changes no input and causes no re-plan.

    Kills: treat every deviceUpdated for a zone member as an input edge.

    M1 phase B builds the zone's inputs; deciding WHICH deviceUpdated
    reaches ingest_presence/ingest_lux is the engine's event classification,
    which is not built yet. Left as a stub deliberately.
    """
    raise NotImplementedError


def test_a_genuine_presence_edge_replans_exactly_once():
    """An off->on transition on a presence device re-plans the zone once --
    not zero times, not once per device.

    Kills: over-correcting the tick fix into a gate that also swallows the
    real transition.

    Mutation applied: Zone.ingest_presence's `return bool(self.presence.update(
    device_id, is_on, now))` -> a gate that drops every "on" once anything has
    ever been seen.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)

    # The first genuine edge: exactly one transition, then silence.
    assert zone.ingest_presence(101, True, NOW) is True
    assert zone.evaluate(NOW, "presence edge").to_state is ZoneState.OCCUPIED
    assert zone.evaluate(at(seconds=1), "presence edge") is None

    # A second device joining is not a second re-plan.
    assert zone.ingest_presence(102, True, at(seconds=2)) is True
    assert zone.evaluate(at(seconds=2), "presence edge") is None

    # The room empties.
    zone.ingest_presence(101, False, at(seconds=10))
    zone.ingest_presence(102, False, at(seconds=10))
    # The hold runs from the last sighting at second 2, so it expires at 302.
    assert zone.evaluate(at(seconds=301), "reconcile tick") is None
    assert zone.evaluate(at(seconds=302), "presence hold expired").to_state is ZoneState.VACANT

    # ...and somebody comes back. This is the transition an over-correction
    # swallows: the device has reported before, so a gate keyed on "have we
    # ever seen this" or "has this device been on" drops it.
    assert zone.ingest_presence(101, True, at(seconds=400)) is True
    move = zone.evaluate(at(seconds=400), "presence edge")
    assert move is not None, "the real transition must not be swallowed"
    assert (move.from_state, move.to_state) == (ZoneState.VACANT, ZoneState.OCCUPIED)
    assert zone.evaluate(at(seconds=401), "presence edge") is None


def test_a_lux_reading_that_stays_inside_the_band_does_not_replan():
    """A lux reading that moves without crossing the hysteresis-widened
    threshold changes no input (R9).

    Kills: re-plan on any change of the lux value. A sensor reporting every
    few seconds would then re-plan as often as the fork did.

    Mutation applied: Lux.dark's `self.changed = previous is not None and
    verdict != previous` -> `self.changed = True`.
    """
    zone = a_zone()
    zone.ingest_presence(101, True, NOW)
    zone.ingest_lux(1800, NOW)
    assert zone.evaluate(NOW, "startup").to_state is ZoneState.OCCUPIED
    assert zone.lux.verdict is True, "the sensor is live, not inert"

    # Readings either side of the threshold but inside the band, in the shape
    # a kitchen sensor actually produces: the zone's own lights lift it over
    # dark_below and it drifts back down again.
    for second, value in enumerate((1700, 1500, 2100, 2199, 2200, 2400, 2499, 1900), start=1):
        assert zone.ingest_lux(value, at(seconds=second)) is False, value
        assert zone.evaluate(at(seconds=second), "lux reading") is None
    assert zone.state is ZoneState.OCCUPIED


def test_a_lux_reading_that_crosses_the_threshold_replans():
    """Crossing dark/bright is an input edge and re-plans the zone.

    Kills: latch dark forever after the first reading -- the fork's mutation,
    which made a zone that went dark once never come back.

    Mutation applied: Lux.dark's `verdict = False` in the leaving-dark branch
    -> `verdict = bool(previous)`, which latches.
    """
    zone = a_zone()
    zone.ingest_presence(101, True, NOW)
    zone.ingest_lux(1800, NOW)
    assert zone.evaluate(NOW, "startup").to_state is ZoneState.OCCUPIED

    # Inside the band on the way up: not yet an edge.
    assert zone.ingest_lux(2400, at(seconds=10)) is False
    assert zone.evaluate(at(seconds=10), "lux reading") is None

    # Clear of the band: the zone comes off duty. This is the assertion the
    # latch mutation fails -- under it the verdict never leaves dark.
    assert zone.ingest_lux(2500, at(seconds=20)) is True
    assert zone.evaluate(at(seconds=20), "lux edge").to_state is ZoneState.OFF_DUTY

    # And back down through dark_below: dark again, and on duty again.
    assert zone.ingest_lux(2199, at(seconds=30)) is True
    zone.ingest_presence(101, True, at(seconds=30))
    assert zone.evaluate(at(seconds=30), "lux edge").to_state is ZoneState.OCCUPIED


def test_a_period_boundary_replans_with_no_device_event_at_all():
    """The zone re-plans when a period starts or ends, driven by the worker's
    timer heap, with nothing on the device bus.

    Kills: hang re-planning off device events only, so a zone whose room is
    empty and quiet never notices the period changed.

    The heap itself is the worker's, and the worker is not built at M1. What
    the zone owes it is next_wake(), and that is what is asserted here: the
    boundary is offered as a wake-up, and evaluating at it moves the state
    with every Indigo lookup made fatal.

    Mutation applied: Zone.next_wake's `boundary = periods_module.next_boundary(
    self.config.periods, now, self.sun)` -> `boundary = None`.
    """
    zone = a_zone(
        [make_period("Evening", "18:00", "21:00", levels={"201": 60, "202": 30})],
        hold_seconds=7200,  # so the hold is NOT the next thing to happen
    )
    zone.ingest_presence(101, True, NOW)
    zone.ingest_lux(1800, NOW)
    assert zone.evaluate(NOW, "startup").to_state is ZoneState.OCCUPIED

    boundary = dt.datetime(2026, 9, 4, 21, 0)
    assert zone.next_wake(NOW) == boundary

    # From here on, touching Indigo at all is fatal: whatever wakes the zone
    # at a period boundary, it is not a device.
    import indigo

    class Fatal(dict):
        def __getitem__(self, key):
            raise AssertionError(
                "the zone read an Indigo object; a period boundary must need none"
            )

    original_devices, original_variables = indigo.devices, indigo.variables
    indigo.devices, indigo.variables = Fatal(), Fatal()
    try:
        assert zone.evaluate(boundary - dt.timedelta(seconds=1), "reconcile tick") is None
        move = zone.evaluate(boundary, "period boundary")
    finally:
        indigo.devices, indigo.variables = original_devices, original_variables

    assert (move.from_state, move.to_state) == (ZoneState.OCCUPIED, ZoneState.OFF_DUTY)
    assert move.cause == "period boundary"
    assert zone.desired_levels(boundary) == {201: "leave", 202: "leave"}


@promise
def test_the_edge_gate_reads_the_before_and_after_snapshots():
    """The gate compares the two snapshots Indigo hands the callback, in both
    directions.

    Kills: gate on the set of changed keys instead. The keys tell you what
    Indigo decided to report, not whether the value the zone reads actually
    moved -- a zone can be woken by a key it does not care about and left
    asleep through a change carried in a key it does.

    M1 phase B builds the zone's inputs; the gate that turns an origDev /
    newDev pair into one of those inputs is the engine's event
    classification, which is not built yet. Left as a stub deliberately.
    """
    raise NotImplementedError
