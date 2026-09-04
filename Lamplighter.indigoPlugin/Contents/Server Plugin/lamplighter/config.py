"""Loading and validating ``lamplighter.json`` (PRD R15; section 5.11).

The bundled ``schema.json`` is the contract, and this module is the half of
it that runs: the plugin is stdlib only, so it cannot import jsonschema at
runtime and instead re-states the same constraints here. That duplication is
deliberate and it is held honest by ``tests/test_config_matches_schema.py``,
which feeds every invalid document the schema tests reject through this
loader and requires the same failing path.

Three properties matter more than the field list:

* **A rejected config is rejected whole.** There is no partial load, no
  falling back to a default for the bad field, no "load the zones that did
  parse". A half-applied config is the state nobody can reason about, and the
  MCP tool that made the edit has to be able to hand the error back verbatim.
  Because nothing is mutated on the way in, a caller that keeps the config it
  already had keeps it intact.

* **Every error carries a path.** ``zones/1/periods/2/levels/144694384`` is
  what turns "invalid config" into a value an author can go and fix. First
  error wins: reporting the first one with its path beats reporting five with
  none.

* **An unknown device id is not a config error.** Ids are not resolved here.
  A device that has been deleted, or an Indigo lookup that is failing right
  now, is a runtime condition the engine warns about once while the zone
  keeps running for its other lights (R8, R15) -- refusing to load the whole
  file over it would take out five working zones for one dead bulb.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .periods import (
    ONE_DAY,
    ConfigError,
    Period,
    SunProvider,
    check_overlaps,
    parse_time_expr,
)

# Re-exported: ConfigError is defined in periods so that check_overlaps can
# raise it without importing this module. It is one class either way.
__all__ = [
    "Config",
    "ConfigError",
    "LuxConfig",
    "OverrideConfig",
    "Period",
    "PeriodOverride",
    "ZoneConfig",
    "load_config",
    "validation_dates",
]

_DEVICE_KEY_RE = re.compile(r"^[1-9][0-9]*$")

_ROOT_KEYS = ("version", "reconcile_seconds", "echo_window_seconds", "zones")
_ZONE_REQUIRED = ("name", "presence_devices", "hold_seconds", "lux", "lights", "periods")
_ZONE_KEYS = _ZONE_REQUIRED + ("enabled", "override")
_LUX_REQUIRED = ("device", "dark_below")
_LUX_KEYS = _LUX_REQUIRED + ("dark_below_variable_id", "hysteresis", "when_unreadable")
_ZONE_OVERRIDE_KEYS = (
    "enabled",
    "duration_minutes",
    "extend_minutes",
    "unlock_on_leave",
    "exclude",
)
_PERIOD_REQUIRED = ("name", "from", "to", "mode", "levels")
_PERIOD_KEYS = _PERIOD_REQUIRED + ("limit", "adjust_by_lux", "override")
_PERIOD_OVERRIDE_KEYS = ("duration_minutes", "extend_minutes")
_MODES = ("on_and_off", "off_only")
_WHEN_UNREADABLE = ("dark", "bright")
_LEVEL_WORDS = ("on", "off", "leave")


# ---------------------------------------------------------------- the shapes


@dataclass(frozen=True)
class LuxConfig:
    """A zone's daylight gate (R9). A zone with no gate has ``lux`` of None."""

    device: int
    dark_below: float
    dark_below_variable_id: int | None = None
    hysteresis: float = 0.0
    when_unreadable: str = "dark"


@dataclass(frozen=True)
class OverrideConfig:
    """How a manual override is held and released for a zone (R10)."""

    enabled: bool = True
    duration_minutes: int = 60
    extend_minutes: int = 0
    unlock_on_leave: bool = True
    exclude: tuple[int, ...] = ()


@dataclass(frozen=True)
class PeriodOverride:
    """Override timing that REPLACES the zone's while a period is active.

    Both fields are required by the schema precisely so that this is a
    replacement and not a merge: a half-specified block would leave which
    value applies depending on which fields someone happened to write (PRD
    section 11, decision 4).
    """

    duration_minutes: int
    extend_minutes: int


@dataclass(frozen=True)
class ZoneConfig:
    """One lighting zone as configured, before any device is looked up."""

    name: str
    presence_devices: tuple[int, ...]
    hold_seconds: int
    lux: LuxConfig | None
    lights: tuple[int, ...]
    periods: tuple[Period, ...]
    enabled: bool = True
    override: OverrideConfig = field(default_factory=OverrideConfig)


