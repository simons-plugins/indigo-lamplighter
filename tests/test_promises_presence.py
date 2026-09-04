"""Presence hold lives in the zone (PRD R4, R10, R13; sections 5.2, 5.4).

Raw sensors -- PIR, mmWave radar, door contacts, or an Occupatum zone device
during migration -- feed one per-zone last-seen timestamp and one hold. No
second plugin is needed to say "still here", and no device needs to tick.
"""

import datetime as dt
import logging

import pytest
from helpers import make_period, make_zone

from lamplighter import compare, persist
from lamplighter.zone import ZoneState

# 20:00, inside the Evening band and after the fixed sun's sunset, so the
# only things moving in these tests are the ones each test moves.
NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
HOLD = 300


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def at(**kwargs):
    return NOW + dt.timedelta(**kwargs)


def a_zone(**fields):
    """A zone with two lights, three presence sensors and a 5-minute hold."""
    fields.setdefault("lights", [201, 202])
    fields.setdefault("presence_devices", [101, 102, 103])
    fields.setdefault("hold_seconds", HOLD)
    fields.setdefault("lux", {"device": 302, "dark_below": 2200, "hysteresis": 300})
    return make_zone(
        [make_period("Evening", "18:00", "23:00", levels={"201": 60, "202": 30})],
        logger=logging.getLogger("test.promises.presence"),
        **fields,
    )


def test_presence_last_seen_survives_a_restart():
    """last_seen is persisted on the zone device and restored at startup
    (R13), so a restart does not turn the lights off on an occupied room.

    Kills: initialise last_seen to None or to now at startup. None empties an
    occupied room; now fills an empty one.

    Mutation applied: persist.apply_persisted's
    `last_seen = _read_time(data, "presence_last_seen", complain)` -> `= None`.
    """
    before = a_zone()
    before.ingest_presence(101, True, NOW)
    before.ingest_lux(1800, NOW)
    assert before.evaluate(NOW, "presence edge").to_state is ZoneState.OCCUPIED

    record = persist.to_persisted(before)
    assert record["presence_last_seen"] == "2026-09-04T20:00:00"

    # The restart. A brand new object, which knows nothing until the record
    # is applied -- asserted, so "it was occupied anyway" cannot pass this.
    restarted = a_zone()
    assert restarted.presence.last_seen is None
    assert restarted.evaluate(at(minutes=2), "startup").to_state is ZoneState.VACANT

    persist.apply_persisted(restarted, record, at(minutes=2))
    assert restarted.presence.last_seen == NOW
    move = restarted.evaluate(at(minutes=2), "state restored")
    assert move.to_state is ZoneState.OCCUPIED

    # And the hold still runs from the ORIGINAL sighting, not from the
    # restart: "now" at startup would give the room five fresh minutes.
    assert restarted.presence.expiry(HOLD) == at(seconds=HOLD)
    assert restarted.evaluate(at(seconds=HOLD), "hold expiry").to_state is ZoneState.VACANT


