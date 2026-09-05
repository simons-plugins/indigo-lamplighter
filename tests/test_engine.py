"""The engine: classification on the callback thread, decisions on the worker.

The promise files pin the rules. This file pins the wiring -- that an event
reaches the right rule, that the worker's timers fire without anything on the
device bus, and that a configuration reload does not throw away the house's
state. It reads as two long scenarios and a set of short questions, because
the wiring is only interesting end to end: every individual step of the
evening below is already covered somewhere else.
"""

import datetime as dt
import logging

import pytest
from helpers import (
    FixedSun,
    RecordingCommander,
    apply_level,
    make_config,
    make_device,
    make_period,
    make_snapshot,
    make_zone_document,
)

from lamplighter import compare
from lamplighter.engine import Engine, presence_is_on, presence_reading
from lamplighter.zone import ZoneState

LOGGER_NAME = "test.engine"
LOG = logging.getLogger(LOGGER_NAME)

#: 19:00 on a September evening: dark enough for the Kitchen sensor, well
#: inside the Evening band, and before the period boundary at 23:00.
EVENING = dt.datetime(2026, 9, 4, 19, 0, 0)


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


class Clock:
    """A clock a test moves by hand. Nothing here ever sleeps."""

    def __init__(self, now=EVENING):
        self.now = now

    def __call__(self):
        return self.now

    def at(self, **kwargs):
        self.now = EVENING + dt.timedelta(**kwargs)
        return self.now


def build(commander=None, clock=None, **zone_fields):
    """A one-zone engine: two lights, one presence device, one lux sensor."""
    zone_fields.setdefault("name", "Kitchen")
    zone_fields.setdefault("lights", [201, 202])
    zone_fields.setdefault("presence_devices", [101])
    zone_fields.setdefault("hold_seconds", 300)
    zone_fields.setdefault("lux", {"device": 302, "dark_below": 2200, "hysteresis": 300})
    zone_fields.setdefault(
        "periods",
        [make_period("Evening", "18:00", "23:00", levels={"201": 60, "202": 30})],
    )
    sun = FixedSun()
    config = make_config([make_zone_document(**zone_fields)], sun=sun)
    clock = clock or Clock()
    changed = []
    engine = Engine(
        config,
        sun,
        commander or RecordingCommander(apply=True),
        logger=LOG,
        clock=clock,
        on_zone_changed=changed.append,
    )
    return engine, engine.zones[zone_fields["name"]], clock, changed


def presence(dev_id, before, after):
    return make_snapshot(dev_id, onState=before), make_snapshot(dev_id, onState=after)


def light(dev_id, before, after, name="Kitchen Pendants"):
    return (
        make_snapshot(dev_id, brightness=before, name=name),
        make_snapshot(dev_id, brightness=after, name=name),
    )


def lux(dev_id, before, after):
    return (
        make_snapshot(dev_id, device_cls="sensor", sensorValue=before),
        make_snapshot(dev_id, device_cls="sensor", sensorValue=after),
    )


# ------------------------------------------------------------- one evening