@dataclass(frozen=True)
class Config:
    """A whole ``lamplighter.json``, validated."""

    version: int
    zones: tuple[ZoneConfig, ...]
    reconcile_seconds: int = 60
    echo_window_seconds: int = 15


# ------------------------------------------------------------- validators
#
# Small and boring on purpose. Each one names the value it rejected and the
# rule it broke, and each takes the path it is standing at, because an error
# without a path is a config author reading a 300-line file by eye.


def _fail(path, message):
    raise ConfigError(message, path=path)


def _kind(value):
    return {
        type(None): "null",
        bool: "a boolean",
        int: "an integer",
        float: "a number",
        str: "a string",
        list: "an array",
        dict: "an object",
    }.get(type(value), f"a {type(value).__name__}")


def _object(value, path, required=(), allowed=()):
    if not isinstance(value, dict):
        _fail(path, f"expected an object, got {_kind(value)}")
    for key in required:
        if key not in value:
            _fail(path, f"missing required key {key!r} (required: {', '.join(required)})")
    for key in value:
        if key not in allowed:
            _fail(path, f"{key!r} is not a known key here (allowed: {', '.join(allowed)})")
    return value


def _bool(value, path):
    if not isinstance(value, bool):
        _fail(path, f"expected true or false, got {_kind(value)}")
    return value


def _int(value, path, minimum=None, maximum=None, what="an integer"):
    # JSON Schema's "integer" accepts an integer-valued float -- 60.0 is 60 --
    # so this does too. Anything the schema blesses and the loader refuses is
    # a config an MCP edit can validate and then fail to load.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, f"expected {what}, got {_kind(value)}")
    if minimum is not None and value < minimum:
        _fail(path, f"{value} is below the minimum of {minimum}")
    if maximum is not None and value > maximum:
        _fail(path, f"{value} is above the maximum of {maximum}")
    return value


def _number(value, path, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, f"expected a number, got {_kind(value)}")
    if minimum is not None and value < minimum:
        _fail(path, f"{value} is below the minimum of {minimum}")
    return value


def _string(value, path, min_length=1, max_length=None, choices=None):
    if not isinstance(value, str):
        _fail(path, f"expected a string, got {_kind(value)}")
    if choices is not None and value not in choices:
        _fail(path, f"{value!r} is not one of: {', '.join(choices)}")
    if len(value) < min_length:
        _fail(path, f"is empty; at least {min_length} character(s) required")
    if max_length is not None and len(value) > max_length:
        _fail(path, f"is {len(value)} characters; the maximum is {max_length}")
    return value


def _array(value, path, min_items=0, unique=False):
    if not isinstance(value, list):
        _fail(path, f"expected an array, got {_kind(value)}")
    if len(value) < min_items:
        _fail(path, f"has {len(value)} entries; at least {min_items} required")
    if unique:
        seen = []
        for item in value:
            if item in seen:
                _fail(path, f"{item!r} appears more than once; entries must be unique")
            seen.append(item)
    return value


def _device_id(value, path):
    return _int(value, path, minimum=1, what="an Indigo device id (a positive integer)")


def _device_ids(value, path, min_items=0):
    items = _array(value, path, min_items=min_items, unique=True)
    return tuple(_device_id(item, f"{path}/{i}") for i, item in enumerate(items))


def _time_expr(value, path):
    try:
        return parse_time_expr(value)
    except ValueError as exc:
        _fail(path, str(exc))


# ------------------------------------------------------------- the sections


def _level(value, path):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _fail(path, f"expected a level, got {_kind(value)}")
    if isinstance(value, int):
        return _int(value, path, minimum=1, maximum=100, what="a level")
    if value in _LEVEL_WORDS:
        return value
    _fail(
        path,
        f"{value!r} is not a level: expected an integer 1-100, "
        f"{', '.join(repr(word) for word in _LEVEL_WORDS)}",
    )


