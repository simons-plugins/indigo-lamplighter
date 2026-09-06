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

from . import devices, persist
from .lux import read_sensor_value
from .override import EchoBook, is_manual_override
from .reconcile import COMMAND_RECHECK_SECONDS, Reconciler
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


def light_reported(previous_dev, current_dev) -> bool:
    """Whether this update carries any change at all in what the device says.

    Not just the brightness: a light coming back to power often reports the
    same level it went dark at, and the only thing that moves is a link
    quality or last-seen state. Any difference between the two snapshots'
    states counts; none, or no before-snapshot to compare with, does not.
    """
    if previous_dev is None:
        return False
    try:
        return dict(previous_dev.states) != dict(current_dev.states)
    except (AttributeError, TypeError):
        return False


#: The true-ish words a presence variable's value is compared against,
#: lower-cased and stripped (PRD section 5.4). Anything else, including an
#: empty string, reads as OFF.
_PRESENCE_TRUE_WORDS = {"true", "on", "yes", "1", "home"}


def variable_is_on(value) -> bool:
    """Is this Indigo variable value a presence-on reading?

    Indigo variable values are strings, and the house's phone-presence
    variable (``SimonHome``) is set to the words "true"/"false", not to an
    on/off state, so this is a word list rather than a boolean cast. Any
    value that is not one of these words -- including an empty one -- is
    OFF, never an error: a presence variable degrades to "nobody here", the
    safe direction for a light.
    """
    return str(value).strip().lower() in _PRESENCE_TRUE_WORDS


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
        #: Zones that have never read the CURRENT state of their input
        #: devices. A zone only learns presence and lux from later device
        #: *edges*, so one that has just been built, reloaded or enabled
        #: knows nothing about the room it is in -- and "nothing" reads
        #: exactly like "empty and bright". Seeded before the first
        #: evaluation, and retried until the devices answer.
        self._unseeded: set = set(self.zones)

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
        device_id = current_dev.id
        if self.reconciler.is_parked(device_id) and light_reported(previous_dev, current_dev):
            # A parked device (reconcile.py) is only retried on the wall
            # clock; a report from it that changes anything it says, however
            # it reads, is evidence it is alive again. Drop its ladder and
            # bring the zone's wake forward to now, independently of whatever
            # the override rule below makes of the same event. The gate
            # matters: Indigo also delivers this callback for updates that
            # change nothing -- the echo of the plugin's own retry among them
            # -- and un-parking on those would put the device straight back
            # on the ladder it was parked to escape.
            self.reconciler.forget(device_id)
            self._wakes[zone.name] = now
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

        Two independent things watch a variable: ``dark_below_variable_id``
        (one per zone's lux gate) and ``presence_variables`` (any number per
        zone). Re-reading is cheap and the verdict is what matters -- a
        threshold nudged from 2200 to 2210, or a value re-set to the same
        true-ish word, moves no zone, and only a flip is an input edge.
        """
        edges = []
        for zone in self.zones.values():
            config = zone.config
            if config.lux is not None and config.lux.dark_below_variable_id == var_id:
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
            if var_id in config.presence_variables:
                edge = self._presence_variable_changed(zone, var_id, now)
                if edge is not None:
                    edges.append(edge)
        return edges

    def _presence_variable_changed(self, zone, var_id, now):
        """A presence variable, gated on the reading exactly as devices are (R4).

        A variable event carries no before/after snapshot the way a device
        event does -- Indigo hands back only the id -- so "the previous
        reading" is read off the zone's own presence state instead: whether
        this id was already counted as on. That answers the same question a
        snapshot comparison would for a device, so a variable re-set to the
        same true-ish value is not an edge here either, and it needs no extra
        state of its own.

        A variable that cannot be looked up is never allowed to raise on the
        callback thread. Gone is warned once and read as off -- the safe
        direction for a light. A lookup that merely *failed* teaches nothing,
        so the event is skipped rather than guessed at (R15); the zone keeps
        whatever it last believed.
        """
        try:
            variable = devices.get_variable(var_id)
        except devices.DeviceGone:
            devices.warn_gone_once(self.logger, var_id, zone.name, kind="variable")
            is_on, label = False, f"variable {var_id}"
        except devices.LookupFailed as exc:
            devices.warn_lookup_failed_once(self.logger, var_id, zone.name, exc.cause, kind="variable")
            return None
        else:
            devices.forget_warnings(var_id, kind="variable")
            is_on = variable_is_on(getattr(variable, "value", None))
            label = getattr(variable, "name", None) or f"variable {var_id}"

        was_on = var_id in zone.presence.on_devices
        if was_on == is_on:
            return None
        if not zone.ingest_presence(var_id, is_on, now):
            return None
        return self._mark_dirty(zone, f"presence: variable {label}", kind="presence")

    # --------------------------------------------------------- the worker thread

    def tick(self, now: dt.datetime) -> TickSummary:
        """One worker pass: dirty zones, then timers, then the reconcile tick."""
        if self._next_reconcile is None:
            self._next_reconcile = now + dt.timedelta(seconds=self.config.reconcile_seconds)

        # Before anything is decided. A zone that has not read its inputs has
        # no evidence at all, and no evidence looks identical to "empty and
        # bright" -- which is how an occupied Hallway gets its lamp switched
        # off four seconds after a configuration reload.
        self.seed_inputs(now)

        transitions, commands, handled = [], [], []

        dirty, self._dirty = self._dirty, {}
        for name, cause in dirty.items():
            zone = self.zones.get(name)
            if zone is None:
                continue  # the zone went away in a reload between mark and drain
            if name in self._unseeded:
                # Its devices did not answer this pass. Keeping the cause and
                # waiting is the only honest option: evaluating now would
                # decide from a gap in the evidence, and the decision it
                # would reach is "vacant, turn the lights off".
                self._dirty.setdefault(name, cause)
                continue
            self._run_zone(zone, now, cause, transitions, commands, handled)

        for zone in self.zones.values():
            if zone.name in handled or zone.name in self._unseeded:
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
                if zone.name in handled or zone.name in self._unseeded or not zone.running:
                    continue
                sent = self.reconciler.run(zone, now)
                handled.append(zone.name)
                if sent:
                    commands.extend(sent)
                    self._notify(zone)
                    # Only ever brings the wake forward, so the periodic pass
                    # gets the same re-check as an event-driven one.
                    self._schedule_wake(zone, now, sent)

        return TickSummary(
            transitions=tuple(transitions),
            commands=tuple(commands),
            evaluated=tuple(handled),
        )

    # ---------------------------------------------------------------- seeding

    def seed_inputs(self, now: dt.datetime) -> tuple:
        """Read the current state of the input devices of unseeded zones.

        A zone learns presence and lux from device *edges*, which is right for
        a running plugin and wrong for one that has just started: at the
        moment a zone is built, reloaded or switched on, nothing has happened
        on the device bus yet, so the zone believes nobody has ever been in
        the room and the lux sensor has never been read. Both of those read
        as "go off duty and turn the lights off", and on the first run on
        jarvis that is exactly what the Hallway did to an occupied room.

        So the zone asks. Presence devices that are on now are ingested as
        seen *now* -- an occupied room at startup is occupied now, and the
        hold should run from now rather than from a timestamp nobody has -- 
        and the lux sensor is read so the first verdict comes from a reading
        instead of a default. A device reporting off changes nothing:
        presence ends by the hold expiring, never by a sensor going quiet.

        Returns the zones that still could not be read, which stay unseeded
        and are retried on the next tick.
        """
        for name in sorted(self._unseeded):
            zone = self.zones.get(name)
            if zone is None:
                self._unseeded.discard(name)
                continue
            if self._seed_zone(zone, now):
                self._unseeded.discard(name)
        return tuple(sorted(self._unseeded))

    def _seed_zone(self, zone, now) -> bool:
        """Read one zone's inputs. False means "ask again next tick".

        The two lookup failures are not the same answer and do not share a
        handler (R15). A device that is *gone* is a configuration problem: it
        is warned about once and skipped for good, because asking again every
        second will never make it exist. A lookup that *failed* taught us
        nothing about the room, so the zone stays unseeded and is retried --
        giving up on it would leave a zone permanently unseeded because the
        server happened to be busy at startup, and an unseeded zone is one
        that never evaluates.

        The lux sensor deliberately does not block the retry: ``read_lux``
        already turns both failures into the configured ``when_unreadable``
        direction, and a zone whose lux sensor is broken must still run.
        """
        readable = True
        for device_id in zone.config.presence_devices:
            try:
                device = devices.get_device(device_id)
            except devices.DeviceGone:
                devices.warn_gone_once(self.logger, device_id, zone.name)
                continue
            except devices.LookupFailed as exc:
                devices.warn_lookup_failed_once(self.logger, device_id, zone.name, exc.cause)
                readable = False
                continue
            devices.forget_warnings(device_id)
            if presence_is_on(device):
                # This is what rebuilds `presence.on_devices` after a restart.
                # Only `last_seen` is persisted (R13); who is reporting *now*
                # is a fact about the room, so it is read from the room. A
                # level sensor that has been on since before the restart is
                # picked up here and holds the zone occupied, which a
                # persisted timestamp on its own could not do.
                zone.ingest_presence(device_id, True, now)
            # Deliberately no `else: ingest(..., False, ...)`. An "off" now
            # stamps last_seen, so seeding the off devices would push the hold
            # forward on every seed and an empty room would never time out.

        for var_id in zone.config.presence_variables:
            try:
                variable = devices.get_variable(var_id)
            except devices.DeviceGone:
                devices.warn_gone_once(self.logger, var_id, zone.name, kind="variable")
                continue
            except devices.LookupFailed as exc:
                devices.warn_lookup_failed_once(self.logger, var_id, zone.name, exc.cause, kind="variable")
                readable = False
                continue
            devices.forget_warnings(var_id, kind="variable")
            if variable_is_on(getattr(variable, "value", None)):
                # Same R-seed rule as a presence device: a zone enabled while
                # the phone-presence variable already reads "true" starts
                # occupied, not "never seen" (the 2026-09-05 defect for a
                # sensor, repeated here for a variable would be the same bug).
                zone.ingest_presence(var_id, True, now)

        zone.read_lux(now)

        if readable:
            self.logger.debug(
                f"{zone.name}: inputs seeded from the devices themselves -- presence "
                f"{'active' if zone.presence.active(now, zone.config.hold_seconds) else 'inactive'}"
                f" (last seen {zone.presence.last_seen or 'never'}), "
                f"lux {zone.lux.value if zone.lux.value is not None else 'unread'}"
            )
        return readable

    def _run_zone(self, zone, now, cause, transitions, commands, handled):
        transition = zone.evaluate(now, cause)
        sent = self.reconciler.run(zone, now) if zone.running else []
        self._schedule_wake(zone, now, sent)
        handled.append(zone.name)
        if transition is not None:
            transitions.append(transition)
        if sent:
            commands.extend(sent)
        if transition is not None or sent:
            self._notify(zone)

    def _schedule_wake(self, zone, now, sent) -> None:
        """The zone's own next wake, brought forward to re-check a command.

        A command that has just been sent is the one thing the zone's own
        timers know nothing about, and it is the one thing worth looking at
        soon: a device either reports back within a few seconds or it did not
        listen. So a pass that sent anything schedules one wake-up
        ``COMMAND_RECHECK_SECONDS`` from now, and the ordinary worker loop
        does the looking.

        This is emphatically not the settle poll PRD section 9 rules out.
        Nothing sleeps, nothing spawns, and nothing re-reads in a loop: it is
        one entry in the timer map the engine already keeps, and if the device
        has landed by then the re-check costs a comparison and clears the
        ladder silently. What it buys is the difference between retrying an
        ignored command in five seconds and retrying it at the next periodic
        pass, up to ``reconcile_seconds`` away.
        """
        wake = zone.next_wake(now)
        if not sent:
            self._wakes[zone.name] = wake
            return
        recheck = now + dt.timedelta(seconds=COMMAND_RECHECK_SECONDS)
        self._wakes[zone.name] = recheck if wake is None else min(wake, recheck)

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
        """When the worker should wake, or ``now`` if there is work waiting.

        A dirty zone that could not read its devices does *not* count as work
        waiting: it will be retried, but retrying it as fast as the worker can
        loop turns an unanswering server into a spin.
        """
        if any(name not in self._unseeded for name in self._dirty):
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
        was_running = zone.running
        if not zone.set_enabled(enabled=enabled):
            return False
        if zone.running and not was_running:
            # It has been off; the room has moved on without it.
            self._unseeded.add(zone.name)
        self._mark_dirty(zone, f"zone {'enabled' if enabled else 'disabled'}", kind="enable")
        return True

    def set_plugin_enabled(self, enabled) -> bool:
        """The controller device's global enable (section 11, decision 1)."""
        changed = False
        for zone in self.zones.values():
            was_running = zone.running
            if zone.set_enabled(plugin_enabled=enabled):
                changed = True
                if zone.running and not was_running:
                    self._unseeded.add(zone.name)
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
        # Every zone was rebuilt from the file, and a zone switched on by that
        # edit has never looked at the room it is now responsible for (R13 put
        # the override back; it cannot put back a reading nobody took).
        self._unseeded = set(rebuilt)

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

    @property
    def unseeded(self) -> tuple:
        """Zones that have not yet read their input devices. Read-only."""
        return tuple(sorted(self._unseeded))