def test_an_evening_in_one_zone():
    """Dark and empty, somebody arrives, somebody reaches for the dial, the
    room empties. The whole of section 5.3 in the order it happens.

    This is the scenario the plugin exists for, and every step is a claim the
    fork got wrong at least once: the lights come on from presence and not
    from a device event, a dial move is an override inside the same callback
    that carried it, an overridden zone is not written to at all, and the
    override is released by the room emptying rather than by running its hour
    out (fork #17).
    """
    commander = RecordingCommander(apply=True)
    engine, zone, clock, changed = build(commander)

    make_device(201, "dimmer", brightness=0, name="Kitchen Pendants")
    make_device(202, "dimmer", brightness=0, name="Kitchen Strips")
    make_device(302, "sensor", sensorValue=1200)

    # 19:00 -- the room is dark and empty. Nothing to do: the lights are
    # already off, so an empty room costs no commands at all.
    engine.device_updated(*lux(302, 4000, 1200), clock.now)
    engine.mark_all_dirty("startup")
    summary = engine.tick(clock.now)
    assert zone.state is ZoneState.VACANT
    assert zone.is_dark() is True
    assert summary.commands == ()
    assert commander.commands == []

    # 19:00:10 -- somebody walks in. The engine classifies the event on the
    # callback thread and marks the zone; the worker does the deciding.
    now = clock.at(seconds=10)
    edges = engine.device_updated(*presence(101, False, True), now)
    assert [edge.kind for edge in edges] == ["presence"]
    assert engine.dirty == {"Kitchen": "presence: Dev-101"}
    assert commander.commands == [], "the callback thread never writes"

    summary = engine.tick(now)
    assert zone.state is ZoneState.OCCUPIED
    assert commander.commands == [(201, 60), (202, 30)]
    assert [command.level for command in summary.commands] == [60, 30]
    assert zone.writes_today == 2
    assert changed and changed[-1] is zone, "M2 must be told the zone moved"

    # 19:00:40 -- the wall dimmer takes the pendants down to 20. The override
    # is created inside this call, before anything could revert it.
    now = clock.at(seconds=40)
    apply_level(make_device(201, "dimmer", brightness=20, name="Kitchen Pendants"), 20)
    edges = engine.device_updated(*light(201, 60, 20), now)
    assert [edge.kind for edge in edges] == ["override"]
    assert zone.override is not None
    assert zone.override.device_id == 201

    summary = engine.tick(now)
    assert zone.state is ZoneState.OVERRIDDEN
    assert summary.commands == ()

    # ...and it stays that way. Every tick for the next four minutes -- four
    # periodic reconcile passes -- writes nothing at all.
    commander.clear()
    for minute in range(1, 5):
        engine.tick(clock.at(seconds=40, minutes=minute))
    assert commander.commands == [], (
        "the reconcile tick wrote to an overridden zone; the person's level "
        "would have snapped back a minute after they set it"
    )
    assert make_device_level(201) == 20

    # 19:05:10 -- the presence hold (300 s from the last sighting at 19:00:10)
    # runs out. Nothing at all has happened on the device bus since; the zone
    # wakes on its own timer.
    now = clock.at(seconds=311)
    assert engine.wakes["Kitchen"] == EVENING + dt.timedelta(seconds=310)
    summary = engine.tick(now)

    assert zone.override is None, (
        "unlock_on_leave must release an override taken while the room was "
        "occupied -- fork #17 armed it only for locks taken in an empty room"
    )
    assert zone.state is ZoneState.VACANT
    assert [transition.to_state for transition in summary.transitions] == [ZoneState.VACANT]
    assert commander.commands == [(201, "off"), (202, "off")]
    assert make_device_level(201) == 0 and make_device_level(202) == 0


def make_device_level(dev_id):
    import indigo

    return indigo.devices[dev_id].brightness


# ---------------------------------------------------------------- the race