def _levels(raw, path, lights):
    """The per-device levels of one period, keyed by device id as an int.

    Two rules the schema states and one it cannot: keys look like device ids,
    values are levels, and every key is a light this zone actually owns. The
    last one is here because writing to a device the zone does not own is the
    kind of mistake that only shows up as a light in another room changing.
    """
    if not isinstance(raw, dict):
        _fail(path, f"expected an object of device id to level, got {_kind(raw)}")
    if not raw:
        _fail(path, "is empty; a period must give at least one light a level")

    levels = {}
    for key, value in raw.items():
        where = f"{path}/{key}"
        if not _DEVICE_KEY_RE.search(key):
            _fail(
                where,
                f"{key!r} is not a device id written as a string "
                "(JSON keys are strings, so an id is written \"1894385558\")",
            )
        device_id = int(key)
        if device_id not in lights:
            _fail(
                where,
                f"{device_id} is not one of this zone's lights "
                f"({', '.join(str(light) for light in lights)}); a period can only "
                "give levels to devices the zone owns",
            )
        levels[device_id] = _level(value, where)
    return levels


def _period_override(raw, path):
    _object(raw, path, required=_PERIOD_OVERRIDE_KEYS, allowed=_PERIOD_OVERRIDE_KEYS)
    return PeriodOverride(
        duration_minutes=_int(raw["duration_minutes"], f"{path}/duration_minutes", minimum=1),
        extend_minutes=_int(raw["extend_minutes"], f"{path}/extend_minutes", minimum=0),
    )


def _period(raw, path, lights):
    _object(raw, path, required=_PERIOD_REQUIRED, allowed=_PERIOD_KEYS)
    return Period(
        name=_string(raw["name"], f"{path}/name", max_length=64),
        start=_time_expr(raw["from"], f"{path}/from"),
        end=_time_expr(raw["to"], f"{path}/to"),
        mode=_string(raw["mode"], f"{path}/mode", choices=_MODES),
        limit=(
            None
            if "limit" not in raw
            else _int(raw["limit"], f"{path}/limit", minimum=1, maximum=100)
        ),
        adjust_by_lux=_bool(raw.get("adjust_by_lux", False), f"{path}/adjust_by_lux"),
        override=(
            None if "override" not in raw else _period_override(raw["override"], f"{path}/override")
        ),
        levels=_levels(raw["levels"], f"{path}/levels", lights),
    )


def _lux(raw, path):
    """The zone's daylight gate, or None.

    Required-but-nullable in the schema so that "no daylight gate" is a
    stated decision rather than an omission, and so a mistake inside the
    block is reported at its own path instead of collapsing into "this is not
    a valid lux block".
    """
    if raw is None:
        return None
    _object(raw, path, required=_LUX_REQUIRED, allowed=_LUX_KEYS)
    return LuxConfig(
        device=_device_id(raw["device"], f"{path}/device"),
        dark_below=_number(raw["dark_below"], f"{path}/dark_below", minimum=0),
        dark_below_variable_id=(
            None
            if "dark_below_variable_id" not in raw
            else _int(
                raw["dark_below_variable_id"],
                f"{path}/dark_below_variable_id",
                minimum=1,
                what="an Indigo variable id (a positive integer)",
            )
        ),
        hysteresis=_number(raw.get("hysteresis", 0), f"{path}/hysteresis", minimum=0),
        when_unreadable=_string(
            raw.get("when_unreadable", "dark"),
            f"{path}/when_unreadable",
            choices=_WHEN_UNREADABLE,
        ),
    )


def _zone_override(raw, path):
    _object(raw, path, allowed=_ZONE_OVERRIDE_KEYS)
    return OverrideConfig(
        enabled=_bool(raw.get("enabled", True), f"{path}/enabled"),
        duration_minutes=_int(
            raw.get("duration_minutes", 60), f"{path}/duration_minutes", minimum=1
        ),
        extend_minutes=_int(raw.get("extend_minutes", 0), f"{path}/extend_minutes", minimum=0),
        unlock_on_leave=_bool(raw.get("unlock_on_leave", True), f"{path}/unlock_on_leave"),
        exclude=_device_ids(raw.get("exclude", []), f"{path}/exclude"),
    )


