"""Manual-override promises (PRD R1-R3, R5-R8, R10; section 7 must-haves).

The fork's numbering is kept in brackets so a promise can be traced back to
the test that proved it in `indigo-auto-lights/tests/`. Every docstring names
the promise in one sentence, then the mutation it must kill -- the wrong
implementation that would still pass a happy-path suite -- and every one of
those mutations has been applied to the source, run, and confirmed to fail
this test and no other's business. Attempt 1 at this rule (fork #15) looked
fine against a happy-path suite.

The rule under test is `override.is_manual_override()`, asked once per
device-change event from the two snapshots Indigo hands `deviceUpdated`. It
is called directly here rather than through the engine, because the promise
is about the rule: the engine's job is only to route the event to it, and
that routing is `tests/test_engine.py`'s business.
"""

import datetime as dt
import logging

import pytest
from helpers import RecordingCommander, make_device, make_period, make_snapshot, make_zone

from lamplighter import compare
from lamplighter.override import EchoBook, is_manual_override
from lamplighter.reconcile import Reconciler
from lamplighter.zone import ZoneState

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
LOGGER_NAME = "test.promises.override"
LOG = logging.getLogger(LOGGER_NAME)

#: The window the PRD configures (section 5.11). Passed explicitly rather
#: than read from a Config, so a test that means to exercise the bound says
#: so in its own body.
WINDOW = 15


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def at(**kwargs):
    return NOW + dt.timedelta(**kwargs)


def occupied_zone(levels=None, lights=(201, 202), **fields):
    """A zone that has evaluated to OCCUPIED, with a plan to be moved off.

    Evaluated, not hand-set: `desired_levels()` reads the state machine's
    last decision, so a zone that has never run has no plan and nothing can
    be judged against it -- which is itself one of the promises below.
    """
    fields.setdefault("lights", list(lights))
    fields.setdefault("presence_devices", [101])
    zone = make_zone(
        [
            make_period(
                "Evening",
                "18:00",
                "23:00",
                levels=levels if levels is not None else {"201": 60, "202": 30},
            )
        ],
        logger=LOG,
        **fields,
    )
    # A PIR trip: on, then immediately off. The sensor is not left reporting,
    # so the hold is running from `now` and every timing assertion below is
    # about the hold rather than about a sensor that is still on. A LEVEL
    # sensor left on is a different case with its own tests.
    zone.ingest_presence(101, True, NOW)
    zone.ingest_presence(101, False, NOW)
    assert zone.evaluate(NOW, "startup").to_state is ZoneState.OCCUPIED
    return zone


def fire(zone, book, dev_id, before, after, *, now=NOW, key="brightness", cls="dimmer"):
    """One device-change event, as a detached before/after snapshot pair."""
    previous = make_snapshot(dev_id, device_cls=cls, **{key: before})
    current = make_snapshot(dev_id, device_cls=cls, **{key: after})
    return is_manual_override(zone, previous, current, now, book, WINDOW, LOG)


# ------------------------------------------------------- the transition rule


def test_override_survives_a_concurrent_revert():
    """[M1] An override created by a device change stands even though the
    plugin's own revert has already put the light back.

    Kills: judge the zone by re-reading live device state. The override erases
    its own evidence -- by the time anything re-reads, the light is back at
    desired -- so only the transition carried in the event can see it.

    Mutation applied: override.is_manual_override's `now_at_desired =
    compare.at_level(current_dev, desired)` -> `... at_level(
    __import__("indigo").devices[device_id], desired)`, a live re-read.
    """
    import indigo

    zone = occupied_zone()
    make_device(201, "dimmer", brightness=60)

    # The person dials 201 down to 20. The worker's revert has ALREADY put the
    # live device back to 60 by the time this callback runs -- the fork
    # measured 112 ms between the change and the lock, and the revert landed
    # inside that.
    previous = make_snapshot(201, brightness=60)
    current = make_snapshot(201, brightness=20)
    assert indigo.devices[201].brightness == 60, "precondition: the revert has landed"

    class Fatal(dict):
        def __getitem__(self, key):
            raise AssertionError(
                "the rule read live device state; the only evidence of an "
                "override is the snapshot pair the event carried"
            )

    original = indigo.devices
    indigo.devices = Fatal()
    try:
        verdict = is_manual_override(zone, previous, current, NOW, EchoBook(), WINDOW, LOG)
    finally:
        indigo.devices = original

    assert verdict is True


