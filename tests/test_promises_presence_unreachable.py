"""A presence device Indigo says is unreachable contributes no presence.

Observed live 2026-10-15/16 (Kitchen): a Matter FP300 dropped off while it
reported motion. Indigo froze it at onOffState True with
``errorState == "unreachable"``; the zone trusted the frozen value and stayed
occupied for ~26 h, an ``unlock_on_leave`` override kept extending, and at dusk
the lights never came on.

The promises, one test each:

* an unreachable device is read as absent, on bare-id and declared inputs;
* going unreachable is an input edge although only ``errorState`` changed
  (the change-gate sees it), and recovery is an edge back, with no restart;
* the hold then runs and the zone empties, and ``unlock_on_leave`` releases;
* seeding counts an unreachable device as having answered "off";
* it is NOT the "unknown" path: a declared state that is merely missing
  still keeps the last reading (issue #11) -- a regression guard;
* one WARNING when it drops, one INFO when it recovers;
* explain names the ignored device.
"""

import datetime as dt
import logging

import pytest
from helpers import FixedSun, RecordingCommander, make_config, make_device, make_period, make_snapshot

from lamplighter import compare
from lamplighter.engine import Engine
from lamplighter.zone import ZoneState

NOW = dt.datetime(2026, 10, 16, 20, 0, 0)
HOLD = 300
DECLARED = {"id": 101, "state": "status", "on_when": True}


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


class Recorder(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))

    def at(self, level):
        return [m for lv, m in self.records if lv == level]


def at(**kwargs):
    return NOW + dt.timedelta(**kwargs)


def an_engine(presence_devices=(101,), logger=None, **fields):
    zone = {
        "name": "Kitchen",
        "presence_devices": list(presence_devices),
        "hold_seconds": HOLD,
        "lux": None,
        "lights": [201],
        "periods": [make_period("Evening", "18:00", "23:00", levels={"201": 60})],
    }
    zone.update(fields)
    make_device(201, "dimmer", name="Kitchen Pendant")
    return Engine(
        make_config([zone]), FixedSun(), RecordingCommander(),
        logger=logger or logging.getLogger("test.unreachable"),
    )


def snap(dev_id, on=True, error="", name="Kitchen matter motion 2 - Motion", **states):
    """A DETACHED presence snapshot, as deviceUpdated hands over."""
    device = make_snapshot(dev_id, device_cls="sensor", name=name, onState=on, **states)
    device.errorState = error
    return device


def live(dev_id, on=True, error="", name="Kitchen matter motion 2 - Motion", **states):
    device = make_device(dev_id, "sensor", name=name, onState=on, **states)
    device.errorState = error
    return device


def test_an_unreachable_device_frozen_on_is_not_presence_at_seeding():
    """Kills: trusting onOffState on a device whose errorState is set.

    Mutation applied: presence_reading's early `if unreachable: return ...`
    -> deleted.
    """
    live(101, on=True, error="unreachable")
    engine = an_engine()
    zone = engine.zones["Kitchen"]

    assert engine.seed_inputs(NOW) == (), "an unreachable device must not leave the zone unseeded"

    assert zone.presence.on_devices == set()
    assert zone.presence.last_value[101] is False
    zone.evaluate(NOW, "startup")
    assert zone.state is not ZoneState.OCCUPIED


def test_a_healthy_device_decides_when_another_is_unreachable():
    live(101, on=True, error="unreachable")
    live(102, on=True, name="Kitchen PIR")
    engine = an_engine([101, 102])

    engine.seed_inputs(NOW)

    assert engine.zones["Kitchen"].presence.on_devices == {102}
    live(102, on=False, name="Kitchen PIR")
    engine2 = an_engine([101, 102])
    engine2.seed_inputs(NOW)
    assert engine2.zones["Kitchen"].presence.on_devices == set()


def test_a_declared_input_that_is_unreachable_is_ignored():
    live(101, on=False, error="unreachable", status=True)
    engine = an_engine([DECLARED])

    assert engine.seed_inputs(NOW) == ()

    assert engine.zones["Kitchen"].presence.on_devices == set()