def _zone(raw, path, sun, today):
    # Fields are checked in the order the schema declares them, so that the
    # first error a config author sees is the earliest thing wrong in the
    # file rather than whichever check happened to run first.
    _object(raw, path, required=_ZONE_REQUIRED, allowed=_ZONE_KEYS)
    name = _string(raw["name"], f"{path}/name", max_length=64)
    enabled = _bool(raw.get("enabled", True), f"{path}/enabled")
    presence_devices = _device_ids(
        raw["presence_devices"], f"{path}/presence_devices", min_items=1
    )
    hold_seconds = _int(raw["hold_seconds"], f"{path}/hold_seconds", minimum=0, maximum=86400)
    lux = _lux(raw["lux"], f"{path}/lux")
    lights = _device_ids(raw["lights"], f"{path}/lights", min_items=1)
    override = _zone_override(raw.get("override", {}), f"{path}/override")
    periods = tuple(
        _period(item, f"{path}/periods/{i}", lights)
        for i, item in enumerate(_array(raw["periods"], f"{path}/periods", min_items=1))
    )
    # The cross-field rule the schema cannot express: no two periods may
    # cover the same minute (R11). Checked last, so a period with a broken
    # time expression is reported as that, not as a mysterious overlap.
    check_overlaps(periods, sun, validation_dates(today), path=f"{path}/periods")

    return ZoneConfig(
        name=name,
        presence_devices=presence_devices,
        hold_seconds=hold_seconds,
        lux=lux,
        lights=lights,
        periods=periods,
        enabled=enabled,
        override=override,
    )


# ------------------------------------------------------------------ loading


def validation_dates(today: dt.date) -> list:
    """The dates :func:`check_overlaps` samples for a zone.

    Today and tomorrow catch every wall-clock overlap, because those repeat
    daily. The solstices and equinoxes catch the ones that only exist for
    part of the year: a period bounded by "sunset+30m" sits clear of its
    neighbour in September and lands inside it in December. Nominal dates are
    close enough -- these are sampling points for the extremes of the sun's
    swing, not an almanac.
    """
    year = today.year
    return [
        today,
        today + ONE_DAY,
        dt.date(year, 3, 20),
        dt.date(year, 6, 21),
        dt.date(year, 9, 22),
        dt.date(year, 12, 21),
    ]


def _read(source):
    """Get the raw document from a path or take the dict as given."""
    if isinstance(source, dict):
        return source
    if not isinstance(source, (str, os.PathLike)):
        _fail("", f"expected a file path or a dict, got {_kind(source)}")

    try:
        text = Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        _fail("", f"{source} could not be read: {exc.strerror}. Nothing was loaded.")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        _fail(
            "",
            f"{source} is not valid JSON: {exc.msg} at line {exc.lineno} column "
            f"{exc.colno}. Nothing was loaded -- a half-written save is refused "
            "whole, not read as far as it parses.",
        )


def load_config(source, sun: SunProvider, today: dt.date) -> Config:
    """Load and validate a configuration, or raise :class:`ConfigError`.

    ``source`` is a path to the JSON file or an already-parsed dict (the
    shape an MCP edit arrives in). ``sun`` and ``today`` are needed because
    the overlap check has to resolve sun-relative period edges against real
    dates.

    Raises :class:`ConfigError` -- carrying ``.path``, the JSON-pointer-like
    trail to the offending value -- and returns nothing at all on failure.
    Device ids are deliberately not resolved: an id that no longer exists is
    a runtime warning, not a reason to refuse the file (R15).
    """
    doc = _read(source)
    _object(doc, "", required=("version", "zones"), allowed=_ROOT_KEYS)

    version = doc["version"]
    if isinstance(version, bool) or version != 1:
        _fail(
            "version",
            f"{version!r} is not 1; this loader reads version 1 files only, and "
            "refuses any other rather than guessing at the shape",
        )

    reconcile_seconds = _int(doc.get("reconcile_seconds", 60), "reconcile_seconds", minimum=10)
    echo_window_seconds = _int(
        doc.get("echo_window_seconds", 15), "echo_window_seconds", minimum=1, maximum=120
    )

    zones = []
    seen = {}
    for index, raw in enumerate(_array(doc["zones"], "zones", min_items=1)):
        zone = _zone(raw, f"zones/{index}", sun, today)
        if zone.name in seen:
            _fail(
                f"zones/{index}/name",
                f"{zone.name!r} is already the name of zone {seen[zone.name]}; zone "
                "names must be unique because state is persisted under the name",
            )
        seen[zone.name] = index
        zones.append(zone)

    return Config(
        version=1,
        zones=tuple(zones),
        reconcile_seconds=reconcile_seconds,
        echo_window_seconds=echo_window_seconds,
    )
