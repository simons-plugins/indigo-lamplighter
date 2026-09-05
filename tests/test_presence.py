"""Unit tests for the presence hold (PRD R4; sections 5.2, 5.4).

The acceptance promises in tests/test_promises_presence.py are the contract;
this is the arithmetic and the edge classification underneath them.
"""

import datetime as dt

import pytest

from lamplighter.presence import Edge, Presence

NOW = dt.datetime(2026, 9, 4, 19, 40, 0)


def at(seconds):
    return NOW + dt.timedelta(seconds=seconds)


# ------------------------------------------------------------- the edges


def test_the_first_on_report_activates():
    presence = Presence()
    assert presence.update(101, True, NOW) is Edge.ACTIVATED
    assert presence.last_seen == NOW


def test_a_second_on_report_refreshes_rather_than_activating():
    """A re-report moves the hold without being a state edge -- the Occupatum
    countdown tick, which the fork turned into a re-plan."""
    presence = Presence()
    presence.update(101, True, NOW)
    assert presence.update(101, True, at(60)) is Edge.REFRESHED
    assert presence.last_seen == at(60)


def test_a_second_device_reporting_on_refreshes_rather_than_activating():
    presence = Presence()
    presence.update(101, True, NOW)
    assert presence.update(102, True, at(5)) is Edge.REFRESHED


def test_an_off_report_clears_the_device_and_starts_the_hold():
    """The off-delay begins when the sensor clears, not at its last "on".

    This is Occupatum's rule and it is the one a level sensor needs: an FP1
    reports "on" once and says nothing for two hours, so a hold measured from
    the last "on" empties the room with somebody sitting in it.
    """
    presence = Presence()
    presence.update(101, True, NOW)
    assert presence.update(101, False, at(30)) is Edge.CLEARED
    assert presence.on_devices == set()
    assert presence.last_seen == at(30), "the hold must run from the clear"


def test_an_off_report_from_a_device_that_was_already_off_changes_nothing():
    """Kills: stamping last_seen on every "off". A sensor that reports "off"
    every thirty seconds would hold an empty room open for ever."""
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(101, False, at(30))

    assert presence.update(101, False, at(90)) is Edge.NONE
    assert presence.last_seen == at(30)


def test_an_off_report_from_an_unknown_device_is_not_an_edge():
    assert Presence().update(999, False, NOW) is Edge.NONE


def test_going_quiet_then_reporting_again_activates():
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(101, False, at(30))
    assert presence.update(101, True, at(60)) is Edge.ACTIVATED


def test_the_edge_enum_is_falsy_only_for_none():
    assert not Edge.NONE
    assert Edge.REFRESHED
    assert Edge.ACTIVATED
    # CLEARED has to be truthy: it is the only notice the worker gets that a
    # hold has started, and a falsy one would never schedule the wake-up.
    assert Edge.CLEARED


def test_only_activation_is_a_state_edge():
    assert Edge.ACTIVATED.is_state_edge
    assert not Edge.REFRESHED.is_state_edge
    assert not Edge.NONE.is_state_edge
    # CLEARED starts the hold rather than ending it, so the room is still
    # occupied when it arrives: a timer edge, not a state edge.
    assert not Edge.CLEARED.is_state_edge


# -------------------------------------------------------------- the hold


def test_presence_is_inactive_before_anything_is_ever_seen():
    presence = Presence()
    assert presence.active(NOW, 300) is False
    assert presence.expiry(300) is None


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, True), (299, True), (300, False), (301, False)],
)
def test_active_is_now_minus_the_clear_under_the_hold(seconds, expected):
    """The edge is exclusive: at exactly `hold_seconds` the room is empty.

    Measured from the moment the last sensor cleared, which is when the hold
    starts. While it was on there was no hold at all.
    """
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(101, False, NOW)
    assert presence.active(at(seconds), 300) is expected


def test_the_expiry_is_the_clear_plus_the_hold():
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(101, False, NOW)
    assert presence.expiry(300) == at(300)


def test_there_is_no_expiry_while_a_sensor_is_still_on():
    """The heart of the level-sensor fix.

    Kills: `expiry` measured from last_seen regardless of the reporting set.
    A wake-up scheduled here fires while the sensor is still on, and the zone
    puts itself VACANT with the person in the chair.
    """
    presence = Presence()
    presence.update(101, True, NOW)

    assert presence.expiry(300) is None
    assert presence.active(at(10_000), 300) is True, "a level sensor holds the room"

    presence.update(101, False, at(10_000))
    assert presence.expiry(300) == at(10_300)


def test_a_later_clear_moves_the_expiry():
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(101, False, at(120))
    assert presence.expiry(300) == at(420)
    assert presence.active(at(400), 300) is True


def test_a_hold_of_zero_follows_the_sensor_exactly():
    """`hold_seconds: 0` is now "no delay after the room clears".

    Under the old timestamp-only rule it meant "never occupied", which was
    arithmetic nobody could use. With the reporting set it is the sensible
    reading: occupied while something is on, empty the instant it is not.
    """
    presence = Presence()
    presence.update(101, True, NOW)
    assert presence.active(NOW, 0) is True

    presence.update(101, False, at(30))
    assert presence.active(at(30), 0) is False


def test_the_reporting_set_is_any_of_not_all_of():
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(102, True, NOW)
    presence.update(101, False, at(10))
    # 102 is still reporting, so the next 101 report is not a fresh activation.
    assert presence.update(101, True, at(20)) is Edge.REFRESHED
    presence.update(102, False, at(30))
    assert presence.on_devices == {101}


# ---------------------------------------------------- the level sensor (FP1)
#
# The Study's presence is an Aqara FP1 radar plus a PIR. A radar is a LEVEL
# sensor: one "on" when somebody arrives, silence while they are there, one
# "off" when they leave. Everything below is the difference between that
# working and the lights going out on somebody sitting still.


def test_a_level_sensor_holds_the_room_for_as_long_as_it_is_on():
    """Kills: `active()` measured from last_seen alone.

    Two hours with no re-report is not a stale reading, it is a radar saying
    "still occupied". The old rule emptied the room after `hold_seconds`.
    """
    presence = Presence()
    presence.update(101, True, NOW)

    for hours in (1, 2, 8):
        assert presence.active(at(hours * 3600), 300) is True, f"{hours}h in"
    assert presence.expiry(300) is None


def test_the_hold_starts_when_the_last_sensor_clears_not_at_the_last_on():
    """Two sensors, and the delay belongs to the second one to let go.

    Kills: starting the hold at the last "on" edge, which would expire this
    room at NOW+300 -- while the radar was still reporting.
    """
    presence = Presence()
    presence.update(101, True, NOW)          # PIR trips
    presence.update(102, True, at(5))        # radar picks them up
    presence.update(101, False, at(20))      # PIR drops almost immediately

    assert presence.active(at(3600), 300) is True, "the radar is still on"

    presence.update(102, False, at(3600))    # they leave
    assert presence.expiry(300) == at(3900)
    assert presence.active(at(3899), 300) is True
    assert presence.active(at(3900), 300) is False


def test_one_sensor_on_and_another_off_is_still_occupied():
    """Any-of, and the "off" must not cancel the one that is still on.

    Kills: treating an "off" as "the room is empty" rather than as "this
    device is no longer reporting".
    """
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(102, True, NOW)

    assert presence.update(101, False, at(10)) is Edge.CLEARED
    assert presence.on_devices == {102}
    assert presence.active(at(10_000), 300) is True
    assert presence.expiry(300) is None