def test_a_declared_input_with_a_missing_state_still_keeps_the_last_reading():
    """Regression guard: unknown is NOT unreachable. A device with no
    errorState and no `status` state keeps its last reading (issue #11).

    Kills: widening the unreachable handling to UnknownPresence.
    """
    live(101, on=True, status=True)
    engine = an_engine([DECLARED])
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    assert zone.presence.on_devices == {101}

    before = snap(101, status=True)
    broken = snap(101, other=True)  # no "status", no errorState
    assert engine.device_updated(before, broken, at(seconds=5)) == []

    assert zone.presence.on_devices == {101}, "unknown must not become off"
    assert 101 not in zone.presence.unreachable


def test_only_errorstate_changing_is_an_edge_and_starts_the_hold():
    """The states are frozen, so the gate must look at errorState too.

    Kills: a gate comparing only the state values -- the update is swallowed
    and the zone stays occupied.

    Mutation applied: presence_reading returns the plain reading even when
    unreachable (gate sees equal tuples).
    """
    live(101, on=True)
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    zone.evaluate(NOW, "startup")
    assert zone.state is ZoneState.OCCUPIED

    edges = engine.device_updated(snap(101, on=True), snap(101, on=True, error="unreachable"), at(seconds=10))

    assert [e.kind for e in edges] == ["presence"]
    assert zone.presence.on_devices == set()
    assert zone.presence.expiry(HOLD) == at(seconds=10 + HOLD), "the hold starts on the CLEARED edge"
    assert zone.evaluate(at(seconds=10 + HOLD), "hold expired").to_state is ZoneState.VACANT


def test_recovery_counts_again_without_a_restart():
    live(101, on=True)
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))
    assert zone.presence.on_devices == set()

    edges = engine.device_updated(snap(101, error="unreachable"), snap(101, on=True), at(seconds=60))

    assert [e.kind for e in edges] == ["presence"]
    assert zone.presence.on_devices == {101}
    assert 101 not in zone.presence.unreachable


def test_unlock_on_leave_releases_when_the_only_present_device_is_unreachable():
    """The 26-hour Kitchen: the override must release at the normal time.

    Kills: an unreachable device still pinning presence, so expiry() stays
    None and the override never releases.
    """
    live(101, on=True)
    engine = an_engine(
        override={"duration_minutes": 600, "extend_minutes": 30, "unlock_on_leave": True}
    )
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    zone.evaluate(NOW, "startup")
    assert zone.start_override(201, at(seconds=5)) is not None
    assert zone.evaluate(at(seconds=5), "override").to_state is ZoneState.OVERRIDDEN

    engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))

    move = zone.evaluate(at(seconds=10 + HOLD), "presence hold expired")
    assert (move.from_state, move.to_state) == (ZoneState.OVERRIDDEN, ZoneState.VACANT)
    assert zone.override is None


def test_warns_once_when_it_drops_and_says_once_when_it_recovers():
    live(101, on=True)
    recorder = Recorder()
    logger = logging.getLogger("test.unreachable.warn")
    logger.setLevel(logging.INFO)
    logger.addHandler(recorder)
    try:
        engine = an_engine(logger=logger)
        engine.seed_inputs(NOW)
        engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))
        engine.device_updated(snap(101, error="unreachable"), snap(101, error="unreachable", lqi=3), at(seconds=20))
        warnings = recorder.at(logging.WARNING)
        assert len(warnings) == 1, warnings
        for needle in ("Kitchen", "Kitchen matter motion 2 - Motion", "101", "unreachable", "ignor"):
            assert needle in warnings[0]
        assert recorder.at(logging.INFO) == []

        engine.device_updated(snap(101, error="unreachable"), snap(101, on=True), at(seconds=30))
        engine.device_updated(snap(101, on=True), snap(101, on=True, lqi=9), at(seconds=40))
        assert len(recorder.at(logging.INFO)) == 1, recorder.records
        assert ("presence-unreachable", "Kitchen", 101) not in compare._WARNED

        # A second outage warns again.
        engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=50))
        assert len(recorder.at(logging.WARNING)) == 2
    finally:
        logger.removeHandler(recorder)


def test_explain_names_the_ignored_device_and_the_status_payload_marks_it():
    import json

    live(101, on=True, error="unreachable")
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    zone.evaluate(NOW, "startup")

    text = zone.explain(NOW)

    assert "presence=inactive (Kitchen matter motion 2 - Motion unreachable, ignored)" in text, text
    inputs = {e["id"]: e for e in json.loads(zone.snapshot()["presence_inputs"])}
    assert inputs[101]["unreachable"] is True and inputs[101]["on"] is False
