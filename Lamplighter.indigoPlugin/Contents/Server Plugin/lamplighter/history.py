"""What actually happened, per zone, for the last 48 hours (PRD section 12).

The status page's day strip showed only the *schedule* -- green for
on_and_off periods, hatching for off_only -- and its user read the green as
"the lights are on", which is not what it meant. This module is the fix: a
bounded, timestamped log of what the engine actually did, per zone, kept
just long enough to draw "today" with a little run-up before midnight for
lights that were already on.

**Six kinds of event, plus one marker**, one dict each, always with ``t``
(an ISO local timestamp, seconds) and ``k`` (the kind):

* ``state``   -- a zone transition (:class:`lamplighter.zone.Transition`).
* ``presence``-- a presence input edge, device or variable.
* ``light``   -- a light's ACTUAL reported level (not what the zone asked
  for; that is ``write``, below).
* ``write``   -- a command the reconciler actually sent.
* ``override``-- an override starting or ending.
* ``offduty`` -- a transition landing in OFF_DUTY, with why.
* ``gap``     -- not a real event: a marker for "history was truncated
  here", so a long run of activity reads as a gap rather than as a silent
  hole a reader would mistake for nothing having happened (R15).

**Retention is two limits, not one.** Time (:data:`RETENTION_HOURS`) is the
one that matters day to day -- nobody needs Tuesday's history. The count
(:data:`MAX_EVENTS`) is the backstop for a zone that is misbehaving and
producing far more events than 48 hours' worth ever should; if it is ever
the count that trims rather than the age, a ``gap`` marker is written so the
page can say so instead of drawing a smooth line through a hole.

**Thread safety.** Recording runs on Indigo callback threads (via
:mod:`lamplighter.engine`); serialising for disk runs on the plugin's
worker thread (via :meth:`History.snapshot_and_clear`). :class:`History`
owns one lock across every mutation and every read of ``zones`` -- append,
trim, forget and snapshot alike -- so a snapshot mid-append can never see a
zone's event list half-built, and an append mid-snapshot can never be lost
(it simply waits its turn and lands in the *next* snapshot instead).

**This module knows nothing about Indigo, the engine or the plugin.** It is
handed timestamps and primitive values and it hands back JSON; the wiring
that decides *when* to call it lives in :mod:`lamplighter.engine` (which
event is worth recording) and in ``plugin.py`` (when to write the file and
where).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading

#: How long an event survives, from "now" at the moment it is trimmed.
RETENTION_HOURS = 48

#: The hard cap on events kept per zone, regardless of age. A backstop, not
#: the normal way history shrinks -- see the module docstring.
MAX_EVENTS = 5000

#: The record's own version. Bumped only when the event or envelope shape
#: changes in a way an old reader could misinterpret.
VERSION = 1

_ISO = "%Y-%m-%dT%H:%M:%S"


def _iso(when: dt.datetime) -> str:
    return when.strftime(_ISO)


def _parse_iso(text):
    """A timestamp from a stored event, or None if it will not parse.

    Never raises: a garbled timestamp on one event must not take the whole
    file down (R15). An event that cannot be timed is dropped rather than
    kept with a guessed time -- see :meth:`ZoneHistory._trim`, which treats
    a `None` here exactly like a stale one: dropped, with the scan
    continuing past it rather than stopping there. Local-time DST fall-back
    can also make one hour of real timestamps read as non-monotonic; the
    scan tolerates that because it only ever drops a stale PREFIX -- the
    worst a little disorder there can do is leave one borderline event
    trimmed a tick early or late, never corrupt the list.
    """
    if not isinstance(text, str):
        return None
    try:
        return dt.datetime.strptime(text, _ISO)
    except ValueError:
        return None


class ZoneHistory:
    """One zone's bounded, time-ordered event list."""

    def __init__(self):
        self.events: list = []
        #: device_id -> the last `light` level recorded for it, so a device
        #: reporting the same level twice in a row (the common case -- most
        #: reports are a link-quality update, not a level change) does not
        #: fill the lane with noise. Rebuilt from `events` after a load.
        self._last_light_level: dict = {}

    def append(self, now: dt.datetime, event_kind: str, **fields) -> None:
        # `event_kind`, not `kind`: a `presence` event's own field is named
        # `kind` ("device" vs "variable"), and a positional/kwarg name
        # collision there is a TypeError, not a subtle bug -- caught once and
        # renamed rather than caught later as "presence events never save".
        event = {"t": _iso(now), "k": event_kind}
        event.update(fields)
        self.events.append(event)
        self._trim(now)

    def append_light(self, now: dt.datetime, device_id, level: int) -> bool:
        """Record a light's reported level. False means it was a duplicate.

        The dedupe is against the last level THIS device recorded, not
        against the whole event stream -- five lights reporting in the same
        second must all get an event, and only a device repeating its own
        last reading is the noise this exists to drop.
        """
        if self._last_light_level.get(device_id) == level:
            return False
        self._last_light_level[device_id] = level
        self.append(now, "light", id=device_id, level=level)
        return True

    def _trim(self, now: dt.datetime) -> None:
        """Age out, then cap, dropping oldest first. Called on every append.

        Age first: it is the rule that matters day to day and the one a
        reader expects ("48 hours"). The count is a backstop for a zone
        producing far more events than that ever should, and only IT gets a
        `gap` marker -- age-trimming a quiet Tuesday needs no explanation,
        losing events to a firehose does.
        """
        # Scans only the stale PREFIX rather than re-parsing every event's
        # timestamp on every append -- events are appended in non-decreasing
        # clock order, so once one is not stale, none after it are either.
        # Re-parsing the whole list on every append (the obvious way to
        # write this) is O(n) *strptime calls* per append, and a device with
        # a firehose of reports made appending itself the dominant cost.
        cutoff = now - dt.timedelta(hours=RETENTION_HOURS)
        stale = 0
        for event in self.events:
            parsed = _parse_iso(event.get("t"))
            if parsed is None:
                # Cannot be timed at all, so it cannot be shown to be
                # fresh either -- dropped as part of the stale prefix, and
                # the scan carries on past it rather than stopping here
                # (which would let every real stale event behind it hide
                # forever behind one garbled timestamp).
                stale += 1
                continue
            if parsed >= cutoff:
                break
            stale += 1
        if stale:
            self.events = self.events[stale:]
        overflow = len(self.events) - MAX_EVENTS
        if overflow > 0:
            self.events = self.events[overflow:]
            if not (self.events and self.events[0].get("k") == "gap"):
                # Stamped with the OLDEST SURVIVING event's own timestamp,
                # not `now`: the gap sits at index 0, and if it carried the
                # (later) append time instead, it would read as fresher
                # than the real event right behind it -- breaking the
                # non-decreasing order this same scan depends on to stop
                # early, which would let a genuinely stale event hide
                # indefinitely behind a gap marker that never itself goes
                # stale. A later overflow that removes this gap along with
                # the prefix ahead of it re-inserts a fresh one, correctly
                # re-stamped against whatever is oldest at that point.
                gap_t = self.events[0]["t"] if self.events else _iso(now)
                self.events.insert(0, {"t": gap_t, "k": "gap"})

    def rebuild_last_levels(self) -> None:
        """Reconstruct the dedupe table from loaded events (after :func:`load`).

        Without this, a restart forgets every device's last level and the
        very next report -- however unchanged -- is written again, which is
        harmless for the page but doubles up the first data point after
        every restart for no reason.
        """
        self._last_light_level = {}
        for event in self.events:
            if event.get("k") == "light" and "id" in event:
                self._last_light_level[event["id"]] = event.get("level")


