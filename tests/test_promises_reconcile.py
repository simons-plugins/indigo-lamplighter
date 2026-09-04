"""Reconcile (PRD R6, section 5.8).

One command per device that is off desired, no settle poll, no confirm thread,
no suppression list, no re-evaluation rate limit. A device that does not land
is picked up at the next reconcile tick with per-device backoff. This is the
whole write machinery, and PRD section 9 records why: if a device does not
confirm, the answer is the tick, not a thread.
"""

import pytest

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)


@promise
def test_one_command_per_device_per_pass():
    """A reconcile pass sends at most one command to each device that is off
    desired, and none to a device inside the band.

    Kills: re-command every device in the plan every pass, which is what makes
    a zone's own echoes a permanent source of events.
    """
    raise NotImplementedError


@promise
def test_a_device_that_has_not_reported_yet_is_not_re_commanded_in_the_pass():
    """Nothing waits for a confirmation inside a pass: a device that has not
    reported back is simply still off desired at the next tick.

    Kills: reintroduce a settle poll. Make the failure fatal -- hand the pass a
    clock or a sleep that raises if touched -- so "we did not wait" is
    asserted rather than assumed.
    """
    raise NotImplementedError


@promise
def test_backoff_doubles_per_device_and_is_capped():
    """A device still off desired is retried at 1, 2, 4, 8 ticks and then at
    the cap, per device.

    Kills: back off per zone, which lets one broken light stall every healthy
    one in the room.
    """
    raise NotImplementedError


@promise
def test_the_first_backoff_step_warns_once_naming_actual_and_desired():
    """One WARNING at the first backoff step, naming the device, its actual
    level and its desired level; not one per tick (R15, section 10).

    Kills: warn every pass (log spam that hides the next problem), and: warn
    never (the fork's silent suppression, where a dead light simply stopped
    being mentioned).
    """
    raise NotImplementedError


@promise
def test_a_device_reporting_at_desired_clears_its_backoff_silently():
    """Landing on desired resets the device's backoff with no log line.

    Kills: keep the backoff until a full pass finds the device idle, so a
    device that recovers stays on a slow retry schedule.
    """
    raise NotImplementedError


@promise
def test_a_slow_reporter_is_reconciled_without_a_retry_storm():
    """A device with a 50 s round trip is neither retried inside the window,
    nor suppressed, nor treated as an override; it is reconciled when it
    finally reports (R6).

    Kills: count a missing confirmation as a failure. 'Kitchen - LED Strip'
    self-locked its zone under the fork and needed exclude_from_lock to work
    at all.
    """
    raise NotImplementedError
