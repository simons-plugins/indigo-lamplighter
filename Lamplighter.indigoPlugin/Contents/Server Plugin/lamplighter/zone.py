"""The zone: four inputs, one state machine, one desired level per light.

This is PRD sections 5.2, 5.3, 5.6 and 5.10 in one object, and the rule that
governs all of it is section 1: **no decision is ever made by reading live
device state and comparing it to a target.** Everything :meth:`Zone.evaluate`
decides comes from when presence was last seen, how bright the room is, which
period it is, and whether a person has taken over. Live device state is read
in exactly two places in this plugin -- override detection, which judges the
*transition* an event carries, and reconcile, which asks "is this one device
where I asked it to be" -- and neither of them is here.

The inputs arrive on the Indigo callback thread through :meth:`ingest_presence`
and :meth:`ingest_lux`, which do the minimum: update the input and answer
whether it was an **edge**. A zone re-plans on an edge and on nothing else
(R4). That is the fix for the fork's re-plan storm -- an Occupatum device
ticking every 1.2 s produced hundreds of re-plans an hour and reverts within a
second of a manual change -- and it is a property of these two methods, not of
a rate limit somewhere downstream.

The state machine itself is small enough to read in one sitting:

* **OFF-DUTY** -- no active period, or the room is bright, or the plugin or
  zone is disabled. What it writes depends on **which** of those is true
  (section 5.3, decided 2026-09-04): ``bright`` turns off every light with a
  level in this period, ``vacant_levels`` included, because daylight makes
  the lights unnecessary outright and this house relies on them going off
  when a room brightens; ``no_period`` and ``disabled`` write nothing at all,
  because the plugin has no opinion about a time it was not configured for
  or a zone it was told to leave alone. :attr:`Zone.off_duty_cause` says
  which, and so does the explain line.
* **VACANT** -- on duty, nobody here: every light with a level goes to its
  period's ``vacant_levels`` entry, or off if it has none; a light set to
  ``leave`` (in ``levels``, or in ``vacant_levels`` for one that has a level)
  is not touched.
* **OCCUPIED** -- on duty, somebody here: the period's levels, capped by
  ``limit``.
* **OVERRIDDEN** -- a person has taken over. Desired is ``leave`` for
  everything; the zone writes nothing at all until the override ends.

Every transition is logged once with the cause and the inputs that fed it
(R14), because the fork's "Triggered by" named the most recently changed
sensor rather than the thing that actually caused the re-plan.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from enum import Enum

from . import compare, devices, periods as periods_module
from .lux import STALE_AFTER_SECONDS, Lux, read_sensor_value
from .presence import Presence

#: A level meaning "never write this device in this period, in any state".
LEAVE = "leave"

#: A level meaning "force this device off".
OFF = "off"

#: A level meaning "on, without naming a percentage".
ON = "on"

#: Why a zone is OFF-DUTY. The three are not interchangeable: they choose
#: different desired levels (section 5.3), and they are ranked in this order
#: because a disabled zone must not write whatever else is true, and a zone
#: with no active period has no levels to write even if the room is dark.
OFF_DUTY_DISABLED = "disabled"
OFF_DUTY_NO_PERIOD = "no_period"
OFF_DUTY_BRIGHT = "bright"


class ZoneState(Enum):
    """Where the zone is (section 5.3). The values are the device state strings."""

    OFF_DUTY = "off_duty"
    VACANT = "vacant"
    OCCUPIED = "occupied"
    OVERRIDDEN = "overridden"


@dataclass
class Override:
    """A person has taken over this zone (R10).

    ``duration_minutes`` and ``extend_minutes`` are copied in at creation
    rather than read from the config at expiry, because the period's own
    override block replaces the zone's *while that period is active* (section
    11, decision 4). An override created during a Dining evening band keeps
    the evening band's timing even if it outlives the band -- the alternative
    is a lock whose expiry moves under it when the clock crosses a boundary.
    """

    device_id: int
    since: dt.datetime
    expires_at: dt.datetime
    extended_count: int = 0
    duration_minutes: int = 60
    extend_minutes: int = 0


@dataclass(frozen=True)
class Transition:
    """One move of the state machine, with the cause and the inputs (R14)."""

    zone: str
    from_state: ZoneState
    to_state: ZoneState
    cause: str
    inputs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LightSet:
    """The zone's lights, split by whether they could be resolved (R15).

    ``gone`` and ``failed`` are separate fields for the reason
    :mod:`lamplighter.devices` exists: a device that is not there and a lookup
    that broke are different facts. ``gone`` is a configuration problem;
    ``failed`` is a server problem and the ids in it are still the zone's.
    """

    live: dict = field(default_factory=dict)
    gone: tuple = ()
    failed: tuple = ()

    @property
    def unavailable(self) -> tuple:
        return tuple(self.gone) + tuple(self.failed)


@dataclass(frozen=True)
class DryRun:
    """What a zone WOULD do at an instant, worked out without touching it.

    The answer to "why will the Kitchen be at 10% at midnight", asked before
    midnight. Every field comes from :meth:`Zone.dry_run`, which reads the
    inputs the zone holds *now* and resolves the period, the state machine
    and the level table against ``at`` -- so this describes a decision the
    zone has not taken and, unless the inputs move, will take.

    It is deliberately not a :class:`Transition`: nothing transitioned.
    """

    at: dt.datetime
    state: ZoneState
    period: str | None
    levels: dict
    text: str


class Zone:
    """One lighting zone: inputs, state machine, desired levels, counters."""

    def __init__(self, config, sun, clock=None, logger=None):
        self.config = config
        self.sun = sun
        #: The worker's clock, injected so nothing here ever sleeps or reads
        #: the wall clock behind a test's back. Carried across a config
        #: reload by :func:`persist.rebuild_zone`.
        self.clock = clock
        self.logger = logger or logging.getLogger("Plugin")

        self.presence = Presence()
        self.lux = Lux(config.lux.device if config.lux else None, self.logger)
        self.override = None

        self.enabled = config.enabled
        self.plugin_enabled = True

        self.state = ZoneState.OFF_DUTY
        self.last_trigger = ""
        self.evaluations_today = 0
        self.writes_today = 0
        self.overrides_today = 0
        self._counters_date = None

        # What the last evaluation saw. snapshot() and explain() report the
        # decision that was actually made, not a fresh guess at one -- asking
        # again could answer differently and the states would then describe a
        # decision the zone never took.
        self._evaluated_at = None
        self._period = None
        self._presence_active = False
        self._dark = False
        self._off_duty = None
        self._unavailable = ()

    def __repr__(self):
        return f"<Zone {self.config.name!r} {self.state.value}>"

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def off_duty_cause(self):
        """Why this zone is off duty, or None if it is not (section 5.3).

        One of ``"bright"``, ``"no_period"`` or ``"disabled"``. It is what
        :meth:`desired_levels` branches on, so it is public: "the zone is off
        duty" is not an answer anybody can act on, and the three causes want
        opposite things done to the lights.
        """
        return self._off_duty if self.state is ZoneState.OFF_DUTY else None

    @property
    def running(self) -> bool:
        """Is this zone allowed to plan and write at all?"""
        return bool(self.enabled and self.plugin_enabled)

    def now(self) -> dt.datetime:
        """The injected clock, or the wall clock if none was injected."""
        return self.clock() if self.clock is not None else dt.datetime.now()

    # ------------------------------------------------------------- the inputs

    def ingest_presence(self, device_id, is_on: bool, now: dt.datetime) -> bool:
        """One presence reading. True if it was an input edge (R4).

        Called on the Indigo callback thread, so it does the minimum: update
        the reporting set, answer whether anything moved.

        A CLEARED edge -- a sensor going off -- is true here, and that is not
        the old behaviour. While a sensor is on there is no hold running at
        all, so the moment it clears is the moment a wake-up has to be
        scheduled; a worker that never heard about it would sleep through the
        room emptying. It still produces no transition of its own: the room
        is occupied for ``hold_seconds`` more.

        ``device_id`` is also, for a presence variable, an Indigo variable
        id -- device ids and variable ids are different Indigo namespaces,
        but both are unique house-wide, so they can share this one id space
        in ``self.presence.on_devices`` without ever colliding.
        """
        if device_id not in self.config.presence_devices and device_id not in self.config.presence_variables:
            return False
        return bool(self.presence.update(device_id, is_on, now))

    def ingest_lux(self, value, now: dt.datetime, reason: str = "") -> bool:
        """One lux reading. True only if the dark verdict flipped (R9).

        This is the shape of the answer that matters: a sensor reporting every
        few seconds moves the value constantly and the verdict almost never,
        so a zone gated on this re-plans when the daylight actually changed
        and not when the sensor spoke.
        """
        if self.config.lux is None:
            return False
        self.lux.update(value, now, reason=reason)
        self.lux.dark(
            self.dark_below(), self.config.lux.hysteresis, self.config.lux.when_unreadable
        )
        return self.lux.changed

    def read_lux(self, now: dt.datetime) -> bool:
        """Read the lux device through Indigo and ingest what it says.

        Both failure directions reach :meth:`ingest_lux` as ``None`` with the
        reason attached, so the one warning the zone emits says which of them
        happened. They are not the same problem: a device that is gone wants
        a config edit, a lookup that failed wants the server looking at.
        """
        if self.config.lux is None:
            return False
        device_id = self.config.lux.device
        try:
            device = devices.get_device(device_id)
        except devices.DeviceGone:
            return self.ingest_lux(None, now, reason=f"device {device_id} does not exist in Indigo")
        except devices.LookupFailed as exc:
            return self.ingest_lux(
                None,
                now,
                reason=(
                    f"the Indigo lookup for device {device_id} failed "
                    f"({type(exc.cause).__name__}: {exc.cause}); this is not the "
                    "device being gone"
                ),
            )
        devices.forget_warnings(device_id)
        return self.ingest_lux(read_sensor_value(device), now)

    def set_enabled(self, enabled=None, plugin_enabled=None) -> bool:
        """Set the zone and/or plugin enable. True if either changed.

        A change is an input edge like any other: the zone goes OFF-DUTY when
        it is switched off and re-plans from its real inputs when it is
        switched back on.
        """
        changed = False
        if enabled is not None and bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            changed = True
        if plugin_enabled is not None and bool(plugin_enabled) != self.plugin_enabled:
            self.plugin_enabled = bool(plugin_enabled)
            changed = True
        return changed

    # ------------------------------------------------------- derived readings

    def active_period(self, now: dt.datetime):
        """The period covering ``now``, or None -- a gap is a real answer."""
        return periods_module.active_period(self.config.periods, now, self.sun)

    def dark_below(self) -> float:
        """The dark threshold: the Indigo variable if there is one, else the file.

        The variable is how the Kitchen threshold is tuned from a control page
        without editing the config. A variable that is missing or does not
        hold a number warns once and falls back to the configured number --
        never to zero, which would make the zone permanently bright and
        would look exactly like a correctly configured daylit room (R15).
        """
        lux_config = self.config.lux
        if lux_config is None or lux_config.dark_below_variable_id is None:
            return lux_config.dark_below if lux_config else 0.0

        var_id = lux_config.dark_below_variable_id
        key = ("lux-variable", var_id)
        try:
            raw = devices.get_variable_value(var_id)
        except devices.DeviceGone:
            compare.warn_once(
                self.logger,
                key,
                f"{self.name}: dark_below variable {var_id} does not exist in "
                f"Indigo; using the configured dark_below of {lux_config.dark_below}.",
            )
            return lux_config.dark_below
        except devices.LookupFailed as exc:
            compare.warn_once(
                self.logger,
                key,
                f"{self.name}: looking up dark_below variable {var_id} failed "
                f"({type(exc.cause).__name__}: {exc.cause}) -- which is not the "
                f"variable being gone; using the configured dark_below of "
                f"{lux_config.dark_below}.",
            )
            return lux_config.dark_below

        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            compare.warn_once(
                self.logger,
                key,
                f"{self.name}: dark_below variable {var_id} holds {raw!r}, which "
                f"is not a number; using the configured dark_below of "
                f"{lux_config.dark_below}.",
            )
            return lux_config.dark_below

        compare.reset_warnings(key)
        return value

    def is_dark(self) -> bool:
        """The daylight verdict. A zone with no lux block is always dark enough.

        "No gate" means the zone follows presence around the clock, which is
        what a hallway with no window wants, so the gate answers True rather
        than being skipped somewhere in the state machine.
        """
        if self.config.lux is None:
            return True
        return self.lux.dark(
            self.dark_below(), self.config.lux.hysteresis, self.config.lux.when_unreadable
        )

    def would_be_dark(self) -> bool:
        """The daylight verdict, reached without moving the Schmitt trigger.

        :meth:`is_dark` is what the state machine calls, and calling it is
        what advances the hysteresis band. This is the same question asked by
        a reporter -- :meth:`dry_run` -- which must not change what the next
        real evaluation decides.
        """
        if self.config.lux is None:
            return True
        return self.lux.would_be_dark(
            self.dark_below(), self.config.lux.hysteresis, self.config.lux.when_unreadable
        )

    def resolve_lights(self, now=None) -> LightSet:
        """Look up this zone's lights, warning once for each that will not resolve.

        A light that is gone is dropped from the working set and the zone
        keeps running for the others -- the alternative, refusing the whole
        zone, takes out four working lights for one dead bulb. A light whose
        lookup *failed* is skipped for this pass and kept, because nothing was
        learned about it (R15).
        """
        live, gone, failed = {}, [], []
        for dev_id in self.config.lights:
            try:
                live[dev_id] = devices.get_device(dev_id)
            except devices.DeviceGone:
                devices.warn_gone_once(self.logger, dev_id, self.name)
                gone.append(dev_id)
            except devices.LookupFailed as exc:
                devices.warn_lookup_failed_once(self.logger, dev_id, self.name, exc.cause)
                failed.append(dev_id)
            else:
                devices.forget_warnings(dev_id)
        self._unavailable = tuple(gone) + tuple(failed)
        return LightSet(live=live, gone=tuple(gone), failed=tuple(failed))

    # --------------------------------------------------------- the overrides

    def override_timing(self, now: dt.datetime):
        """``(duration_minutes, extend_minutes)`` in force right now.

        The active period's ``override`` block *replaces* the zone's, both
        fields together, rather than merging field by field -- a half
        specified block would leave which value applies depending on which
        fields someone happened to write (section 11, decision 4).
        """
        period = self.active_period(now)
        timing = self.config.override
        if period is not None and period.override is not None:
            timing = period.override
        return timing.duration_minutes, timing.extend_minutes

    def start_override(self, device_id, now: dt.datetime):
        """Record that a person has taken this zone over. The Override, or None.

        None means the zone is configured ``override.enabled: false`` -- the
        never-lock hallway -- where a manual change is noticed and the zone
        keeps commanding its desired levels anyway.
        """
        if not self.config.override.enabled:
            self.logger.debug(
                f"{self.name}: device {device_id} changed but this zone never locks "
                "(override.enabled is false); the zone keeps its desired levels."
            )
            return None

        duration, extend = self.override_timing(now)
        self.override = Override(
            device_id=device_id,
            since=now,
            expires_at=now + dt.timedelta(minutes=duration),
            extended_count=0,
            duration_minutes=duration,
            extend_minutes=extend,
        )
        self._roll_counters(now)
        self.overrides_today += 1
        self.logger.info(
            f"{self.name}: override taken by device {device_id} at {now:%H:%M:%S}, "
            f"holding {duration} min (until {self.override.expires_at:%H:%M:%S})"
            + (f", extending {extend} min while occupied" if extend else "")
        )
        return self.override

    def end_override(self, reason: str, now: dt.datetime):
        """Release the override. Returns the one that ended, or None."""
        ended = self.override
        if ended is None:
            return None
        self.override = None
        self.logger.info(
            f"{self.name}: override released ({reason}) after "
            f"{_minutes(now - ended.since)} min, taken by device {ended.device_id}"
            + (f", extended {ended.extended_count}x" if ended.extended_count else "")
        )
        return ended

    def _age_override(self, now, presence_active) -> None:
        """Extend or end an override that has reached one of its edges (R10)."""
        override = self.override
        if override is None:
            return

        if self._released_by_leaving(now, presence_active):
            self.end_override("the room emptied and unlock_on_leave is set", now)
            return

        if now < override.expires_at:
            return

        if presence_active and override.extend_minutes > 0:
            override.expires_at = now + dt.timedelta(minutes=override.extend_minutes)
            override.extended_count += 1
            self.logger.info(
                f"{self.name}: override extended {override.extend_minutes} min "
                f"(the room is still occupied), now until "
                f"{override.expires_at:%H:%M:%S}; extension "
                f"{override.extended_count}"
            )
            return

        self.end_override("it expired with the room empty", now)

    def _released_by_leaving(self, now, presence_active) -> bool:
        """Should unlock-on-leave release this override right now (R10)?

        Two conditions, and the second is the one the fork got wrong in both
        directions. Unlock-on-leave has to fire for an override created while
        the room was *occupied* -- fork #17 armed it only for overrides
        created in an already-empty room, so every override a person made
        while standing there ran its full hour. But it must not fire for an
        override created in an empty room -- the lock-zone action, or an app
        command from another building -- which would evaporate on the next
        evaluation.

        Both fall out of comparing the presence hold's expiry with the moment
        the override was taken: an expiry later than ``since`` means the room
        was occupied when the override was created, and the hold has since
        run out. No extra flag, and it survives persistence because both
        halves are already persisted.
        """
        if not self.config.override.unlock_on_leave or presence_active:
            return False
        expiry = self.presence.expiry(self.config.hold_seconds)
        return expiry is not None and expiry > self.override.since

    def override_holds_at(self, at: dt.datetime, presence_active: bool) -> bool:
        """Would the override still be in force at ``at``? Asks; never ages.

        The three edges :meth:`_age_override` acts on -- unlock-on-leave, the
        expiry, and the extension -- read rather than applied, so that
        :meth:`dry_run` can answer "what will this zone be doing at 22:00"
        without releasing a lock the person who owns the house is still
        holding. The two must agree; ``test_zone`` pins them together.
        """
        override = self.override
        if override is None:
            return False
        if self._released_by_leaving(at, presence_active):
            return False
        if at < override.expires_at:
            return True
        return bool(presence_active and override.extend_minutes > 0)

    # ------------------------------------------------------ the state machine

    def evaluate(self, now: dt.datetime, cause: str):
        """Run the state machine. Returns the :class:`Transition`, or None.

        None means nothing moved, and nothing moving is the common case: the
        zone is asked to think on every input edge and most edges confirm
        what it already believed. A caller writes only when this returns
        something, or when the reconcile tick tells it to.
        """
        self._roll_counters(now)
        self.evaluations_today += 1
        self.last_trigger = cause

        period = self.active_period(now)
        presence_active = self.presence.active(now, self.config.hold_seconds)
        dark = self.is_dark()

        # The override's own clock runs before the state is chosen, so that an
        # expiry and the transition it causes are one evaluation, not two.
        self._age_override(now, presence_active)

        off_duty = self._off_duty_reason(period, dark)
        new_state = self._state_for(off_duty, self.override is not None, presence_active)

        self._evaluated_at = now
        self._period = period
        self._presence_active = presence_active
        self._dark = dark
        self._off_duty = off_duty

        previous = self.state
        if new_state is previous:
            return None

        self.state = new_state
        inputs = self.inputs(now)
        self.logger.info(
            f"{self.name}: {previous.value} -> {new_state.value} ({cause}); "
            + ", ".join(f"{key}={value!r}" for key, value in inputs.items())
        )
        return Transition(
            zone=self.name,
            from_state=previous,
            to_state=new_state,
            cause=cause,
            inputs=inputs,
        )

    def _state_for(self, off_duty, overridden, presence_active) -> ZoneState:
        """Which state these three facts put the zone in (section 5.3).

        Pure, and separate from :meth:`evaluate`, so that :meth:`dry_run` can
        ask the state machine the question without running it. Two copies of
        this ranking would be two state machines.
        """
        if off_duty is not None and not (off_duty == OFF_DUTY_BRIGHT and overridden):
            # An override outlasts the room going bright, and only that: a
            # person who took the lights over keeps them until the override
            # ends, daylight or not. It does not outlast being switched off
            # or the period ending, because those two write nothing either
            # way and reporting a disabled zone as OVERRIDDEN would lie about
            # what it is doing.
            return ZoneState.OFF_DUTY
        if overridden:
            return ZoneState.OVERRIDDEN
        if presence_active:
            return ZoneState.OCCUPIED
        return ZoneState.VACANT

    def _off_duty_reason(self, period, dark):
        """Why the zone would be off duty, or None if it would not be.

        Ranked, not combined. A disabled zone must write nothing whatever
        else is true, so that comes first; a zone with no active period has
        no levels to write even in the dark, so that comes second; and
        `bright` is left as the only cause that carries a plan.
        """
        if not self.running:
            return OFF_DUTY_DISABLED
        if period is None:
            return OFF_DUTY_NO_PERIOD
        if not dark:
            return OFF_DUTY_BRIGHT
        return None

    def inputs(self, now: dt.datetime) -> dict:
        """The four inputs as they stood at the last evaluation (R14)."""
        return {
            "period": self._period.name if self._period else None,
            "presence_active": self._presence_active,
            "presence_last_seen": self.presence.last_seen,
            "dark": self._dark,
            "lux": self.lux.value,
            "lux_unreadable": self.lux.unreadable,
            "override_device": self.override.device_id if self.override else None,
            "enabled": self.running,
            "off_duty_cause": self.off_duty_cause,
        }

    def _roll_counters(self, now: dt.datetime) -> None:
        """Zero the day's counters at local midnight (section 5.10)."""
        today = now.date()
        if self._counters_date == today:
            return
        self._counters_date = today
        self.evaluations_today = 0
        self.writes_today = 0
        self.overrides_today = 0

    # ------------------------------------------------------- the desired plan

    def desired_levels(self, now: dt.datetime) -> dict:
        """What every light in this zone should be, right now (sections 5.3, 5.6).

        One entry per light, always: a light absent from the period's
        ``levels`` is ``leave``, exactly as if it had been written that way,
        because defaulting a missing level to off makes every unlisted light a
        light the zone turns off.

        This is the only thing reconcile reads. That is what gives ``leave``
        its guarantee -- a device whose desired level is ``leave`` is not
        written in any state, not on, not off, not at the reconcile tick --
        and it is why the guarantee is one line here rather than a special
        case in the writer.

        The table itself is :meth:`_plan_for`, one line down, parameterised
        by the state rather than reading ``self.state``: that is what lets
        :meth:`dry_run` ask the same question about a state the zone is not
        in, without a second copy of the table to keep in step.
        """
        return self._plan_for(self.state, self._off_duty, self.active_period(now))

    def _plan_for(self, state, off_duty, period) -> dict:
        """The level table of :meth:`desired_levels`, for any state.

        Read the docstring above this one: everything it promises is decided
        here. ``state`` and ``off_duty`` are passed rather than read so that
        the live plan and a dry run's plan cannot drift apart.
        """
        plan = {light: LEAVE for light in self.config.lights}

        if state is ZoneState.OVERRIDDEN:
            # A person has taken over: desired is whatever the devices are.
            return plan
        if period is None:
            return plan

        if state is ZoneState.OFF_DUTY:
            # Which off duty? `bright` is VACANT's plan: the daylight has made
            # the lights unnecessary and this house relies on them going off
            # when a room brightens, whether or not anybody is standing in it.
            # `no_period` and `disabled` write nothing -- the plugin has no
            # opinion about a time it was not configured for, or about a zone
            # it was told to leave alone.
            if off_duty == OFF_DUTY_BRIGHT:
                return self._all_off(period, plan)
            return plan

        if state is ZoneState.VACANT:
            return self._vacant_plan(period, plan)

        # OCCUPIED.
        if period.mode == "off_only":
            # Never turns a light on, whatever presence does. Not "off"
            # either: the mode turns lights off when the room empties, and a
            # light a person switched on while they are here is theirs.
            return plan

        if period.adjust_by_lux:
            # Not implemented in v1 (section 5.6). The loader refuses the flag
            # outright on a zone that HAS a lux block, so the only way to
            # arrive here is the harmless half: a zone with no sensor to scale
            # against, where the flag could never have done anything. It still
            # says so once rather than passing in silence.
            compare.warn_once(
                self.logger,
                ("adjust-by-lux", self.name, period.name),
                f"{self.name}: period {period.name!r} sets adjust_by_lux, which is "
                "not implemented in this version; the configured levels are used "
                "unscaled. This zone has no lux block, so there was nothing to "
                "scale against either -- on a zone with one the loader refuses the "
                "file rather than letting it run like this.",
            )

        for light in self.config.lights:
            level = period.levels.get(light, LEAVE)
            if level == LEAVE:
                continue
            plan[light] = OFF if level == OFF else _capped(level, period.limit)
        return plan

    def _all_off(self, period, plan) -> dict:
        """Off for every light with a level; ``leave`` devices untouched.

        Used only for OFF-DUTY's ``bright`` cause, which is deliberately not
        :meth:`_vacant_plan`: daylight makes the lights unnecessary outright,
        dim level included, whether or not the room later empties on its own.
        """
        for light in self.config.lights:
            if period.levels.get(light, LEAVE) != LEAVE:
                plan[light] = OFF
        return plan

    def _vacant_plan(self, period, plan) -> dict:
        """The VACANT plan, honouring the period's ``vacant_levels`` (R12).

        Only lights with a non-``leave`` level in ``levels`` are touched at
        all -- a light this period does not manage while occupied is not
        managed while vacant either. For each of those, the desired level is
        the light's ``vacant_levels`` entry, mapped exactly as an occupied
        level is (an int capped by the period's ``limit``, ``on`` becomes the
        capped level, ``off`` forces off, ``leave`` leaves it alone); a light
        absent from ``vacant_levels`` goes off, exactly as before this key
        existed.
        """
        for light in self.config.lights:
            if period.levels.get(light, LEAVE) == LEAVE:
                continue
            vacant_level = period.vacant_levels.get(light, OFF)
            if vacant_level == LEAVE:
                continue
            plan[light] = OFF if vacant_level == OFF else _capped(vacant_level, period.limit)
        return plan

    def desired_summary(self, now: dt.datetime) -> str:
        """The plan on one line, for the zone's device state (section 5.10)."""
        plan = self.desired_levels(now)
        return ", ".join(f"{light}={plan[light]}" for light in sorted(plan))

    # ------------------------------------------------------------ the timers

    def next_wake(self, now: dt.datetime):
        """When this zone next needs thinking about, with no event at all.

        The four things that move without anything happening on the device
        bus: the presence hold expiring, the override expiring, a period
        boundary, and local midnight for the counters. A zone whose room is
        empty and quiet still notices the period changed, which is the half of
        R4 that device events cannot supply.

        In practice this never answers None -- midnight is always ahead of us
        -- but it is written to, because a caller must not assume a wake-up
        exists.
        """
        candidates = [dt.datetime.combine(now.date() + periods_module.ONE_DAY, dt.time())]

        # None while any presence device is still reporting, and that is the
        # point: there is no hold to expire until the room clears. Scheduling
        # one anyway is how a zone on a level sensor wakes up in the middle
        # of somebody sitting still and puts itself VACANT.
        hold_expiry = self.presence.expiry(self.config.hold_seconds)
        if hold_expiry is not None:
            candidates.append(hold_expiry)
        if self.override is not None:
            candidates.append(self.override.expires_at)
        boundary = periods_module.next_boundary(self.config.periods, now, self.sun)
        if boundary is not None:
            candidates.append(boundary)

        ahead = [when for when in candidates if when > now]
        return min(ahead) if ahead else None

    # ------------------------------------------------------------ the report

    def explain(self, now: dt.datetime) -> str:
        """One line: the state, why it is in it, and the inputs that decided.

        This is the line that replaces reading the plugin's mind from the
        event log, so it says the degradations out loud: an unreadable sensor
        and a stale reading are named here, never rounded off into a number
        that looks like a working sensor (R15).

        It reports the LAST evaluation, not a fresh one -- asking again could
        answer differently and the line would then describe a decision the
        zone never took. For "what would it decide at 22:00", ask
        :meth:`dry_run`.
        """
        why = self._why(self.state, self._off_duty, self._presence_active)
        parts = self._input_parts(now, self._period, self._presence_active, self._dark)
        return (
            f"{self.name} is {self.state.value} because {why}. "
            + "; ".join(parts)
            + f". Last trigger: {self.last_trigger or 'none'}."
        )

    def _why(self, state, off_duty, presence_active) -> str:
        """The reason half of an explain line, for any state (R14)."""
        if state is ZoneState.OFF_DUTY:
            # The cause is named as well as described, because it is what
            # decides whether the lights go off or are left alone, and a
            # reader who only sees "off duty" cannot tell which happened.
            why = {
                OFF_DUTY_DISABLED: (
                    "the plugin is off" if not self.plugin_enabled else "the zone is off"
                ),
                OFF_DUTY_NO_PERIOD: "no period covers this time",
                OFF_DUTY_BRIGHT: "the room is bright",
            }.get(off_duty, "it has not been evaluated yet")
            return why + f" (off-duty cause: {off_duty})"
        if state is ZoneState.OVERRIDDEN and self.override is not None:
            why = (
                f"device {self.override.device_id} took it over at "
                f"{self.override.since:%H:%M:%S}, held until "
                f"{self.override.expires_at:%H:%M:%S}"
            )
            if off_duty == OFF_DUTY_BRIGHT:
                why += ", and it outlasts the room going bright"
            return why
        if presence_active:
            return "someone is here"
        return "the presence hold has expired"

    def _input_parts(self, now, period, presence_active, dark) -> list:
        """The inputs half of an explain line: one ``key=value`` per input."""
        parts = [
            f"period={period.name if period else 'none'}",
            f"presence={'active' if presence_active else 'inactive'}"
            + f" ({self._presence_phrase()})",
            f"lux={self._lux_phrase(now, dark)}",
            f"override={self.override.device_id if self.override else 'none'}",
        ]
        if self._unavailable:
            parts.append(
                "unavailable lights="
                + "/".join(str(dev_id) for dev_id in self._unavailable)
            )
        return parts

    def dry_run(self, at: dt.datetime) -> DryRun:
        """What this zone would decide at ``at``, deciding nothing (5.13).

        The question an author asks before saving a period edit, and the one
        an MCP caller asks on their behalf: *at midnight, which period is
        this, what state would the machine be in, and what would each light
        be told?* The period and the level table are resolved against ``at``;
        every input -- presence, lux, the override, the enables -- is read as
        it stands **now**, because that is the only honest answer available:
        nobody knows whether the room will be occupied at midnight.

        Nothing here writes, evaluates, reconciles or moves a counter. Three
        of the pieces it needs are hazardous asked the ordinary way and each
        has a read-only twin: :meth:`would_be_dark` rather than
        :meth:`is_dark`, which would advance the hysteresis band;
        :meth:`override_holds_at` rather than ``_age_override``, which would
        release a live lock; and :meth:`_plan_for` rather than
        :meth:`desired_levels`, which reads the state the zone is actually
        in. Getting any of those wrong makes *asking* a question change the
        answer -- the one failure a dry run must not have.
        """
        period = self.active_period(at)
        presence_active = self.presence.active(at, self.config.hold_seconds)
        dark = self.would_be_dark()
        overridden = self.override_holds_at(at, presence_active)
        off_duty = self._off_duty_reason(period, dark)
        state = self._state_for(off_duty, overridden, presence_active)
        levels = self._plan_for(state, off_duty, period)

        text = (
            f"{self.name} would be {state.value} at {at:%Y-%m-%dT%H:%M} because "
            f"{self._why(state, off_duty, presence_active)}. "
            + "; ".join(self._input_parts(at, period, presence_active, dark))
            + ". Levels there: "
            + (", ".join(f"{light}={levels[light]}" for light in sorted(levels)) or "none")
            + ". This is a dry run against the inputs as they stand now: nothing "
            "was evaluated, nothing was written, and the zone is unchanged."
        )
        return DryRun(
            at=at,
            state=state,
            period=period.name if period else None,
            levels=levels,
            text=text,
        )

    def _presence_label(self, input_id) -> str:
        """A presence input's name for the explain line, device or variable.

        ``self.presence.on_devices`` holds device ids and variable ids in one
        set (see :meth:`ingest_presence`), so the label has to ask which
        namespace this id actually belongs to before resolving it -- a
        variable id handed to the device lookup would just fail and read as
        "device 1872770829", which is not what a phone-presence variable is.
        """
        if input_id in self.config.presence_variables:
            return _variable_label(input_id)
        return _device_label(input_id)

    def _presence_phrase(self) -> str:
        """Why presence reads the way it does: who is on, or how long ago.

        The two are genuinely different states and the line has to say which.
        "active" with a sensor still on means the room is held open for as
        long as that sensor says so; "active" on the hold means it is
        counting down and the reader can work out from when. Collapsing them
        into "last seen 20:14:03" is what made the Study's radar look like a
        sensor that had stopped reporting.
        """
        if self.presence.on_devices:
            names = ", ".join(
                self._presence_label(input_id) for input_id in sorted(self.presence.on_devices)
            )
            return f"{names} on"
        if self.presence.last_seen is None:
            return "never seen"
        return f"hold, last seen {self.presence.last_seen:%H:%M:%S}"

    def _lux_phrase(self, now: dt.datetime, dark: bool) -> str:
        if self.config.lux is None:
            return "no sensor (this zone has no daylight gate)"
        threshold = f"{'dark' if dark else 'bright'} below {self.dark_below():g}"
        if self.lux.unreadable:
            return f"UNREADABLE ({self.lux.reason}), treated as {threshold}"
        if self.lux.value is None:
            return f"not yet read, treated as {threshold}"
        age = self.lux.age(now)
        stale = self.lux.stale(now, STALE_AFTER_SECONDS)
        return (
            f"{self.lux.value:g}"
            + (f" STALE, read {_minutes(age)} min ago" if stale and age else "")
            + f", {threshold}"
        )

    def snapshot(self) -> dict:
        """The zone's Indigo device states (section 5.10).

        Strings, numbers and booleans only -- Indigo device states hold
        nothing else. The convention for "there is no value" is the empty
        string, never a zero: a lux of 0.0 published for a sensor that has
        never been read is exactly the quiet zero R15 forbids, and it looks
        identical to a pitch-dark room.
        """
        now = self._evaluated_at or self.now()
        override = self.override
        return {
            "state": self.state.value,
            "presence_active": bool(self._presence_active),
            "presence_last_seen": _iso(self.presence.last_seen),
            "lux": self.lux.value if self.lux.value is not None else "",
            "dark": bool(self._dark),
            "period": self._period.name if self._period else "",
            "override_device": override.device_id if override else "",
            "override_expires": _iso(override.expires_at) if override else "",
            "desired_summary": self.desired_summary(now),
            "explain": self.explain(now),
            "evaluations_today": self.evaluations_today,
            "writes_today": self.writes_today,
            "overrides_today": self.overrides_today,
            "last_trigger": self.last_trigger,
        }


