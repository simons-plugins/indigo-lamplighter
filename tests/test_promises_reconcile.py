"""Reconcile (PRD R6, section 5.8).

One command per device that is off desired, no settle poll, no confirm thread,
no suppression list, no re-evaluation rate limit. A device that does not land
is picked up at the next reconcile tick with per-device backoff. This is the
whole write machinery, and PRD section 9 records why: if a device does not
confirm, the answer is the tick, not a thread.

Every docstring names the mutation it kills; each has been applied to the
source, run, and confirmed to fail the test that names it.
"""

import datetime as dt
import logging
import time

import pytest
from helpers import (
    RecordingCommander,
    apply_level,
    make_device,
    make_period,
    make_snapshot,
    make_zone,
)

from lamplighter import compare
from lamplighter.override import EchoBook, is_manual_override
from lamplighter.reconcile import BACKOFF_TICKS, Reconciler
from lamplighter.zone import ZoneState

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
LOGGER_NAME = "test.promises.reconcile"
LOG = logging.getLogger(LOGGER_NAME)

#: The tick the PRD defaults to (section 5.11). Passes below are spaced by it
#: so "ticks" in the backoff ladder means the same thing here as in the plugin.
TICK = dt.timedelta(seconds=60)


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


def run_passes(reconciler, zone, count, before_pass=None):
    """Run `count` reconcile passes a tick apart. Returns {device: [passes]}."""
    fired = {}
    for number in range(1, count + 1):
        moment = NOW + TICK * number
        if before_pass is not None:
            before_pass(number, moment)
        for command in reconciler.run(zone, moment):
            fired.setdefault(command.device_id, []).append(number)
    return fired


def test_one_command_per_device_per_pass():
    """A reconcile pass sends at most one command to each device that is off
    desired, and none to a device inside the band.

    Kills: re-command every device in the plan every pass, which is what makes
    a zone's own echoes a permanent source of events.

    Mutation applied: reconcile.Reconciler.run's `if compare.at_level(device,
    level): self._clear_backoff(device_id); continue` -> `if False: ...`.
    """
    zone = occupied_zone({"201": 60, "202": 30, "203": 60}, (201, 202, 203))
    make_device(201, "dimmer", brightness=0)  # off desired
    make_device(202, "dimmer", brightness=29)  # inside the band of 30
    make_device(203, "dimmer", brightness=100)  # off desired

    commander = RecordingCommander(apply=False)
    sent = Reconciler(commander, EchoBook(), LOG).run(zone, NOW)

    assert commander.commands == [(201, 60), (203, 60)]
    assert [command.device_id for command in sent] == [201, 203]
    assert commander.ids().count(201) == 1
    assert 202 not in commander.ids(), (
        "a device inside the band was commanded; every such command is an echo "
        "the zone then has to reason about"
    )
    assert zone.writes_today == 2


def test_a_device_that_has_not_reported_yet_is_not_re_commanded_in_the_pass(monkeypatch):
    """Nothing waits for a confirmation inside a pass: a device that has not
    reported back is simply still off desired at the next tick.

    Kills: reintroduce a settle poll. Make the failure fatal -- hand the pass a
    clock or a sleep that raises if touched -- so "we did not wait" is
    asserted rather than assumed.

    Mutation applied: reconcile.Reconciler.run gains, after
    `self.commander.set_level(device, level)`, a `time.sleep(0)` and a
    re-check that re-commands a device still off desired -- the settle poll,
    in the smallest shape it would come back in.
    """

    def fatal_sleep(*args, **kwargs):
        raise AssertionError(
            "the reconcile pass slept. PRD section 9: if a device does not "
            "confirm, the answer is the next tick, not a thread and not a poll"
        )

    monkeypatch.setattr(time, "sleep", fatal_sleep)

    zone = occupied_zone({"201": 60}, (201,))
    make_device(201, "dimmer", brightness=0)
    commander = RecordingCommander(apply=False)  # the device never reports back

    sent = Reconciler(commander, EchoBook(), LOG).run(zone, NOW)

    assert commander.commands == [(201, 60)], "one command, and no waiting for it"
    assert len(sent) == 1


