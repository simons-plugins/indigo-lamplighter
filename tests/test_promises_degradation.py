"""Degradation paths (PRD R8, R15; PRD section 11 decision 3).

Every degradation says so. An unusable precondition is a failed call, not an
empty result, and a warning is emitted once per condition per device rather
than on every pass. The workspace convention this follows is in the root
CLAUDE.md: a tool that returns an honest-looking answer because it could not
do its job is the bug class a green suite hides.
"""

import datetime as dt
import logging

import pytest
from helpers import make_device, make_period, make_zone

from lamplighter import compare
from lamplighter.devices import DeviceGone, LookupFailed
from lamplighter.zone import ZoneState

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
LOGGER = "test.promises.degradation"


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def at(**kwargs):
    return NOW + dt.timedelta(**kwargs)


def a_zone(when_unreadable="dark", **fields):
    fields.setdefault("lights", [201, 202])
    fields.setdefault(
        "lux",
        {
            "device": 302,
            "dark_below": 2200,
            "hysteresis": 300,
            "when_unreadable": when_unreadable,
        },
    )
    return make_zone(
        [make_period("Evening", "18:00", "23:00", levels={"201": 60, "202": 30})],
        logger=logging.getLogger(LOGGER),
        **fields,
    )


def test_an_unreadable_lux_sensor_makes_the_zone_dark_and_warns_once(caplog):
    """With `when_unreadable: "dark"` (the default) the zone treats an
    unreadable lux device as dark, keeps following presence, and warns once
    (decision 3).

    Kills: treat an unreadable sensor as "not dark", which is a room that goes
    dark because a sensor dropped off the mesh -- and does it silently.

    Mutation applied: Lux.dark's `verdict = when_unreadable == "dark"` ->
    `verdict = False`.
    """
    zone = a_zone()
    assert zone.config.lux.when_unreadable == "dark", "the schema default"

    # The sensor is not in indigo.devices at all, so read_lux takes the whole
    # path: a failed lookup, a reason, a warning, and a verdict.
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for second in range(10):
            zone.read_lux(at(seconds=second))

    assert zone.lux.unreadable is True
    assert zone.lux.value is None, "unreadable is not a reading of zero"
    assert zone.is_dark() is True

    assert len(caplog.records) == 1, "once per condition, not once per read"
    message = caplog.records[0].getMessage()
    assert "302" in message and "does not exist" in message
    assert "not a lux of zero" in message

    # ...and the lights keep following presence, which is the point of the
    # direction: an indoor room does not go dark because a sensor dropped off
    # the mesh.
    zone.ingest_presence(101, True, NOW)
    assert zone.evaluate(NOW, "lux unreadable").to_state is ZoneState.OCCUPIED
    assert zone.desired_levels(NOW) == {201: 60, 202: 30}
    assert "UNREADABLE" in zone.explain(NOW)


def test_when_unreadable_bright_takes_the_zone_off_duty_and_warns_once():
    """The per-zone flag genuinely switches the failure direction: `bright`
    takes the zone OFF-DUTY, which is what a garden wants.

    Kills: read the flag and ignore it, which passes every test written
    against the default.

    Mutation applied: Lux.dark's `verdict = when_unreadable == "dark"` ->
    `verdict = True`, which is the flag being ignored in the other direction.
    """
    garden = a_zone(when_unreadable="bright")
    indoors = a_zone(when_unreadable="dark")

    for zone in (garden, indoors):
        zone.ingest_presence(101, True, NOW)
        zone.ingest_lux(1800, NOW)
        assert zone.evaluate(NOW, "startup").to_state is ZoneState.OCCUPIED

    # The same failure, on two zones that differ only in the flag.
    for zone in (garden, indoors):
        zone.ingest_lux(None, at(minutes=1), reason="the sensor stopped reporting")

    assert garden.is_dark() is False
    assert garden.evaluate(at(minutes=1), "lux unreadable").to_state is ZoneState.OFF_DUTY
    assert garden.off_duty_cause == "bright"

    # ...and off duty because BRIGHT means VACANT's plan (section 5.3), so
    # the lights go off. That is the direction the flag was chosen for: on a
    # garden a stuck-on floodlight is the worse outcome, and "believe it is
    # bright" has to mean the same thing whether the belief came from a
    # reading or from the sensor being unreadable.
    assert garden.desired_levels(at(minutes=1)) == {201: "off", 202: "off"}

    assert indoors.is_dark() is True
    assert indoors.evaluate(at(minutes=1), "lux unreadable") is None
    assert indoors.state is ZoneState.OCCUPIED

    # And both say so rather than looking like a working sensor.
    assert "UNREADABLE" in garden.explain(at(minutes=1))
    assert "UNREADABLE" in indoors.explain(at(minutes=1))


