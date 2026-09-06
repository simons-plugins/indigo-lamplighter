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
from lamplighter.reconcile import COMMAND_RECHECK_SECONDS
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
    # The PIR's own hardware hold is short and it drops again immediately.
    # THAT is what starts the zone's 300 s hold: while a sensor is reporting
    # there is no hold running at all. A level sensor (the Study's radar)
    # would stay on here and hold the room open until it cleared.
    engine.device_updated(*presence(101, True, False), now)
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


#: The house's phone-presence variable, set to "true"/"false" by the app.
SIMON_HOME = 1872770829


def test_a_presence_variable_turning_true_makes_the_zone_occupied():
    """SimonHome is a presence input like any device (PRD 5.4): true makes
    the zone occupied, and false starts the hold rather than ending it
    outright -- the same shape as `test_an_evening_in_one_zone`'s device.

    Kills: `variable_updated` never looking at `presence_variables` at all.
    """
    import indigo

    engine, zone, clock, _changed = build(presence_variables=[SIMON_HOME])
    indigo.variables[SIMON_HOME] = indigo.Variable(SIMON_HOME, "SimonHome", "false")
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)
    engine.mark_all_dirty("startup")
    engine.tick(clock.now)
    assert zone.state is ZoneState.VACANT

    indigo.variables[SIMON_HOME].value = "true"
    edges = engine.variable_updated(SIMON_HOME, clock.at(seconds=1))
    assert [edge.kind for edge in edges] == ["presence"]
    assert engine.dirty == {"Kitchen": "presence: variable SimonHome"}
    engine.tick(clock.at(seconds=1))
    assert zone.state is ZoneState.OCCUPIED

    # False clears the reporting variable but does not end presence outright:
    # the hold has to run out first, exactly as it does for a device.
    indigo.variables[SIMON_HOME].value = "false"
    edges = engine.variable_updated(SIMON_HOME, clock.at(seconds=2))
    assert [edge.kind for edge in edges] == ["presence"]
    engine.tick(clock.at(seconds=2))
    assert zone.state is ZoneState.OCCUPIED, "the hold has not expired yet"

    engine.tick(clock.at(seconds=2 + 300 + 1))
    assert zone.state is ZoneState.VACANT


def test_a_presence_variable_true_at_startup_is_seeded():
    """The R-seed rule for a variable: a zone enabled while SimonHome already
    reads "true" starts occupied, without any `variable_updated` call.

    Kills: seeding reading `presence_devices` and skipping
    `presence_variables` entirely -- the same jarvis defect
    (`test_a_zone_enabled_while_the_room_is_occupied_does_not_turn_the_lights_off`)
    repeated for a variable instead of a sensor.
    """
    import indigo

    engine, zone, clock, _changed = build(presence_variables=[SIMON_HOME])
    indigo.variables[SIMON_HOME] = indigo.Variable(SIMON_HOME, "SimonHome", "true")
    make_device(101, "relay", onState=False)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    engine.seed_inputs(clock.now)

    assert SIMON_HOME in zone.presence.on_devices
    engine.mark_all_dirty("startup")
    engine.tick(clock.now)
    assert zone.state is ZoneState.OCCUPIED


