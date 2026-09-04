"""The daylight gate, as a Schmitt trigger (PRD R9, R15; section 11, dec. 3).

A zone is dark below ``dark_below`` and bright again only above
``dark_below + hysteresis``. The band applies **only on the way out of dark**,
and it exists because the Kitchen's lux sensor is in the kitchen: the zone's
own lights lift the reading by 100-200 lux, so a plain threshold gives
lights on -> reading rises -> not dark -> lights off -> reading falls, for
ever.

**A lux change is not an input; a change of verdict is.** This is fork issue
#13 solved by design rather than by a rate limit. A sensor reporting every
few seconds moves ``value`` constantly and moves ``dark`` almost never, so
:attr:`changed` -- did the verdict flip -- is what the zone re-plans on. Two
readings ten lux apart on the same side of the threshold are not an event.

**Unreadable is a third answer.** A sensor that cannot be read is not 0 and
is not "not dark". It takes the direction the zone configured (section 11,
decision 3): ``dark`` (the default) keeps an indoor room following presence
when a sensor drops off the mesh, ``bright`` takes a garden off duty, where a
stuck-on floodlight is the worse outcome. Either way it warns once per
condition per device, and the key is released when a readable value comes
back so that a later, separate outage is reported afresh instead of being
latched into silence.
"""

from __future__ import annotations

import datetime as dt
import logging

from . import compare

#: How old a reading may be before the zone calls it stale in its explain
#: line (R15). Not a configuration value: it is a reporting threshold, not a
#: control input -- the verdict still comes from the last reading either way,
#: because a stale reading is better evidence than no reading. What it must
#: never do is look like a working sensor, which is the failure a lux device
#: that quietly stopped reporting produces: a room that is wrong all day.
STALE_AFTER_SECONDS = 1800


class Lux:
    """One zone's lux input: the reading, the verdict, and the flip.

    ``value`` is the last reading that could be read at all and survives an
    unreadable one, so the explain line can say "1800, 45 minutes ago" rather
    than nothing. ``unreadable`` is what governs the verdict; ``value`` is
    only ever evidence.
    """

    def __init__(self, device_id=None, logger=None, dark=None):
        self.device_id = device_id
        self.logger = logger or logging.getLogger("Plugin")
        self.value = None
        self.read_at = None
        self.unreadable = False
        self.reason = ""
        #: Did the last :meth:`dark` call flip the verdict? The input edge.
        self.changed = False
        self._dark = dark

    # ----------------------------------------------------------- the reading

    @property
    def warn_key(self):
        """The :func:`compare.warn_once` key for "this sensor is unreadable"."""
        return ("lux-unreadable", self.device_id)

    def update(self, value, now: dt.datetime, reason: str = "") -> None:
        """Take one reading. ``None`` means the sensor could not be read.

        ``reason`` is why it could not be read -- "device 302 does not exist",
        "the Indigo lookup failed" -- and goes into the one warning, because
        those two are different problems with different fixes and a message
        that does not distinguish them is the quiet zero R15 forbids.
        """
        number = _as_number(value)
        if number is None:
            self.unreadable = True
            self.reason = reason or (
                "the sensor did not report a number"
                if value is not None
                else "the sensor could not be read"
            )
            compare.warn_once(
                self.logger,
                self.warn_key,
                f"lux sensor {self.device_id} is unreadable: {self.reason}. The "
                "zone is using its configured 'when_unreadable' direction, not a "
                "reading; this is not a lux of zero.",
            )
            return

        if self.unreadable:
            # The condition cleared. Release the key so that the *next*
            # outage warns again instead of being swallowed by the first.
            compare.reset_warnings(self.warn_key)
        self.unreadable = False
        self.reason = ""
        self.value = number
        self.read_at = now

    def stale(self, now: dt.datetime, after_seconds: int = STALE_AFTER_SECONDS) -> bool:
        """Is the last reading old enough that the zone should say so?"""
        if self.read_at is None:
            return True
        return now - self.read_at > dt.timedelta(seconds=after_seconds)

    def age(self, now: dt.datetime):
        """How long ago the last readable value arrived, or None."""
        if self.read_at is None:
            return None
        return now - self.read_at

    # ----------------------------------------------------------- the verdict

    @property
    def verdict(self):
        """The current dark verdict, or None if none has been reached yet."""
        return self._dark

    def seed(self, dark) -> None:
        """Restore a persisted verdict without calling it a flip (R13).

        Used at startup and after a config reload. Seeding matters because
        the Schmitt trigger's memory *is* the persisted state: without it a
        zone that was dark comes back with no verdict, and the first reading
        inside the band lands on the wrong side.
        """
        if isinstance(dark, bool):
            self._dark = dark

    def dark(self, dark_below, hysteresis=0.0, when_unreadable="dark") -> bool:
        """The Schmitt verdict, and whether it just flipped.

        Enter dark below ``dark_below``; leave dark only at or above
        ``dark_below + hysteresis``. Between the two, keep the verdict --
        that gap is the whole trigger, and it is one-sided on purpose: the
        band is there to stop the zone's own lights ending the dark, not to
        delay the dark starting.
        """
        previous = self._dark

        if self.unreadable:
            verdict = when_unreadable == "dark"
        elif self.value is None:
            # Nothing read yet. A verdict restored from the persisted state
            # is real evidence and outranks the configured direction (R13):
            # after a restart the zone knows it was dark five minutes ago,
            # and the direction is for when it knows nothing at all.
            verdict = previous if previous is not None else when_unreadable == "dark"
        elif self.value < dark_below:
            verdict = True
        elif self.value >= dark_below + hysteresis:
            verdict = False
        else:
            # Inside the band: hold. With no verdict yet there is nothing to
            # hold, and the reading is at or above dark_below, so it is not
            # dark -- the band never creates a dark the threshold did not.
            verdict = previous if previous is not None else False

        self.changed = previous is not None and verdict != previous
        self._dark = verdict
        return verdict


def read_sensor_value(device):
    """The lux reading a device carries, or None if it carries none.

    ``sensorValue`` first, because that is what an Indigo sensor device
    publishes and what the schema names; the states dict second, for a plugin
    device that only publishes the state. ``None`` is returned rather than
    guessed at, and the caller turns it into the unreadable path.
    """
    value = _as_number(getattr(device, "sensorValue", None))
    if value is not None:
        return value
    states = getattr(device, "states", None)
    if isinstance(states, dict):
        return _as_number(states.get("sensorValue"))
    return None


def _as_number(value):
    """``value`` as a float, or None if it is not a number at all.

    ``bool`` is excluded: ``isinstance(True, int)`` is true in Python, and an
    on/off state that leaked into a lux field must not read as 1 lux.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