class History:
    """Every zone's :class:`ZoneHistory`, and the JSON envelope around them."""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("Plugin")
        self.zones: dict = {}
        #: True once something has been recorded since the last successful
        #: write. The plugin's periodic flush reads this rather than writing
        #: unconditionally every HISTORY_WRITE_INTERVAL_SECONDS -- a quiet
        #: house at 3 a.m. costs nothing.
        self.dirty = False
        #: Recording runs on Indigo callback threads; serialising for disk
        #: runs on the worker thread. Every mutation and every read of
        #: `zones` used to build a snapshot goes through this lock, so the
        #: two can never interleave inside one zone's event list.
        self._lock = threading.Lock()

    def _zone(self, zone_name: str) -> ZoneHistory:
        """Look up or create one zone's history. Caller must hold `_lock`."""
        zone_history = self.zones.get(zone_name)
        if zone_history is None:
            zone_history = ZoneHistory()
            self.zones[zone_name] = zone_history
        return zone_history

    # ------------------------------------------------------------- recording

    def record_state(self, zone_name, now, to_state, from_state, cause) -> None:
        with self._lock:
            self._zone(zone_name).append(now, "state", to=to_state, **from_field(from_state), cause=cause)
            self.dirty = True

    def record_presence(self, zone_name, now, input_id, kind, on) -> None:
        with self._lock:
            self._zone(zone_name).append(now, "presence", id=input_id, kind=kind, on=bool(on))
            self.dirty = True

    def record_light(self, zone_name, now, device_id, level) -> None:
        with self._lock:
            if self._zone(zone_name).append_light(now, device_id, level):
                self.dirty = True

    def record_write(self, zone_name, now, device_id, level) -> None:
        with self._lock:
            self._zone(zone_name).append(now, "write", id=device_id, level=level)
            self.dirty = True

    def record_override(self, zone_name, now, phase, device, reason) -> None:
        with self._lock:
            self._zone(zone_name).append(now, "override", phase=phase, device=device, reason=reason)
            self.dirty = True

    def record_offduty(self, zone_name, now, cause) -> None:
        with self._lock:
            self._zone(zone_name).append(now, "offduty", cause=cause)
            self.dirty = True

    def forget(self, zone_name: str) -> None:
        """Drop one zone's history entirely -- it left the configuration.

        Called from the engine's reload for zones no longer configured;
        without this a departed zone's history sits in memory (and in the
        written file) forever. Dropping the whole `ZoneHistory` drops its
        `_last_light_level` dedupe table along with it, so a device later
        reused by a different zone starts fresh rather than inheriting a
        stale last-level from a zone that no longer exists.
        """
        with self._lock:
            if self.zones.pop(zone_name, None) is not None:
                self.dirty = True

    # ---------------------------------------------------------- (de)serialise

    def _to_json_locked(self, now: dt.datetime) -> str:
        """Build the envelope. Caller must hold `_lock`."""
        return json.dumps(
            {
                "version": VERSION,
                "generated_at": _iso(now),
                "retention_hours": RETENTION_HOURS,
                "zones": {
                    name: {"events": list(zone_history.events)}
                    for name, zone_history in list(self.zones.items())
                },
            }
        )

    def to_json(self, now=None) -> str:
        """The whole store as JSON text, ready to write to disk."""
        now = now or dt.datetime.now()
        with self._lock:
            return self._to_json_locked(now)

    def snapshot_and_clear(self, now=None) -> str:
        """Serialise the whole store and clear `dirty`, in one lock hold.

        Used by the plugin's periodic flush in place of `to_json()` plus a
        separate `dirty = False`: doing both under the SAME lock acquisition
        is what stops an append landing between the serialise and the clear
        from being silently marked "already written" when it was not. If
        the write that follows this call fails, the caller puts `dirty`
        back to True itself -- this method has no way to know the write
        succeeded, only that it captured everything recorded up to `now`.
        """
        now = now or dt.datetime.now()
        with self._lock:
            text = self._to_json_locked(now)
            self.dirty = False
        return text

    def load(self, text: str, now=None) -> None:
        """Replace the in-memory store with ``text``, or start empty.

        Never raises (R15): malformed JSON, a wrong or missing version, or a
        shape this reader does not recognise are each warned once and leave
        the store exactly as it was before the call -- empty, at startup,
        which is the only time this is called.
        """
        now = now or dt.datetime.now()
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            self.logger.warning(
                f"Lamplighter: the history file could not be parsed ({exc}); "
                "starting today's history empty rather than guessing at what it held"
            )
            return

        if not isinstance(data, dict):
            self.logger.warning(
                f"Lamplighter: the history file is a {type(data).__name__}, not an "
                "object; starting today's history empty"
            )
            return

        version = data.get("version")
        if version != VERSION:
            self.logger.warning(
                f"Lamplighter: the history file is version {version!r}, and this "
                f"plugin writes version {VERSION}; starting today's history empty "
                "rather than guessing at a shape it does not recognise"
            )
            return

        zones = data.get("zones")
        if not isinstance(zones, dict):
            self.logger.warning(
                "Lamplighter: the history file has no readable zones object; "
                "starting today's history empty"
            )
            return

        loaded = {}
        rejected = 0
        for name, zone_data in zones.items():
            events = zone_data.get("events") if isinstance(zone_data, dict) else None
            if not isinstance(events, list):
                continue
            zone_history = ZoneHistory()
            kept = []
            for event in events:
                if not _event_looks_valid(event):
                    rejected += 1
                    continue
                kept.append(event)
            zone_history.events = kept
            zone_history._trim(now)
            zone_history.rebuild_last_levels()
            loaded[name] = zone_history
        with self._lock:
            self.zones = loaded
        if rejected:
            self.logger.warning(
                f"Lamplighter: {rejected} event(s) in the history file were "
                "malformed and dropped; the rest of the file loaded normally"
            )