def test_own_ramp_never_overrides():
    """[M2] The plugin's own ramp toward a desired level never creates an
    override.

    Kills: judge the new state alone. Every intermediate step of a ramp is off
    desired; only the before-state tells them apart from a dial move, and the
    before-state of our own write is always off desired because we only ever
    command a device that is not already there (R2).

    Mutation applied: override.is_manual_override's `if not (was_at_desired
    and not now_at_desired):` -> `if not (not now_at_desired):`.
    """
    zone = occupied_zone()
    book = EchoBook()

    for before, after in ((0, 12), (12, 35), (35, 58), (58, 60)):
        assert fire(zone, book, 201, before, after) is False, (
            f"the plugin's own ramp step {before}->{after} created an override; "
            "the rule must read the BEFORE state, not just the new one"
        )


def test_late_reporter_never_overrides():
    """[M3] A device reporting from an already-off-desired state cannot create
    an override, however late the report arrives.

    Kills: drop the before-state check. A late reporter (R6) is
    the reason: its reports arrive against a history that is already off
    desired, so nothing changed hands.

    Mutation applied: override.is_manual_override's `if not (was_at_desired
    and not now_at_desired):` -> `if not (not now_at_desired):`.
    """
    zone = occupied_zone()
    # The room empties, so the plan is now off for both lights.
    zone.ingest_presence(101, False, at(seconds=1))
    assert zone.evaluate(at(seconds=400), "presence hold expired").to_state is ZoneState.VACANT
    assert zone.desired_levels(at(seconds=400)) == {201: "off", 202: "off"}

    book = EchoBook()
    for before, after in ((30, 0), (30, 12)):
        assert fire(zone, book, 201, before, after, now=at(seconds=400)) is False, (
            f"a late report {before}->{after} against desired 'off' created an "
            "override; the before-state was already off desired"
        )


def test_turn_on_flash_never_overrides():
    """[M4] A lamp that flashes to full on its way to a dim level does not
    create an override.

    Kills: treat any jump away from desired as manual. The before-state is 0,
    already off desired, so the flash is not a transition off desired (R7).

    Mutation applied: override.is_manual_override's `if not (was_at_desired
    and not now_at_desired):` -> `if not (not now_at_desired):`.
    """
    zone = occupied_zone(levels={"201": 30, "202": 30})
    assert zone.desired_levels(NOW)[201] == 30

    # The Tuya TS0502B reports 100 for a moment on the way to 30.
    assert fire(zone, EchoBook(), 201, 0, 100) is False


def test_sibling_mid_write_cannot_override_this_device():
    """[M5] A sibling light still mid-write must not create an override on the
    device that actually reported.

    Kills: restore the whole-zone live check ("is anything off desired right
    now?"). The rule is per device.

    Mutation applied: override.is_manual_override's `now_at_desired =
    compare.at_level(current_dev, desired)` -> an all() over every device in
    zone.desired_levels(), read live from indigo.devices.
    """
    zone = occupied_zone()
    make_device(201, "dimmer", brightness=60)
    # 202 is mid-write: commanded to 30, still sitting at 12.
    make_device(202, "dimmer", brightness=12)
    assert zone.desired_levels(NOW) == {201: 60, 202: 30}

    # 201 reports its own settled value. Nothing about it moved.
    assert fire(zone, EchoBook(), 201, 60, 60) is False


def test_genuine_change_still_overrides_while_sibling_mid_write():
    """[M5 sibling] The per-device rule narrows the question; it does not go
    blind.

    Kills: fixing M5 by suppressing override detection whenever any device in
    the zone is off desired. A real dial move on the reporting device must
    still land.

    Mutation applied: override.is_manual_override gains, before the transition
    check, `if any(not compare.at_level(indigo.devices[i], l) for i, l in
    zone.desired_levels(now).items() if l != LEAVE): return False`.
    """
    zone = occupied_zone()
    make_device(201, "dimmer", brightness=60)
    make_device(202, "dimmer", brightness=12)  # still mid-write

    assert fire(zone, EchoBook(), 201, 60, 20) is True


