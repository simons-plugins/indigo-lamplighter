"""Zone state onto Indigo device states, and back again (PRD 5.10, 5.11).

The whole of the mapping between what the engine knows and what an Indigo
device carries, kept here rather than in ``plugin.py`` for one reason: it is
the part with decisions in it, and decisions have to be testable without a
server. ``plugin.py`` calls these four functions and does no thinking of its
own about keys, formats or types.

**Every value published here is a string, a number or a bool**, because
Indigo device states hold nothing else (PRD section 9), and every state
carries a ``uiValue`` so the device list reads as English rather than as a
timestamp with a ``T`` in it.

**Nothing is ever published as a quiet zero.** ``lux`` is declared a *string*
state, not a number, precisely so that "this sensor has never been read" can
be published as ``""`` instead of ``0`` -- a numeric state has no way to say
"no value", and a lux of 0 for a sensor that has never answered is
indistinguishable from a pitch-dark room (R15). The number is still there for
anybody who wants it, formatted with ``%g`` so 1800.0 reads as ``1800``, and
``dark`` -- which is the verdict a trigger should actually gate on -- is a
real boolean.

**The persisted record rides along as device states** with a ``persist_``
prefix (section 5.10: "Persisted across restarts"). That is what makes an
override taken at 19:46 survive a plugin restart at 19:50 without a second
file to write, fsync and corrupt. The encoding is deliberately explicit in
both directions:

* ``dark`` is three-valued -- True, False and *never decided* -- so it is
  written as ``"true"``, ``"false"`` and ``""`` rather than as a boolean
  state, where "unset" would arrive back as False and seed a verdict the
  zone never took (see :mod:`lamplighter.persist` on why the Schmitt
  trigger's memory matters).
* a state that was never written comes back as ``""`` and is **left out of
  the record entirely**, so :func:`lamplighter.persist.apply_persisted` sees
  a missing field rather than an empty one and says nothing about it.
* anything unparseable is passed through *unchanged*. Decoding never raises
  and never guesses: ``persist`` already reads one field at a time and names
  what it skipped, and that is where a garbled value should be reported.
"""

from __future__ import annotations

#: The zone device's live states, in the order PRD section 5.10 lists them.
#: This tuple, ``zone.snapshot()``'s keys and the ``<State>`` ids in
#: Devices.xml are pinned equal by ``tests/test_indigo_sync.py`` -- a state
#: that exists in one and not the others is either invisible in Indigo or an
#: update Indigo rejects, and neither shows up at runtime as anything but a
#: device that quietly stopped moving.
ZONE_STATE_KEYS = (
    "state",
    "presence_active",
    "presence_last_seen",
    "lux",
    "dark",
    "period",
    "override_device",
    "override_expires",
    "desired_summary",
    "explain",
    "evaluations_today",
    "writes_today",
    "overrides_today",
    "last_trigger",
)

#: What separates a persisted field from a live one on the same device.
PERSIST_PREFIX = "persist_"

#: The fields :func:`lamplighter.persist.to_persisted` writes.
PERSIST_KEYS = (
    "version",
    "presence_last_seen",
    "dark",
    "override_device",
    "override_since",
    "override_expires",
    "override_extended_count",
    "override_duration_minutes",
    "override_extend_minutes",
)

PERSIST_STATE_KEYS = tuple(PERSIST_PREFIX + key for key in PERSIST_KEYS)

#: Every state the ``lamplighter_zone`` device type declares.
ZONE_DEVICE_STATE_KEYS = ZONE_STATE_KEYS + PERSIST_STATE_KEYS

#: The ``lamplighter_controller`` device: the counters summed, and whether
#: the configuration file is actually the one running (section 5.10).
CONTROLLER_STATE_KEYS = (
    "zones",
    "zones_enabled",
    "zones_overridden",
    "evaluations_today",
    "writes_today",
    "overrides_today",
    "config_status",
    "config_loaded_at",
    "config_zone_count",
)

#: Persisted fields that are numbers. Everything else is a timestamp string
#: or ``dark``, and both are read by ``persist`` rather than here.
_PERSIST_INT_KEYS = (
    "version",
    "override_device",
    "override_extended_count",
    "override_duration_minutes",
    "override_extend_minutes",
)

#: The device id :mod:`lamplighter.engine` records for a `lock zone` action.
_MANUAL_LOCK_DEVICE_ID = -1


# ------------------------------------------------------------- the live states


def states_for_zone(snapshot) -> list:
    """One ``updateStatesOnServer`` payload from ``zone.snapshot()``.

    Iterates :data:`ZONE_STATE_KEYS` rather than the snapshot, so the payload
    can never contain a key the device type does not declare -- Indigo logs
    an error for those and drops the whole update. A key the snapshot does
    not carry is published as the empty string, which is this codebase's
    "there is no value": it cannot raise, and it cannot invent a reading.
    """
    return [_zone_state(key, snapshot.get(key, "")) for key in ZONE_STATE_KEYS]


