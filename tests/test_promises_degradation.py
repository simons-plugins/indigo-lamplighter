"""Degradation paths (PRD R8, R15; PRD section 11 decision 3).

Every degradation says so. An unusable precondition is a failed call, not an
empty result, and a warning is emitted once per condition per device rather
than on every pass. The workspace convention this follows is in the root
CLAUDE.md: a tool that returns an honest-looking answer because it could not
do its job is the bug class a green suite hides.
"""

import pytest

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)


@promise
def test_an_unreadable_lux_sensor_makes_the_zone_dark_and_warns_once():
    """With `when_unreadable: "dark"` (the default) the zone treats an
    unreadable lux device as dark, keeps following presence, and warns once
    (decision 3).

    Kills: treat an unreadable sensor as "not dark", which is a room that goes
    dark because a sensor dropped off the mesh -- and does it silently.
    """
    raise NotImplementedError


@promise
def test_when_unreadable_bright_takes_the_zone_off_duty_and_warns_once():
    """The per-zone flag genuinely switches the failure direction: `bright`
    takes the zone OFF-DUTY, which is what a garden wants.

    Kills: read the flag and ignore it, which passes every test written
    against the default.
    """
    raise NotImplementedError


@promise
def test_the_unreadable_warning_is_once_per_condition_not_once_per_pass():
    """The warning fires once while the condition lasts and again only after
    the sensor recovers and fails afresh (section 10).

    Kills: warn on every read (log spam), and: latch the warning forever, so a
    second, later failure is never reported.
    """
    raise NotImplementedError


@promise
def test_a_light_whose_state_cannot_be_read_is_excluded_from_override_detection():
    """A device with neither a readable brightness nor a readable onState
    warns once and is excluded from override detection, but is still commanded
    and the zone keeps working for its other lights (R8).

    Kills: let the unreadable device fall through to a default of at-desired
    (it can then never override) or off-desired (it overrides constantly), and:
    drop the whole zone. The fork's fall-through here was a level-5 log line.
    """
    raise NotImplementedError


@promise
def test_an_indigo_lookup_failure_is_not_reported_as_device_gone():
    """`indigo.devices[...]` raising KeyError means the device is gone; any
    other exception means the lookup itself failed, and the two get different
    handling and different messages (R15).

    Kills: one `except Exception` that treats every failure as "device gone".
    A transient server problem then becomes a confident wrong answer, and the
    zone drops a light it still owns.
    """
    raise NotImplementedError


@promise
def test_recording_a_command_never_costs_the_command():
    """If the pre-command state cannot be read, the command is still sent; the
    device loses its echo record, not its write (R3, R8).

    Kills: record first and send inside the same try, so an unreadable device
    is never commanded at all.
    """
    raise NotImplementedError


@promise
def test_a_stale_lux_reading_says_so():
    """A lux reading older than the zone would trust is reported as stale in
    the zone's explain line rather than used as if it were current (R15).

    Kills: use the last value forever, which is indistinguishable from a
    working sensor right up until the room is wrong all day.
    """
    raise NotImplementedError
