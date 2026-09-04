"""Presence hold, inside the zone (PRD R4, R13; sections 5.2, 5.4).

A zone owns one :class:`Presence`. Every presence device it has -- a PIR, an
mmWave radar, a door contact, an Occupatum zone device during migration --
feeds the same object, and one of them reporting "on" is presence: any-of,
never all-of, because all-of turns a two-sensor room into a room that is
never occupied.

What the object holds is a single ``last_seen`` timestamp. What it answers is
``now - last_seen < hold``. That is the whole of section 5.4, and it is why
no second plugin is needed to say "still here" and no device needs to tick:
a raw PIR with a 10 s hardware hold and ``hold_seconds: 300`` behaves exactly
like an Occupatum zone with a 300 s off-delay, without the device that
reports every 1.2 seconds.

**The two kinds of edge.** The fork re-planned on every update of a presence
device, and an Occupatum countdown ticking "on, on, on" produced hundreds of
re-plans an hour (R4). The fix is not to ignore repeated "on" readings --
that stops the hold ever being refreshed and empties an occupied room after
``hold_seconds`` -- it is to distinguish two things a repeated reading can be:

* :attr:`Edge.ACTIVATED` -- nothing was reporting and now something is. The
  zone's *state* may change, so the state machine has to run.
* :attr:`Edge.REFRESHED` -- something was already reporting. ``last_seen``
  moves, so the hold expires later and the worker's wake-up has to be
  rescheduled, but no state can have changed and re-planning would be work
  for nothing.
* :attr:`Edge.NONE` -- an "off" report. On its own it changes nothing at all:
  presence ends by the hold expiring, never by a sensor going quiet, which is
  what makes a 10-second PIR usable as a 5-minute hold.

``Edge`` is falsy only for ``NONE``, so ``if presence.update(...)`` reads as
"was this an input edge at all", and ``.is_state_edge`` separates the two
that are.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum


class Edge(Enum):
    """What one presence reading was, to the zone."""

    #: Nothing changed: an "off" report, which is not an input on its own.
    NONE = "none"
    #: ``last_seen`` moved while presence was already being reported. A timer
    #: edge (the hold expires later) but not a state edge.
    REFRESHED = "refreshed"
    #: Nothing was reporting and now something is. A state edge.
    ACTIVATED = "activated"

    def __bool__(self) -> bool:
        """True for any edge at all, so the caller can gate on the result."""
        return self is not Edge.NONE

    @property
    def is_state_edge(self) -> bool:
        """Does this edge need the state machine run, or only a reschedule?"""
        return self is Edge.ACTIVATED


class Presence:
    """One zone's last-seen timestamp and the devices currently reporting.

    ``last_seen`` is the persisted half (R13): restored at startup so that a
    restart does not turn the lights off in an occupied room. ``on_devices``
    is live bookkeeping -- it is what makes ACTIVATED mean "nothing was
    reporting" rather than "the hold had expired", and it is rebuilt from the
    device bus rather than from disk.
    """

    def __init__(self, last_seen=None, on_devices=()):
        self.last_seen = last_seen
        self.on_devices = set(on_devices)

    def update(self, device_id, is_on: bool, now: dt.datetime) -> Edge:
        """Feed one presence reading in; say what kind of edge it was.

        An "off" reading only removes the device from the reporting set. It
        deliberately does not touch ``last_seen`` and deliberately is not an
        edge: presence ends when the hold expires, which the worker already
        has a timer for, not when a sensor drops.
        """
        if not is_on:
            self.on_devices.discard(device_id)
            return Edge.NONE

        was_quiet = not self.on_devices
        self.on_devices.add(device_id)
        self.last_seen = now
        return Edge.ACTIVATED if was_quiet else Edge.REFRESHED

    def active(self, now: dt.datetime, hold_seconds: int) -> bool:
        """Is the zone occupied? ``now - last_seen < hold`` (section 5.2).

        Derived from the timestamp and nothing else -- not from whether a
        device is currently on -- so a sensor that reports once and goes
        quiet still holds the room for the full ``hold_seconds``, and a
        sensor stuck on that stops reporting still lets the room empty.

        ``hold_seconds: 0`` therefore means presence is never active. That is
        the arithmetic the PRD specifies and it is a real configuration
        choice, not an accident: a zone that wants to follow raw device state
        is not what this design does.
        """
        if self.last_seen is None:
            return False
        return now - self.last_seen < dt.timedelta(seconds=hold_seconds)

    def expiry(self, hold_seconds: int):
        """When presence stops being active, or None if it never started.

        This is what the worker's timer heap sleeps until, and what
        unlock-on-leave is judged against (R10).
        """
        if self.last_seen is None:
            return None
        return self.last_seen + dt.timedelta(seconds=hold_seconds)
