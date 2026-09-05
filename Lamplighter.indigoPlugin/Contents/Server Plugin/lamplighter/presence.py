"""Presence hold, inside the zone (PRD R4, R13; sections 5.2, 5.4).

A zone owns one :class:`Presence`. Every presence device it has -- a PIR, an
mmWave radar, a door contact, an Occupatum zone device during migration --
feeds the same object, and one of them reporting "on" is presence: any-of,
never all-of, because all-of turns a two-sensor room into a room that is
never occupied.

**The room is occupied while any sensor is on; the hold starts when the last
one clears.** These are Occupatum's semantics, and they are not the same as
"``now - last_seen < hold``", which is what this module used to answer. The
difference is the difference between working and not working for a *level*
sensor. An Aqara FP1 radar reports "on" once when somebody comes into the
room and then says nothing at all until they leave -- that is what a level
sensor is. Under a pure timestamp rule its single "on" ages out after
``hold_seconds`` and the zone turns the lights off with the person sitting
still in the chair, which is exactly what the Study did. An edge sensor (a
PIR) re-reports while there is movement and hides the bug; a radar does not.

So the state lives in two places and both matter:

* :attr:`on_devices` -- who is reporting **right now**. Non-empty means
  occupied, full stop, however long ago the reading arrived.
* :attr:`last_seen` -- when the picture last changed. It is stamped on an
  "on" reading *and on an "off" one*, because the hold is a delay after the
  room clears, not a delay after the last sighting.

Only ``last_seen`` is persisted (R13). The reporting set is rebuilt at
startup by :meth:`lamplighter.engine.Engine._seed_zone` reading the devices
themselves, because who is on *now* is a fact about the room and not about
what the plugin believed when it stopped.

**The kinds of edge.** The fork re-planned on every update of a presence
device, and an Occupatum countdown ticking "on, on, on" produced hundreds of
re-plans an hour (R4). The fix is not to ignore repeated "on" readings --
that stops the hold ever being refreshed -- it is to distinguish what a
reading actually did:

* :attr:`Edge.ACTIVATED` -- nothing was reporting and now something is. The
  zone's *state* may change, so the state machine has to run.
* :attr:`Edge.REFRESHED` -- something was already reporting. Nothing about
  the verdict can have changed, and re-planning would be work for nothing.
* :attr:`Edge.CLEARED` -- a sensor that *was* on has gone off. This is a
  timer edge and it is why "off" is no longer ignored: until it happens
  there is no hold running at all (:meth:`expiry` answers None while
  anything is on), and it is the moment the hold starts. A worker that did
  not hear about it would never schedule the wake-up that empties the room.
* :attr:`Edge.NONE` -- a reading that changed nothing: an "off" from a device
  that was already off, or from a device this zone does not own.

``Edge`` is falsy only for ``NONE``, so ``if presence.update(...)`` reads as
"was this an input edge at all". ``.is_state_edge`` is narrower: it marks the
one edge that can move the state machine *by itself*. CLEARED usually cannot
-- the hold has only just started -- so it is a timer edge, and the state it
eventually causes comes from the wake-up, not from the reading.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum


class Edge(Enum):
    """What one presence reading was, to the zone."""

    #: A reading that changed nothing: "off" from a device already off.
    NONE = "none"
    #: Something else was already reporting. Not a timer edge and not a state
    #: edge: while anything is on there is no hold to move.
    REFRESHED = "refreshed"
    #: Nothing was reporting and now something is. A state edge.
    ACTIVATED = "activated"
    #: A device that was on has cleared. A timer edge: the hold starts here.
    CLEARED = "cleared"

    def __bool__(self) -> bool:
        """True for any edge at all, so the caller can gate on the result."""
        return self is not Edge.NONE

    @property
    def is_state_edge(self) -> bool:
        """Does this edge move the state machine by itself?

        Only ACTIVATED does. CLEARED starts the hold rather than ending it,
        so the room is still occupied when it arrives and the transition it
        leads to belongs to the wake-up ``hold_seconds`` later. (With
        ``hold_seconds: 0`` the two coincide; the state machine is asked
        either way, because the engine runs it on any edge at all.)
        """
        return self is Edge.ACTIVATED


class Presence:
    """One zone's reporting sensors and the moment the picture last changed.

    ``on_devices`` is the primary answer: while it is non-empty the room is
    occupied, no matter how stale the reading, which is what makes a level
    sensor work. ``last_seen`` is what runs the delay once it empties, and is
    the half that is persisted across a restart (R13).
    """

    def __init__(self, last_seen=None, on_devices=()):
        self.last_seen = last_seen
        self.on_devices = set(on_devices)

    def update(self, device_id, is_on: bool, now: dt.datetime) -> Edge:
        """Feed one presence reading in; say what kind of edge it was."""
        if is_on:
            was_quiet = not self.on_devices
            self.on_devices.add(device_id)
            self.last_seen = now
            return Edge.ACTIVATED if was_quiet else Edge.REFRESHED

        if device_id not in self.on_devices:
            # Nothing to clear. Not an edge, and it must not stamp
            # ``last_seen``: a device that reports "off" every thirty seconds
            # would otherwise hold an empty room open for ever.
            return Edge.NONE

        self.on_devices.discard(device_id)
        # Stamped on the way OUT as well as the way in. This is the whole of
        # "the off-delay starts when the last sensor clears": while another
        # sensor is still on the value is not read (see `active`), and when
        # this was the last one it is the instant the hold begins.
        self.last_seen = now
        return Edge.CLEARED

    def active(self, now: dt.datetime, hold_seconds: int) -> bool:
        """Is the zone occupied? Any sensor on, or still inside the hold.

        The first clause is the one a level sensor needs: an FP1 that has
        been on for two hours is two hours of presence, not one reading that
        expired after ``hold_seconds``.

        ``hold_seconds: 0`` now means "no delay after the room clears" rather
        than "never occupied": while a sensor is on the room is occupied
        whatever the hold is, and the instant the last one goes off it is
        not. That is a usable configuration, which the old arithmetic's
        "never active" was not.
        """
        if self.on_devices:
            return True
        if self.last_seen is None:
            return False
        return now - self.last_seen < dt.timedelta(seconds=hold_seconds)

    def expiry(self, hold_seconds: int):
        """When presence stops being active, or None if nothing is running.

        None has two meanings and both are "do not schedule a wake-up for
        this": nothing has ever been seen, or **a sensor is on right now**,
        in which case there is no expiry to schedule -- the hold has not
        started and will not start until the sensor clears. Scheduling one
        anyway is how a level sensor's zone wakes up mid-occupancy and puts
        itself VACANT.

        This is also what unlock-on-leave is judged against (R10).
        """
        if self.on_devices:
            return None
        if self.last_seen is None:
            return None
        return self.last_seen + dt.timedelta(seconds=hold_seconds)