def test_presence_is_any_of_the_zones_devices():
    """Any one presence device reporting on makes the zone occupied
    (section 5.4).

    Kills: require all of them, which turns a two-sensor room into a room that
    is never occupied.

    Mutation applied: Zone.evaluate's `presence_active = self.presence.active(
    now, self.config.hold_seconds)` -> `... and len(self.presence.on_devices)
    == len(self.config.presence_devices)`.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)

    # One of three, and not the first one listed.
    assert zone.ingest_presence(102, True, NOW) is True
    assert zone.evaluate(NOW, "presence edge").to_state is ZoneState.OCCUPIED

    # Every device on its own does it, and a device outside the zone does not.
    for device_id in (101, 102, 103):
        fresh = a_zone()
        fresh.ingest_lux(1800, NOW)
        fresh.ingest_presence(device_id, True, NOW)
        assert fresh.evaluate(NOW, "presence edge").to_state is ZoneState.OCCUPIED

    stranger = a_zone()
    stranger.ingest_lux(1800, NOW)
    assert stranger.ingest_presence(999, True, NOW) is False
    assert stranger.evaluate(NOW, "presence edge").to_state is ZoneState.VACANT


def test_a_re_report_of_on_refreshes_last_seen_without_replanning():
    """A repeated "on" reading moves last_seen forward even though it is not
    an input edge and causes no re-plan (R4 + section 5.4).

    Kills: fixing the re-plan storm by ignoring repeated "on" readings
    entirely, which stops the hold ever being refreshed and empties an
    occupied room after `hold_seconds`.

    Mutation applied: Presence.update's `self.last_seen = now` ->
    `self.last_seen = self.last_seen or now`.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)
    assert zone.evaluate(NOW, "presence edge").to_state is ZoneState.OCCUPIED
    assert zone.next_wake(NOW) == at(seconds=HOLD)

    # The Occupatum countdown: the same "on", again, 100 seconds later.
    assert zone.ingest_presence(101, True, at(seconds=100)) is True
    assert zone.presence.last_seen == at(seconds=100)
    assert zone.next_wake(at(seconds=100)) == at(seconds=100 + HOLD)

    # It moved the timer and nothing else: no transition, so nothing is
    # written and the zone is not re-planned.
    assert zone.evaluate(at(seconds=100), "presence re-report") is None
    assert zone.state is ZoneState.OCCUPIED

    # The hold now runs from the second sighting. Under the mutation the room
    # empties here, with somebody still standing in it.
    assert zone.evaluate(at(seconds=350), "hold check") is None
    assert zone.state is ZoneState.OCCUPIED
    assert zone.evaluate(at(seconds=400), "hold expiry").to_state is ZoneState.VACANT


def test_hold_expiry_turns_the_lights_off_exactly_once():
    """Crossing `hold_seconds` since last_seen is an input edge: the zone goes
    VACANT and writes once.

    Kills: poll the hold on every reconcile tick and re-command the lights off
    each time, which reduces a person re-entering the room to a race.

    Mutation applied: Zone.evaluate's `if new_state is previous: return None`
    -> `if False: return None`.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)
    zone.evaluate(NOW, "presence edge")

    assert zone.evaluate(at(seconds=299), "reconcile tick") is None

    move = zone.evaluate(at(seconds=300), "presence hold expired")
    assert (move.from_state, move.to_state) == (ZoneState.OCCUPIED, ZoneState.VACANT)
    assert zone.desired_levels(at(seconds=300)) == {201: "off", 202: "off"}

    # Every tick after it is silent. A zone that answers "go off" once a
    # minute makes somebody walking back in race the next tick.
    for tick in range(1, 30):
        assert zone.evaluate(at(seconds=300 + tick * 60), "reconcile tick") is None
    assert zone.state is ZoneState.VACANT


def test_unlock_on_leave_fires_from_hold_expiry_with_an_override_held():
    """An override is released when the zone's own presence hold expires, even
    though the override was created while the room was occupied (R10).

    Kills: arm unlock-on-leave only for overrides created in an already-empty
    room -- fork #17, where every override made by a person standing in the
    room ran its full duration.

    Mutation applied: Zone._released_by_leaving's
    `return expiry is not None and expiry > self.override.since` ->
    `return expiry is None or expiry <= self.override.since`, which is fork
    #17's rule exactly.
    """
    zone = a_zone(override={"duration_minutes": 60, "unlock_on_leave": True})
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)
    zone.evaluate(NOW, "presence edge")

    # The override is taken with the room OCCUPIED -- the case the fork never
    # armed -- and it is a full hour from expiring.
    override = zone.start_override(201, at(seconds=30))
    assert override.expires_at == at(seconds=30, minutes=60)
    assert zone.evaluate(at(seconds=30), "override started").to_state is ZoneState.OVERRIDDEN

    # Still held while somebody is here, and the zone still writes nothing.
    assert zone.evaluate(at(seconds=200), "reconcile tick") is None
    assert zone.desired_levels(at(seconds=200)) == {201: "leave", 202: "leave"}

    # The hold expires 55 minutes before the override would have.
    move = zone.evaluate(at(seconds=300), "presence hold expired")
    assert (move.from_state, move.to_state) == (ZoneState.OVERRIDDEN, ZoneState.VACANT)
    assert zone.override is None
    assert zone.desired_levels(at(seconds=300)) == {201: "off", 202: "off"}