def test_our_echo_after_the_plan_reverts_does_not_lock_but_a_real_change_does():
    """The fork's #16 race, end to end through the engine.

    We command a light on; presence drops before the light reports; the plan
    is now off, which is exactly the state we commanded the light AWAY from.
    The queued echo arrives as a textbook at-desired -> off-desired transition
    and must not lock. The very next change -- a person putting the light
    somewhere we never asked any device to go -- must.

    Both halves matter. The echo book is a licence to ignore evidence, so its
    bounds are the promise: one command, one transition, and then the zone
    listens again.
    """
    commander = RecordingCommander(apply=False)  # the light reports late
    engine, zone, clock, _changed = build(commander)

    make_device(201, "dimmer", brightness=0, name="Kitchen Pendants")
    make_device(202, "dimmer", brightness=30, name="Kitchen Strips")
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)
    assert zone.state is ZoneState.OCCUPIED
    assert commander.commands == [(201, 60)], "202 was already at its level"
    assert engine.echo_book.pending(201) == (0,), (
        "the record is the state the light was commanded AWAY from"
    )

    # The lights the zone just switched on lift the in-room sensor over the
    # threshold, so the room is no longer dark and the plan reverts to off --
    # onto the very state the light was commanded away from. This is fork #16,
    # and it happens in seconds, well inside the echo window.
    now = clock.at(seconds=2)
    edges = engine.device_updated(*lux(302, 1200, 2600), now)
    assert [edge.kind for edge in edges] == ["lux"]
    engine.tick(now)
    assert zone.state is ZoneState.OFF_DUTY and zone.off_duty_cause == "bright"
    assert zone.desired_levels(now)[201] == "off"

    # The queued deviceUpdated for our own command finally lands.
    now = clock.at(seconds=4)
    assert engine.device_updated(*light(201, 0, 60), now) == []
    assert zone.override is None, (
        "our own echo locked the zone after the plan reverted; that is fork "
        "#16, and it is why the pre-command state is recorded at all"
    )
    assert engine.echo_book.pending(201) == (), "the excuse is spent"

    # And now a person, moving the light to a value we have never commanded
    # anything to. One command excuses one transition, and this is not it.
    now = clock.at(seconds=6)
    edges = engine.device_updated(*light(201, 0, 45), now)
    assert [edge.kind for edge in edges] == ["override"]
    assert zone.override is not None and zone.override.device_id == 201


# ------------------------------------------------------------ the classifier


def test_a_device_no_zone_uses_costs_nothing():
    """The callback thread sees every device in the server. A boiler, a train
    board and a doorbell must cost a dictionary lookup and nothing else."""
    engine, zone, clock, _changed = build()
    zone.ingest_presence = lambda *a, **k: pytest.fail("an unrelated device reached the zone")
    zone.ingest_lux = lambda *a, **k: pytest.fail("an unrelated device reached the zone")

    assert engine.device_updated(*light(999, 0, 100), clock.now) == []
    assert engine.dirty == {}


def test_a_lux_reading_that_does_not_cross_the_threshold_is_not_an_edge():
    """The sensor reports every few seconds; the verdict moves twice a day."""
    engine, zone, clock, _changed = build()
    make_device(302, "sensor", sensorValue=1200)
    engine.device_updated(*lux(302, 4000, 1200), clock.now)
    engine.mark_all_dirty("startup")
    engine.tick(clock.now)
    assert zone.lux.verdict is True

    for second, (before, after) in enumerate(
        [(1200, 1800), (1800, 2100), (2100, 2400), (2400, 2199)], start=1
    ):
        assert engine.device_updated(*lux(302, before, after), clock.at(seconds=second)) == []
    assert engine.dirty == {}

    # Clear of the hysteresis band, the verdict flips and the zone re-plans.
    edges = engine.device_updated(*lux(302, 2199, 2600), clock.at(seconds=10))
    assert [edge.kind for edge in edges] == ["lux"]
    assert zone.lux.verdict is False


def test_a_dark_below_variable_change_re_plans_only_when_the_verdict_flips():
    """The Kitchen threshold is tuned from a control page, so the variable is
    an input like any other -- and like any other, only its edges count."""
    import indigo

    engine, zone, clock, _changed = build(
        lux={
            "device": 302,
            "dark_below": 2200,
            "dark_below_variable_id": 77,
            "hysteresis": 0,
        }
    )
    indigo.variables[77] = indigo.Variable(77, "kitchen_dark_below", "2200")
    make_device(302, "sensor", sensorValue=2000)
    engine.device_updated(*lux(302, 4000, 2000), clock.now)
    engine.mark_all_dirty("startup")
    engine.tick(clock.now)
    assert zone.lux.verdict is True

    # Nudged, but not past the reading.
    indigo.variables[77].value = "2100"
    assert engine.variable_updated(77, clock.at(seconds=1)) == []

    # Dropped below it: the room is now "bright" by configuration.
    indigo.variables[77].value = "1900"
    edges = engine.variable_updated(77, clock.at(seconds=2))
    assert [edge.kind for edge in edges] == ["variable"]
    assert zone.lux.verdict is False

    # A variable no zone gates on is not this engine's business.
    indigo.variables[78] = indigo.Variable(78, "something_else", "1")
    assert engine.variable_updated(78, clock.at(seconds=3)) == []


