"""Zone state across a restart and a config reload (PRD R13; sections 5.10, 5.11).

The fork announced "all locks and zone state has been reset" on every single
reload, which meant an override created at 19:46 was gone by 19:50 because
somebody edited an unrelated zone. Three things survive here instead:
``presence_last_seen``, the override, and the dark verdict.

Each is persisted for a reason that is not obvious until it is missing:

* **presence_last_seen** -- without it a restart in an occupied room starts
  from "nobody has ever been here" and turns the lights off on the person
  standing in it. It is the only half of presence that is persisted: which
  sensors are reporting *now* is deliberately NOT written down, because it is
  a fact about the room rather than about the plugin, and a stale set read
  off a device would hold a zone occupied on a sensor that had gone off while
  the plugin was stopped. ``Engine._seed_zone`` rebuilds it from the devices
  at startup instead. (A config *reload* is different: the plugin never
  stopped, so ``rebuild_zone`` carries the live set across.)
* **the override** -- the point of R13. It carries its own
  ``duration_minutes`` and ``extend_minutes`` because the period that created
  it may not be the period it expires in (section 11, decision 4).
* **dark** -- the Schmitt trigger's memory *is* the verdict (R9). A zone that
  comes back with no verdict decides the first reading inside the hysteresis
  band on the wrong side of it, and a kitchen with its lights on reads inside
  that band all evening.

Everything is written as strings and numbers, because Indigo device states
hold nothing else (PRD section 9), timestamps in one fixed ISO format, and
the whole record carries a ``version``.

**Reading is tolerant, one field at a time.** Missing, garbled or
future-versioned data warns and is skipped -- the field, not the record.
Refusing the whole record over one bad timestamp throws away a perfectly good
override, and silently accepting it invents state that was never true. The
complaints are returned as well as logged, so a caller can put them in a
payload rather than leaving them only in the log (R15).
"""

from __future__ import annotations

import datetime as dt
import logging

from .zone import Override, Zone

#: The record's own version. Bumped only when the field meanings change.
VERSION = 1

_ISO = "%Y-%m-%dT%H:%M:%S"


def to_persisted(zone: Zone) -> dict:
    """The zone's surviving state as a flat record of strings and numbers."""
    override = zone.override
    return {
        "version": VERSION,
        "presence_last_seen": _iso(zone.presence.last_seen),
        "dark": zone.lux.verdict,
        "override_device": override.device_id if override else "",
        "override_since": _iso(override.since) if override else "",
        "override_expires": _iso(override.expires_at) if override else "",
        "override_extended_count": override.extended_count if override else 0,
        "override_duration_minutes": (
            override.duration_minutes if override else zone.config.override.duration_minutes
        ),
        "override_extend_minutes": (
            override.extend_minutes if override else zone.config.override.extend_minutes
        ),
    }


def apply_persisted(zone: Zone, data, now: dt.datetime, logger=None) -> list:
    """Restore what survives onto ``zone``. Returns the complaints, if any.

    An empty list means the record was applied whole. Anything in it is a
    field that was missing or unusable and was skipped, named so that the
    condition is visible in a payload and not only in the log.
    """
    logger = logger or getattr(zone, "logger", None) or logging.getLogger("Plugin")
    complaints = []

    def complain(message):
        complaints.append(message)
        logger.warning(f"{zone.name}: persisted state -- {message}")

    if not isinstance(data, dict):
        complain(
            f"the record is {type(data).__name__}, not an object; nothing was "
            "restored and the zone starts from its configuration"
        )
        return complaints

    version = data.get("version")
    if version is None:
        complain("no version key; reading it as version 1 and taking what parses")
    elif version != VERSION:
        complain(
            f"version {version!r}, and this plugin writes version {VERSION}; the "
            "fields it recognises are restored individually and anything it does "
            "not understand is left alone"
        )

    last_seen = _read_time(data, "presence_last_seen", complain)
    if last_seen is not None:
        if last_seen > now:
            # A clock change, or a record written by a machine ahead of this
            # one. The claim "presence was seen" is still good; only the when
            # is broken, so it is clamped rather than thrown away -- the cost
            # of being wrong is one hold period of lights, and the cost of
            # discarding it is the lights going off on somebody.
            complain(
                f"presence_last_seen is {last_seen:%Y-%m-%d %H:%M:%S}, which is in "
                f"the future; clamped to now ({now:%Y-%m-%d %H:%M:%S})"
            )
            last_seen = now
        zone.presence.last_seen = last_seen

    dark = data.get("dark")
    if dark is not None:
        if isinstance(dark, bool):
            zone.lux.seed(dark)
        else:
            complain(f"dark is {dark!r}, not true or false; the verdict starts unset")

    _apply_override(zone, data, now, complain)
    return complaints