def test_a_missing_presence_variable_is_warned_once_and_read_as_off(caplog):
    """A deleted or mistyped variable id must never take the callback thread
    down, and must never read as presence.

    Kills: letting the `KeyError` from `indigo.variables[...]` escape, and
    treating "cannot look this up" as "somebody is home".
    """
    engine, zone, clock, _changed = build(presence_variables=[SIMON_HOME])
    # SIMON_HOME is deliberately never installed in indigo.variables.
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        edges = engine.variable_updated(SIMON_HOME, clock.now)
        edges += engine.variable_updated(SIMON_HOME, clock.at(seconds=1))

    assert edges == []
    assert SIMON_HOME not in zone.presence.on_devices
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "warned once, not on every call"

    engine.mark_all_dirty("startup")
    engine.tick(clock.at(seconds=2))
    assert zone.state is not ZoneState.OCCUPIED


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("True ", True),
        ("on", True),
        ("yes", True),
        ("1", True),
        ("home", True),
        ("false", False),
        ("", False),
        ("0", False),
        ("away", False),
    ],
)
def test_variable_truthiness_word_list(value, expected):
    """The exact word list PRD 5.4 documents -- neither wider nor narrower."""
    from lamplighter.engine import variable_is_on

    assert variable_is_on(value) is expected


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

    # The lights were just commanded, so the next wake is the five-second
    # re-check and not the boundary. It finds them where they were asked to
    # be, sends nothing, and the zone's own timers take over again.
    recheck = EVENING + dt.timedelta(seconds=COMMAND_RECHECK_SECONDS)
    assert engine.wakes["Kitchen"] == recheck
    assert engine.tick(recheck).commands == ()

    boundary = dt.datetime(2026, 9, 4, 21, 0)
    assert engine.wakes["Kitchen"] == boundary
    assert engine.next_wake(recheck) == EVENING + dt.timedelta(seconds=60), (
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

    # Consume the post-command re-check first. The lights landed, so it sends
    # nothing and hands the zone back to its own timers -- which is what
    # leaves the periodic pass as the only thing watching for drift.
    engine.tick(clock.at(seconds=COMMAND_RECHECK_SECONDS))
    assert commander.commands == []

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


# ------------------------------------------------- the post-command re-check
#
# A command is the one thing a zone's own timers know nothing about. Before
# this existed, a device that simply ignored a command waited for the periodic
# pass -- up to `reconcile_seconds`, a minute by default -- and a device that
# was merely slow to report was warned about at that same pass. One wake-up
# five seconds after any command fixes both, and it is a timer entry rather
# than a poll: PRD section 9 rules out the settle poll, not the clock.


def misses(caplog):
    """The reconcile "did not reach its desired level" warnings, and only those."""
    return [r for r in caplog.records if "did not reach" in r.getMessage()]


def test_a_command_brings_the_zones_next_wake_forward_to_re_check_it():
    """Kills: leaving the wake at the zone's own timers after a command.

    The zone would then not look at the device again until a period boundary,
    a hold expiry or the periodic pass -- whichever came first, and none of
    them is about the command that was just sent.
    """
    engine, zone, clock, _changed = build()
    make_device(101, "relay", onState=False)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    summary = engine.tick(clock.now)

    assert summary.commands, "this pass must have sent something to be a test"
    assert engine.wakes["Kitchen"] == clock.now + dt.timedelta(
        seconds=COMMAND_RECHECK_SECONDS
    )
    # The zone's own next wake is hours away; the re-check is what brought it in.
    assert zone.next_wake(clock.now) > clock.now + dt.timedelta(seconds=60)


def test_an_ignored_command_is_re_sent_about_five_seconds_later_with_one_warning(caplog):
    """A device that did not listen gets a second command in seconds, not at
    the next periodic pass.

    Kills: no re-check wake. The retry then waits for the reconcile tick, so a
    light that missed its command stays wrong for up to a minute -- which for
    a hallway is the whole of the time somebody is walking through it.

    Mutation applied: Engine._schedule_wake's `recheck if wake is None else
    min(wake, recheck)` -> `wake`.
    """
    commander = RecordingCommander(apply=False)  # nothing ever lands
    engine, zone, clock, _changed = build(commander)
    make_device(101, "relay", onState=False)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        engine.tick(clock.now)
        assert commander.ids() == [201, 202]
        assert misses(caplog) == [], "the first command toward a target never warns"

        # Five seconds later, and well before the 60 s periodic pass.
        commander.clear()
        recheck = clock.at(seconds=COMMAND_RECHECK_SECONDS)
        summary = engine.tick(recheck)

    assert commander.ids() == [201, 202], "the ignored command was not re-sent"
    assert [command.backoff_step for command in summary.commands] == [2, 2]
    assert len(misses(caplog)) == 2, "one warning per device, at the first genuine miss"


def test_a_device_that_lands_by_the_re_check_clears_silently(caplog):
    """The common case, and the one the false warnings came from.

    A Z-Wave lamp reports back a second or two after being told. The re-check
    finds it where it was asked to be, clears the ladder and says nothing.

    Kills: warning on the re-check regardless of what the device now reads.
    """
    engine, zone, clock, _changed = build()  # the default commander applies
    make_device(101, "relay", onState=False)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        engine.tick(clock.now)
        summary = engine.tick(clock.at(seconds=COMMAND_RECHECK_SECONDS))

    assert summary.commands == (), "nothing to do: the lamps landed"
    assert misses(caplog) == [], [r.getMessage() for r in caplog.records]
    assert engine.reconciler.backoff_step(201) == 0, "the ladder was cleared"
    # And the zone is back on its own timers rather than re-checking for ever.
    assert engine.wakes["Kitchen"] > clock.at(seconds=COMMAND_RECHECK_SECONDS * 2)


def test_the_hallway_false_warning_does_not_come_back(caplog):
    """The live regression, end to end, four times an evening on jarvis.

    Hold expires -> lamp commanded off -> the PIR re-trips within a minute,
    before any pass has observed the lamp at off -> the zone wants 80 again.
    The lamp had done exactly what it was told, twice, and was reported as

        did not reach its desired level. It reads 0 and the zone wants 80

    Kills: any regression that lets a ladder built for one target be read as
    a failure of the next one -- through the engine, not just the reconciler,
    because the wake scheduling is what decides when the second look happens.
    """
    commander = RecordingCommander(apply=True)
    engine, zone, clock, _changed = build(commander, hold_seconds=300)
    make_device(101, "relay", onState=False)
    lamp = make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    # Somebody walks through: the PIR trips and drops, the lamps come on.
    engine.device_updated(*presence(101, False, True), clock.now)
    engine.device_updated(*presence(101, True, False), clock.now)
    engine.tick(clock.now)
    assert zone.state is ZoneState.OCCUPIED
    engine.tick(clock.at(seconds=COMMAND_RECHECK_SECONDS))  # they landed

    # The hold expires. The lamps are commanded off and DO go off.
    empty = clock.at(seconds=300)
    commander.clear()
    summary = engine.tick(empty)
    assert [command.level for command in summary.commands] == ["off", "off"]
    assert lamp.brightness == 0

    # The PIR re-trips 30 s later -- before the five-second re-check has been
    # given a chance to observe the lamps at off, because no worker pass has
    # run in between. This is the exact gap the false warning came from.
    back = clock.at(seconds=330)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        engine.device_updated(*presence(101, False, True), back)
        engine.device_updated(*presence(101, True, False), back)
        summary = engine.tick(back)

    assert zone.state is ZoneState.OCCUPIED
    assert [command.level for command in summary.commands] == [60, 30]
    assert [command.backoff_step for command in summary.commands] == [1, 1], (
        "a first attempt at a new target, not a retry of the 'off'"
    )
    assert misses(caplog) == [], (
        "the lamp did what it was told twice and was called broken: "
        + "; ".join(r.getMessage() for r in misses(caplog))
    )


# ------------------------------------------------------- parked, then reported
#
# A device wedged behind a switch that is normally off (the Dining Room)
# walks the whole backoff ladder and is then parked (reconcile.py). Any
# report from it afterwards -- even one that is still off desired -- is
# evidence it is alive again, so its ladder is dropped and the zone is woken
# to try again at once, rather than waiting out the rest of the parked
# interval. A device still partway up the ladder must not get this treatment,
# or a ramping dimmer reporting its own intermediate levels would restart its
# ladder on every report.


def test_a_parked_device_that_reports_is_commanded_at_the_next_pass():
    """A parked device that reports anything is un-parked and commanded at
    the very next pass.

    Kills: engine.Engine._light_changed's `self.reconciler.forget(device_id)`
    (and the wake brought forward beside it) deleted.
    """
    commander = RecordingCommander(apply=False)  # 201 never lands on its own
    engine, zone, clock, _changed = build(
        commander,
        lights=[201],
        hold_seconds=1200,
        periods=[make_period("Evening", "18:00", "23:00", levels={"201": 60})],
    )
    make_device(101, "relay", onState=False)
    make_device(201, "dimmer", brightness=0, name="Dining Room Strip")
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)  # pass 1: the first command, never warns

    # Passes 2 through 16, a minute apart: the ladder (1, 2, 4, 8) and one
    # more command past it, which is what parks the device.
    for number in range(1, 16):
        engine.tick(clock.at(seconds=60 * number))

    assert engine.reconciler.backoff_step(201) == 5, "one command past the whole ladder"
    assert engine.reconciler.is_parked(201) is True

    # It reports -- still off desired (0 -> 100, not the desired 60), so this
    # is not an override transition (was_at_desired is False either way,
    # since neither 0 nor 100 is 60) -- but it is evidence the device itself
    # is alive again.
    apply_level(make_device(201, "dimmer", brightness=100, name="Dining Room Strip"), 100)
    now = clock.at(seconds=60 * 15 + 30)
    engine.device_updated(*light(201, 0, 100, name="Dining Room Strip"), now)

    assert engine.reconciler.backoff_step(201) == 0, "the ladder was dropped"
    assert engine.next_wake(now) <= now, "the zone was woken to try again at once"

    commander.clear()
    summary = engine.tick(now)
    assert [command.device_id for command in summary.commands] == [201], (
        "the un-parked device was commanded at the very next pass"
    )