def test_presence_readings_are_any_of_across_the_three_places_indigo_puts_them():
    """A plugin device that publishes only `states["onOffState"]` is still a
    presence device, and a tuple of all three is what the gate compares."""
    attribute_only = make_snapshot(101, onState=True)
    assert presence_is_on(attribute_only) is True

    states_only = make_snapshot(101, device_cls="device", onState=True)
    del states_only.onState
    del states_only.states["onState"]
    assert presence_reading(states_only) == (None, None, True)
    assert presence_is_on(states_only) is True

    nothing_at_all = make_snapshot(101, device_cls="device", onState=False)
    assert presence_is_on(nothing_at_all) is False


# ---------------------------------------------------------------- the timers


def test_a_period_boundary_wakes_a_quiet_zone():
    """An empty, quiet room still notices the period ended: the wake-up comes
    from the zone's own timer, not from anything on the device bus."""
    engine, zone, clock, _changed = build(
        hold_seconds=10800,  # the hold expires at 22:00, after the boundary
        periods=[make_period("Evening", "18:00", "21:00", levels={"201": 60, "202": 30})],
    )
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)
    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)
    assert zone.state is ZoneState.OCCUPIED

    boundary = dt.datetime(2026, 9, 4, 21, 0)
    assert engine.wakes["Kitchen"] == boundary
    assert engine.next_wake(clock.now) == EVENING + dt.timedelta(seconds=60), (
        "the worker wakes for the earlier of the zone's timer and the "
        "reconcile tick"
    )

    summary = engine.tick(boundary)
    assert [transition.cause for transition in summary.transitions] == ["period boundary"]
    assert zone.state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "no_period"
    assert summary.commands == (), "no period means no opinion, not lights off"


def test_the_periodic_pass_runs_every_reconcile_seconds():
    """A device that quietly drifts off desired between events is picked up by
    the tick and by nothing else. This is what replaces the recovery scan."""
    commander = RecordingCommander(apply=True)
    engine, zone, clock, _changed = build(commander)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)
    commander.clear()

    # Something else in the house dims the pendants. No event reaches us.
    apply_level(make_device(201, "dimmer", brightness=5), 5)
    engine.tick(clock.at(seconds=30))
    assert commander.commands == [], "the tick is every reconcile_seconds, not every call"

    engine.tick(clock.at(seconds=61))
    assert commander.commands == [(201, 60)]


def test_run_forever_stops_and_sleeps_no_longer_than_the_next_wake():
    """The worker loop is `tick`, `next_wake`, sleep -- and the sleep is
    injected, so nothing in this suite ever waits for wall-clock time."""
    engine, _zone, clock, _changed = build()
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    slept = []

    class StopAfter:
        def __init__(self, rounds):
            self.rounds = rounds

        def is_set(self):
            self.rounds -= 1
            return self.rounds < 0

    def sleep_fn(seconds):
        slept.append(seconds)
        clock.now = clock.now + dt.timedelta(seconds=seconds)

    engine.run_forever(StopAfter(3), sleep_fn)

    assert len(slept) == 3
    assert all(0 <= seconds <= 60 for seconds in slept), slept