def test_backoff_doubles_per_device_and_is_capped():
    """A device still off desired is retried at 1, 2, 4, 8 ticks and then at
    the cap, per device.

    Kills: back off per zone, which lets one broken light stall every healthy
    one in the room.

    Mutation applied: reconcile.Reconciler.run's `backoff =
    self._backoff.get(device_id)` and `self._advance_backoff(device_id,
    this_pass)` -> keyed on `zone.name` instead of the device.
    """
    zone = occupied_zone({"201": 60, "202": 30}, (201, 202))
    make_device(201, "dimmer", brightness=0)  # the broken bulb: never lands
    make_device(202, "dimmer", brightness=0)  # healthy: lands when commanded

    commander = RecordingCommander(apply={202})
    reconciler = Reconciler(commander, EchoBook(), LOG)

    def knock_202_off_desired(number, _moment):
        if number == 3:
            apply_level(make_device(202, "dimmer", brightness=5), 5)

    fired = run_passes(reconciler, zone, 30, before_pass=knock_202_off_desired)

    assert fired[201] == [1, 2, 4, 8, 16, 24], (
        "the ladder is 1, 2, 4, 8 ticks and then held at the cap of "
        f"{BACKOFF_TICKS[-1]}"
    )
    assert fired[202] == [1, 3], (
        "the healthy light was stalled by the broken one; backoff is per "
        "device precisely so that one dead bulb does not take the room with it"
    )


def test_the_first_backoff_step_warns_once_naming_actual_and_desired(caplog):
    """One WARNING at the first backoff step, naming the device, its actual
    level and its desired level; not one per tick (R15, section 10).

    Kills: warn every pass (log spam that hides the next problem), and: warn
    never (the fork's silent suppression, where a dead light simply stopped
    being mentioned).

    Mutations applied, one at a time: reconcile.Reconciler._warn_backoff's
    warn key `("backoff", device.id)` -> `("backoff", device.id, object())`
    (spam), and Reconciler.run's `if backoff is not None and backoff.step >=
    1:` -> `if False:` (silence).
    """
    zone = occupied_zone({"201": 60}, (201,))
    make_device(201, "dimmer", brightness=12, name="Late Strip")
    commander = RecordingCommander(apply=False)
    reconciler = Reconciler(commander, EchoBook(), LOG)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        reconciler.run(zone, NOW + TICK)
        assert caplog.records == [], "the first command is not a backoff step"

        run_passes(reconciler, zone, 20)

    assert len(caplog.records) == 1, "once per condition per device, not once per tick"
    message = caplog.records[0].getMessage()
    assert "Late Strip" in message and "201" in message
    assert "12" in message and "60" in message, "actual and desired must both be named"


def test_a_device_reporting_at_desired_clears_its_backoff_silently(caplog):
    """Landing on desired resets the device's backoff with no log line.

    Kills: keep the backoff until a full pass finds the device idle, so a
    device that recovers stays on a slow retry schedule.

    Mutation applied: reconcile.Reconciler.run's `if compare.at_level(device,
    level): self._clear_backoff(device_id); continue` -> the same without the
    `self._clear_backoff(device_id)` line.
    """
    zone = occupied_zone({"201": 60}, (201,))
    device = make_device(201, "dimmer", brightness=0)
    commander = RecordingCommander(apply=False)
    reconciler = Reconciler(commander, EchoBook(), LOG)

    reconciler.run(zone, NOW + TICK)  # pass 1: commanded
    reconciler.run(zone, NOW + TICK * 2)  # pass 2: first backoff step
    assert reconciler.backoff_step(201) == 2

    # It finally reports at desired -- late, but it got there (R6).
    apply_level(device, 60)
    caplog.clear()  # the first backoff step above legitimately warned
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert reconciler.run(zone, NOW + TICK * 4) == []
    assert caplog.records == [], "a device that recovers is not news"
    assert reconciler.backoff_step(201) == 0, (
        "the backoff survived the device landing; the next change would then "
        "wait out a schedule earned by a fault that is over"
    )

    # And the next fault starts from the beginning, not from where the last
    # one left off: commanded on this pass and on the next.
    apply_level(device, 5)
    commander.clear()
    fired = run_passes(reconciler, zone, 6)
    assert fired[201][:2] == [1, 2], (
        "a recovered device was still on the old slow schedule when it failed "
        f"again: commanded on passes {fired[201]} of this run, when a device "
        "whose backoff was cleared starts the ladder afresh at 1 then 2"
    )