def _event_looks_valid(event) -> bool:
    """Is this loaded event shaped closely enough to trust (R15)?

    Beyond `t`/`k` (already required to be a str), an `id` -- when present
    -- must be hashable (an int or a str: a device id or a manual-lock
    sentinel), never a list or dict a corrupted file could contain. A
    `level` -- when present -- must be one of the types a real event can
    ever carry: an int/float (a `light` event's reported brightness, or a
    dimmer `write`), a str (`"on"`/`"off"`), or a bool (a relay `write`) --
    never a list or dict either. Anything else is dropped rather than
    carried forward to confuse the page's parser or crash a dict lookup
    keyed on it.
    """
    if not isinstance(event, dict):
        return False
    if not isinstance(event.get("t"), str) or "k" not in event:
        return False
    if "id" in event and not isinstance(event["id"], (int, str)):
        return False
    if "level" in event and not isinstance(event["level"], (int, float, str, bool)):
        return False
    return True


def from_field(from_state) -> dict:
    """`{"from": ...}` only when there is one -- the seam a state event and
    an "it just started here" event would otherwise need two shapes for.
    (The `None` branch below is reserved for "no prior state" -- a fresh
    zone's very first transition -- and is the only caller that passes it;
    every other caller has a real prior state.)"""
    return {} if from_state is None else {"from": from_state}
