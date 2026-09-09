"""Presence inputs read by a named state (issue #11).

A Texecom alarm zone is a perfectly good PIR that does not present as a
motion sensor: it publishes ``status``, not ``onState``. Until now the only
way to give one to a zone was a masquerade device -- an extra device and an
extra hop, worth about a second on the path a light turns on. An entry of
``{"id": ..., "state": "status", "on_when": true}`` reads the real device.

The promises, one test each:

* the bare-id form is untouched;
* a declared state drives presence, and its ``on_when`` comparison is
  permissive across the forms plugins publish;
* a malformed entry is a config error naming the zone and the value;
* **an unreadable declared state is UNKNOWN, never "off"** -- at an edge it
  leaves the zone's picture alone, and at seeding it leaves the zone
  unseeded to be retried, exactly as a failed Indigo lookup does.
"""

import datetime as dt
import logging

import pytest
from helpers import FixedSun, RecordingCommander, make_config, make_device, make_period

from lamplighter import compare
from lamplighter.config import ConfigError
from lamplighter.engine import Engine
from lamplighter.zone import ZoneState

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
HOLD = 300
LOG = logging.getLogger("test.promises.presence_state")

ALARM_ZONE = {"id": 101, "state": "status", "on_when": True}


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


class Recorder(logging.Handler):
    """Collects the WARNING lines a test needs to prove were emitted."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def an_engine(presence_devices, logger=LOG):
    config = make_config(
        [
            {
                "name": "Kitchen",
                "presence_devices": presence_devices,
                "hold_seconds": HOLD,
                "lux": None,
                "lights": [201],
                "periods": [make_period("Evening", "18:00", "23:00", levels={"201": 60})],
            }
        ]
    )
    return Engine(config, FixedSun(), RecordingCommander(), logger=logger)


def test_a_bare_id_is_still_read_as_a_motion_sensor():
    """The integer form must not regress: it reads onState, as it always did.

    Kills: routing every presence device through the declared-state reader,
    which would make an ordinary PIR unreadable and its zone permanently
    unoccupied.
    """
    make_device(101, "sensor", name="Kitchen PIR", onState=True)
    make_device(201, "dimmer", name="Kitchen Pendant")
    engine = an_engine([101])

    engine.seed_inputs(NOW)

    assert engine.zones["Kitchen"].presence.on_devices == {101}


def test_a_declared_state_drives_presence_without_a_masquerade_device():
    """`{"id": .., "state": "status"}` reads the real device's own state.

    Kills: ignoring the object form's `state` and falling back to onState --
    an alarm zone has none, so the zone would never see the room.

    Mutation applied: engine.presence_reading's `if declared is not None`
    branch -> deleted.
    """
    make_device(101, "device", name="Kitchen PIR", status=True)
    make_device(201, "dimmer", name="Kitchen Pendant")
    engine = an_engine([ALARM_ZONE])
    zone = engine.zones["Kitchen"]

    engine.seed_inputs(NOW)

    assert zone.presence.on_devices == {101}
    engine.mark_all_dirty("startup")
    engine.tick(NOW)
    assert zone.state is ZoneState.OCCUPIED


def test_on_when_matches_across_the_forms_plugins_publish_it_in():
    """A state of the string "true" satisfies `"on_when": true`.

    Indigo plugins publish the same fact as True, "true", "On" and 1. A
    config author writing `"on_when": true` means all of them; a strict `==`
    would leave a working sensor reading as an empty room.

    Kills: comparing the state to on_when with `==` alone.
    """
    make_device(101, "device", name="Kitchen PIR", status="True")
    make_device(201, "dimmer", name="Kitchen Pendant")
    engine = an_engine([ALARM_ZONE])

    engine.seed_inputs(NOW)

    assert engine.zones["Kitchen"].presence.on_devices == {101}


def test_a_state_that_does_not_match_on_when_reads_as_off():
    """The other direction: a non-matching value is an honest "off"."""
    make_device(101, "device", name="Kitchen PIR", status=False)
    make_device(201, "dimmer", name="Kitchen Pendant")
    engine = an_engine([ALARM_ZONE])

    engine.seed_inputs(NOW)

    presence = engine.zones["Kitchen"].presence
    assert presence.on_devices == set()
    assert presence.last_value[101] is False, "off is a reading, not 'never asked'"


@pytest.mark.parametrize(
    "entry, expect",
    [
        ({"id": 101}, "missing required key 'state'"),
        ({"id": 101, "state": "status", "when": True}, "not a known key"),
        ({"id": 0, "state": "status"}, "below the minimum"),
        ({"id": 101, "state": ""}, "is empty"),
        ({"id": 101, "state": "status", "on_when": None}, "got null"),
        ("101", "expected an Indigo device id"),
    ],
)
def test_a_malformed_presence_entry_is_a_config_error_with_its_path(entry, expect):
    """Config is hot-reloaded, so a bad entry must be refused loudly rather
    than skipped: a presence input the zone silently stopped reading is a
    zone that stops seeing the room.

    Kills: accepting the object form with a try/except that drops what it
    cannot parse.
    """
    with pytest.raises(ConfigError) as raised:
        an_engine([entry])

    assert "zones/0/presence_devices" in raised.value.path
    assert expect in str(raised.value)


def test_the_same_device_may_not_be_listed_twice_in_either_form():
    """`[101, {"id": 101, ...}]` is a duplicate the array's uniqueItems rule
    cannot see, and two entries for one device would give it two readings.
    """
    with pytest.raises(ConfigError) as raised:
        an_engine([101, ALARM_ZONE])

    assert "[101]" in str(raised.value)
    assert "more than once" in str(raised.value)


def test_a_zone_whose_only_presence_input_is_unknown_is_left_unseeded():
    """The degradation path. A device that does not publish the named state
    says NOTHING about the room; with no other input answering, seeding the
    zone would write "nobody here" from a reading nobody took.

    Kills: seeding a zone whose inputs all failed to read. It starts VACANT
    and turns the lights off in an occupied room.
    """
    make_device(101, "device", name="Kitchen PIR", state_of_zone=True)  # not "status"
    make_device(201, "dimmer", name="Kitchen Pendant")
    recorder = Recorder()
    logger = logging.getLogger("test.presence_state.unreadable.seed")
    logger.addHandler(recorder)
    engine = an_engine([ALARM_ZONE], logger=logger)
    try:
        assert engine.seed_inputs(NOW) == ("Kitchen",), "unseeded, so it is retried"
    finally:
        logger.removeHandler(recorder)

    assert engine.zones["Kitchen"].presence.last_value == {}, "unknown is not 'off'"
    assert any("does not publish a state named 'status'" in m for m in recorder.messages)
    assert any("UNKNOWN, not off" in m for m in recorder.messages)
    assert any("left unseeded" in m for m in recorder.messages), (
        "the warning must say what this caller did"
    )


def test_one_unknown_input_does_not_hold_back_a_zone_that_has_another():
    """The other half of the same rule: an unknown input is one input, not
    the room. A zone with a working PIR alongside a misspelt alarm-zone entry
    still seeds and still runs.

    Kills: `readable = False` on a missing state, which leaves a zone with a
    perfectly good sensor permanently unseeded -- warned once, then silent,
    with its lights never automated again.
    """
    make_device(101, "device", name="Kitchen PIR", state_of_zone=True)  # not "status"
    make_device(102, "sensor", name="Kitchen Radar", onState=True)
    make_device(201, "dimmer", name="Kitchen Pendant")
    engine = an_engine([ALARM_ZONE, 102])
    zone = engine.zones["Kitchen"]

    assert engine.seed_inputs(NOW) == (), "the radar answered; the zone can run"
    assert zone.presence.on_devices == {102}
    engine.mark_all_dirty("startup")
    engine.tick(NOW)
    assert zone.state is ZoneState.OCCUPIED


def test_a_state_that_is_present_but_unset_is_unknown_not_off():
    """A device that publishes the state but has not filled it in yet is the
    restart case, and it must read the way the sensor path reads a None: no
    reading at all.

    Kills: `presence_state_is_on(None, True)` -> False, which turns "has not
    reported yet" into a confident empty room.
    """
    make_device(101, "device", name="Kitchen PIR", status=None)
    make_device(201, "dimmer", name="Kitchen Pendant")
    engine = an_engine([ALARM_ZONE])

    assert engine.seed_inputs(NOW) == ("Kitchen",)
    assert engine.zones["Kitchen"].presence.last_value == {}, "None is not 'off'"


def test_a_broken_states_container_is_not_reported_as_a_wrong_state_name():
    """The two-failure rule, inside one device: a `states` that cannot be
    subscripted at all says nothing about the state NAME.

    Kills: `except (KeyError, TypeError)`, which sends a reader off to check
    a spelling that is fine while a plugin is mid-reload.
    """

    class Broken:
        id = 101
        name = "Kitchen PIR"
        states = 17  # not subscriptable

    recorder = Recorder()
    logger = logging.getLogger("test.presence_state.broken")
    logger.addHandler(recorder)
    make_device(201, "dimmer", name="Kitchen Pendant")
    engine = an_engine([ALARM_ZONE], logger=logger)
    import indigo

    indigo.devices[101] = Broken()
    try:
        assert engine.seed_inputs(NOW) == ("Kitchen",)
    finally:
        logger.removeHandler(recorder)

    assert any("NOT evidence it is wrong" in m for m in recorder.messages), (
        recorder.messages
    )
    assert not any("does not publish a state named" in m for m in recorder.messages)


def test_an_unreadable_declared_state_at_an_edge_does_not_empty_the_room():
    """The same rule on the callback thread: a device that stops publishing
    the state must not be read as a sensor that went off.

    Kills: letting UnreadablePresenceState fall through to
    `presence_is_on(...) is False`, which clears the reporting set and starts
    the hold on a room somebody is standing in.
    """
    make_device(101, "device", name="Kitchen PIR", status=True)
    make_device(201, "dimmer", name="Kitchen Pendant")
    recorder = Recorder()
    logger = logging.getLogger("test.presence_state.unreadable.edge")
    logger.addHandler(recorder)
    engine = an_engine([ALARM_ZONE], logger=logger)
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    assert zone.presence.on_devices == {101}

    before = make_device(101, "device", name="Kitchen PIR", status=True)
    broken = make_device(101, "device", name="Kitchen PIR", other=True)  # no "status"
    try:
        assert engine.device_updated(before, broken, NOW) == []
    finally:
        logger.removeHandler(recorder)

    assert zone.presence.on_devices == {101}, "unknown must not clear the room"
    assert any("does not publish a state named 'status'" in m for m in recorder.messages)
    assert any("keeps the last reading" in m for m in recorder.messages), (
        "an operator must be told the zone may stay occupied on a stale reading"
    )
