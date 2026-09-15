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

``last_seen`` and :attr:`confirmed_vacant_last_seen` are persisted (R13). The
reporting set is rebuilt at startup by
:meth:`lamplighter.engine.Engine._seed_zone` reading the devices themselves,
because who is on *now* is a fact about the room and not about what the
plugin believed when it stopped.

**A period boundary must not revive an expired hold (issue #15).**
``hold_seconds`` can change at a period boundary (a period may carry its own,
overriding the zone's while it is active), and the hold judged is always the
one for the period active *now* -- so a boundary can lengthen or shorten a
hold that is still running. But the same arithmetic applied to a hold that
has already run out re-occupies a room nobody is in: Early's 900 s hold
expires a sighting at 07:42, and at 08:00 Working Day's 3600 s hold, measured
against that same 07:27 sighting, has not "expired" yet by the naive
arithmetic -- so a room that has been empty for eighteen minutes turns its
lights back on. :attr:`confirmed_vacant_last_seen` is the fix: the first time
:meth:`active` finds the hold run out for a given ``last_seen`` (on_devices
empty), it remembers that ``last_seen`` value, and every later call -- at any
hold_seconds -- reads as inactive for that same ``last_seen`` regardless.
Only a fresh edge, which moves ``last_seen`` (see :meth:`update`), or a live
sensor clears it.

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

    def __init__(
        self,
        last_seen=None,
        on_devices=(),
        last_value=None,
        last_input_id=None,
        confirmed_vacant_last_seen=None,
    ):
        self.last_seen = last_seen
        self.on_devices = set(on_devices)
        #: The ``last_seen`` value a real evaluation has already judged the
        #: hold expired for (on_devices empty, and the hold run out) -- see
        #: the module docstring's "A period boundary must not revive an
        #: expired hold". ``None`` means nothing has been confirmed vacant
        #: for the current ``last_seen`` yet. Set only by :meth:`active`,
        #: the mutating read real evaluation calls; :meth:`would_be_active`
        #: reads it without writing, exactly like :class:`~lamplighter.lux.Lux`'s
        #: ``is_dark``/``would_be_dark`` split.
        self.confirmed_vacant_last_seen = confirmed_vacant_last_seen
        #: The last reading ingested for each input, device or variable id ->
        #: bool. Not the same question as ``on_devices``: a device that
        #: reported off is *removed* from ``on_devices`` (it must not linger
        #: there as a stale "on"), but this dict still has to say "off" for
        #: it rather than "never asked" -- which is what the status page's
        #: per-input chips need (PRD section 5.10, "presence_inputs"). Never
        #: persisted: it is rebuilt across a restart the same way
        #: ``on_devices`` is (seeded from the devices), and carried across a
        #: config reload by ``persist.rebuild_zone``.
        self.last_value = dict(last_value or {})
        #: The input id the most recent edge (ACTIVATED or CLEARED) belonged
        #: to, so the status page can point at "the one that last mattered"
        #: without parsing the ``last_trigger`` cause string back into an id.
        self.last_input_id = last_input_id

    def update(self, device_id, is_on: bool, now: dt.datetime) -> Edge:
        """Feed one presence reading in; say what kind of edge it was."""
        self.last_value[device_id] = bool(is_on)

        if is_on:
            was_quiet = not self.on_devices
            self.on_devices.add(device_id)
            self.last_seen = now
            edge = Edge.ACTIVATED if was_quiet else Edge.REFRESHED
            if edge is Edge.ACTIVATED:
                self.last_input_id = device_id
            return edge

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
        self.last_input_id = device_id
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

        This is the **confirming** read: the moment it finds the hold run
        out, it remembers ``last_seen`` in :attr:`confirmed_vacant_last_seen`
        so a later period boundary that lengthens ``hold_seconds`` cannot
        revive presence from that same sighting (issue #15, module
        docstring). Called by a real evaluation only. A caller that must not
        change what the zone will next decide -- a dry run, a status line --
        calls :meth:`would_be_active` instead, exactly as :meth:`Zone.is_dark`
        and :meth:`Zone.would_be_dark` split for the same reason.
        """
        active = self._active(now, hold_seconds)
        if not active and not self.on_devices and self.last_seen is not None:
            self.confirmed_vacant_last_seen = self.last_seen
        return active

    def would_be_active(self, now: dt.datetime, hold_seconds: int) -> bool:
        """What :meth:`active` would answer, without confirming anything.

        Reads :attr:`confirmed_vacant_last_seen` -- so it still honours an
        expired hold a real evaluation already found -- but never sets it,
        which is what lets a dry run ask "would this zone be occupied at
        22:00" without deciding, for the zone, that it is not occupied now.
        """
        return self._active(now, hold_seconds)

    def _active(self, now: dt.datetime, hold_seconds: int) -> bool:
        """The read-only arithmetic shared by :meth:`active` and
        :meth:`would_be_active`."""
        if self.on_devices:
            return True
        if self.last_seen is None:
            return False
        if self.confirmed_vacant_last_seen == self.last_seen:
            # This exact sighting was already judged expired by a real
            # evaluation. Only a fresh edge (a new last_seen) or a live
            # sensor (the on_devices check above) can make the zone occupied
            # again -- recomputing against a boundary-lengthened hold_seconds
            # must not (issue #15).
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