def test_a_slow_reporter_is_reconciled_without_a_retry_storm(caplog):
    """A device that reports late is neither retried inside the window,
    nor suppressed, nor treated as an override; it is reconciled when it
    finally reports (R6).

    Kills: count a missing confirmation as a failure. A late reporter
    self-locked its zone under the fork and needed exclude_from_lock to work
    at all.

    Mutations applied, one at a time: reconcile.Reconciler.run's `if backoff
    is not None and backoff.next_due > this_pass:` -> `if False:` (the retry
    storm), and override.is_manual_override's `was_at_desired =
    compare.at_level(previous_dev, desired)` -> `was_at_desired = True` (the
    self-lock).
    """
    zone = occupied_zone({"201": 60}, (201,))
    device = make_device(201, "dimmer", brightness=0, name="Late Strip")
    book = EchoBook()
    commander = RecordingCommander(apply=False)
    reconciler = Reconciler(commander, book, LOG)

    # Five passes -- five minutes -- while the strip says nothing at all.
    fired = run_passes(reconciler, zone, 5)
    assert fired[201] == [1, 2, 4], (
        "the un-landed device was retried on every pass; that is the storm the "
        f"backoff exists to prevent -- it fired on {fired[201]}"
    )

    # Then it reports, mid-ramp, from the state we commanded it away from.
    # This is the event that self-locked the Kitchen under the fork.
    previous = make_snapshot(201, brightness=0)
    current = make_snapshot(201, brightness=45)
    late = NOW + TICK * 5
    assert is_manual_override(zone, previous, current, late, book, 15, LOG) is False, (
        "a slow reporter's own echo created an override; the before-state was "
        "already off desired, so nothing changed hands"
    )
    assert zone.override is None

    # And when it lands, it is simply at desired: no retry, no suppression.
    apply_level(device, 60)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert reconciler.run(zone, NOW + TICK * 6) == []
        assert reconciler.run(zone, NOW + TICK * 7) == []
    assert reconciler.backoff_step(201) == 0
    assert commander.ids().count(201) == 3, "no further commands after it landed"


# ------------------------------------------------- backoff belongs to a target
#
# The Hallway, four times in one evening: the hold expired, the lamp was
# commanded off, the PIR re-tripped before any pass had seen the lamp at off,
# and the lamp coming on to 80 was reported as
#
#   WARNING ... did not reach its desired level. It reads 0 and the zone
#   wants 80, so it is being commanded again on a backoff of 1/2/4/8 ticks
#
# about a lamp that had done exactly what it was told, twice. The ladder was
# a record of the "off" command, and it was read as a failure of the "80" one.


def a_zone_with_one_lamp():
    return occupied_zone({"201": 80}, [201])


def test_a_new_desired_level_discards_the_old_backoff(caplog):
    """A change of target is a first command, never a retry.

    Kills: keeping a device's ladder across a change of desired level, which
    is the live Hallway false warning exactly -- and worse than noise, because
    the stale ladder can also DELAY the new command by up to eight ticks.

    Mutation applied: Reconciler.run's `if backoff is not None and
    backoff.level != level: self._clear_backoff(device_id); backoff = None`
    -> deleted.
    """
    zone = a_zone_with_one_lamp()
    lamp = make_device(201, "dimmer", brightness=80)
    apply_level(lamp, 80)
    reconciler = Reconciler(RecordingCommander(), EchoBook(), LOG)

    # The room empties: the lamp is commanded off, and no pass observes it at
    # off before the person comes back -- which is the whole of the bug.
    zone.ingest_presence(101, False, NOW)
    vacant = NOW + dt.timedelta(seconds=400)
    zone.evaluate(vacant, "hold expired")
    assert zone.desired_levels(vacant)[201] == "off"
    assert [command.level for command in reconciler.run(zone, vacant)] == ["off"]
    assert reconciler.backoff_step(201) == 1, "an unconfirmed command is on the ladder"

    # The PIR re-trips. The lamp still reads 80 because nothing moved it, and
    # the zone now wants 80 again -- a different target from the one the
    # ladder is about.
    back = vacant + dt.timedelta(seconds=30)
    zone.ingest_presence(101, True, back)
    zone.evaluate(back, "presence edge")
    assert zone.desired_levels(back)[201] == 80

    apply_level(lamp, 0)  # the lamp DID go off, and reports it late
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        sent = reconciler.run(zone, back)

    assert [command.level for command in sent] == [80], "commanded, not held back"
    assert sent[0].backoff_step == 1, "a first attempt at a new target"
    assert caplog.records == [], (
        "a lamp that did what it was told twice was reported as broken: "
        + "; ".join(record.getMessage() for record in caplog.records)
    )


