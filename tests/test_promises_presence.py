"""Presence hold lives in the zone (PRD R4, R10, R13; sections 5.2, 5.4).

Raw sensors -- PIR, mmWave, door contacts, or an Occupatum zone device during
migration -- feed one per-zone last-seen timestamp and one hold. No second
plugin is needed to say "still here", and no device needs to tick.
"""

import pytest

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)


@promise
def test_presence_last_seen_survives_a_restart():
    """last_seen is persisted on the zone device and restored at startup
    (R13), so a restart does not turn the lights off on an occupied room.

    Kills: initialise last_seen to None or to now at startup. None empties an
    occupied room; now fills an empty one.
    """
    raise NotImplementedError


@promise
def test_presence_is_any_of_the_zones_devices():
    """Any one presence device reporting on makes the zone occupied
    (section 5.4).

    Kills: require all of them, which turns a two-sensor room into a room that
    is never occupied.
    """
    raise NotImplementedError


@promise
def test_a_re_report_of_on_refreshes_last_seen_without_replanning():
    """A repeated "on" reading moves last_seen forward even though it is not
    an input edge and causes no re-plan (R4 + section 5.4).

    Kills: fixing the re-plan storm by ignoring repeated "on" readings
    entirely, which stops the hold ever being refreshed and empties an
    occupied room after `hold_seconds`.
    """
    raise NotImplementedError


@promise
def test_hold_expiry_turns_the_lights_off_exactly_once():
    """Crossing `hold_seconds` since last_seen is an input edge: the zone goes
    VACANT and writes once.

    Kills: poll the hold on every reconcile tick and re-command the lights off
    each time, which reduces a person re-entering the room to a race.
    """
    raise NotImplementedError


@promise
def test_unlock_on_leave_fires_from_hold_expiry_with_an_override_held():
    """An override is released when the zone's own presence hold expires, even
    though the override was created while the room was occupied (R10).

    Kills: arm unlock-on-leave only for overrides created in an already-empty
    room -- fork #17, where every override made by a person standing in the
    room ran its full duration.
    """
    raise NotImplementedError
