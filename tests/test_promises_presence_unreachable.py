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


def test_recovery_with_the_states_still_frozen_does_not_trust_them():
    """indigo-matter clears errorState on Reachable=True and updates NO state,
    so the "on" it still carries is the one frozen at drop time.

    Kills: ingesting the reading the moment errorState clears.

    Mutation applied: _recovery_hold returns False unconditionally.
    """
    live(101, on=True)
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))

    engine.device_updated(snap(101, error="unreachable"), snap(101, on=True), at(seconds=60))

    assert zone.presence.on_devices == set(), "a frozen on is not a fresh report"
    assert zone.presence.last_value[101] is False
    assert 101 in zone.presence.awaiting
    assert 101 not in zone.presence.unreachable
    assert zone.evaluate(at(seconds=10 + HOLD), "hold expired").to_state is ZoneState.VACANT
    assert zone.state is ZoneState.VACANT

    # Same reading again (a link-quality tick): still not a report.
    engine.device_updated(snap(101, on=True), snap(101, on=True, lqi=9), at(seconds=400))
    assert zone.presence.on_devices == set()

    # A real change counts, and clears the marker.
    engine.device_updated(snap(101, on=True), snap(101, on=False), at(seconds=410))
    assert 101 not in zone.presence.awaiting
    edges = engine.device_updated(snap(101, on=False), snap(101, on=True), at(seconds=420))
    assert [e.kind for e in edges] == ["presence"]
    assert zone.presence.on_devices == {101}


def test_recovery_with_a_different_reading_counts_immediately():
    """Kills: holding a recovered device absent even when its reading moved."""
    live(101, on=False)
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    engine.device_updated(snap(101, on=False), snap(101, on=False, error="unreachable"), at(seconds=10))

    edges = engine.device_updated(snap(101, on=False, error="unreachable"), snap(101, on=True), at(seconds=60))

    assert [e.kind for e in edges] == ["presence"]
    assert zone.presence.on_devices == {101}
    assert 101 not in zone.presence.awaiting


def test_a_real_on_after_recovery_reoccupies_a_vacant_zone():
    live(101, on=True)
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    zone.evaluate(NOW, "startup")
    engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))
    engine.device_updated(snap(101, error="unreachable"), snap(101, on=True), at(seconds=60))
    zone.evaluate(at(seconds=10 + HOLD), "hold expired")
    assert zone.state is ZoneState.VACANT

    engine.device_updated(snap(101, on=True), snap(101, on=False), at(seconds=400))
    engine.device_updated(snap(101, on=False), snap(101, on=True), at(seconds=410))

    assert zone.evaluate(at(seconds=410), "presence").to_state is ZoneState.OCCUPIED


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
    live(102, on=False, name="Kitchen PIR")
    recorder = Recorder()
    logger = logging.getLogger("test.unreachable.warn")
    logger.setLevel(logging.INFO)
    logger.addHandler(recorder)
    try:
        engine = an_engine([101, 102], logger=logger)
        engine.seed_inputs(NOW)
        engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))
        engine.device_updated(snap(101, error="unreachable"), snap(101, error="unreachable", lqi=3), at(seconds=20))
        warnings = recorder.at(logging.WARNING)
        assert len(warnings) == 1, warnings
        for needle in ("Kitchen", "Kitchen matter motion 2 - Motion", "101", "unreachable", "ignor"):
            assert needle in warnings[0]
        assert recorder.at(logging.INFO) == []

        engine.device_updated(snap(101, error="unreachable"), snap(101, on=False), at(seconds=30))
        engine.device_updated(snap(101, on=False), snap(101, on=False, lqi=9), at(seconds=40))
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


# ------------------------------------------------ review round: what else must hold


def test_seeding_drops_a_device_that_went_unreachable_while_counted_on():
    """A hot reload carries on_devices; a device that went unreachable with no
    event (plugin busy, reload mid-outage) must not stay counted on.

    Kills: _seed_zone's unreachable branch only setting last_value.
    """
    live(101, on=True)
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    zone.evaluate(NOW, "startup")
    assert zone.presence.on_devices == {101}

    live(101, on=True, error="unreachable")
    engine.reload(engine.config, at(seconds=5))
    assert engine.seed_inputs(at(seconds=5)) == ()
    zone = engine.zones["Kitchen"]

    assert zone.presence.on_devices == set()
    assert zone.presence.expiry(HOLD) == at(seconds=5 + HOLD)
    assert zone.evaluate(at(seconds=5 + HOLD), "hold expired").to_state is ZoneState.VACANT