def test_override_lands_while_our_own_write_is_in_flight():
    """[M6] A manual change that arrives during the plugin's own write burst
    creates an override.

    Kills: keep a `writing -> ignore events` guard. That guard existed to stop
    self-locking, which the transition rule now prevents by construction, but
    it discarded real input at exactly the moment a person is most likely to
    act -- with the lights visibly moving.

    Mutation applied: override.is_manual_override gains, after the exclude
    check, `if any(echo_book.pending(other) for other in zone.config.lights):
    return False` -- the checked-out guard, in the shape this design would
    take it.
    """
    zone = occupied_zone()
    make_device(201, "dimmer", brightness=60)
    make_device(202, "dimmer", brightness=0)

    # A real write burst: the reconciler has just commanded 202 and it has not
    # reported back, so the zone genuinely has a command in flight.
    book = EchoBook()
    commander = RecordingCommander(apply=False)
    Reconciler(commander, book, LOG).run(zone, NOW)
    assert commander.commands == [(202, 30)], "precondition: a write is in flight"
    assert book.pending(202) == (0,)

    # The person reaches for the dial while the lights are visibly moving.
    assert fire(zone, book, 201, 60, 20) is True


def test_relay_switched_off_from_desired_overrides():
    """[M7] Relays go through the same rule against onState.

    Kills: only judge dimmers. Off->on from rest is our own work (before-state
    off desired); on->off from the settled desired state is a person at the
    switch.

    Mutation applied: override.is_manual_override gains, before the transition
    check, `if not compare.is_dimmer(current_dev): return False`.
    """
    zone = occupied_zone(levels={"201": 60, "203": "on"}, lights=(201, 203))
    make_device(203, "relay", onState=True)
    assert zone.desired_levels(NOW)[203] == "on"

    # Our own turn-on: the before-state was off, already off desired.
    assert fire(zone, EchoBook(), 203, False, True, key="onState", cls="relay") is False

    # A person at the switch: on -> off, from the settled desired state.
    assert fire(zone, EchoBook(), 203, True, False, key="onState", cls="relay") is True


def test_relay_with_desired_off_overrides_when_switched_on():
    """[M7 polarity] A relay whose desired level is `off` creates an override
    when a person switches it ON.

    Kills: treat "on" as at-desired and "off" as off-desired. Every other
    relay case has a desired of on, so that mutation passes them all.

    Mutation applied: compare.at_level's relay branch, `return actual ==
    wants_on` -> `return actual`.
    """
    zone = occupied_zone(levels={"201": 60, "203": "on"}, lights=(201, 203))
    zone.ingest_presence(101, False, at(seconds=1))
    assert zone.evaluate(at(seconds=400), "presence hold expired").to_state is ZoneState.VACANT
    assert zone.desired_levels(at(seconds=400))[203] == "off", "VACANT: off, not leave"

    assert (
        fire(zone, EchoBook(), 203, False, True, key="onState", cls="relay", now=at(seconds=400))
        is True
    )


def test_readback_inside_the_band_is_not_a_change():
    """[M8] The override rule uses the same proportional band as the send
    path: max(1, ceil(target x 0.10)) (R5).

    Kills: exact equality. zigbee2mqtt truncates 30 to 29 and a group dimmer
    reads back 45..48 for 50; under equality every one of those is a manual
    override.

    Mutation applied: compare.level_matches's `return abs(actual - target) <=
    band(target)` -> `return actual == target`.
    """
    zone = occupied_zone(levels={"201": 50, "202": 30})
    book = EchoBook()

    # The group dimmer asked for 50 reads back 45..48; the zigbee2mqtt dimmer
    # asked for 30 reads back 29. Every one of these is at desired.
    for before, after in ((50, 48), (50, 45), (48, 46)):
        assert fire(zone, book, 201, before, after) is False, (before, after)
    assert fire(zone, book, 202, 30, 29) is False


