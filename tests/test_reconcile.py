"""The commander and the pass's edges (PRD R6, R15; section 5.8).

The ladder, the warning and the one-command-per-pass rule are pinned in
`test_promises_reconcile.py`. This file covers the two things underneath
them: which Indigo verb a level turns into, and what the pass does when a
device will not resolve at all.
"""

import datetime as dt
import logging

import pytest
from helpers import RecordingCommander, make_device, make_period, make_zone

from lamplighter import compare
from lamplighter.override import EchoBook
from lamplighter.reconcile import IndigoCommander, Reconciler
from lamplighter.zone import ZoneState

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
LOGGER_NAME = "test.reconcile"
LOG = logging.getLogger(LOGGER_NAME)


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def occupied_zone(levels, lights):
    zone = make_zone(
        [make_period("Evening", "18:00", "23:00", levels=levels)],
        logger=LOG,
        lights=list(lights),
        presence_devices=[101],
    )
    zone.ingest_presence(101, True, NOW)
    assert zone.evaluate(NOW, "startup").to_state is ZoneState.OCCUPIED
    return zone


# ------------------------------------------------------------ the verbs


def test_the_commander_picks_the_verb_from_the_device_and_the_level():
    """One method, three verbs. A relay handed an int is translated rather
    than refused: a relay reads any level above zero as on, and refusing would
    make a mixed zone a configuration error instead of a room."""
    dimmer = make_device(201, "dimmer", brightness=0)
    relay = make_device(203, "relay", onState=False)
    commander = IndigoCommander(LOG)

    commander.set_level(dimmer, 60)
    assert dimmer.brightness == 60 and dimmer.onState is True

    commander.set_level(dimmer, "off")
    assert dimmer.brightness == 0 and dimmer.onState is False

    commander.set_level(dimmer, "on")
    assert dimmer.onState is True

    commander.set_level(relay, "on")
    assert relay.onState is True
    commander.set_level(relay, "off")
    assert relay.onState is False
    commander.set_level(relay, 60)
    assert relay.onState is True, "an int on a relay means on"


# ------------------------------------------------------- the two lookups


def test_a_missing_device_is_dropped_with_a_warning_and_the_zone_keeps_going(caplog):
    """A dead bulb must not take four working lights with it (R15)."""
    zone = occupied_zone({"201": 60, "202": 30}, (201, 202))
    make_device(201, "dimmer", brightness=0)
    # 202 is not in indigo.devices at all.

    commander = RecordingCommander(apply=True)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for _pass in range(5):
            Reconciler(commander, EchoBook(), LOG).run(zone, NOW)

    assert commander.commands == [(201, 60)], "the working light was still driven"
    assert len(caplog.records) == 1, "once per condition per device"
    assert "does not exist" in caplog.records[0].getMessage()


def test_a_broken_lookup_skips_the_pass_and_is_not_reported_as_gone(caplog, monkeypatch):
    """`KeyError` means gone; anything else means the lookup broke, and the
    two must never share a handler. A transient server problem that reads as
    "device gone" quietly shrinks a zone to the lights that answered."""
    import indigo

    zone = occupied_zone({"201": 60, "202": 30}, (201, 202))
    lamp = make_device(201, "dimmer", brightness=0)

    class Flaky(dict):
        def __getitem__(self, key):
            if key == 202:
                raise RuntimeError("Indigo server is not responding")
            return super().__getitem__(key)

    monkeypatch.setattr(indigo, "devices", Flaky({201: lamp}))
    commander = RecordingCommander(apply=True)
    reconciler = Reconciler(commander, EchoBook(), LOG)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        reconciler.run(zone, NOW)

    assert commander.commands == [(201, 60)]
    message = caplog.records[0].getMessage()
    assert "NOT" in message and "RuntimeError" in message
    assert "does not exist" not in message, (
        "the message must not send a reader looking for a device that is "
        "probably fine"
    )

    # Nothing was learned about 202, so nothing about it was decided: it keeps
    # its place in the zone and its backoff, and comes straight back.
    assert reconciler.backoff_step(202) == 0, "a skipped pass is not a failed write"
    monkeypatch.setattr(indigo, "devices", {201: lamp, 202: make_device(202, "dimmer")})
    commander.clear()
    reconciler.run(zone, NOW + dt.timedelta(seconds=60))
    assert commander.commands == [(202, 30)]


def test_a_leave_level_is_never_written_in_any_state():
    """The guarantee `leave` carries, at the tick as much as anywhere: a light
    the zone has no opinion about is not touched, on, off, or ever."""
    zone = occupied_zone({"201": 60, "203": "leave"}, (201, 203))
    make_device(201, "dimmer", brightness=60)  # already there
    make_device(203, "dimmer", brightness=100)  # blazing away, and not ours

    commander = RecordingCommander(apply=True)
    reconciler = Reconciler(commander, EchoBook(), LOG)
    for number in range(1, 20):
        reconciler.run(zone, NOW + dt.timedelta(seconds=60 * number))

    assert commander.commands == []
    assert reconciler.backoff_step(203) == 0