def test_seeding_expires_an_unlock_on_leave_override_for_a_dropped_device():
    live(101, on=True)
    engine = an_engine(
        override={"duration_minutes": 600, "extend_minutes": 30, "unlock_on_leave": True}
    )
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    zone.evaluate(NOW, "startup")
    zone.start_override(201, at(seconds=2))
    zone.evaluate(at(seconds=2), "override")

    live(101, on=True, error="unreachable")
    engine.reload(engine.config, at(seconds=5))
    engine.seed_inputs(at(seconds=5))
    zone = engine.zones["Kitchen"]

    move = zone.evaluate(at(seconds=5 + HOLD), "presence hold expired")
    assert move.to_state is ZoneState.VACANT
    assert zone.override is None


@pytest.mark.parametrize("text", ["timeout", "cmd failed", "setting failed"])
def test_only_the_word_unreachable_counts_as_absent(text):
    """indigo-matter also sets these on healthy devices.

    Kills: treating any non-empty errorState as unreachable.
    """
    live(101, on=True, error=text)
    recorder = Recorder()
    logger = logging.getLogger("test.unreachable.other")
    logger.setLevel(logging.INFO)
    logger.addHandler(recorder)
    try:
        engine = an_engine(logger=logger)
        zone = engine.zones["Kitchen"]
        engine.seed_inputs(NOW)
        zone.evaluate(NOW, "startup")

        assert zone.presence.on_devices == {101}
        assert zone.state is ZoneState.OCCUPIED
        assert zone.presence.unreachable == {}
        engine.device_updated(snap(101, error=text), snap(101, error=text, lqi=2), at(seconds=5))
        warnings = recorder.at(logging.WARNING)
        assert len(warnings) == 1 and text in warnings[0], warnings

        # Clearing re-arms the warning.
        engine.device_updated(snap(101, error=text), snap(101, lqi=3), at(seconds=10))
        engine.device_updated(snap(101), snap(101, error=text), at(seconds=20))
        assert len(recorder.at(logging.WARNING)) == 2
    finally:
        logger.removeHandler(recorder)


def test_unreachable_is_matched_case_and_whitespace_insensitively():
    live(101, on=True, error=" Unreachable ")
    engine = an_engine()
    engine.seed_inputs(NOW)
    assert engine.zones["Kitchen"].presence.on_devices == set()


def test_warns_once_when_every_presence_input_is_unreachable_and_rearms():
    """The zone can only go vacant now, whatever the room is doing.

    Kills: no zone-level signal (the device warnings alone read as ignorable
    when a second sensor exists).
    """
    live(101, on=True)
    live(102, on=True, name="Kitchen PIR")
    recorder = Recorder()
    logger = logging.getLogger("test.unreachable.zone")
    logger.setLevel(logging.INFO)
    logger.addHandler(recorder)
    try:
        engine = an_engine([101, 102], logger=logger)
        zone = engine.zones["Kitchen"]
        engine.seed_inputs(NOW)
        engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))
        zone_warnings = lambda: [m for m in recorder.at(logging.WARNING) if "no reachable presence input" in m]
        assert zone_warnings() == [], "one input is still reachable"

        engine.device_updated(
            snap(102, name="Kitchen PIR"), snap(102, name="Kitchen PIR", error="unreachable"), at(seconds=20)
        )
        engine.device_updated(
            snap(102, name="Kitchen PIR", error="unreachable"),
            snap(102, name="Kitchen PIR", error="unreachable", lqi=1),
            at(seconds=21),
        )
        assert len(zone_warnings()) == 1, recorder.records
        assert "Kitchen" in zone_warnings()[0] and "vacant" in zone_warnings()[0]
        assert "no reachable presence input" in zone.explain(at(seconds=22))

        # 102 reports a different reading: reachable and counted again.
        engine.device_updated(
            snap(102, name="Kitchen PIR", error="unreachable"),
            snap(102, on=False, name="Kitchen PIR"),
            at(seconds=30),
        )
        assert [m for m in recorder.at(logging.INFO) if "reporting again" in m]
        assert "no reachable presence input" not in zone.explain(at(seconds=31))

        engine.device_updated(
            snap(102, on=False, name="Kitchen PIR"),
            snap(102, name="Kitchen PIR", error="unreachable"),
            at(seconds=40),
        )
        assert len(zone_warnings()) == 2, "the second outage must warn again"
    finally:
        logger.removeHandler(recorder)