def test_band_is_exact_at_zero_and_one_hundred_and_a_real_move_overrides():
    """[M8 edges] 0 and 100 compare exactly, and a move outside the band still
    creates an override.

    Kills: widen the band until nothing counts, and: apply the proportional
    band at the ends, where 100 would accept 90 as "on" and 0 would accept 1
    as "off".

    Mutations applied, one at a time: compare.band's `return max(BAND_FLOOR,
    math.ceil(target * BAND_FRACTION))` -> `return 100`, and
    compare.level_matches's `if target <= 0 or target >= 100:` -> `if False:`.
    """
    zone = occupied_zone(levels={"201": 100, "202": 60})

    # 100 is exact: a light we never finished driving to full is one a person
    # can see is wrong.
    assert fire(zone, EchoBook(), 201, 100, 90) is True

    # ...and a real move outside the band on an ordinary level.
    assert fire(zone, EchoBook(), 202, 60, 20) is True

    # 0 is exact too, on the other side: with the zone off duty every light
    # with a level is off, and a light showing 1 is not off.
    zone.ingest_presence(101, False, at(seconds=1))
    zone.evaluate(at(seconds=400), "presence hold expired")
    assert zone.desired_levels(at(seconds=400))[202] == "off"
    assert fire(zone, EchoBook(), 202, 0, 1, now=at(seconds=400)) is True


def test_no_before_state_means_no_override_and_a_warning(caplog):
    """[M9] An event with no before-state is not judged at all, and says so.

    Kills: fall back to judging the new value alone when the before-state is
    missing -- which is exactly the M2 mutation. The skipped judgement must
    reach the log; a silent skip is the fork's level-5 fall-through (R8, R15).

    Mutation applied: override.is_manual_override's `if previous_dev is None:`
    -> `if False:`.
    """
    zone = occupied_zone()
    current = make_snapshot(201, brightness=20, name="Desk Lamp")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        verdict = is_manual_override(zone, None, current, NOW, EchoBook(), WINDOW, LOG)

    assert verdict is False
    assert len(caplog.records) == 1, "once per device, not once per event (section 10)"
    message = caplog.records[0].getMessage()
    assert "no before-state" in message
    assert "Desk Lamp" in message and zone.name in message
    assert "nothing about it was decided" in message, (
        "the message must not read as 'the device is where we want it'"
    )


# ------------------------------------------------------- who cannot override


def test_excluded_device_never_overrides():
    """[M10a] A device in the zone's `override.exclude` never creates an
    override, but is still commanded normally (R6).

    Kills: implement exclusion by dropping the device from the plan, which
    would also stop the plugin driving it.

    Mutations applied, one at a time: override.is_manual_override's `if
    device_id in zone.config.override.exclude:` -> `if False:`, and
    reconcile.Reconciler.run's `if level == LEAVE:` -> `if level == LEAVE or
    device_id in zone.config.override.exclude:`.
    """
    zone = occupied_zone(override={"exclude": [202]})
    assert zone.config.override.exclude == (202,)

    # A late reporter reports from its desired level and does not lock.
    assert fire(zone, EchoBook(), 202, 30, 5) is False
    # ...while the light beside it is judged normally.
    assert fire(zone, EchoBook(), 201, 60, 20) is True

    # And it is still driven: exclusion is from DETECTION, not from the plan.
    make_device(201, "dimmer", brightness=60)
    make_device(202, "dimmer", brightness=0)
    commander = RecordingCommander()
    Reconciler(commander, EchoBook(), LOG).run(zone, NOW)
    assert commander.commands == [(202, 30)], (
        "an excluded device must still be commanded; exclusion keeps it out of "
        "override detection, not out of the zone"
    )


def test_zone_with_override_disabled_never_locks():
    """[M10b] A zone with `override.enabled: false` never locks -- the Hallway
    (R10).

    Kills: honour the flag only on the timing path, so the zone still enters
    OVERRIDDEN and simply expires quickly.

    Mutation applied: override.is_manual_override's `if not
    zone.config.override.enabled:` -> `if False:`.
    """
    hallway = occupied_zone(override={"enabled": False})
    assert hallway.config.override.enabled is False

    assert fire(hallway, EchoBook(), 201, 60, 20) is False
    assert hallway.override is None
    assert hallway.evaluate(at(seconds=1), "the change was noticed") is None
    assert hallway.state is ZoneState.OCCUPIED, "never OVERRIDDEN, not even briefly"


def test_device_with_no_level_in_this_period_never_overrides():
    """[M10c] A light the active period gives no level -- absent, or `leave`
    -- has nothing to compare against and cannot create an override.

    Kills: default a missing level to off, which makes every such device
    permanently off desired.

    Mutation applied: zone.Zone.desired_levels's `level = period.levels.get(
    light, LEAVE)` -> `level = period.levels.get(light, OFF)`.
    """
    zone = occupied_zone(levels={"201": 60}, lights=(201, 203))
    assert zone.desired_levels(NOW) == {201: 60, 203: "leave"}

    # Somebody switches 203 on. The zone has no opinion about it at all.
    assert fire(zone, EchoBook(), 203, 0, 100) is False

    # And the same for a level written out as "leave".
    spelled_out = occupied_zone(levels={"201": 60, "203": "leave"}, lights=(201, 203))
    assert fire(spelled_out, EchoBook(), 203, 0, 100) is False


