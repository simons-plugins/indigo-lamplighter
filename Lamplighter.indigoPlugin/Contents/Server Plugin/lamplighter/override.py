"""Did a person just take this zone over? (PRD R1-R3, R7, R8; section 5.7.)

One question, asked once per device-change event, on the Indigo callback
thread, from the two snapshots Indigo hands ``deviceUpdated`` and from
nothing else.

**The transition rule (R1).** A manual override is a device moving *from* its
desired level *to* something else. Not "is the device off desired now" -- that
is every intermediate step of the plugin's own ramp (R2). Not "is anything in
this zone off desired" -- that is a sibling still mid-write locking a device
that never moved (M5). And emphatically not a live re-read: the fork's first
attempt did that and lost every override to its own revert, because by the
time anything re-read the light the plugin had already put it back. The
evidence for an override exists for a few hundred milliseconds and only in the
event; the rule is built around that fact.

**The echo window (R3).** The transition rule is nearly enough on its own,
because we only ever command a device that is *not* at its desired level, so
every echo of our own command starts from an off-desired state and fails
condition 2. The gap it leaves is a re-plan landing between our command and
its echo: we command the light on, the room brightens, desired goes back to
off -- and the queued on-echo now reads as at-desired -> off-desired, a
textbook override that is really ours.

:class:`EchoBook` closes it by remembering, per device, the state we commanded
the device *away from*. That is what the echo transition starts from, whatever
value the device reports on the way out of it -- a dimmer ramping to 60
reports 12 first, a number we never asked anyone for. Matching on the
commanded value instead finds nothing.

Two bounds keep that from becoming a blanket excuse for real people: each
record excuses exactly one transition and is then consumed, and each record
expires with ``echo_window_seconds``. The documented cost is one manual change
swallowed inside the window when it happens to start from a state we
commanded the device away from; the alternative is a zone that locks itself
every time daylight moves.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import deque

from . import compare
from .zone import LEAVE, OFF_DUTY_DISABLED, OFF_DUTY_NO_PERIOD, ZoneState

#: How many pre-command states are kept per device. A burst longer than this
#: is a ramp, and a ramp's later steps need no excuse at all: their previous
#: state is already off desired, so the transition rule alone acquits them.
HISTORY_PER_DEVICE = 4


def unreadable_key(device_id):
    """The :func:`compare.warn_once` key for "this light cannot be read" (R8).

    Shared with :mod:`lamplighter.reconcile` on purpose. One dead light is
    one condition however many places notice it, and section 10 says one
    WARNING per condition per device.
    """
    return ("light-unreadable", device_id)


def no_before_state_key(device_id):
    """The warn key for "this event arrived with no before-state" (M9)."""
    return ("no-before-state", device_id)


class EchoBook:
    """The states we commanded each device away from, and when (R3).

    Written by the reconcile pass on the worker thread, read by the override
    rule on the Indigo callback thread, so it owns a lock. It is deliberately
    the only shared mutable state between the two.
    """

    def __init__(self, history_per_device: int = HISTORY_PER_DEVICE):
        self._history_per_device = history_per_device
        self._records: dict = {}
        self._lock = threading.Lock()

    def note_pre_command(self, dev_id, pre_state, now: dt.datetime) -> None:
        """Record the state ``dev_id`` was in as we command it away from it.

        ``pre_state`` is :func:`compare.reading` taken immediately before the
        command -- an int brightness or a bool on/off -- because the echo is
        the device's own report of *leaving* that state.
        """
        with self._lock:
            history = self._records.get(dev_id)
            if history is None:
                history = deque(maxlen=self._history_per_device)
                self._records[dev_id] = history
            history.append((pre_state, now))

    def consume_echo(self, dev_id, previous_dev, now: dt.datetime, window_seconds):
        """Age in seconds of the command this transition echoes, or None.

        A match means the transition *started* from a state we commanded this
        device away from inside the window. The record is removed: one
        command excuses one transition, or a person switching a light off,
        watching the zone put it back and switching it off again is never
        heard.

        Records older than the window are pruned on the way past, so an
        unmatched book does not grow.
        """
        cutoff = now - dt.timedelta(seconds=window_seconds)
        with self._lock:
            history = self._records.get(dev_id)
            if not history:
                return None
            while history and history[0][1] < cutoff:
                history.popleft()
            for index, (pre_state, noted_at) in enumerate(history):
                try:
                    matched = compare.at_level(previous_dev, pre_state)
                except compare.UnreadableDevice:
                    # The snapshot cannot be read, so it cannot be shown to
                    # be ours. Falling through to "no match" is the strict
                    # direction: the transition rule alone then decides.
                    return None
                if matched:
                    del history[index]
                    return (now - noted_at).total_seconds()
        return None

    def forget(self, dev_id=None) -> None:
        """Drop one device's records, or all of them. Used by a config reload."""
        with self._lock:
            if dev_id is None:
                self._records.clear()
            else:
                self._records.pop(dev_id, None)

    def pending(self, dev_id) -> tuple:
        """The pre-command states currently on record for one device.

        Read by tests and by ``explain``-style reporting. A tuple, so a
        caller cannot mutate the book by accident.
        """
        with self._lock:
            return tuple(pre for pre, _noted_at in self._records.get(dev_id, ()))