def test_a_report_from_a_device_still_on_the_ladder_keeps_its_ladder():
    """A device only partway up the ladder is not parked, so a report from
    it -- even one still off desired -- must not restart its ladder: a
    ramping dimmer reports an intermediate level on every step, and
    un-parking it there would turn the ladder into a command storm.

    Kills: engine.Engine._light_changed un-parking unconditionally, without
    first checking `self.reconciler.is_parked(device_id)`.
    """
    commander = RecordingCommander(apply=False)
    engine, zone, clock, _changed = build(
        commander,
        lights=[201],
        hold_seconds=1200,
        periods=[make_period("Evening", "18:00", "23:00", levels={"201": 60})],
    )
    make_device(101, "relay", onState=False)
    make_device(201, "dimmer", brightness=0, name="Ramping Dimmer")
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)  # pass 1
    engine.tick(clock.at(seconds=60))  # pass 2

    assert engine.reconciler.backoff_step(201) == 2, "two un-landed commands"
    assert engine.reconciler.is_parked(201) is False

    now = clock.at(seconds=90)
    engine.device_updated(*light(201, 0, 30, name="Ramping Dimmer"), now)

    assert engine.reconciler.backoff_step(201) == 2, (
        "a report from a device still on the ladder reset it, which turns a "
        "ramp reporting its own intermediate levels into a command storm"
    )