def test_a_zone_changed_callback_that_raises_does_not_stop_the_engine(caplog):
    """The callback publishes device states and persists. It must not be able
    to leave the next zone unreconciled (R15: say so, then carry on)."""
    sun = FixedSun()
    config = make_config(
        [
            make_zone_document(
                name="Hallway",
                lights=[201],
                periods=[make_period("Evening", "18:00", "23:00", levels={"201": 60})],
            ),
            make_zone_document(
                name="Study",
                lights=[202],
                periods=[make_period("Evening", "18:00", "23:00", levels={"202": 30})],
            ),
        ],
        sun=sun,
    )
    commander = RecordingCommander(apply=True)

    def explode(zone):
        raise RuntimeError(f"publishing {zone.name} failed")

    engine = Engine(config, sun, commander, logger=LOG, clock=Clock(), on_zone_changed=explode)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    engine.device_updated(*presence(101, False, True), EVENING)
    engine.mark_all_dirty("startup")

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        engine.tick(EVENING)

    assert commander.commands == [(201, 60), (202, 30)], (
        "one zone's callback raising left the other zone unreconciled"
    )
    assert len(caplog.records) == 2
    assert "may be behind" in caplog.records[0].getMessage()


# ---------------------------------------------------------------- the reload


def test_a_reload_keeps_the_override_and_the_presence_hold():
    """R13, through the engine: a config edit at 19:50 must not throw away an
    override taken at 19:46. The fork announced "all locks and zone state has
    been reset" on every single reload."""
    engine, zone, clock, _changed = build()
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)
    now = clock.at(seconds=40)
    apply_level(make_device(201, "dimmer", brightness=20), 20)
    engine.device_updated(*light(201, 60, 20), now)
    engine.tick(now)
    assert zone.state is ZoneState.OVERRIDDEN
    taken_at = zone.override.since

    # Somebody edits an unrelated field of the same zone.
    sun = engine.sun
    edited = make_config(
        [
            make_zone_document(
                name="Kitchen",
                lights=[201, 202],
                presence_devices=[101],
                hold_seconds=600,  # the edit
                lux={"device": 302, "dark_below": 2200, "hysteresis": 300},
                periods=[make_period("Evening", "18:00", "23:00", levels={"201": 60, "202": 30})],
            )
        ],
        sun=sun,
    )
    engine.reload(edited, clock.at(seconds=50))
    reloaded = engine.zones["Kitchen"]

    assert reloaded is not zone, "the zone object is rebuilt from the file"
    assert reloaded.config.hold_seconds == 600, "the edit took effect"
    assert reloaded.override is not None
    assert reloaded.override.device_id == 201
    assert reloaded.override.since == taken_at
    assert reloaded.presence.last_seen == EVENING
    assert reloaded.lux.verdict is True, "the Schmitt trigger's memory IS the verdict"

    engine.tick(clock.at(seconds=51))
    assert reloaded.state is ZoneState.OVERRIDDEN


def test_a_reload_that_removes_a_zone_forgets_its_timers_and_its_backoff():
    """A zone deleted from the file stops being the engine's business, and
    takes its per-device bookkeeping with it -- a plugin that ran for a year
    must not carry a record per device ever removed."""
    engine, zone, clock, _changed = build()
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)
    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)
    assert engine.wakes and engine.echo_book.pending(201) == (0,)

    replacement = make_config(
        [
            make_zone_document(
                name="Study",
                lights=[203],
                periods=[make_period("Evening", "18:00", "23:00", levels={"203": 60})],
            )
        ],
        sun=engine.sun,
    )
    engine.reload(replacement, clock.at(seconds=5))

    assert set(engine.zones) == {"Study"}
    assert "Kitchen" not in engine.wakes
    assert engine.echo_book.pending(201) == ()
    assert engine.reconciler.backoff_step(201) == 0


# ---------------------------------------------------------------- the actions


def test_the_actions_move_the_zone_without_a_device_event():
    """Reset override, set enabled, and the global enable: each is an input
    edge like any other, and each takes effect on the next tick."""
    commander = RecordingCommander(apply=True)
    engine, zone, clock, _changed = build(commander)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)
    assert zone.state is ZoneState.OCCUPIED

    engine.lock_zone("Kitchen", clock.at(seconds=10))
    engine.tick(clock.now)
    assert zone.state is ZoneState.OVERRIDDEN

    assert engine.reset_override() == ["Kitchen"]
    engine.tick(clock.at(seconds=20))
    assert zone.state is ZoneState.OCCUPIED
    assert zone.override is None

    # Disabled: the plugin has no opinion, so nothing is written either way.
    commander.clear()
    assert engine.set_plugin_enabled(False) is True
    engine.tick(clock.at(seconds=30))
    assert zone.state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "disabled"
    assert commander.commands == [], "a disabled plugin leaves the lights alone"

    assert engine.set_plugin_enabled(True) is True
    engine.tick(clock.at(seconds=40))
    assert zone.state is ZoneState.OCCUPIED