def is_manual_override(
    zone,
    previous_dev,
    current_dev,
    now: dt.datetime,
    echo_book: EchoBook,
    window_seconds: int,
    logger=None,
) -> bool:
    """Is this one device-change event a person taking the zone over?

    The four conditions of section 5.7, in order, cheapest first. Every one
    of them is a place the fork got it wrong at least once, so each returns
    with a DEBUG line naming the values that fed the decision (R14).
    """
    logger = logger or zone.logger
    device_id = getattr(current_dev, "id", None)
    device_name = getattr(current_dev, "name", None) or f"device {device_id}"

    if previous_dev is None:
        # Production always supplies Indigo's origDev, so this is a call site
        # that has silently disabled override detection for a device. Section
        # 10 puts it at WARNING, once per device, because at DEBUG it would
        # go unnoticed for months (M9, R15).
        compare.warn_once(
            logger,
            no_before_state_key(device_id),
            f"{zone.name}: the change from {device_name} arrived with no "
            "before-state, so the transition cannot be judged and manual "
            "override detection is off for this event. This is not the device "
            "being at its desired level -- nothing about it was decided.",
        )
        return False

    # ------------------------------------------------- condition 1: standing
    #
    # Does this device have a desired level to be moved away from, and is this
    # zone one that locks at all? Every branch here answers "there is nothing
    # to judge", which is a different answer from "it was judged and was not
    # an override", and the DEBUG lines say which.

    if not zone.running:
        # Checked here rather than at write time. A disabled zone that banked
        # overrides would act on all of them the moment it was switched back
        # on (M10d).
        logger.debug(
            f"{zone.name}: change from {device_name} ignored -- the zone or the "
            "plugin is disabled, so it has no desired level to be moved off"
        )
        return False

    if zone.state is ZoneState.OVERRIDDEN:
        # Already someone else's. Desired is "whatever the devices are", so
        # there is no level a change could move away from.
        logger.debug(
            f"{zone.name}: change from {device_name} ignored -- the zone is "
            "already overridden"
        )
        return False

    if zone.off_duty_cause in (OFF_DUTY_DISABLED, OFF_DUTY_NO_PERIOD):
        logger.debug(
            f"{zone.name}: change from {device_name} ignored -- the zone is off "
            f"duty ({zone.off_duty_cause}) and has no opinion about its lights"
        )
        return False

    if not zone.config.override.enabled:
        # The never-lock hallway (R10). The change is noticed and the zone
        # keeps commanding its desired levels; it simply never enters
        # OVERRIDDEN. Checked here and not only on the timing path, or the
        # zone would lock and merely expire quickly (M10b).
        logger.debug(
            f"{zone.name}: change from {device_name} noticed, but this zone "
            "never locks (override.enabled is false)"
        )
        return False

    if device_id in zone.config.override.exclude:
        # Excluded from *detection*, not from the plan: a late reporter is
        # still commanded normally (R6, M10a).
        logger.debug(
            f"{zone.name}: change from {device_name} ignored -- the device is in "
            "override.exclude; it is still commanded normally"
        )
        return False

    desired = zone.desired_levels(now).get(device_id, LEAVE)
    if desired == LEAVE:
        # Absent from the period's levels, or explicitly `leave`. A missing
        # level must never default to off, which would make every unlisted
        # light permanently off desired (M10c).
        logger.debug(
            f"{zone.name}: change from {device_name} ignored -- the zone has no "
            "desired level for it right now (leave)"
        )
        return False

    # --------------------------------------- conditions 2 and 3: the transition

    try:
        was_at_desired = compare.at_level(previous_dev, desired)
        now_at_desired = compare.at_level(current_dev, desired)
    except compare.UnreadableDevice as exc:
        # R8. Not "no override" quietly -- a device that can never lock its
        # zone and never says why is the fork's level-5 fall-through.
        compare.warn_once(
            logger,
            unreadable_key(device_id),
            f"{zone.name}: {device_name} ({device_id}) cannot be read "
            f"({exc.reason}), so its "
            f"change cannot be judged against the desired level {desired!r}. It "
            "is excluded from override detection until it can be read; the zone "
            "keeps working for its other lights and still commands this one.",
        )
        return False

    if not (was_at_desired and not now_at_desired):
        logger.debug(
            f"{zone.name}: change from {device_name} is not a transition off "
            f"desired={desired!r} (was_at_desired={was_at_desired}, "
            f"now_at_desired={now_at_desired}); no override"
        )
        return False

    # -------------------------------------------- condition 4: is it our echo?

    echo_age = echo_book.consume_echo(device_id, previous_dev, now, window_seconds)
    if echo_age is not None:
        logger.debug(
            f"{zone.name}: change from {device_name} moved off a state this "
            f"plugin commanded it away from {echo_age:.1f}s ago, so it is that "
            f"command's echo arriving after desired moved back to {desired!r}; "
            "no override"
        )
        return False

    logger.debug(
        f"{zone.name}: {device_name} moved off desired={desired!r} "
        f"(was_at_desired=True, now_at_desired=False, not an echo); "
        "this is a manual override"
    )
    return True