def test_a_recovered_device_awaiting_a_report_counts_as_unavailable_in_explain():
    live(101, on=True)
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))
    engine.device_updated(snap(101, error="unreachable"), snap(101, on=True), at(seconds=20))
    zone.evaluate(at(seconds=20), "presence")

    text = zone.explain(at(seconds=21))

    assert "Kitchen matter motion 2 - Motion reachable again, waiting for a fresh report" in text, text


def test_recovery_is_not_announced_when_the_device_cannot_be_read():
    """Kills: logging "reading it normally" before the read has succeeded."""
    live(101, on=True, status=True)
    recorder = Recorder()
    logger = logging.getLogger("test.unreachable.read")
    logger.setLevel(logging.INFO)
    logger.addHandler(recorder)
    try:
        engine = an_engine([DECLARED], logger=logger)
        zone = engine.zones["Kitchen"]
        engine.seed_inputs(NOW)
        engine.device_updated(snap(101, status=True), snap(101, status=True, error="unreachable"), at(seconds=10))

        # Reachable again but the declared state is missing.
        engine.device_updated(snap(101, status=True, error="unreachable"), snap(101, other=1), at(seconds=20))
        assert [m for m in recorder.at(logging.INFO) if "reachable again" in m] == []
        assert 101 in zone.presence.unreachable

        engine.device_updated(snap(101, other=1), snap(101, status=False), at(seconds=30))
        assert len([m for m in recorder.at(logging.INFO) if "reachable again" in m]) == 1
    finally:
        logger.removeHandler(recorder)


def test_a_declared_state_input_frozen_true_going_unreachable_is_one_edge():
    live(101, on=False, status=True)
    engine = an_engine([DECLARED])
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    assert zone.presence.on_devices == {101}

    edges = engine.device_updated(
        snap(101, status=True), snap(101, status=True, error="unreachable"), at(seconds=10)
    )

    assert [e.kind for e in edges] == ["presence"]
    assert zone.presence.on_devices == set()
    assert zone.presence.expiry(HOLD) == at(seconds=10 + HOLD)


def test_one_sensor_dropping_leaves_the_other_holding_the_zone():
    live(101, on=True)
    live(102, on=True, name="Kitchen PIR")
    engine = an_engine([101, 102])
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    zone.evaluate(NOW, "startup")

    engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))

    assert zone.presence.on_devices == {102}
    assert zone.presence.expiry(HOLD) is None
    zone.evaluate(at(seconds=10 + 2 * HOLD), "tick")
    assert zone.state is ZoneState.OCCUPIED


def test_hot_reload_carries_the_unreachable_and_awaiting_memory():
    live(101, on=True)
    live(102, on=True, name="Kitchen PIR")
    engine = an_engine([101, 102])
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    engine.device_updated(snap(101), snap(101, error="unreachable"), at(seconds=10))
    engine.device_updated(
        snap(102, name="Kitchen PIR"), snap(102, name="Kitchen PIR", error="unreachable"), at(seconds=11)
    )
    engine.device_updated(
        snap(102, name="Kitchen PIR", error="unreachable"), snap(102, name="Kitchen PIR"), at(seconds=12)
    )
    assert 102 in zone.presence.awaiting

    engine.reload(engine.config, at(seconds=20))
    rebuilt = engine.zones["Kitchen"]

    assert set(rebuilt.presence.unreachable) == {101}
    assert set(rebuilt.presence.awaiting) == {102}
    # ... and the frozen reading survived too: 101 recovers unchanged, held.
    live(101, on=True)
    engine.device_updated(snap(101, error="unreachable"), snap(101, on=True), at(seconds=30))
    assert rebuilt.presence.on_devices == set()


def test_a_device_that_is_gone_at_seeding_forgets_its_unreachable_memory():
    live(101, on=True, error="unreachable")
    engine = an_engine()
    zone = engine.zones["Kitchen"]
    engine.seed_inputs(NOW)
    assert 101 in zone.presence.unreachable

    import indigo

    del indigo.devices[101]
    engine.reload(engine.config, at(seconds=5))
    engine.seed_inputs(at(seconds=5))
    zone = engine.zones["Kitchen"]

    assert zone.presence.unreachable == {} and zone.presence.awaiting == {}
