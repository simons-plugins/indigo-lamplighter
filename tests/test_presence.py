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


def test_an_off_report_is_not_an_edge_and_does_not_touch_last_seen():
    """Presence ends when the hold expires, never when a sensor goes quiet --
    that is what makes a 10-second PIR usable as a 5-minute hold."""
    presence = Presence()
    presence.update(101, True, NOW)
    assert presence.update(101, False, at(30)) is Edge.NONE
    assert presence.last_seen == NOW


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


def test_only_activation_is_a_state_edge():
    assert Edge.ACTIVATED.is_state_edge
    assert not Edge.REFRESHED.is_state_edge
    assert not Edge.NONE.is_state_edge


# -------------------------------------------------------------- the hold


def test_presence_is_inactive_before_anything_is_ever_seen():
    presence = Presence()
    assert presence.active(NOW, 300) is False
    assert presence.expiry(300) is None


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, True), (299, True), (300, False), (301, False)],
)
def test_active_is_now_minus_last_seen_under_the_hold(seconds, expected):
    """The edge is exclusive: at exactly `hold_seconds` the room is empty."""
    presence = Presence()
    presence.update(101, True, NOW)
    assert presence.active(at(seconds), 300) is expected


def test_the_expiry_is_last_seen_plus_the_hold():
    presence = Presence()
    presence.update(101, True, NOW)
    assert presence.expiry(300) == at(300)


def test_a_re_report_moves_the_expiry():
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(101, True, at(120))
    assert presence.expiry(300) == at(420)
    assert presence.active(at(400), 300) is True


def test_a_hold_of_zero_is_never_active():
    """The PRD's arithmetic, stated so nobody later reads it as a bug: a zone
    that wants to follow raw device state is not what this design does."""
    presence = Presence()
    presence.update(101, True, NOW)
    assert presence.active(NOW, 0) is False


def test_the_reporting_set_is_any_of_not_all_of():
    presence = Presence()
    presence.update(101, True, NOW)
    presence.update(102, True, NOW)
    presence.update(101, False, at(10))
    # 102 is still reporting, so the next 101 report is not a fresh activation.
    assert presence.update(101, True, at(20)) is Edge.REFRESHED
    presence.update(102, False, at(30))
    assert presence.on_devices == {101}
