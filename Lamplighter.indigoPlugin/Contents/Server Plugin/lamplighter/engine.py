"""The process model: callbacks classify, one worker decides (PRD 5.1).

Two threads and a clear division of labour between them.

**The Indigo callback thread** runs :meth:`Engine.device_updated` and
:meth:`Engine.variable_updated`. They do three things and no more: judge a
light's change against the override rule (which has to happen here, on the
event, because the evidence does not survive), update a zone's *inputs*, and
mark the zone dirty. A burst of device events costs a few dictionary updates,
so the callback thread is never the thing that is late -- the fork's Occupatum
zone ticking every 1.2 s put ten seconds of lag between a person moving a
dimmer and the plugin hearing about it.

**The worker thread** runs :meth:`Engine.tick`. It drains the dirty zones,
runs the state machine, reconciles, and then looks at its timers. Nothing else
writes to a light.

The classification is the half of R4 that lives here. A zone re-plans on an
input edge and on nothing else, and an "input edge" is decided by comparing
the *readings* the two snapshots carry -- not by looking at which keys Indigo
put in its diff. Those are different questions: the keys say what the device
chose to report, and a device re-reporting a value it already held names the
key without the reading having moved. The fork gated on the keys once and
re-planned the Kitchen about once a second.

The other half of R4 lives in the timers. A period boundary, a presence hold
expiring, an override expiring and local midnight all move with nothing on the
device bus at all, so each zone publishes its next wake-up and the worker
sleeps until the earliest of them. There is no ``threading.Timer`` per zone,
and there is no settle poll anywhere: PRD section 9 is explicit that when a
device does not confirm, the answer is the next tick.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from . import persist
from .lux import read_sensor_value
from .override import EchoBook, is_manual_override
from .reconcile import Reconciler
from .zone import Zone

#: The device id recorded for an override created by the `lock zone` action,
#: which has no device behind it (section 5.13, decision 2). Deliberately not
#: 0: :mod:`lamplighter.persist` reads 0 as "no override", so a lock taken by
#: a script would not survive a restart.
MANUAL_LOCK_DEVICE_ID = -1

#: The longest the worker sleeps in one go, whatever the timers say. A cap
#: rather than a poll: it bounds how stale a clock change or a missed wake-up
#: can leave a zone, and costs one evaluation a minute per zone that finds
#: nothing to do.
MAX_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class TickSummary:
    """What one worker pass did, for the caller, the tests and the log."""

    transitions: tuple = ()
    commands: tuple = ()
    evaluated: tuple = ()

    def __bool__(self) -> bool:
        return bool(self.transitions or self.commands)


@dataclass(frozen=True)
class Edge:
    """One input edge the callback thread found, and where it came from."""

    zone: str
    cause: str
    kind: str = ""


def presence_reading(device) -> tuple:
    """The part of a presence device the zone actually reads.

    Three values, because on/off reaches this plugin three ways: the
    ``onState`` attribute the IOM documents, ``states["onState"]``, and
    ``states["onOffState"]`` for a plugin device that publishes the state and
    no attribute. Carrying all three makes the reading strictly finer-grained
    than the verdict drawn from it, so comparing two of these tuples can
    produce a redundant edge but can never miss a real one.

    A device with no states mapping is read as an empty one rather than
    raising: an unreadable snapshot must not take the callback thread down.
    """
    states = getattr(device, "states", None) or {}
    return (
        getattr(device, "onState", None),
        states.get("onState"),
        states.get("onOffState"),
    )


def presence_is_on(device) -> bool:
    """Is this presence device reporting? Any of the three readings, any-of.

    Any-of and not all-of, at both levels: across the readings of one device,
    because a device that publishes only ``onOffState`` is still reporting;
    and across the devices of a zone, which :class:`~lamplighter.presence.Presence`
    does, because all-of turns a two-sensor room into a room that is never
    occupied.
    """
    return any(bool(value) for value in presence_reading(device) if value is not None)


class Engine:
    """Every zone, one echo book, one reconciler, one dirty set."""

    def __init__(
        self,
        config,
        sun,
        commander,
        logger=None,
        clock=None,
        on_zone_changed=None,
    ):
        self.config = config
        self.sun = sun
        self.commander = commander
        self.logger = logger or logging.getLogger("Plugin")
        self.clock = clock
        #: Called with a zone after any transition or write, so M2 can persist
        #: its state and publish its Indigo device states without the engine
        #: knowing that Indigo devices exist.
        self.on_zone_changed = on_zone_changed

        self.echo_book = EchoBook()
        self.reconciler = Reconciler(commander, self.echo_book, self.logger)

        self.zones = {
            zone_config.name: Zone(zone_config, sun, clock=clock, logger=self.logger)
            for zone_config in config.zones
        }
        self.plugin_enabled = True

        #: Zone name -> the cause that made it dirty. A dict, so a burst of
        #: events on one zone is one evaluation, and the FIRST cause is kept:
        #: it is the one that actually moved something (R14).
        self._dirty: dict = {}
        #: Zone name -> when the worker should next think about it with no
        #: event at all. This is the timer heap of section 5.1, flat because
        #: six zones do not need a heap.
        self._wakes: dict = {}
        self._next_reconcile = None

    def __repr__(self):
        return f"<Engine {len(self.zones)} zones>"

    def now(self) -> dt.datetime:
        return self.clock() if self.clock is not None else dt.datetime.now()

    # ------------------------------------------------------ the callback thread

    def device_updated(self, previous_dev, current_dev, now: dt.datetime) -> list:
        """Classify one ``deviceUpdated`` event. Returns the edges it found.

        Called on the Indigo callback thread for every device in the server,
        so the first thing it does for a device no zone uses is nothing.
        """
        device_id = getattr(current_dev, "id", None)
        if device_id is None:
            return []

        edges = []
        for zone in self.zones.values():
            config = zone.config
            if device_id in config.lights:
                edge = self._light_changed(zone, previous_dev, current_dev, now)
                if edge is not None:
                    edges.append(edge)
            if device_id in config.presence_devices:
                edge = self._presence_changed(zone, previous_dev, current_dev, now)
                if edge is not None:
                    edges.append(edge)
            if config.lux is not None and device_id == config.lux.device:
                edge = self._lux_changed(zone, previous_dev, current_dev, now)
                if edge is not None:
                    edges.append(edge)
        return edges

    def _light_changed(self, zone, previous_dev, current_dev, now):
        """The override rule, on the event, before anything can revert it (R1)."""
        if not is_manual_override(
            zone,
            previous_dev,
            current_dev,
            now,
            self.echo_book,
            self.config.echo_window_seconds,
            self.logger,
        ):
            return None
        device_id = current_dev.id
        if zone.start_override(device_id, now) is None:
            # A never-lock zone noticed the change and keeps its levels.
            return None
        name = getattr(current_dev, "name", None) or f"device {device_id}"
        return self._mark_dirty(zone, f"override: {name}", kind="override")

    def _presence_changed(self, zone, previous_dev, current_dev, now):
        """Presence, gated on the reading rather than on the event (R4)."""
        device_id = current_dev.id
        if previous_dev is not None and presence_reading(previous_dev) == presence_reading(
            current_dev
        ):
            # The Occupatum countdown tick, the display string, the battery
            # level. Nothing the zone reads has moved.
            self.logger.debug(
                f"{zone.name}: update from device {device_id} carried no change to "
                "its presence reading; not an input edge"
            )
            return None
        # No before-state means there is nothing to compare, and suppressing a
        # re-plan on a comparison that could not be made is a lights-never-
        # respond failure. The gate does not apply rather than guessing.
        if not zone.ingest_presence(device_id, presence_is_on(current_dev), now):
            return None
        name = getattr(current_dev, "name", None) or f"device {device_id}"
        return self._mark_dirty(zone, f"presence: {name}", kind="presence")

    def _lux_changed(self, zone, previous_dev, current_dev, now):
        """Lux, gated on the reading and then on the verdict (R9)."""
        device_id = current_dev.id
        reading = read_sensor_value(current_dev)
        if previous_dev is not None and read_sensor_value(previous_dev) == reading:
            self.logger.debug(
                f"{zone.name}: update from device {device_id} carried no change to "
                "its lux reading; not an input edge"
            )
            return None
        flipped = zone.ingest_lux(
            reading,
            now,
            reason="" if reading is not None else "the sensor reported no readable value",
        )
        if not flipped:
            # The sensor moved and the verdict did not. This is the common
            # case for a sensor that reports every few seconds, and it is why
            # a lux device does not re-plan a zone the way it did in the fork.
            return None
        return self._mark_dirty(zone, f"lux {'dark' if zone.lux.verdict else 'bright'}", kind="lux")

    def variable_updated(self, var_id, now: dt.datetime) -> list:
        """An Indigo variable changed; re-read the zones that gate on it.

        Only ``dark_below_variable_id`` is watched. Re-reading is cheap and
        the verdict is what matters: a threshold nudged from 2200 to 2210
        moves no zone, and only a flip is an input edge.
        """
        edges = []
        for zone in self.zones.values():
            config = zone.config
            if config.lux is None or config.lux.dark_below_variable_id != var_id:
                continue
            zone.is_dark()
            if zone.lux.changed:
                edges.append(
                    self._mark_dirty(
                        zone,
                        f"dark_below variable {var_id} changed the verdict to "
                        f"{'dark' if zone.lux.verdict else 'bright'}",
                        kind="variable",
                    )
                )
        return edges

    # --------------------------------------------------------- the worker thread

    def tick(self, now: dt.datetime) -> TickSummary:
        """One worker pass: dirty zones, then timers, then the reconcile tick."""
        if self._next_reconcile is None:
            self._next_reconcile = now + dt.timedelta(seconds=self.config.reconcile_seconds)

        transitions, commands, handled = [], [], []

        dirty, self._dirty = self._dirty, {}
        for name, cause in dirty.items():
            zone = self.zones.get(name)
            if zone is None:
                continue  # the zone went away in a reload between mark and drain
            self._run_zone(zone, now, cause, transitions, commands, handled)

        for zone in self.zones.values():
            if zone.name in handled:
                continue
            due = self._wakes.get(zone.name)
            if due is not None and due <= now:
                self._run_zone(
                    zone, now, self._wake_cause(zone, now), transitions, commands, handled
                )

        if now >= self._next_reconcile:
            # The periodic pass of section 5.8: the answer to a device that
            # did not land, in place of the settle poll, the confirm thread
            # and the retry machinery the fork needed.
            self._next_reconcile = now + dt.timedelta(seconds=self.config.reconcile_seconds)
            for zone in self.zones.values():
                if zone.name in handled or not zone.running:
                    continue
                sent = self.reconciler.run(zone, now)
                handled.append(zone.name)
                if sent:
                    commands.extend(sent)
                    self._notify(zone)

        return TickSummary(
            transitions=tuple(transitions),
            commands=tuple(commands),
            evaluated=tuple(handled),
        )

    def _run_zone(self, zone, now, cause, transitions, commands, handled):
        transition = zone.evaluate(now, cause)
        sent = self.reconciler.run(zone, now) if zone.running else []
        self._wakes[zone.name] = zone.next_wake(now)
        handled.append(zone.name)
        if transition is not None:
            transitions.append(transition)
        if sent:
            commands.extend(sent)
        if transition is not None or sent:
            self._notify(zone)

    def _wake_cause(self, zone, now) -> str:
        """Which timer just fired, named for the log line (R14).

        Asked at the moment of waking rather than recorded when the wake was
        scheduled, because an override can be extended and a hold refreshed
        in between, and a cause that says why the zone was *going* to wake is
        not the cause it woke for.
        """
        override = zone.override
        if override is not None and override.expires_at <= now:
            return "override expiry"
        hold_expiry = zone.presence.expiry(zone.config.hold_seconds)
        if hold_expiry is not None and hold_expiry <= now:
            return "presence hold expired"
        period = zone.active_period(now)
        if (period.name if period is not None else None) != zone.inputs(now).get("period"):
            return "period boundary"
        return "midnight"

    def next_wake(self, now: dt.datetime):
        """When the worker should wake, or ``now`` if there is work waiting."""
        if self._dirty:
            return now
        candidates = [when for when in self._wakes.values() if when is not None]
        if self._next_reconcile is not None:
            candidates.append(self._next_reconcile)
        return min(candidates) if candidates else None

    def run_forever(self, stop_event, sleep_fn) -> None:
        """The plugin's ``runConcurrentThread`` loop.

        ``sleep_fn`` is injected and ``stop_event`` is Indigo's stop signal,
        so the loop itself has nothing in it that a test would have to wait
        for. Tests drive :meth:`tick` directly and never come through here.
        """
        while not stop_event.is_set():
            now = self.now()
            try:
                self.tick(now)
            except Exception:
                # One zone's bad pass must not take the worker down; the next
                # tick is a second chance and the traceback says what broke.
                self.logger.exception("Lamplighter: the worker pass raised")
            wake = self.next_wake(self.now())
            delay = MAX_SLEEP_SECONDS
            if wake is not None:
                delay = max(0.0, (wake - self.now()).total_seconds())
            sleep_fn(min(delay, MAX_SLEEP_SECONDS))

    # --------------------------------------------------------------- the actions

    def lock_zone(self, zone_name, now: dt.datetime):
        """Create an override with no device change behind it (section 5.13).

        What scripts wanted from the fork and could not have: a lock taken
        from an app or an action group, without moving a light and without
        depending on a write landing to be noticed.
        """
        zone = self.zones[zone_name]
        created = zone.start_override(MANUAL_LOCK_DEVICE_ID, now)
        if created is None:
            return None
        self._mark_dirty(zone, "lock zone action", kind="override")
        return created

    def reset_override(self, zone_name=None, now=None):
        """Release one zone's override, or every zone's. Returns the names."""
        now = now or self.now()
        released = []
        targets = self.zones.values() if zone_name is None else [self.zones[zone_name]]
        for zone in targets:
            if zone.end_override("the reset override action ran", now) is not None:
                released.append(zone.name)
                self._mark_dirty(zone, "reset override action", kind="override")
        return released

    def set_zone_enabled(self, zone_name, enabled) -> bool:
        """Enable or disable one zone. True if it changed."""
        zone = self.zones[zone_name]
        if not zone.set_enabled(enabled=enabled):
            return False
        self._mark_dirty(zone, f"zone {'enabled' if enabled else 'disabled'}", kind="enable")
        return True

    def set_plugin_enabled(self, enabled) -> bool:
        """The controller device's global enable (section 11, decision 1)."""
        changed = False
        for zone in self.zones.values():
            if zone.set_enabled(plugin_enabled=enabled):
                changed = True
                self._mark_dirty(
                    zone,
                    f"plugin {'enabled' if enabled else 'disabled'}",
                    kind="enable",
                )
        self.plugin_enabled = bool(enabled)
        return changed

    def mark_all_dirty(self, cause: str) -> None:
        """Every zone re-plans on the next tick. Startup, and reload."""
        for zone in self.zones.values():
            self._mark_dirty(zone, cause, kind="all")

    # --------------------------------------------------------------- the reload

    def reload(self, new_config, now: dt.datetime) -> None:
        """Swap in a new configuration, carrying the house's state across.

        The zone objects are rebuilt from the file and the persisted state is
        put back on top, which is what stops an unrelated edit at 19:50
        throwing away an override taken at 19:46 (R13). What does *not* carry
        is anything that is a fact about the file: a zone switched off in the
        new configuration is off.
        """
        rebuilt = {}
        for zone_config in new_config.zones:
            existing = self.zones.get(zone_config.name)
            if existing is None:
                rebuilt[zone_config.name] = Zone(
                    zone_config, self.sun, clock=self.clock, logger=self.logger
                )
            else:
                rebuilt[zone_config.name] = persist.rebuild_zone(existing, zone_config, now)

        for departed in set(self.zones) - set(rebuilt):
            self._wakes.pop(departed, None)
            self._dirty.pop(departed, None)
            self.logger.info(f"Lamplighter: zone {departed!r} is no longer configured")

        self.zones = rebuilt
        self.config = new_config

        # Backoff and echo records are facts about the hardware, not about the
        # file, so they survive -- but only for devices some zone still owns,
        # or a long-lived plugin accumulates a record per device ever removed.
        still_ours = {dev_id for zone in self.zones.values() for dev_id in zone.config.lights}
        for dev_id in list(self.reconciler._backoff):
            if dev_id not in still_ours:
                self.reconciler.forget(dev_id)
                self.echo_book.forget(dev_id)

        self.mark_all_dirty("configuration reloaded")

    # ---------------------------------------------------------------- the report

    def snapshot(self) -> dict:
        """Every zone's Indigo device states, by zone name (section 5.10)."""
        return {name: zone.snapshot() for name, zone in self.zones.items()}

    def explain(self, zone_name=None, now=None) -> dict:
        """One line per zone saying why it is doing what it is doing."""
        now = now or self.now()
        targets = self.zones.values() if zone_name is None else [self.zones[zone_name]]
        return {zone.name: zone.explain(now) for zone in targets}

    def persisted(self) -> dict:
        """Every zone's surviving state, ready for the zone devices (R13)."""
        return {name: persist.to_persisted(zone) for name, zone in self.zones.items()}

    def restore(self, records: dict, now: dt.datetime) -> dict:
        """Put persisted state back onto the zones at startup. The complaints."""
        complaints = {}
        for name, record in (records or {}).items():
            zone = self.zones.get(name)
            if zone is None:
                continue
            said = persist.apply_persisted(zone, record, now, logger=self.logger)
            if said:
                complaints[name] = said
        return complaints

    # ----------------------------------------------------------------- internals

    def _mark_dirty(self, zone, cause: str, kind: str = "") -> Edge:
        """Mark a zone for the next worker pass. The first cause wins.

        First and not last, because a burst is one evaluation and the cause
        that is worth logging is the one that actually moved an input, not
        whichever event happened to arrive last before the worker woke up.
        """
        if zone.name not in self._dirty:
            self._dirty[zone.name] = cause
        return Edge(zone=zone.name, cause=cause, kind=kind)

    def _notify(self, zone) -> None:
        if self.on_zone_changed is None:
            return
        try:
            self.on_zone_changed(zone)
        except Exception:
            # The callback publishes device states and persists; it must not
            # be able to stop the engine reconciling the next zone.
            self.logger.exception(
                f"{zone.name}: the zone-changed callback raised; the engine "
                "carries on, but this zone's device states and persisted "
                "state may be behind"
            )

    @property
    def dirty(self) -> dict:
        """The zones waiting for the worker, with their causes. Read-only."""
        return dict(self._dirty)

    @property
    def wakes(self) -> dict:
        """Each zone's scheduled wake-up. Read-only."""
        return dict(self._wakes)