# ---------------------------------------------------------------- the seeding
#
# The first run on jarvis: the Hallway zone was switched on by a configuration
# reload while its Occupatum presence device was reporting and the lamp was at
# 100. Lamplighter logged `off_duty -> vacant (configuration reloaded)` with
# `presence_active=False, presence_last_seen=None, lux=None` and then turned
# the lamp off on the person standing under it.
#
# One cause, two gaps: a zone only ever learned presence and lux from later
# device *edges*, so at the moment it was built, reloaded or enabled it had no
# evidence at all -- and no evidence is indistinguishable from an empty, bright
# room. These pin the fix: a zone reads its own input devices before it is
# allowed to decide anything.


def test_a_zone_enabled_while_the_room_is_occupied_does_not_turn_the_lights_off():
    """The jarvis defect, in one test.

    Kills: leaving a freshly built or reloaded zone to wait for an edge. With
    seeding skipped the zone evaluates VACANT from `last_seen=None` and the
    reconcile pass commands the lamp off -- on an occupied room, four seconds
    after a configuration reload.
    """
    commander = RecordingCommander(apply=True)
    engine, zone, clock, _changed = build(commander, enabled=False)
    make_device(101, "relay", name="Hallway Motion", onState=True)
    make_device(201, "dimmer", brightness=60)
    make_device(202, "dimmer", brightness=30)
    make_device(302, "sensor", sensorValue=1200)

    assert engine.set_zone_enabled("Kitchen", True) is True
    engine.tick(clock.now)

    assert zone.state is ZoneState.OCCUPIED
    assert zone.presence.last_seen == clock.now, "an occupied room at startup is occupied NOW"
    assert commander.commands == [], "the lights are already at their levels"


def test_a_zone_reads_its_lux_sensor_before_its_first_decision():
    """Kills: leaving the verdict at its default until the sensor speaks.

    A sensor that reports every few minutes leaves a zone deciding from a
    verdict nobody took for as long as it stays quiet, and the default is the
    one that takes an indoor zone off duty in a dark room.
    """
    engine, zone, clock, _changed = build()
    make_device(101, "relay", onState=False)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    assert zone.lux.value is None
    engine.tick(clock.now)

    assert zone.lux.value == 1200
    assert zone.lux.verdict is True, "1200 is below the 2200 threshold"


def test_a_presence_device_reporting_off_at_seeding_changes_nothing():
    """Presence ends by the hold expiring, never by a sensor being quiet.

    Kills: ingesting the reading whatever it says, which stamps `last_seen`
    on every reload and makes an empty room look occupied for a whole hold.
    """
    engine, zone, clock, _changed = build()
    make_device(101, "relay", onState=False)
    make_device(302, "sensor", sensorValue=1200)

    engine.seed_inputs(clock.now)

    assert zone.presence.last_seen is None