def _device_label(dev_id) -> str:
    """A presence device's name for the explain line, or its id.

    A label and only a label: it never gates anything, so a lookup that will
    not answer falls back to the id rather than taking the line down. The
    zone's unavailable-lights list is where a device that cannot be resolved
    is actually reported.
    """
    try:
        name = getattr(devices.get_device(dev_id), "name", "")
    except (devices.DeviceGone, devices.LookupFailed):
        return f"device {dev_id}"
    return str(name) or f"device {dev_id}"


def _variable_label(var_id) -> str:
    """A presence variable's name for the explain line, prefixed like R14 wants.

    Same fallback rule as :func:`_device_label`: a label only, so a lookup
    that will not answer falls back to the id rather than taking the line
    down.
    """
    try:
        name = getattr(devices.get_variable(var_id), "name", "")
    except (devices.DeviceGone, devices.LookupFailed):
        return f"variable {var_id}"
    return f"variable {name}" if name else f"variable {var_id}"


def _capped(level, limit):
    """A level with the period's ``limit`` applied (R12, section 5.6).

    The cap applies to every device, not only to the ones a lux adjustment
    touched. In the fork ``limit_brightness`` lived inside the
    adjust-brightness branch, so it silently did nothing for every device with
    an explicit level -- which was all of them.

    ``on`` means full, and full is capped like any other level: a relay reads
    any integer as on, so nothing is lost by naming the number.
    """
    if limit is None:
        return level
    if level == ON:
        return limit
    return min(level, limit)


def _iso(when):
    """A timestamp as a fixed-format string, or "" -- never a fake one."""
    return when.strftime("%Y-%m-%dT%H:%M:%S") if when else ""


def _minutes(delta):
    return int(delta.total_seconds() // 60) if delta else 0
