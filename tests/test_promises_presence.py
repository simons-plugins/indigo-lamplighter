"""Presence hold lives in the zone (PRD R4, R10, R13; sections 5.2, 5.4).

Raw sensors -- PIR, mmWave radar, door contacts, or an Occupatum zone device
during migration -- feed one per-zone reporting set and one hold. No second
plugin is needed to say "still here", and no device needs to tick.

The rule these all rest on is Occupatum's: **occupied while any sensor is on,
and the off-delay starts when the last one clears.** Two sensor kinds make
that concrete, and the tests below keep both honest:

* an *edge* sensor (a PIR) trips and drops again straight away, so the hold
  is doing all the work;
* a *level* sensor (the Study's Aqara FP1 radar) reports "on" once and then
  says nothing at all until the room empties, so the reporting set is doing
  all the work and the hold does not start until it clears.
"""

import datetime as dt
import logging

import pytest
from helpers import (
    FixedSun,
    RecordingCommander,
    make_config,
    make_device,
    make_period,
    make_zone,
)

from lamplighter import compare, persist
from lamplighter.engine import Engine
from lamplighter.zone import ZoneState

# 20:00, inside the Evening band and after the fixed sun's sunset, so the
# only things moving in these tests are the ones each test moves.
NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
HOLD = 300
LOG = logging.getLogger("test.promises.presence")


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
    """A repeated "on" reading moves the hold forward even though it is not a
    state edge and causes no re-plan (R4 + section 5.4).

    Kills: fixing the re-plan storm by ignoring repeated "on" readings
    entirely, which stops the hold ever being refreshed and empties a room
    somebody is still walking about in.

    Mutation applied: Presence.update's `self.last_seen = now` on the "on"
    path -> `self.last_seen = self.last_seen or now`. Note the assertion
    order below: last_seen is checked immediately after the "on" and before
    the matching "off", because the "off" stamps it too and would otherwise
    mask the mutation entirely.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    # A PIR: it trips and drops again, and the hold runs from the drop.
    zone.ingest_presence(101, True, NOW)
    zone.ingest_presence(101, False, NOW)
    assert zone.evaluate(NOW, "presence edge").to_state is ZoneState.OCCUPIED
    assert zone.next_wake(NOW) == at(seconds=HOLD)

    # Somebody moves again 100 seconds later and the PIR trips a second time.
    assert zone.ingest_presence(101, True, at(seconds=100)) is True
    assert zone.presence.last_seen == at(seconds=100), "the on reading moves the hold"
    zone.ingest_presence(101, False, at(seconds=100))
    assert zone.next_wake(at(seconds=100)) == at(seconds=100 + HOLD)

    # It moved the timer and nothing else: no transition, so nothing is
    # written and the zone is not re-planned.
    assert zone.evaluate(at(seconds=100), "presence re-report") is None
    assert zone.state is ZoneState.OCCUPIED

    # The hold now runs from the second trip. Under the mutation the room
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
    # A PIR: it trips and drops again, and the hold runs from the drop.
    zone.ingest_presence(101, True, NOW)
    zone.ingest_presence(101, False, NOW)
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
    # A PIR: it trips and drops again, and the hold runs from the drop.
    zone.ingest_presence(101, True, NOW)
    zone.ingest_presence(101, False, NOW)
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


# --------------------------------------------- the level sensor (section 5.4)
#
# The Study runs an Aqara FP1 radar alongside a PIR. A radar is a LEVEL
# sensor: one "on" when somebody comes in, silence for as long as they stay,
# one "off" when they go. Every promise below was broken by measuring presence
# as `now - last_seen < hold`, and each one broke in the same direction --
# lights off with somebody sitting still in the room.


def test_a_level_sensor_holds_the_zone_occupied_for_as_long_as_it_is_on():
    """A radar reporting "on" and then nothing is presence, not a stale
    reading. Two hours of silence from an FP1 means two hours of somebody
    sitting in the chair.

    Kills: `active()` derived from last_seen alone, which is what the Study
    ran and why its lights went out on a person reading.

    Mutation applied: Presence.active's `if self.on_devices: return True` ->
    deleted, so the timestamp decides on its own.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)  # the radar sees somebody. That is all.

    assert zone.evaluate(NOW, "presence edge").to_state is ZoneState.OCCUPIED

    # Every check for the next two hours, long past the five-minute hold.
    for minutes in (4, 5, 6, 30, 120):
        assert zone.evaluate(at(minutes=minutes), "reconcile tick") is None, f"{minutes}m"
        assert zone.state is ZoneState.OCCUPIED, f"{minutes}m"
        assert zone.desired_levels(at(minutes=minutes)) == {201: 60, 202: 30}


