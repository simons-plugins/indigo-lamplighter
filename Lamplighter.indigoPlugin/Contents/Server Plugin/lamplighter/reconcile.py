"""One command per device that is off desired, and nothing else (PRD R6; 5.8).

This is the entire write machinery, and its shortness is the point. There is
no settle poll, no confirm thread, no consecutive-failure counter, no
suppression list, no recovery scan, no writer re-evaluation and no
re-evaluation rate limit. All of those existed in the fork to make a decision
taken from live state safe, and section 1 removed the decision.

What replaces them is the tick. A device that does not land is still off
desired at the next pass, so it is commanded again, on a per-device backoff of
1, 2, 4 and 8 ticks, with one WARNING at the first backoff step naming the
device, what it reads and what was asked for. Once that ladder is walked the
device is parked: it is retried by the wall clock (:data:`PARKED_RETRY_SECONDS`)
rather than by counting passes, because passes are one counter shared by every
zone in the house and a busy evening can run through eight of them in well
under a minute -- which is exactly how two Zigbee lights behind a switch that
is normally off turned into a hundred wasted unicasts an hour. A device that
reports at desired clears its own backoff silently, and so does a parked
device that reports *any change at all* -- a link quality, a last-seen, the
same level it went dark at: a changed report is evidence it is alive again,
so its ladder is dropped and it is tried again at the very next pass rather
than waiting out the rest of the parked interval. An update that changes
nothing (Indigo delivers those too, the echo of our own retry among them) is
not a report and leaves the device parked.

**PRD section 9 records why, and it is worth repeating here**: if a device
does not confirm, the answer is the reconcile tick, not a thread. The first
flaky light will make a settle poll look reasonable again. It is not: a poll
blocks the worker, a thread per write reintroduces the concurrency the fork
spent its bug list on, and neither of them makes a light that is not listening
listen.

Backoff lives here, keyed by device, and not on the zone: one broken bulb must
not stall the four working ones beside it.

**Backoff belongs to a device AND the level it was asked for.** A device's
ladder is a record of one command not landing, so it means nothing once the
zone wants something else: the next command is a first attempt at a new
target, not a retry of the old one. Getting this wrong produced four false
warnings in one evening on the Hallway -- the hold expired, the lamp was
commanded off, the PIR re-tripped before any pass had seen it at off, and the
lamp coming on to 80 was reported as "did not reach its desired level. It
reads 0 and the zone wants 80" about a lamp that was working perfectly.

**A command is re-checked once, five seconds later.** This is not a settle
poll and not a thread: the reconciler asks the engine to bring that zone's
next wake-up forward (:data:`COMMAND_RECHECK_SECONDS`), and the ordinary
worker pass does the looking. It buys the thing the periodic tick could not:
a device that genuinely ignored a command is re-sent in about five seconds
rather than up to a full reconcile interval, and a device that simply had not
reported yet clears silently at the same moment instead of being warned about.

The five seconds is also a floor, not just a schedule. A pass can run sooner
for a reason that has nothing to do with the command just sent -- another
zone's own wake, an input edge such as the room's own lux sensor reacting to
the lights coming on -- and such a pass leaves a device commanded within the
window alone: it is in flight, not failed, and is neither re-commanded nor
warned about. Only a pass at or after the five seconds is entitled to judge
it, whatever woke the zone.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import indigo

from . import compare, devices
from .override import unreadable_key
from .zone import LEAVE, OFF, ON

#: The backoff ladder, in ticks between attempts, held at the last entry.
BACKOFF_TICKS = (1, 2, 4, 8)

#: How long after a command the zone is woken to see whether it landed. One
#: re-check, scheduled through the engine's existing wake mechanism -- there
#: is still no poll, no sleep and no thread (PRD section 9). Long enough for a
#: Z-Wave or Zigbee device to report back, short enough that a genuinely
#: ignored command is retried in seconds rather than at the next periodic pass.
COMMAND_RECHECK_SECONDS = 5.0

#: How often a device is retried once it is past the whole backoff ladder --
#: commanded more times than ``BACKOFF_TICKS`` has entries. This is wall
#: clock, not passes: ``passes`` is one counter shared by every zone, so a
#: house with several zones and the five-second re-check above can run
#: through the ladder's cap of eight ticks in under a minute, and a device
#: wedged behind a switch that is normally off gets commanded that often for
#: as long as the zone wants it. Ten minutes between attempts is long enough
#: that a device stuck this way stops being the dominant source of Zigbee or
#: Z-Wave traffic in the house, and short enough that a device that comes
#: back on its own is not left off desired for long.
PARKED_RETRY_SECONDS = 600.0


def backoff_key(device_id, level):
    """The ``warn_once`` key for "this device did not reach THIS level".

    The level is part of the key so that a later genuine miss on a different
    target is reported rather than swallowed by a warning latched for the
    previous one, while repeats of the same miss stay quiet.
    """
    return ("backoff", device_id, level)


@dataclass(frozen=True)
class Command:
    """One command this pass sent, for the log line and for the caller."""

    zone: str
    device_id: int
    device_name: str
    level: object
    actual: object = None
    backoff_step: int = 0


@runtime_checkable
class Commander(Protocol):
    """Whatever actually talks to the lights.

    One method, because the plugin only ever does one thing to a light. The
    protocol exists so that the reconcile pass can be tested without Indigo
    and so that the plugin, an action and a dry run can share it.
    """

    def set_level(self, device, level) -> None:  # pragma: no cover - protocol
        ...


class IndigoCommander:
    """The real one: Indigo's own verbs, chosen by what the device is.

    A dimmer gets ``setBrightness``; a relay, and any device whose desired
    level is the bare ``on``/``off``, gets ``turnOn``/``turnOff``. An int on a
    relay is not an error -- a relay reads any level above zero as on -- so it
    is translated rather than refused.
    """

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("Plugin")

    def set_level(self, device, level) -> None:
        device_id = device.id
        if level == OFF or level is False or level == 0:
            indigo.device.turnOff(device_id)
            return
        if level == ON or level is True:
            indigo.device.turnOn(device_id)
            return
        if compare.is_dimmer(device):
            indigo.dimmer.setBrightness(device_id, value=int(level))
            return
        indigo.device.turnOn(device_id)


@dataclass
class _Backoff:
    """One device's place on the ladder, for one desired level.

    ``step`` counts commands sent while the device has not landed; ``next_due``
    is the pass number at which it may be commanded again; ``level`` is what
    those commands were asking for. ``not_before`` is set once ``step`` has
    walked past the whole ladder, and from then on governs retries instead of
    ``next_due`` -- see :data:`PARKED_RETRY_SECONDS`. When the zone starts
    wanting something else the whole entry is discarded -- it is a record of
    one target not being reached, and it says nothing about the next one.
    """

    step: int = 0
    next_due: int = 0
    level: object = None
    not_before: dt.datetime | None = None
    sent_at: dt.datetime | None = None


class Reconciler:
    """Sends the commands a zone's plan implies, once per pass, per device."""

    def __init__(self, commander: Commander, echo_book, logger=None):
        self.commander = commander
        self.echo_book = echo_book
        self.logger = logger or logging.getLogger("Plugin")
        #: Passes run so far. The backoff ladder counts in these, so it is
        #: the reconciler's own clock and nothing else's.
        self.passes = 0
        self._backoff: dict = {}

    # ---------------------------------------------------------------- the pass

    def run(self, zone, now: dt.datetime) -> list:
        """Command every device in ``zone`` that is off its desired level.

        Returns the commands sent, in order. An empty list is the common
        case and the good one: the plan matched the house.
        """
        self.passes += 1
        this_pass = self.passes
        sent = []

        for device_id, level in zone.desired_levels(now).items():
            if level == LEAVE:
                # The guarantee `leave` carries: never written, in any state,
                # not even at the tick. It is one line here because
                # desired_levels() is the only thing this pass reads.
                continue

            try:
                device = devices.get_device(device_id)
            except devices.DeviceGone:
                devices.warn_gone_once(self.logger, device_id, zone.name)
                continue
            except devices.LookupFailed as exc:
                # Nothing was learned about this device, so nothing about it
                # is decided: it is skipped for this pass and kept, and its
                # backoff is left exactly where it was.
                devices.warn_lookup_failed_once(self.logger, device_id, zone.name, exc.cause)
                continue
            devices.forget_warnings(device_id)

            readable = True
            try:
                if compare.at_level(device, level):
                    self._clear_backoff(device_id)
                    continue
            except compare.UnreadableDevice as exc:
                readable = False
                self._warn_unreadable(zone, device, exc)

            backoff = self._backoff.get(device_id)
            if backoff is not None and backoff.level != level:
                # The zone wants something else now. Whatever the old ladder
                # recorded was about reaching the OLD level, so the command
                # below is a first attempt and not a retry: no warning, and no
                # backoff delay in front of it. This is the Hallway bug --
                # "off" was commanded, the PIR re-tripped before any pass had
                # seen the lamp at off, and the lamp coming on to 80 was
                # reported as a failure to reach a level nobody had asked for
                # when that command was sent.
                self._clear_backoff(device_id)
                backoff = None
            if (
                backoff is not None
                and backoff.sent_at is not None
                and (now - backoff.sent_at) < dt.timedelta(seconds=COMMAND_RECHECK_SECONDS)
            ):
                elapsed = (now - backoff.sent_at).total_seconds()
                self.logger.debug(
                    f"{zone.name}: {device.name} was commanded {elapsed:.1f} s "
                    "ago and is still in flight; not judged"
                )
                continue
            if backoff is not None and backoff.next_due > this_pass:
                self.logger.debug(
                    f"{zone.name}: {device.name} is off desired={level!r} but is "
                    f"backing off until pass {backoff.next_due} (this is pass "
                    f"{this_pass}); not commanded"
                )
                continue
            if backoff is not None and backoff.not_before is not None and backoff.not_before > now:
                self.logger.debug(
                    f"{zone.name}: {device.name} is off desired={level!r} but is "
                    f"parked until {backoff.not_before.isoformat()} (wall clock; "
                    f"this pass is at {now.isoformat()}); not commanded"
                )
                continue

            actual = self._actual(device) if readable else None
            if backoff is not None and backoff.step >= 1:
                # Reached only on the re-check or a later pass: the first
                # command toward any target never warns.
                self._warn_backoff(zone, device, actual, level, backoff.step)

            if readable:
                # Bookkeeping must never cost the command. A device that
                # cannot be read simply gets no echo record and falls back to
                # the transition rule alone -- stricter, not blinder (R3, R8).
                try:
                    self.echo_book.note_pre_command(device_id, compare.reading(device), now)
                except compare.UnreadableDevice as exc:
                    self._warn_unreadable(zone, device, exc)

            self.commander.set_level(device, level)
            zone.writes_today += 1
            step = self._advance_backoff(device_id, this_pass, now, level)
            sent.append(
                Command(
                    zone=zone.name,
                    device_id=device_id,
                    device_name=device.name,
                    level=level,
                    actual=actual,
                    backoff_step=step,
                )
            )

        if sent:
            self.logger.info(
                f"{zone.name}: reconcile pass {this_pass} commanded "
                + ", ".join(
                    f"{command.device_name} {command.actual!r}->{command.level!r}"
                    for command in sent
                )
            )
        return sent

    # ------------------------------------------------------------- the ladder

    def backoff_step(self, device_id) -> int:
        """How many un-landed commands this device has had. 0 means none."""
        backoff = self._backoff.get(device_id)
        return backoff.step if backoff else 0

    def next_due(self, device_id):
        """The pass at which this device may be commanded again, or None."""
        backoff = self._backoff.get(device_id)
        return backoff.next_due if backoff else None

    def is_parked(self, device_id) -> bool:
        """Past the whole ladder, so only retried by the wall clock.

        Read by the engine (:meth:`Engine._light_changed`): any report from a
        parked device is evidence it is alive again, which is not true of a
        device still partway up the ladder -- a ramping dimmer reports every
        intermediate level on its own, and un-parking it there would turn the
        ladder into a command storm.
        """
        backoff = self._backoff.get(device_id)
        return backoff is not None and backoff.step > len(BACKOFF_TICKS)

    def forget(self, device_id=None) -> None:
        """Drop one device's backoff, or all of it (a config reload)."""
        if device_id is None:
            for known_id in list(self._backoff):
                self._clear_backoff(known_id)
            self._backoff.clear()
        else:
            self._clear_backoff(device_id)

    def _advance_backoff(self, device_id, this_pass, now, level) -> int:
        backoff = self._backoff.get(device_id)
        if backoff is None:
            backoff = _Backoff()
            self._backoff[device_id] = backoff
        delay = BACKOFF_TICKS[min(backoff.step, len(BACKOFF_TICKS) - 1)]
        backoff.step += 1
        backoff.next_due = this_pass + delay
        backoff.level = level
        backoff.sent_at = now
        if backoff.step > len(BACKOFF_TICKS):
            # The whole ladder has been walked. From here the pass-counted
            # `next_due` above is academic -- passes are one counter shared
            # by every zone, so it can be reached in seconds -- and
            # `not_before` is what actually governs the next retry.
            backoff.not_before = now + dt.timedelta(seconds=PARKED_RETRY_SECONDS)
        return backoff.step

    def _clear_backoff(self, device_id) -> None:
        """Landing on desired resets the ladder, with no log line.

        Silently, because a device that recovers is not news; and completely,
        because a device kept on a slow retry schedule after it started
        answering is a device that takes eight ticks to notice the next
        change. The warning key goes with it, so a *later* failure of the
        same device is reported afresh rather than latched into silence --
        and the key is the one for the level the ladder was about, which is
        why the entry is read before it is dropped.
        """
        backoff = self._backoff.pop(device_id, None)
        if backoff is not None:
            compare.reset_warnings(backoff_key(device_id, backoff.level))
        compare.reset_warnings(unreadable_key(device_id))

    # ------------------------------------------------------------ the warnings

    def _warn_backoff(self, zone, device, actual, level, step) -> None:
        parked_minutes = PARKED_RETRY_SECONDS / 60
        compare.warn_once(
            self.logger,
            backoff_key(device.id, level),
            f"{zone.name}: {device.name} ({device.id}) did not reach its desired "
            f"level. It reads {actual!r} and the zone wants {level!r}, so it is "
            f"being commanded again on a backoff of "
            f"{'/'.join(str(tick) for tick in BACKOFF_TICKS)} ticks and, once "
            f"that ladder is walked, every {parked_minutes:g} minutes after "
            "that by the wall clock. This is not a device the plugin has given "
            "up on -- it is one worth looking at -- and any change in what it "
            "reports, even one still off desired, brings the next attempt "
            "forward to right away.",
        )

    def _warn_unreadable(self, zone, device, exc) -> None:
        compare.warn_once(
            self.logger,
            unreadable_key(device.id),
            f"{zone.name}: {device.name} ({device.id}) cannot be read "
            f"({exc.reason}). It is still commanded -- the plugin cannot tell "
            "whether it is already where it should be, so it sends the command "
            "anyway -- but it gets no record of the state it was commanded away "
            "from, so a delayed report of that command may be read as a manual "
            "override, and it is excluded from override detection until it can "
            "be read.",
        )

    @staticmethod
    def _actual(device):
        """What the device says it is, or None if it cannot say. Never a guess."""
        try:
            return compare.reading(device)
        except compare.UnreadableDevice:
            return None
