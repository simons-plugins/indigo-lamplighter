"""Unit tests for the tolerance rules (PRD R5, R8; section 5.9).

These sit under the acceptance promises rather than beside them: the band
edges and the unreadable-device rule are arithmetic and dispatch, and they
are cheaper to pin here than through a zone. The promises that build on them
live in tests/test_promises_*.py.
"""

import logging
import types

import pytest
from helpers import make_device

from lamplighter import compare
from lamplighter.compare import UnreadableDevice, at_level, level_matches, reading


# ------------------------------------------------------------- the band (R5)


@pytest.mark.parametrize(
    "target,expected",
    [
        (1, 1),  # the floor, not 10% of 1 rounded down to nothing
        (5, 1),
        (10, 1),
        (11, 2),  # ceil(1.1)
        (30, 3),
        (50, 5),
        (99, 10),
    ],
)
def test_the_band_is_ten_percent_with_a_floor_of_one(target, expected):
    assert compare.band(target) == expected


@pytest.mark.parametrize("actual", [0, 1, 99, 101])
def test_one_hundred_is_exact(actual):
    """A 99 the plugin calls "at 100" is a light it never finished driving."""
    assert not level_matches(actual, 100)


def test_one_hundred_matches_itself():
    assert level_matches(100, 100)


@pytest.mark.parametrize("actual", [1, 2, 5])
def test_zero_is_exact(actual):
    """0 is off. A light at 1 is not off, whatever the proportional band says."""
    assert not level_matches(actual, 0)
    assert level_matches(0, 0)


@pytest.mark.parametrize(
    "actual,target,expected",
    [
        (29, 30, True),  # zigbee2mqtt truncating downwards
        (31, 30, True),
        (33, 30, True),  # exactly ceil(10% of 30)
        (34, 30, False),
        (45, 50, True),  # a group dimmer reading back low
        (48, 50, True),
        (44, 50, False),
        (2, 1, True),  # floor band either side of 1
        (3, 1, False),
    ],
)
def test_the_band_is_symmetric_and_closed(actual, target, expected):
    assert level_matches(actual, target) is expected


# --------------------------------------------------------- reading a device


def test_a_dimmer_reads_as_its_brightness():
    dimmer = make_device(1, "dimmer", brightness=42)
    assert reading(dimmer) == 42


def test_a_relay_reads_as_its_on_state_not_its_brightness():
    """The harness gives every stub device a brightness; a relay's is noise.

    In the IOM a RelayDevice has no brightness at all, so reading one as 0
    would make every relay permanently "off desired" and re-commanded on
    every reconcile tick.
    """
    relay = make_device(2, "relay", onState=True, brightness=0)
    assert reading(relay) is True


def test_a_device_with_only_a_state_dict_reads_from_on_off_state():
    """`dev.onState` is documented as a shortcut for `states['onOffState']`."""
    device = types.SimpleNamespace(id=3, name="Plugin Relay", states={"onOffState": True})
    assert reading(device) is True


def test_an_unreadable_device_raises_and_names_itself():
    """Never None, never 0, never False (R8)."""
    device = types.SimpleNamespace(id=4, name="Ghost Lamp")
    with pytest.raises(UnreadableDevice) as caught:
        reading(device)
    assert caught.value.device_id == 4
    assert caught.value.device_name == "Ghost Lamp"
    assert caught.value.reason
    assert "Ghost Lamp" in str(caught.value)


def test_a_dimmer_without_a_readable_brightness_is_unreadable():
    dimmer = make_device(5, "dimmer")
    dimmer.brightness = None
    with pytest.raises(UnreadableDevice):
        reading(dimmer)


# ----------------------------------------------------------------- at_level


@pytest.mark.parametrize(
    "brightness,level,expected",
    [
        (48, 50, True),
        (44, 50, False),
        (0, "off", True),
        (1, "off", False),
        (0, False, True),
        (1, "on", True),
        (0, "on", False),
        (100, True, True),
        (0, True, False),
    ],
)
def test_a_dimmer_compares_by_brightness(brightness, level, expected):
    dimmer = make_device(6, "dimmer", brightness=brightness)
    assert at_level(dimmer, level) is expected


@pytest.mark.parametrize(
    "on_state,level,expected",
    [
        (True, "on", True),
        (True, True, True),
        (True, 50, True),  # a relay treats any integer as "on"
        (True, "off", False),
        (True, False, False),
        (False, "off", True),
        (False, False, True),
        (False, 50, False),
        (False, "on", False),
    ],
)
def test_a_relay_compares_by_on_state_in_both_polarities(on_state, level, expected):
    relay = make_device(7, "relay", onState=on_state)
    assert at_level(relay, level) is expected


def test_leave_is_never_compared():
    """A device the zone never writes is never compared either.

    Kills: accept "leave" and answer False, which makes every left-alone
    device look off-desired to the reconcile pass.
    """
    dimmer = make_device(8, "dimmer", brightness=30)
    with pytest.raises(ValueError, match="leave"):
        at_level(dimmer, "leave")


def test_an_unusable_level_is_reported_before_the_device_is_read():
    """The cheap check runs first, so a caller bug is not masked by a dead
    device -- and a dead device is not blamed for a caller bug."""
    device = types.SimpleNamespace(id=9, name="Ghost Lamp")
    with pytest.raises(ValueError) as caught:
        at_level(device, "leave")
    assert not isinstance(caught.value, UnreadableDevice)


def test_at_level_propagates_unreadable_rather_than_guessing():
    """Kills: fall through to True (can never override) or False (overrides
    constantly) -- the fork's level-5 log line (R8)."""
    device = types.SimpleNamespace(id=10, name="Ghost Lamp")
    with pytest.raises(UnreadableDevice):
        at_level(device, 50)


# ---------------------------------------------------------------- warn_once


def test_warn_once_warns_once_then_again_after_the_condition_clears(caplog):
    """Section 10: once per condition per device -- not once per pass, and
    not latched forever, which would hide the second outage as completely as
    no warning hides the first."""
    compare.reset_warnings()
    logger = logging.getLogger("test.warn_once")

    with caplog.at_level(logging.WARNING, logger="test.warn_once"):
        assert compare.warn_once(logger, ("lux", 17), "sensor 17 is unreadable") is True
        assert compare.warn_once(logger, ("lux", 17), "sensor 17 is unreadable") is False
        assert compare.warn_once(logger, ("lux", 18), "sensor 18 is unreadable") is True
        assert len(caplog.records) == 2

        compare.reset_warnings(("lux", 17))
        assert compare.warn_once(logger, ("lux", 17), "sensor 17 is unreadable again") is True
        assert compare.warn_once(logger, ("lux", 18), "sensor 18 is unreadable") is False
        assert len(caplog.records) == 3

    compare.reset_warnings()