def test_disabled_zone_never_overrides():
    """[M10d] A disabled zone, or one under a disabled controller, records no
    override.

    Kills: check the enable flag only at write time, so a zone accumulates
    overrides while off and acts on them the moment it is re-enabled.

    Mutation applied: override.is_manual_override's `if not zone.running:` ->
    `if False:`.
    """
    # Switched off between the last evaluation and the event, which is the
    # shape that catches a flag read at write time: the zone still believes it
    # is OCCUPIED and still has a plan on record.
    zone = occupied_zone()
    assert zone.set_enabled(enabled=False) is True
    assert zone.state is ZoneState.OCCUPIED and zone.desired_levels(NOW)[201] == 60

    assert fire(zone, EchoBook(), 201, 60, 20) is False
    assert zone.override is None

    # The controller device, switched off above the zone.
    under_controller = occupied_zone()
    assert under_controller.set_enabled(plugin_enabled=False) is True
    assert fire(under_controller, EchoBook(), 201, 60, 20) is False
    assert under_controller.override is None


# --------------------------------------------------------- the echo window


def commanded_zone(book, commander=None, levels=None):
    """A zone whose light has just been commanded, with the record to prove it.

    Driven through the real Reconciler rather than by calling
    `note_pre_command` by hand: what gets recorded -- the state we commanded
    the device AWAY from, not the value we asked for -- is half of what M12
    is about, and a test that records it by hand cannot see it go wrong.
    """
    zone = occupied_zone(levels=levels or {"201": 60, "202": 30})
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=30)
    commander = commander or RecordingCommander(apply=False)
    Reconciler(commander, book, LOG).run(zone, NOW)
    assert commander.commands == [(201, 60)], "precondition: 201 was commanded"
    assert book.pending(201) == (0,), (
        "precondition: the record is the state the device was commanded AWAY "
        "from (0), not the value commanded (60)"
    )
    return zone


def revert_to_off(zone, when):
    """The re-plan that lands between our command and its echo: desired -> off."""
    zone.ingest_presence(101, False, when)
    zone.evaluate(when + dt.timedelta(seconds=400), "presence hold expired")
    assert zone.desired_levels(when + dt.timedelta(seconds=400))[201] == "off"
    return when + dt.timedelta(seconds=400)


def test_echo_after_the_desired_level_reverts_is_excused_once():
    """[M12a] An echo of our own command that arrives after the desired level
    has moved back onto the device's pre-command state is still ours, and is
    excused exactly once (R3).

    Kills: match echoes on the value we commanded rather than the value we
    commanded the device AWAY from. In-room lux rises, the zone stops being
    dark, the lights are commanded off, and the queued on-echo then reads as a
    manual override.

    Mutation applied: reconcile.Reconciler.run's `self.echo_book
    .note_pre_command(device_id, compare.reading(device), now)` ->
    `...note_pre_command(device_id, level, now)`.
    """
    book = EchoBook()
    zone = commanded_zone(book)
    # Deliberately inside the window: this is the queued-echo race, not a
    # change that happened minutes later.
    later = NOW + dt.timedelta(seconds=5)
    zone.ingest_presence(101, False, NOW)
    zone.evaluate(later, "the room emptied")
    zone.presence.last_seen = None
    assert zone.evaluate(later, "the room emptied").to_state is ZoneState.VACANT
    assert zone.desired_levels(later)[201] == "off"

    # The queued deviceUpdated for our own 0 -> 60 command finally lands. Its
    # before-state of 0 now sits exactly on the new desired level.
    assert fire(zone, book, 201, 0, 60, now=later) is False
    assert book.pending(201) == (), "the record must be consumed"

    # The same transition again is a person putting the light back on after
    # the zone dropped it. One command excuses one transition.
    assert fire(zone, book, 201, 0, 60, now=later) is True