def _zone_state(key, value) -> dict:
    if key in ("presence_active", "dark"):
        return {"key": key, "value": bool(value), "uiValue": "yes" if value else "no"}

    if key in ("evaluations_today", "writes_today", "overrides_today"):
        count = _as_int(value, 0)
        return {"key": key, "value": count, "uiValue": str(count)}

    if key == "state":
        text = str(value or "")
        return {"key": key, "value": text, "uiValue": text.replace("_", " ") or "unknown"}

    if key == "lux":
        if value in (None, ""):
            # Never 0: see the module docstring. A sensor that has not been
            # read says so, in the state and in the device list.
            return {"key": key, "value": "", "uiValue": "unknown"}
        number = _as_float(value)
        if number is None:
            return {"key": key, "value": str(value), "uiValue": str(value)}
        return {"key": key, "value": f"{number:g}", "uiValue": f"{number:g} lux"}

    if key == "override_device":
        if value in (None, ""):
            return {"key": key, "value": "", "uiValue": "none"}
        if _as_int(value, None) == _MANUAL_LOCK_DEVICE_ID:
            return {"key": key, "value": str(value), "uiValue": "manual lock"}
        return {"key": key, "value": str(value), "uiValue": str(value)}

    if key in ("presence_last_seen", "override_expires"):
        text = str(value or "")
        return {"key": key, "value": text, "uiValue": _clock(text) or "never"}

    text = str(value or "")
    return {"key": key, "value": text, "uiValue": text or "none"}


def controller_states(
    engine, config_status="ok", config_loaded_at="", config_zone_count=0
) -> list:
    """The controller device's states: the zones counted, the counters summed.

    ``config_status`` is here rather than derived because the engine has no
    idea a file exists. It is the one place a person can see that the plugin
    is running yesterday's configuration because today's does not parse --
    the ERROR in the log scrolls away, this does not.

    ``config_loaded_at`` and ``config_zone_count`` are passed in for the same
    reason and one more: they describe **the last successful load**, not the
    engine as it stands. Deriving the count from ``engine.zones`` would make
    it a live number that happens to agree, and the pair has to be a record
    of one event -- "the configuration I am running was loaded at T and had N
    zones" -- so that a caller reading both gets one consistent answer.

    They exist because every other state here moves on an ordinary worker
    pass, so a caller watching for "the file was reloaded" has nothing to
    watch. These two move only when a load succeeds.
    """
    zones = list(engine.zones.values())
    counts = {
        "zones": len(zones),
        "zones_enabled": sum(1 for zone in zones if zone.running),
        "zones_overridden": sum(1 for zone in zones if zone.override is not None),
        "evaluations_today": sum(zone.evaluations_today for zone in zones),
        "writes_today": sum(zone.writes_today for zone in zones),
        "overrides_today": sum(zone.overrides_today for zone in zones),
    }
    states = [
        {"key": key, "value": value, "uiValue": str(value)} for key, value in counts.items()
    ]
    status = str(config_status or "ok")
    states.append({"key": "config_status", "value": status, "uiValue": status})
    # "" rather than a made-up timestamp: nothing has loaded yet is a real
    # answer, and a plausible-looking time would be the quiet lie R15 forbids.
    loaded_at = str(config_loaded_at or "")
    states.append(
        {
            "key": "config_loaded_at",
            "value": loaded_at,
            "uiValue": loaded_at or "never",
        }
    )
    count = int(config_zone_count or 0)
    states.append({"key": "config_zone_count", "value": count, "uiValue": str(count)})
    return states


# -------------------------------------------------------- the persisted record


def persist_to_states(persisted) -> list:
    """A :func:`lamplighter.persist.to_persisted` record as device states."""
    return [
        {
            "key": PERSIST_PREFIX + key,
            "value": _encode(persisted.get(key)),
            "uiValue": _encode(persisted.get(key)) or "none",
        }
        for key in PERSIST_KEYS
    ]


def states_to_persisted(dev_states) -> dict:
    """The record a zone device is carrying, ready for ``apply_persisted``.

    Returns ``{}`` for a device that has never been written, which is the
    signal to skip the restore entirely rather than hand ``persist`` an empty
    record and make it complain about a missing version on every first start.

    Never raises. A value that will not decode is passed through as it was
    found, because the field-by-field complaints in
    :func:`lamplighter.persist.apply_persisted` are the right place for a
    garbled value to be reported -- and losing the whole record over one bad
    timestamp throws away a perfectly good override.
    """
    record = {}
    for key in PERSIST_KEYS:
        raw = _read_state(dev_states, PERSIST_PREFIX + key)
        if raw is None or raw == "":
            continue
        record[key] = _decode(key, raw)
    return record


def _read_state(dev_states, key):
    try:
        return dev_states.get(key)
    except Exception:
        # A states mapping that will not answer is not a reason to take the
        # restore down; the field is simply not there.
        return None


def _encode(value):
    """A persisted value as a device state: strings, and "" for absent."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _decode(key, raw):
    """One persisted value back from a device state. Never raises."""
    if key == "dark":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        return raw
    if key in _PERSIST_INT_KEYS:
        return _as_int(raw, raw)
    return raw


# --------------------------------------------------------------------- helpers


def _as_int(value, default):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clock(iso_timestamp) -> str:
    """``2026-09-04T19:46:03`` as ``19:46:03``; anything else unchanged."""
    if not iso_timestamp:
        return ""
    _, separator, time_part = iso_timestamp.partition("T")
    return time_part if separator else iso_timestamp