def _apply_override(zone, data, now, complain):
    """Restore the override, or explain why there is none to restore."""
    raw_device = data.get("override_device")
    if raw_device in (None, "", 0):
        return

    device_id = _read_int(data, "override_device", complain)
    expires_at = _read_time(data, "override_expires", complain)
    if device_id is None or expires_at is None:
        complain(
            "the override record is incomplete (it needs both a device and an "
            "expiry); no override was restored, so the zone resumes its normal "
            "levels rather than holding a lock it cannot describe"
        )
        return

    since = _read_time(data, "override_since", complain)
    if since is None:
        # unlock_on_leave compares the presence hold's expiry against this,
        # so a missing 'since' is not cosmetic. Now is the conservative
        # choice: it makes the override look freshly taken, which at worst
        # delays an unlock-on-leave release by one hold.
        complain(f"the override has no usable start time; using now ({now:%H:%M:%S})")
        since = now

    zone.override = Override(
        device_id=device_id,
        since=since,
        expires_at=expires_at,
        extended_count=_read_int(data, "override_extended_count", complain, default=0),
        duration_minutes=_read_int(
            data,
            "override_duration_minutes",
            complain,
            default=zone.config.override.duration_minutes,
        ),
        extend_minutes=_read_int(
            data,
            "override_extend_minutes",
            complain,
            default=zone.config.override.extend_minutes,
        ),
    )


def rebuild_zone(old_zone: Zone, new_config, now: dt.datetime) -> Zone:
    """A zone built from new configuration, carrying the old one's state.

    This is the whole of the hot reload (section 5.11): the zone object is
    rebuilt from the file, then the state is put back. What comes back is
    everything that is a fact about the house rather than a fact about the
    file -- the override, presence, the dark verdict, the day's counters, and
    the global enable, which lives on the controller device and not in this
    file.

    ``enabled`` deliberately does *not* carry: a config edit that switches a
    zone off has to take effect, or the file stops meaning anything. Neither
    does the lux reading when the sensor has been changed, because it is a
    reading from a different device.
    """
    fresh = Zone(new_config, old_zone.sun, clock=old_zone.clock, logger=old_zone.logger)
    apply_persisted(fresh, to_persisted(old_zone), now, logger=old_zone.logger)

    # Live bookkeeping the persisted record does not carry, because it is
    # rebuilt from the device bus rather than from disk -- but throwing it
    # away across a reload would re-announce presence that never stopped.
    fresh.presence.on_devices = {
        dev_id for dev_id in old_zone.presence.on_devices if dev_id in new_config.presence_devices
    }
    fresh.plugin_enabled = old_zone.plugin_enabled
    fresh.state = old_zone.state
    fresh.last_trigger = old_zone.last_trigger
    fresh.evaluations_today = old_zone.evaluations_today
    fresh.writes_today = old_zone.writes_today
    fresh.overrides_today = old_zone.overrides_today
    fresh._counters_date = old_zone._counters_date

    same_sensor = (
        old_zone.config.lux is not None
        and new_config.lux is not None
        and old_zone.config.lux.device == new_config.lux.device
    )
    if same_sensor:
        fresh.lux.value = old_zone.lux.value
        fresh.lux.read_at = old_zone.lux.read_at
        fresh.lux.unreadable = old_zone.lux.unreadable
        fresh.lux.reason = old_zone.lux.reason

    return fresh


# ------------------------------------------------------------------ readers


def _read_time(data, key, complain):
    """A timestamp from the record, or None with a complaint."""
    raw = data.get(key)
    if raw in (None, ""):
        return None
    if isinstance(raw, dt.datetime):
        return raw
    try:
        return dt.datetime.strptime(str(raw), _ISO)
    except (TypeError, ValueError):
        complain(f"{key} is {raw!r}, which is not a {_ISO} timestamp; it was skipped")
        return None


def _read_int(data, key, complain, default=None):
    """An integer from the record, or ``default`` with a complaint."""
    raw = data.get(key)
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        complain(f"{key} is {raw!r}, not a number; using {default!r}")
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        complain(f"{key} is {raw!r}, which is not a number; using {default!r}")
        return default


def _iso(when):
    return when.strftime(_ISO) if when else ""
