"""Is this device at the level we want it at? (PRD R5, R8; section 5.9.)

Two rules live here and nothing else does.

**The band (R5).** Hardware does not store what you send it. zigbee2mqtt
truncates both ways, so a 30 comes back as 29; a group dimmer asked for 50
reads back somewhere in 45..48. Comparing for equality turns every one of
those into a device that is permanently off desired, which under section 5.8
is a device the reconcile tick commands forever. The band is
``max(1, ceil(target * 0.10))``, with 0 and 100 compared exactly because
"off" and "full" are the two values a user can see are wrong.

**Unreadable is not a value (R8).** A device with neither a readable
brightness nor a readable on/off state raises :class:`UnreadableDevice`. It
never reads as 0, as False, or as at-desired. That is the whole point: the
fork's fall-through here returned a default, which meant an unreadable light
either could never create an override or created one on every event, and said
so only in a level-5 log line. Raising forces every caller to decide, and the
caller that decides "exclude it from override detection but keep commanding
it" says so with :func:`warn_once`.

This module holds no policy about what to do with an unreadable device -- who
warns, who excludes, who still writes -- because those answers differ between
the override rule and the reconcile pass.
"""

from __future__ import annotations

import math

import indigo

#: The smallest band, in brightness points. A target of 1..10 gets exactly
#: this, so a 5 matches 4..6 and nothing wider.
BAND_FLOOR = 1

#: The proportional part of the band: 10 % of the target, rounded up.
BAND_FRACTION = 0.10


class UnreadableDevice(Exception):
    """A device's state could not be read at all (R8).

    Carries the device id, its name and why, because the warning a caller
    writes has to name the device a person would go and look at.
    """

    def __init__(self, device_id, device_name, reason):
        super().__init__(f"{device_name} (id {device_id}): {reason}")
        self.device_id = device_id
        self.device_name = device_name
        self.reason = reason


def band(target: int) -> int:
    """The tolerance, in points, allowed either side of ``target`` (R5)."""
    return max(BAND_FLOOR, math.ceil(target * BAND_FRACTION))


def level_matches(actual: int, target: int) -> bool:
    """Is ``actual`` brightness close enough to ``target`` to leave alone?

    0 and 100 are exact: "off" and "full" are the two levels whose being
    one point out is visible, and a 99 that we call 100 is a light the user
    can see we never finished driving.
    """
    if target <= 0 or target >= 100:
        return actual == target
    return abs(actual - target) <= band(target)


def _brightness(device):
    """This device's brightness as an int, or None if it has none.

    ``bool`` is excluded deliberately: ``isinstance(True, int)`` is true in
    Python, and a relay whose onState leaked into a brightness attribute must
    not read as brightness 1.
    """
    value = getattr(device, "brightness", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _on_state(device):
    """This device's on/off state as a bool, or None if it has none.

    The property is ``dev.onState``; the IOM documents it as a shortcut for
    ``dev.states['onOffState']``, so the states dict is the fallback for a
    plugin device that only publishes the state.
    """
    value = getattr(device, "onState", None)
    if value is None:
        states = getattr(device, "states", None)
        if isinstance(states, dict):
            value = states.get("onOffState")
    if isinstance(value, (bool, int)):
        return bool(value)
    return None


def is_dimmer(device) -> bool:
    """Does this device compare by brightness rather than by on/off?

    The isinstance check comes first because it is the only one that is right
    in both worlds: the IOM gives a DimmerDevice both ``brightness`` and
    ``onState``, and the test harness gives its RelayDevice a ``brightness``
    attribute too. Falling back to the duck-typed check afterwards covers a
    plugin device that is dimmable without subclassing.
    """
    if isinstance(device, indigo.DimmerDevice):
        return True
    if isinstance(device, indigo.RelayDevice):
        return False
    return _brightness(device) is not None


def reading(device):
    """What this device says it is: an int brightness or a bool on/off state.

    Never returns None. A device that can say neither raises
    :class:`UnreadableDevice`, so "I could not read it" can never be mistaken
    for "it is off" (R8, R15).
    """
    device_id = getattr(device, "id", None)
    device_name = getattr(device, "name", None) or f"device {device_id}"

    if is_dimmer(device):
        value = _brightness(device)
        if value is not None:
            return value
        raise UnreadableDevice(
            device_id, device_name, "no readable brightness on a dimmable device"
        )

    value = _on_state(device)
    if value is not None:
        return value
    raise UnreadableDevice(
        device_id,
        device_name,
        "neither a readable brightness nor a readable on/off state",
    )


def _wants_on(level) -> bool:
    """Does this level mean the light should be on at all?"""
    if isinstance(level, bool):
        return level
    if isinstance(level, int):
        return level > 0
    if level in ("on", "off"):
        return level == "on"
    raise ValueError(
        f"{level!r} is not a level to compare against: expected an int 1-100, "
        "'on', 'off', True or False ('leave' is never compared, because a "
        "device set to leave is never written)"
    )


def at_level(device, level) -> bool:
    """Is ``device`` already at ``level``, within the band (R5)?

    ``level`` is an int 1..100, ``"on"``, ``"off"``, ``True`` or ``False``.
    ``"leave"`` is not accepted: a device the zone never writes is never
    compared either, and passing it here is a caller bug worth a loud
    :class:`ValueError` rather than a quiet False.

    Raises :class:`UnreadableDevice` when the device cannot be read; the
    caller decides whether that means "skip it" or "warn and exclude it"
    (R8).
    """
    # The level is checked before the device is read, so a caller that passes
    # a level this function cannot compare hears about its own bug even when
    # the device it passed is also unreadable.
    wants_on = _wants_on(level)
    actual = reading(device)

    if isinstance(actual, bool):  # relay: only on/off is observable
        return actual == wants_on

    if isinstance(level, bool) or not isinstance(level, int):
        # "on"/True on a dimmer means any light at all; "off"/False means none.
        return actual > 0 if wants_on else actual == 0

    return level_matches(actual, level)


# --------------------------------------------------------- warn-once policy
#
# Section 10: one WARNING per condition per device, not one per pass. The set
# is module level because the conditions it tracks outlive any one object --
# a zone rebuilt by a config reload must not re-announce a sensor that has
# been dead since Tuesday. A condition that clears calls reset_warnings(key)
# so that a later, separate failure is reported afresh rather than latched
# into silence forever.

_WARNED: set = set()


def warn_once(logger, key, message) -> bool:
    """Log ``message`` at WARNING the first time ``key`` is seen.

    Returns True if it logged, so a caller can tell a new condition from a
    continuing one without keeping its own bookkeeping.
    """
    if key in _WARNED:
        return False
    _WARNED.add(key)
    logger.warning(message)
    return True


def reset_warnings(key=None) -> None:
    """Forget one warning key, or all of them when ``key`` is None.

    Called when a condition clears: the sensor answered, the device reported.
    The next failure of that same condition warns again, because a warning
    that latches forever hides the second outage as thoroughly as no warning
    at all hides the first.
    """
    if key is None:
        _WARNED.clear()
    else:
        _WARNED.discard(key)