def test_a_new_target_does_not_wait_out_the_old_ladder():
    """The other half of the same bug, and the one that hurts.

    A device several steps up the ladder has `next_due` many passes away. If
    the ladder survives a change of target, the zone's brand new command
    queues behind a failure that was about something else -- and the lights
    simply do not come on when somebody walks in.

    Kills: the same deletion as above, caught through timing rather than
    through the log, which is the half a "no warning" assertion misses.
    """
    zone = a_zone_with_one_lamp()
    make_device(201, "dimmer", brightness=40)  # stuck at 40: it never lands
    reconciler = Reconciler(RecordingCommander(), EchoBook(), LOG)

    # The room empties and the lamp refuses to go off, so the ladder climbs
    # on the "off" target. Most of these passes are skipped by the backoff,
    # which is exactly what puts next_due out of reach.
    zone.ingest_presence(101, False, NOW)
    vacant = NOW + dt.timedelta(seconds=400)
    zone.evaluate(vacant, "hold expired")
    assert zone.desired_levels(vacant)[201] == "off"
    for step in range(10):
        reconciler.run(zone, vacant + TICK * step)
    assert reconciler.backoff_step(201) >= 3
    assert reconciler.next_due(201) > reconciler.passes + 1, (
        "the ladder must actually be holding this device off, or the "
        "assertion below proves nothing"
    )

    # Somebody walks back in. 80 is a different target and goes out now.
    back = vacant + TICK * 10
    zone.ingest_presence(101, True, back)
    zone.evaluate(back, "presence edge")

    sent = reconciler.run(zone, back)
    assert [command.level for command in sent] == [80], (
        "the new command queued behind a ladder about the old target"
    )


def test_a_genuine_miss_on_a_later_target_is_still_reported(caplog):
    """Quieting the false warning must not quieten the true one.

    Kills: keying `warn_once` on the device alone. The "off" miss latches the
    key, and a genuine failure to reach 80 later in the evening is then
    swallowed -- the device would be silently broken.

    Mutation applied: Reconciler._warn_backoff's `backoff_key(device.id,
    level)` -> `("backoff", device.id)`.
    """
    zone = a_zone_with_one_lamp()
    make_device(201, "dimmer", brightness=0)
    reconciler = Reconciler(RecordingCommander(), EchoBook(), LOG)

    # A genuine miss on "off": commanded, then commanded again, warning once.
    zone.ingest_presence(101, False, NOW)
    vacant = NOW + dt.timedelta(seconds=400)
    zone.evaluate(vacant, "hold expired")
    apply_level(make_device(201, "dimmer", brightness=40), 40)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        reconciler.run(zone, vacant)
        reconciler.run(zone, vacant + TICK)
    assert len([r for r in caplog.records if "did not reach" in r.getMessage()]) == 1

    # Now a genuine miss on 80, which is a different condition entirely.
    back = vacant + TICK * 2
    zone.ingest_presence(101, True, back)
    zone.evaluate(back, "presence edge")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        reconciler.run(zone, back)          # first attempt at 80: silent
        reconciler.run(zone, back + TICK)   # it did not land: report it
    misses = [r for r in caplog.records if "did not reach" in r.getMessage()]
    assert len(misses) == 1, "a real failure on a new target must be reported"
    assert "wants 80" in misses[0].getMessage()