def test_no_hold_wake_is_scheduled_while_a_sensor_is_still_reporting():
    """The other half, and the one that makes it a *silent* failure.

    Even with `active()` right, a wake-up scheduled at last_seen + hold fires
    in the middle of an occupancy. The zone would wake, find itself still
    occupied and do nothing -- but only because `active()` is right; the wake
    itself is a claim that the room might be empty, and it is wrong.

    Kills: `expiry()` returning last_seen + hold regardless of the reporting
    set.

    Mutation applied: Presence.expiry's `if self.on_devices: return None` ->
    deleted.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)
    zone.evaluate(NOW, "presence edge")

    assert zone.presence.expiry(HOLD) is None
    # The only wake-ups left are the period boundary and midnight, both hours
    # away. Nothing in the next hour claims this room might have emptied.
    assert zone.next_wake(NOW) > at(seconds=HOLD)

    # And once they leave, the hold is scheduled from THAT moment.
    zone.ingest_presence(101, False, at(hours=2))
    assert zone.next_wake(at(hours=2)) == at(hours=2, seconds=HOLD)


def test_the_off_delay_starts_when_the_last_sensor_clears():
    """Occupatum's rule exactly: the delay belongs to the clear, not to the
    last sighting (section 5.4).

    Kills: leaving `last_seen` untouched on an "off" reading. The hold would
    then run from whenever the sensor last said "on" -- which for a radar is
    when the person ARRIVED, so a two-hour visit ends the moment they stand
    up.

    Mutation applied: Presence.update's `self.last_seen = now` on the "off"
    path -> deleted.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)
    zone.evaluate(NOW, "presence edge")

    # Two hours later they leave. The hold starts HERE.
    left = at(hours=2)
    assert zone.ingest_presence(101, False, left) is True
    assert zone.presence.last_seen == left

    assert zone.evaluate(left, "sensor cleared") is None, "still occupied: the hold runs"
    assert zone.state is ZoneState.OCCUPIED
    assert zone.evaluate(left + dt.timedelta(seconds=HOLD - 1), "tick") is None
    move = zone.evaluate(left + dt.timedelta(seconds=HOLD), "presence hold expired")
    assert move.to_state is ZoneState.VACANT


def test_one_sensor_clearing_does_not_empty_a_room_another_still_reports():
    """The PIR drops; the radar has not. Any-of applies to the live set, not
    only to the last reading.

    Kills: treating any "off" as "the room is empty", which is the obvious
    reading of "the off-delay starts when a sensor clears" and is wrong.

    Mutation applied: Presence.update's `if device_id not in self.on_devices:
    return Edge.NONE` guard plus the discard -> clear the whole set.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)          # PIR
    zone.ingest_presence(102, True, at(seconds=5))  # radar
    zone.evaluate(NOW, "presence edge")

    zone.ingest_presence(101, False, at(seconds=20))  # the PIR drops

    assert zone.presence.on_devices == {102}
    assert zone.presence.expiry(HOLD) is None, "the radar is still on"
    assert zone.evaluate(at(hours=1), "reconcile tick") is None
    assert zone.state is ZoneState.OCCUPIED


def test_a_sensor_already_on_at_startup_is_seeded_and_holds_the_room():
    """The restart case. Only `last_seen` is persisted, so a zone that came
    back up would otherwise know nothing about a radar that has been on since
    before the restart -- and its persisted timestamp is already older than
    the hold.

    Kills: seeding presence from the persisted record alone. The Study's
    radar had been on for an hour when the plugin restarted; without reading
    the device the zone starts VACANT and turns the lights off.

    Mutation applied: Engine._seed_zone's `if presence_is_on(device):
    zone.ingest_presence(device_id, True, now)` -> deleted.
    """
    make_device(101, "relay", name="Study - Radar", onState=True)
    make_device(201, "dimmer", name="Study Lamp")
    make_device(202, "dimmer", name="Study Strip")
    config = make_config(
        [
            {
                "name": "Study",
                "presence_devices": [101],
                "hold_seconds": HOLD,
                "lux": None,
                "lights": [201, 202],
                "periods": [
                    make_period("Evening", "18:00", "23:00", levels={"201": 60, "202": 30})
                ],
            }
        ]
    )
    engine = Engine(config, FixedSun(), RecordingCommander(), logger=LOG)
    zone = engine.zones["Study"]

    engine.seed_inputs(NOW)

    assert zone.presence.on_devices == {101}, "the radar was on; the zone must know"

    # Well past the hold, with nothing on the device bus at all.
    engine.mark_all_dirty("startup")
    engine.tick(NOW)
    assert zone.state is ZoneState.OCCUPIED
    assert engine.tick(at(minutes=30)) is not None
    assert zone.state is ZoneState.OCCUPIED, "a seeded level sensor holds the room"


def test_explain_says_which_sensor_is_holding_the_room():
    """"active" has two causes now and the line has to say which.

    A reader who sees only "last seen 20:00:03" two hours later concludes the
    sensor has stopped reporting, when in fact it is on and that is precisely
    why the room is occupied.

    Kills: reporting last_seen whatever the reporting set holds.
    """
    zone = a_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)
    zone.evaluate(NOW, "presence edge")

    line = zone.explain(at(hours=2))
    assert "presence=active" in line
    assert "on)" in line, line
    assert "last seen" not in line, "a sensor that is ON is not a stale sighting"

    zone.ingest_presence(101, False, at(hours=2))
    zone.evaluate(at(hours=2), "sensor cleared")
    held = zone.explain(at(hours=2))
    assert "presence=active (hold, last seen 22:00:00)" in held, held
