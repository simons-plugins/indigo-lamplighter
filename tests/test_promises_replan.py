"""Re-plan only on input edges (PRD R4, section 5.2).

A zone re-plans when an INPUT changes: presence on/off, presence last-seen
crossing the hold, lux crossing the hysteresis-widened threshold, a period
boundary, an override starting or ending. Never on a device update as such.

The fork re-planned on every event: an Occupatum zone device ticking its
countdown every 1.2 s produced hundreds of re-plans an hour, reverts within a
second of a manual change, and 10 s of callback lag.
"""

import pytest

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)


@promise
def test_a_presence_device_re_reporting_on_does_not_replan():
    """A presence device that reports "on" again, unchanged, causes no
    re-plan -- the live bug.

    Kills: re-evaluate on any update of a presence device. This is the
    Occupatum countdown tick.
    """
    raise NotImplementedError


@promise
def test_a_display_string_or_timer_update_does_not_replan():
    """An update that touches only a display string, an uptime counter or a
    battery level changes no input and causes no re-plan.

    Kills: treat every deviceUpdated for a zone member as an input edge.
    """
    raise NotImplementedError


@promise
def test_a_genuine_presence_edge_replans_exactly_once():
    """An off->on transition on a presence device re-plans the zone once --
    not zero times, not once per device.

    Kills: over-correcting the tick fix into a gate that also swallows the
    real transition.
    """
    raise NotImplementedError


@promise
def test_a_lux_reading_that_stays_inside_the_band_does_not_replan():
    """A lux reading that moves without crossing the hysteresis-widened
    threshold changes no input (R9).

    Kills: re-plan on any change of the lux value. A sensor reporting every
    few seconds would then re-plan as often as the fork did.
    """
    raise NotImplementedError


@promise
def test_a_lux_reading_that_crosses_the_threshold_replans():
    """Crossing dark/bright is an input edge and re-plans the zone.

    Kills: latch dark forever after the first reading -- the fork's mutation,
    which made a zone that went dark once never come back.
    """
    raise NotImplementedError


@promise
def test_a_period_boundary_replans_with_no_device_event_at_all():
    """The zone re-plans when a period starts or ends, driven by the worker's
    timer heap, with nothing on the device bus.

    Kills: hang re-planning off device events only, so a zone whose room is
    empty and quiet never notices the period changed.
    """
    raise NotImplementedError


@promise
def test_the_edge_gate_reads_the_before_and_after_snapshots():
    """The gate compares the two snapshots Indigo hands the callback, in both
    directions.

    Kills: gate on the set of changed keys instead. The keys tell you what
    Indigo decided to report, not whether the value the zone reads actually
    moved -- a zone can be woken by a key it does not care about and left
    asleep through a change carried in a key it does.
    """
    raise NotImplementedError