def test_a_lookup_that_failed_at_seeding_is_retried_and_decides_nothing_meanwhile(monkeypatch):
    """A busy server must not cost a zone its evidence, or its future.

    Kills two opposite mistakes. Giving up after the first failure leaves the
    zone permanently unseeded, so it decides from nothing for ever; evaluating
    it anyway while unseeded decides VACANT from a gap and turns the lights
    off. Neither shows up as an error: both look like a quiet zone.
    """
    from lamplighter import devices as devices_module

    commander = RecordingCommander(apply=True)
    engine, zone, clock, _changed = build(commander)
    make_device(101, "relay", name="Kitchen Motion", onState=True)
    make_device(201, "dimmer", brightness=60)
    make_device(202, "dimmer", brightness=30)
    make_device(302, "sensor", sensorValue=1200)

    real = devices_module.get_device
    unanswered = {"still": True}

    def flaky(dev_id):
        if unanswered["still"] and dev_id == 101:
            raise devices_module.LookupFailed(dev_id, RuntimeError("the server is busy"))
        return real(dev_id)

    monkeypatch.setattr(devices_module, "get_device", flaky)

    # The live path: a reload marks every zone dirty, so this zone WOULD be
    # evaluated on this pass if the unseeded guard were not there.
    engine.mark_all_dirty("configuration reloaded")
    engine.tick(clock.now)
    assert engine.unseeded == ("Kitchen",)
    assert zone.state is not ZoneState.VACANT, "an unseeded zone must not decide it is empty"
    assert commander.commands == [], "and it must not write on the strength of that"
    assert "Kitchen" in engine.dirty, "the cause is kept for the pass that can read the devices"

    unanswered["still"] = False
    engine.tick(clock.at(seconds=1))

    assert engine.unseeded == ()
    assert zone.state is ZoneState.OCCUPIED
    assert commander.commands == []


def test_a_presence_device_that_is_gone_is_warned_about_once_and_not_retried_for_ever():
    """Gone is a configuration problem; asking again will never fix it.

    Kills: treating DeviceGone as a retryable failure, which leaves the zone
    unseeded for ever and therefore never evaluated at all -- the whole zone
    silently stops, from one id left behind in the configuration.
    """
    engine, zone, clock, _changed = build()
    make_device(302, "sensor", sensorValue=1200)

    engine.seed_inputs(clock.now)

    assert engine.unseeded == ()


def test_a_live_presence_reading_refreshes_an_older_persisted_timestamp():
    """Restore first, then seed: the record is what the zone knew, the device
    is what the room is doing.

    Kills: seeding before restore, which lets a stale persisted timestamp
    overwrite the fact that somebody is in the room right now.
    """
    from lamplighter import persist

    engine, zone, clock, _changed = build()
    make_device(101, "relay", onState=True)
    make_device(302, "sensor", sensorValue=1200)

    engine.restore(
        {"Kitchen": {"version": persist.VERSION, "presence_last_seen": "2026-09-04T18:00:00"}},
        clock.now,
    )
    assert zone.presence.last_seen == dt.datetime(2026, 9, 4, 18, 0, 0)

    engine.seed_inputs(clock.now)

    assert zone.presence.last_seen == clock.now


def test_a_reload_re_seeds_every_zone():
    """R13 restores what the zone knew; only the devices know the room.

    Kills: carrying the old zone's inputs across a reload and calling it
    seeded. The rebuilt zone's `enabled` comes from the file, so a reload is
    exactly when a zone can go from off to on with no idea what the room is
    doing.
    """
    engine, zone, clock, _changed = build(enabled=False)
    make_device(101, "relay", onState=True)
    make_device(201, "dimmer", brightness=60)
    make_device(202, "dimmer", brightness=30)
    make_device(302, "sensor", sensorValue=1200)
    engine.tick(clock.now)

    sun = FixedSun()
    reloaded = make_config(
        [
            make_zone_document(
                name="Kitchen",
                lights=[201, 202],
                presence_devices=[101],
                hold_seconds=300,
                lux={"device": 302, "dark_below": 2200, "hysteresis": 300},
                enabled=True,
                periods=[make_period("Evening", "18:00", "23:00", levels={"201": 60, "202": 30})],
            )
        ],
        sun=sun,
    )
    engine.reload(reloaded, clock.at(seconds=10))
    assert engine.unseeded == ("Kitchen",)

    engine.tick(clock.now)

    assert engine.zones["Kitchen"].state is ZoneState.OCCUPIED
    assert engine.zones["Kitchen"].presence.last_seen == clock.now