def test_the_unreadable_warning_is_once_per_condition_not_once_per_pass(caplog):
    """The warning fires once while the condition lasts and again only after
    the sensor recovers and fails afresh (section 10).

    Kills: warn on every read (log spam), and: latch the warning forever, so a
    second, later failure is never reported.

    Mutations applied, one at a time: compare.warn_once's `if key in _WARNED:
    return False` -> `if False: return False` (spam), and Lux.update's
    `compare.reset_warnings(self.warn_key)` -> `pass` (latch).
    """
    zone = a_zone()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        # The outage. Sixty reads, one warning.
        for second in range(60):
            zone.ingest_lux(None, at(seconds=second), reason="the sensor stopped reporting")
        assert len(caplog.records) == 1
        assert "302" in caplog.records[0].getMessage()

        # It comes back.
        zone.ingest_lux(1800, at(minutes=2))
        assert len(caplog.records) == 1
        assert zone.lux.unreadable is False

        # And fails again, later, for its own reasons. A latched warning
        # hides this second outage as completely as no warning hid the first.
        for second in range(60):
            zone.ingest_lux(None, at(minutes=3, seconds=second), reason="it dropped again")
        assert len(caplog.records) == 2
        assert "dropped again" in caplog.records[1].getMessage()


@promise
def test_a_light_whose_state_cannot_be_read_is_excluded_from_override_detection():
    """A device with neither a readable brightness nor a readable onState
    warns once and is excluded from override detection, but is still commanded
    and the zone keeps working for its other lights (R8).

    Kills: let the unreadable device fall through to a default of at-desired
    (it can then never override) or off-desired (it overrides constantly), and:
    drop the whole zone. The fork's fall-through here was a level-5 log line.

    Override detection is not built at M1 phase B. Left as a stub.
    """
    raise NotImplementedError


def test_an_indigo_lookup_failure_is_not_reported_as_device_gone(monkeypatch, caplog):
    """`indigo.devices[...]` raising KeyError means the device is gone; any
    other exception means the lookup itself failed, and the two get different
    handling and different messages (R15).

    Kills: one `except Exception` that treats every failure as "device gone".
    A transient server problem then becomes a confident wrong answer, and the
    zone drops a light it still owns.

    Mutation applied: devices.get_device's two clauses collapsed into
    `except Exception: raise DeviceGone(dev_id) from None`.
    """
    import indigo

    from lamplighter import devices

    class Flaky(dict):
        """201 answers, 202's lookup breaks, 203 is genuinely not there."""

        def __getitem__(self, key):
            if key == 202:
                raise RuntimeError("Indigo server is not responding")
            return super().__getitem__(key)

    lamp = make_device(201, "dimmer", name="Desk Lamp")
    monkeypatch.setattr(indigo, "devices", Flaky({201: lamp}))

    # At the boundary: two exceptions, and the broken one is not the gone one.
    with pytest.raises(LookupFailed) as broken:
        devices.get_device(202)
    assert not isinstance(broken.value, DeviceGone)
    with pytest.raises(DeviceGone):
        devices.get_device(203)

    # And through the zone, which is where the difference matters.
    zone = a_zone(lights=[201, 202, 203])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        found = zone.resolve_lights()

    assert found.live == {201: lamp}
    assert found.failed == (202,), "a broken lookup is not a missing device"
    assert found.gone == (203,)

    messages = {record.getMessage() for record in caplog.records}
    failed_message = next(message for message in messages if "202" in message)
    gone_message = next(message for message in messages if "203" in message)
    assert "NOT" in failed_message and "RuntimeError" in failed_message
    assert "does not exist" in gone_message
    assert "does not exist" not in failed_message, (
        "the message must not tell a reader to go looking for a device that "
        "is probably fine"
    )

    # 202 is still the zone's light. Nothing was learned about it, so nothing
    # about it was decided: when the server answers again it comes straight
    # back, with no configuration change.
    assert 202 in zone.config.lights
    monkeypatch.setattr(indigo, "devices", {201: lamp, 202: make_device(202, "dimmer")})
    assert set(zone.resolve_lights().live) == {201, 202}


@promise
def test_recording_a_command_never_costs_the_command():
    """If the pre-command state cannot be read, the command is still sent; the
    device loses its echo record, not its write (R3, R8).

    Kills: record first and send inside the same try, so an unreadable device
    is never commanded at all.

    Commands are not built at M1 phase B. Left as a stub.
    """
    raise NotImplementedError


def test_a_stale_lux_reading_says_so():
    """A lux reading older than the zone would trust is reported as stale in
    the zone's explain line rather than used as if it were current (R15).

    Kills: use the last value forever, which is indistinguishable from a
    working sensor right up until the room is wrong all day.

    Mutation applied: Lux.stale's `return now - self.read_at >
    dt.timedelta(seconds=after_seconds)` -> `return False`.
    """
    from lamplighter.lux import STALE_AFTER_SECONDS

    zone = a_zone()
    zone.ingest_presence(101, True, NOW)
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "startup")

    fresh = zone.explain(at(minutes=1))
    assert "lux=1800" in fresh
    assert "STALE" not in fresh

    # Long enough that the sensor has plainly stopped reporting. The reading
    # is still the best evidence there is and is still used -- what it must
    # not do is look current.
    old = at(seconds=STALE_AFTER_SECONDS + 60)
    zone.evaluate(old, "reconcile tick")
    line = zone.explain(old)
    assert "STALE" in line
    assert "min ago" in line
    assert "1800" in line, "the value is still reported, just not as current"
    assert zone.state is ZoneState.VACANT  # the hold expired long ago

    # A reading that arrives makes it current again.
    zone.ingest_lux(1750, old)
    assert "STALE" not in zone.explain(old)