def test_a_no_change_update_from_a_parked_device_does_not_unpark_it():
    """An update that changes nothing the device reports is not a report:
    Indigo delivers deviceUpdated for the echo of the plugin's own retry
    among other no-change updates, and un-parking on those would put the
    device straight back on the ladder every ten minutes.

    Kills: engine.Engine._light_changed's `and light_reported(previous_dev,
    current_dev)` gate deleted.
    """
    commander = RecordingCommander(apply=False)
    engine, zone, clock, _changed = build(
        commander,
        lights=[201],
        hold_seconds=1200,
        periods=[make_period("Evening", "18:00", "23:00", levels={"201": 60})],
    )
    make_device(101, "relay", onState=False)
    make_device(201, "dimmer", brightness=0, name="Dining Room Strip")
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    for number in range(0, 16):
        engine.tick(clock.at(seconds=60 * number))
    assert engine.reconciler.is_parked(201) is True

    now = clock.at(seconds=60 * 15 + 30)
    wake_before = engine.next_wake(now)
    engine.device_updated(*light(201, 0, 0, name="Dining Room Strip"), now)

    assert engine.reconciler.is_parked(201) is True, (
        "a no-change update un-parked the device; the echo of its own retry "
        "would now restart the ladder every parked interval"
    )
    assert engine.reconciler.backoff_step(201) == 5
    assert engine.next_wake(now) == wake_before, "the zone was not woken for nothing"


def test_a_reload_that_moves_no_zone_still_republishes_it():
    """An evaluation publishes the zone even when its state does not change.

    Live 2026-09-06 20:37: a config reload published every rebuilt zone
    before the worker evaluated it, and the Bedroom and Kitchen -- which
    landed in the same state they were already in -- kept that snapshot
    ("period=none", presence inactive) on their devices indefinitely.

    Kills: engine.Engine._run_zone notifying only `if transition is not None
    or sent`.
    """
    commander = RecordingCommander(apply=True)
    engine, zone, clock, changed = build(
        commander,
        lights=[201],
        hold_seconds=1200,
        periods=[make_period("Evening", "18:00", "23:00", levels={"201": 60})],
    )
    make_device(101, "relay", onState=True)
    make_device(201, "dimmer", brightness=0)
    make_device(302, "sensor", sensorValue=1200)

    engine.device_updated(*presence(101, False, True), clock.now)
    engine.tick(clock.now)
    assert engine.zones[zone.name].state is ZoneState.OCCUPIED, "precondition"
    changed.clear()

    later = clock.at(seconds=60)
    engine.reload(engine.config, later)
    summary = engine.tick(later)
    assert summary.transitions == (), "precondition: the reload moved nothing"
    assert engine.zones[zone.name].state is ZoneState.OCCUPIED
    assert zone.name in [z.name for z in changed], (
        "the rebuilt zone was evaluated but never republished; its device keeps "
        "the pre-evaluation snapshot the reload pushed"
    )