def test_ramp_out_of_the_recorded_state_needs_no_excuse():
    """[M12b] A dimmer ramping out of the recorded pre-command state reports
    an intermediate value, not the commanded one, and needs no excuse.

    Kills: consume the recorded state on any event from that device, which
    spends the one excuse on a step that never needed it and leaves the real
    echo unexcused.

    Mutation applied: override.is_manual_override gains
    `echo_book.consume_echo(device_id, previous_dev, now, window_seconds)`
    immediately before the transition check, so the record is spent whatever
    the transition turns out to be.
    """
    book = EchoBook()
    zone = commanded_zone(book)

    # The first ramp step toward 60 arrives while desired is still 60. Its
    # before-state of 0 is off desired, so the transition rule alone acquits
    # it -- no excuse is needed and none must be spent.
    assert fire(zone, book, 201, 0, 12, now=NOW + dt.timedelta(seconds=1)) is False
    assert book.pending(201) == (0,), (
        "a step that needed no excuse consumed one; the real echo is now "
        "unprotected"
    )

    # Now the re-plan lands and the real echo follows it.
    later = NOW + dt.timedelta(seconds=5)
    zone.presence.last_seen = None
    zone.evaluate(later, "the room emptied")
    assert zone.desired_levels(later)[201] == "off"
    assert fire(zone, book, 201, 0, 60, now=later) is False
    assert book.pending(201) == ()


def test_manual_move_onto_the_recorded_state_still_overrides():
    """[M12c] The excuse covers transitions OUT of the recorded state, not
    transitions onto it.

    Kills: excuse any event that touches the recorded value. A person dialling
    a light back to exactly where it was before our command is still a person.

    Mutation applied: override.is_manual_override's `echo_book.consume_echo(
    device_id, previous_dev, now, window_seconds)` -> `...consume_echo(
    device_id, current_dev, now, window_seconds)`.
    """
    book = EchoBook()
    zone = commanded_zone(book)
    assert book.pending(201) == (0,)

    # The light reached 60, and a person switches it off. The transition ENDS
    # on the recorded state of 0, which is not what the record is for.
    assert fire(zone, book, 201, 60, 0, now=NOW + dt.timedelta(seconds=2)) is True
    assert book.pending(201) == (0,), "a change that was not excused consumed a record"


def test_the_echo_excuse_expires_with_the_window():
    """[M12d] The excuse is time-boxed by `echo_window_seconds`: an old
    command cannot cover a new change.

    Kills: keep pre-command states until they are used. The documented cost of
    this rule is that one manual change inside the window is swallowed; the
    window and the single-use rule are the two things that bound it.

    Mutation applied: override.EchoBook.consume_echo's `while history and
    history[0][1] < cutoff:` -> `while False:`.
    """
    book = EchoBook()
    zone = commanded_zone(book)
    zone.presence.last_seen = None
    zone.evaluate(NOW + dt.timedelta(seconds=1), "the room emptied")
    assert zone.desired_levels(NOW)[201] == "off"

    # Inside the window: the accepted cost -- this one change is read as ours.
    inside = NOW + dt.timedelta(seconds=WINDOW - 1)
    assert fire(zone, book, 201, 0, 60, now=inside) is False

    # Outside it: a person moving a light off a state we commanded it away
    # from a minute ago is a person, not a queued echo.
    book.note_pre_command(201, 0, NOW)
    outside = NOW + dt.timedelta(seconds=WINDOW + 1)
    assert fire(zone, book, 201, 0, 60, now=outside) is True
    assert book.pending(201) == (), "the expired record must be pruned, not left to rot"


# --------------------------------------------------- holding and releasing


def test_override_extends_while_presence_is_active():
    """An override that expires with the room still occupied extends by
    `extend_minutes` instead of ending (R10).

    Kills: read presence from a cached value captured when the override was
    created, so a room still in use loses its override at the first expiry.

    Mutation applied: zone.Zone.evaluate's `self._age_override(now,
    presence_active)` -> `self._age_override(now, False)`.
    """
    zone = occupied_zone(
        override={"duration_minutes": 60, "extend_minutes": 30, "unlock_on_leave": False}
    )
    override = zone.start_override(201, NOW)
    assert override.expires_at == at(minutes=60)

    # The room is still in use at the expiry: somebody moves at minute 59, so
    # the hold is still running a minute later. (Trip and drop: a sensor left
    # ON would hold the room for ever and the override would never end.)
    zone.ingest_presence(101, True, at(minutes=59))
    zone.ingest_presence(101, False, at(minutes=59))
    assert zone.evaluate(at(minutes=60), "override expiry") is not None
    assert zone.state is ZoneState.OVERRIDDEN
    assert zone.override is not None, "an occupied room must not lose its override"
    assert zone.override.expires_at == at(minutes=90)
    assert zone.override.extended_count == 1

    # ...and when the room finally empties, it ends.
    assert zone.evaluate(at(minutes=90), "override expiry") is not None
    assert zone.override is None
    assert zone.state is ZoneState.VACANT


def test_lock_zone_action_creates_an_override_without_a_device_change():
    """The `lock zone` action creates an override with no device event behind
    it (PRD section 5.13, decision 2) -- what scripts wanted from the fork.

    Kills: implement it by writing a light and letting the transition rule
    notice, which both moves the lights and depends on the write landing.

    Mutations applied, one at a time: engine.Engine.lock_zone gains a
    `self.commander.set_level(...)` over every planned device before
    `zone.start_override(...)`, and `MANUAL_LOCK_DEVICE_ID = -1` -> `= 0`,
    which persist.py reads as "no override at all".
    """
    from helpers import FixedSun, make_config, make_zone_document

    from lamplighter import persist
    from lamplighter.engine import MANUAL_LOCK_DEVICE_ID, Engine

    sun = FixedSun()
    config = make_config(
        [
            make_zone_document(
                lights=[201, 202],
                periods=[make_period("Evening", "18:00", "23:00", levels={"201": 60, "202": 30})],
            )
        ],
        sun=sun,
    )
    commander = RecordingCommander(apply=True)
    engine = Engine(config, sun, commander, logger=LOG, clock=lambda: NOW)
    make_device(201, "dimmer", brightness=0)
    make_device(202, "dimmer", brightness=0)

    # A zone with a plan on record, which is the only state in which a lock
    # action could write anything: locking a zone that has never evaluated
    # would let the mutation pass on a technicality.
    engine.device_updated(
        make_snapshot(101, onState=False), make_snapshot(101, onState=True), NOW
    )
    engine.tick(NOW)
    assert commander.commands == [(201, 60), (202, 30)]
    commander.clear()

    created = engine.lock_zone("Study", NOW)

    assert created is not None
    assert created.device_id == MANUAL_LOCK_DEVICE_ID
    assert commander.commands == [], (
        "the lock action moved a light; it must create the override directly, "
        "not write and hope the transition rule notices"
    )

    zone = engine.zones["Study"]
    summary = engine.tick(NOW)
    assert zone.state is ZoneState.OVERRIDDEN
    assert summary.commands == ()

    # And it survives a restart, which a device id of 0 would not: persist.py
    # reads 0 as "there is no override here".
    record = persist.to_persisted(zone)
    fresh = persist.rebuild_zone(zone, zone.config, NOW)
    assert record["override_device"] == MANUAL_LOCK_DEVICE_ID
    assert fresh.override is not None
    assert fresh.override.device_id == MANUAL_LOCK_DEVICE_ID


def test_an_overridden_zone_writes_nothing():
    """In OVERRIDDEN the desired level IS whatever the devices are: the zone
    issues no commands at all, including at the reconcile tick (section 5.3).

    Kills: keep planning in OVERRIDDEN and merely suppress the write, which
    leaves the reconcile tick free to "fix" the person's level.

    Mutation applied: zone.Zone.desired_levels's `if self.state is
    ZoneState.OVERRIDDEN: return plan` -> `if False: return plan`.
    """
    zone = occupied_zone()
    make_device(201, "dimmer", brightness=20)  # where the person left it
    make_device(202, "dimmer", brightness=30)

    zone.start_override(201, NOW)
    assert zone.evaluate(at(seconds=1), "override: Desk Lamp").to_state is ZoneState.OVERRIDDEN
    assert zone.desired_levels(at(seconds=1)) == {201: "leave", 202: "leave"}

    commander = RecordingCommander()
    reconciler = Reconciler(commander, EchoBook(), LOG)
    for minute in range(1, 30):
        reconciler.run(zone, at(minutes=minute))

    assert commander.commands == [], (
        "the reconcile tick wrote to an overridden zone and would have put the "
        "person's level back"
    )
    assert zone.writes_today == 0
