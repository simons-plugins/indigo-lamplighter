#!/usr/bin/env python3
"""One-way converter: Auto Lights ``auto_lights_conf.json`` -> ``lamplighter.json``.

Migration step 2 (PRD section 6). It reads the fork's live configuration and
emits a Lamplighter one with every zone ``enabled: false``, so that both
engines can sit installed side by side while zones are moved across one at a
time and never both drive the same lights.

The two formats are different on purpose (PRD section 3), so this is a
translation and not a re-serialisation. Three of those differences are worth
knowing before reading the code:

* **Auto Lights resolves overlapping periods by list order; Lamplighter
  refuses overlaps outright (R11).** A zone whose ladder is "Evening 19:00 to
  midnight" plus "All Day Zone" works in the fork because the first match
  wins, and would not load here at all. So a period is trimmed against every
  period listed before it in ``lighting_period_ids``, which is exactly the
  band the fork actually ran it for. The trim is reported, never silent.

* **The fork's period test is ``from <= now <= to`` with no midnight wrap**,
  which means a band written "19:00 to 00:00" never fired. Lamplighter reads
  the same pair as a band that crosses midnight -- almost certainly what the
  author meant, but a behaviour change, so it is reported.

* **A ``device_period_map`` cell of ``false`` means two different things in
  the fork** depending on the zone's ``off_lights_behavior``: force the light
  off, or leave it alone. Lamplighter has separate ``"off"`` and ``"leave"``
  levels (R12), so the pairing is resolved here, per zone, once.

Everything dropped or approximated is reported -- to stderr, and as the
second element of :func:`convert`'s return value. A converter that quietly
lost a setting would be discovered as a dark room, so the report is the
product as much as the JSON is (R15).

Usage::

    python3 tools/convert_autolights_config.py auto_lights_conf.json \\
        [-o lamplighter.json] [--hold 300] \\
        [--presence "Hallway=33980440,1107013217"]

Exit status: 0 converted and validated, 1 the result does not load through
the plugin's own loader, 2 the input or the arguments are unusable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import types
from pathlib import Path

#: Presence hold applied to every zone. Auto Lights had no per-zone hold --
#: it lived in the Occupatum zone device's off-delay, which is not in this
#: file -- so there is nothing to read and this is an assumption.
DEFAULT_HOLD_SECONDS = 300

#: What Lamplighter's schema defaults ``override.duration_minutes`` to. Used
#: when the fork has nothing usable to say, because the schema's minimum is 1
#: and the fork's own default is 0.
LAMPLIGHTER_DEFAULT_LOCK_MINUTES = 60

FORK_SUFFIX = " Auto Lights"
MINUTES_IN_DAY = 24 * 60
MAX_NAME = 64

MODES = {"On and Off": "on_and_off", "Off Only": "off_only"}

#: The ``off_lights_behavior`` value under which the fork force-offs a light
#: that a period excludes. Matched on the prefix, as the caller specified,
#: because the full string carries a qualifier ("unless zone is locked").
FORCE_OFF_PREFIX = "force off"


class ConversionError(ValueError):
    """Input the converter will not guess at: a bad file, an unknown zone."""


# --------------------------------------------------------------- the sun


class FixedSun:
    """A sun that never moves, for the overlap check the loader runs.

    Indigo is not importable outside the server, and the loader needs *a*
    ``SunProvider`` because a period edge may be sun-relative. Nothing this
    converter emits is sun-relative -- Auto Lights has no sun expressions, and
    turning a fixed "Dusk" band into ``sunset-30m`` is the manual step the
    report names -- so these values are never actually consulted. They are
    fixed and blunt so that if that ever stops being true, the resulting
    period lands somewhere obviously synthetic rather than plausibly wrong.
    """

    SUNRISE = dt.time(6, 30)
    SUNSET = dt.time(19, 45)

    def sunrise(self, date):
        return dt.datetime.combine(date, self.SUNRISE)

    def sunset(self, date):
        return dt.datetime.combine(date, self.SUNSET)


def _loader():
    """Import the plugin's own ``load_config``, from the bundle next door.

    The point of validating with the plugin's loader rather than with
    jsonschema is that the loader is what will actually read the file on
    jarvis, and it is deliberately stricter than the schema in one place
    (``adjust_by_lux``). A file this tool blessed and the plugin then refused
    would be the worst of both.

    ``lamplighter.periods`` imports ``indigo`` at module scope, so a stub
    stands in when there is no server -- but only if nothing has installed a
    real one already, which is what the test suite's conftest does.
    """
    plugin_dir = (
        Path(__file__).resolve().parents[1]
        / "Lamplighter.indigoPlugin"
        / "Contents"
        / "Server Plugin"
    )
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    if "indigo" not in sys.modules:
        sys.modules["indigo"] = types.ModuleType("indigo")

    from lamplighter.config import ConfigError, load_config  # noqa: E402

    return load_config, ConfigError


# ------------------------------------------------------------ small parts


def zone_name(raw) -> str:
    """The fork's zone name with its " Auto Lights" tail removed."""
    name = str(raw or "").strip()
    if name.endswith(FORK_SUFFIX):
        name = name[: -len(FORK_SUFFIX)].strip()
    return name


def parse_presence(specs) -> dict:
    """Parse ``--presence "Zone Name=id,id"`` arguments into {name: [ids]}.

    This is how an Occupatum zone is expanded into the raw sensors behind it
    (PRD section 6, step 4) at conversion time rather than by hand afterwards.
    """
    parsed = {}
    for spec in specs or ():
        if "=" not in spec:
            raise ConversionError(
                f"--presence {spec!r} is not 'Zone Name=id,id': it has no '='."
            )
        name, _, ids = spec.partition("=")
        name = name.strip()
        if not name:
            raise ConversionError(f"--presence {spec!r} names no zone before the '='.")
        devices = []
        for token in ids.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                devices.append(int(token))
            except ValueError:
                raise ConversionError(
                    f"--presence {spec!r}: {token!r} is not an Indigo device id."
                ) from None
        if not devices:
            raise ConversionError(
                f"--presence {spec!r} lists no device ids; a zone must have at least one."
            )
        parsed[name] = devices
    return parsed


def _unique(*groups):
    """The ids in ``groups``, de-duplicated, first appearance order kept."""
    seen = []
    for group in groups:
        for item in group or ():
            if isinstance(item, int) and not isinstance(item, bool) and item not in seen:
                seen.append(item)
    return seen


def _int_or_none(value):
    """``value`` if it is a real int (not a bool, not a string), else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _fmt(minute: int) -> str:
    """A minute-of-day as the schema's ``HH:MM``; 1440 is the end of the day."""
    minute %= MINUTES_IN_DAY
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _end_minutes(hour, minute) -> int:
    """A fork period's ``to`` as an EXCLUSIVE minute-of-day in 1..1440.

    The fork's ``to`` is inclusive and its ladders end at 23:59, meaning "the
    rest of the day"; Lamplighter's is exclusive and writes that as "00:00"
    (midnight at the end of the period). A ``to`` of 00:00 means the same
    thing, so both land on 1440.
    """
    total = int(hour) * 60 + int(minute)
    if total in (0, MINUTES_IN_DAY - 1):
        return MINUTES_IN_DAY
    return total


def _segments(start: int, end: int):
    """The band ``[start, end)`` as half-open segments inside one day.

    ``end <= start`` is a band that crosses midnight and comes back as two.
    """
    if end > start:
        return [(start, end)]
    segments = []
    if start < MINUTES_IN_DAY:
        segments.append((start, MINUTES_IN_DAY))
    if end > 0:
        segments.append((0, end))
    return segments


def _mask(segments):
    day = bytearray(MINUTES_IN_DAY)
    for start, end in segments:
        for minute in range(max(0, start), min(MINUTES_IN_DAY, end)):
            day[minute] = 1
    return day


def _runs(day):
    """The maximal runs of set minutes in ``day``, rejoined across midnight.

    A remainder that touches both ends of the day is one band that wraps, not
    two -- which is how "All Day Zone minus an evening" would come back if the
    evening sat in the middle rather than at the end.
    """
    runs = []
    start = None
    for minute in range(MINUTES_IN_DAY):
        if day[minute] and start is None:
            start = minute
        elif not day[minute] and start is not None:
            runs.append((start, minute))
            start = None
    if start is not None:
        runs.append((start, MINUTES_IN_DAY))

    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == MINUTES_IN_DAY:
        wrapped = (runs[-1][0], runs[0][1])
        runs = [wrapped] + runs[1:-1]
    return runs


# ------------------------------------------------------------- the levels


def _level_for(cell, missing, force_off, where, report):
    """One ``device_period_map`` cell as a Lamplighter level (R12).

    The fork reads a cell four ways: absent and ``true`` both mean "included
    with no explicit level", which its calculation turns into full brightness
    once ``adjust_brightness`` is off; ``false`` means excluded, which is
    force-off or leave-alone depending on the zone; an int 1..100 is the
    level. Lamplighter names all four outcomes directly.
    """
    if cell is missing or cell is True:
        return "on"
    if cell is False:
        return "off" if force_off else "leave"

    value = _int_or_none(cell)
    if value is None:
        report(
            f"{where}: level {cell!r} is neither a boolean nor an integer; Auto "
            "Lights would have ignored it and used its calculated brightness, so "
            "it is written as 'on'."
        )
        return "on"
    if value <= 0:
        return "off"
    if value > 100:
        report(f"{where}: level {value} is above 100; clamped to 100.")
        return 100
    return value


# -------------------------------------------------------------- one zone


def _convert_lux(raw_zone, name, report):
    settings = raw_zone.get("minimum_luminance_settings") or {}
    devices = [d for d in (raw_zone.get("device_settings") or {}).get("luminance_dev_ids") or []]

    if not devices:
        return None
    if len(devices) > 1:
        report(
            f"{name}: {len(devices)} luminance devices "
            f"({', '.join(str(d) for d in devices)}). Auto Lights averaged them; "
            f"Lamplighter has one sensor per zone, so {devices[0]} was used and the "
            "rest were DROPPED."
        )

    minimum = settings.get("minimum_luminance")
    dark_below = minimum if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) else 0
    lux = {"device": devices[0], "dark_below": dark_below}

    variable_id = _int_or_none(settings.get("minimum_luminance_var_id"))
    if settings.get("minimum_luminance_use_variable") and variable_id:
        lux["dark_below_variable_id"] = variable_id
        if minimum is None:
            report(
                f"{name}: the darkness threshold comes from variable {variable_id} and "
                "no fixed minimum_luminance was set, so dark_below is 0 -- the value "
                "Lamplighter falls back to if that variable cannot be read. Set a "
                "sensible fixed value: 0 means 'never dark'."
            )

    hysteresis = settings.get("luminance_hysteresis")
    lux["hysteresis"] = hysteresis if isinstance(hysteresis, (int, float)) and not isinstance(hysteresis, bool) else 0
    lux["when_unreadable"] = "dark"
    return lux


def _convert_override(raw_zone, name, on_lights, plugin_config, report):
    behavior = raw_zone.get("behavior_settings") or {}
    advanced = raw_zone.get("advanced_settings") or {}
    excluded = [d for d in advanced.get("exclude_from_lock_dev_ids") or []]

    # The fork had no per-zone "never lock" switch, so a zone that must never
    # lock was written by excluding every one of its lights. That idiom is a
    # setting here, and keeping the exclusions as well would say the same
    # thing twice in a way a later edit could half-undo.
    never_lock = bool(on_lights) and all(dev in excluded for dev in on_lights)
    enabled = not never_lock

    duration = _int_or_none(behavior.get("lock_duration"))
    if duration is None or duration <= 0:
        duration = _int_or_none(plugin_config.get("default_lock_duration")) or 0
    if duration < 1:
        inert = "" if enabled else " The zone never locks, so the value is inert."
        report(
            f"{name}: no usable lock duration (the zone says "
            f"{behavior.get('lock_duration')!r} and the fork's plugin_config sets no "
            f"default_lock_duration), so override.duration_minutes is Lamplighter's "
            f"default of {LAMPLIGHTER_DEFAULT_LOCK_MINUTES}.{inert}"
        )
        duration = LAMPLIGHTER_DEFAULT_LOCK_MINUTES

    if behavior.get("extend_lock_when_active"):
        extend = _int_or_none(behavior.get("lock_extension_duration"))
        if extend is None or extend <= 0:
            extend = _int_or_none(plugin_config.get("default_lock_extension_duration")) or 0
            report(
                f"{name}: locks extend while the room is occupied but no extension "
                f"length is set (the zone says "
                f"{behavior.get('lock_extension_duration')!r}), and the fork's "
                f"plugin_config sets no default; override.extend_minutes is {max(extend, 0)}."
            )
        extend = max(extend, 0)
    else:
        extend = 0

    return {
        "enabled": enabled,
        "duration_minutes": duration,
        "extend_minutes": extend,
        "unlock_on_leave": bool(behavior.get("unlock_when_no_presence")),
        "exclude": [] if never_lock else excluded,
    }, extend


def _convert_periods(raw_zone, name, lights, on_lights, force_off, periods_by_id, zone_extend, report):
    """The zone's period ladder, trimmed so that no two bands overlap (R11)."""
    device_map = raw_zone.get("device_period_map") or {}
    missing = object()
    claimed = bytearray(MINUTES_IN_DAY)
    claimed_by = {}  # minute -> the period name that took it
    out = []

    for period_id in raw_zone.get("lighting_period_ids") or []:
        raw = periods_by_id.get(period_id)
        if raw is None:
            report(
                f"{name}: lighting period id {period_id} is referenced by the zone but "
                "is not defined in lighting_periods; DROPPED."
            )
            continue

        period_name = str(raw.get("name") or f"Period {period_id}")[:MAX_NAME]
        where = f"{name} / {period_name}"

        mode = MODES.get(raw.get("mode"))
        if mode is None:
            report(
                f"{where}: mode {raw.get('mode')!r} is not one Auto Lights defines; "
                "written as 'on_and_off'."
            )
            mode = "on_and_off"

        to_hour, to_minute = int(raw.get("to_time_hour") or 0), int(raw.get("to_time_minute") or 0)
        start = int(raw.get("from_time_hour") or 0) * 60 + int(raw.get("from_time_minute") or 0)
        end = _end_minutes(to_hour, to_minute)
        # Judged on the fork's own numbers, not the converted ones: its test is
        # `from <= now <= to` with no midnight wrap, so a band whose written end
        # is before its written start never fired at all. Reading it as a band
        # that wraps is almost certainly the intent, but it is a live change.
        if to_hour * 60 + to_minute < start:
            report(
                f"{where}: runs {_fmt(start)} to {to_hour:02d}:{to_minute:02d}, which "
                "never fired under Auto Lights -- its period test is 'from <= now <= to' "
                f"with no midnight wrap. Written here as {_fmt(start)} to {_fmt(end)}, "
                "which DOES run. Confirm that is what was meant."
            )

        wanted = _mask(_segments(start, end))
        remainder = bytearray(MINUTES_IN_DAY)
        collided = set()
        for minute in range(MINUTES_IN_DAY):
            if not wanted[minute]:
                continue
            if claimed[minute]:
                collided.add(claimed_by[minute])
            else:
                remainder[minute] = 1

        bands = _runs(remainder)
        if collided:
            others = ", ".join(repr(other) for other in sorted(collided))
            if not bands:
                report(
                    f"{where}: entirely covered by {others}, which Auto Lights lists "
                    "first, so it never fired; DROPPED. Auto Lights resolved overlaps "
                    "by list order; Lamplighter refuses them outright (R11)."
                )
                continue
            shown = ", ".join(f"{_fmt(s)}-{_fmt(e)}" for s, e in bands)
            report(
                f"{where}: overlapped {others} and was trimmed to {shown} -- the band "
                "Auto Lights actually ran it for, since it resolved overlaps by list "
                "order (first match wins) and Lamplighter refuses them outright (R11)."
            )

        levels = {}
        for dev_id in lights:
            if dev_id in on_lights:
                cell = device_map.get(str(dev_id), {}).get(str(period_id), missing)
                levels[str(dev_id)] = _level_for(cell, missing, force_off, f"{where} / {dev_id}", report)
            else:
                # An "off light" is only ever turned off by the fork; it has no
                # on-level in any period, so it says so in every period rather
                # than being absent, which would read as "leave".
                levels[str(dev_id)] = "off"

        limit = _int_or_none(raw.get("limit_brightness"))
        if limit is not None and limit > 100:
            report(f"{where}: limit_brightness {limit} is above 100; clamped to 100.")
            limit = 100

        lock_duration = _int_or_none(raw.get("lock_duration"))
        period_override = None
        if lock_duration is not None and lock_duration > 0:
            period_override = {"duration_minutes": lock_duration, "extend_minutes": zone_extend}
            report(
                f"{where}: carries its own lock_duration of {lock_duration}, written as "
                f"a period override of {lock_duration} minutes (extend "
                f"{zone_extend}, the zone's). Auto Lights' tooltip calls that field "
                "seconds while its code uses minutes -- check the value."
            )

        for index, (band_start, band_end) in enumerate(bands):
            entry = {
                "name": (period_name if index == 0 else f"{period_name} ({index + 1})")[:MAX_NAME],
                "from": _fmt(band_start),
                "to": _fmt(band_end),
                "mode": mode,
            }
            if limit is not None and limit > 0:
                entry["limit"] = limit
            if period_override is not None:
                entry["override"] = dict(period_override)
            entry["levels"] = dict(levels)
            out.append(entry)

        for minute in range(MINUTES_IN_DAY):
            if remainder[minute]:
                claimed[minute] = 1
                claimed_by[minute] = period_name

    return out


# ---------------------------------------------------------- the whole file


def convert(document, hold_seconds=DEFAULT_HOLD_SECONDS, presence=None):
    """Convert a parsed ``auto_lights_conf.json`` into ``(config, report)``.

    ``presence`` is ``{zone name: [device id, ...]}`` from ``--presence``; a
    name that matches no zone is an error rather than a no-op, because a
    typo there silently leaves the Occupatum device in place and the operator
    believes they have migrated a zone they have not.
    """
    if not isinstance(document, dict):
        raise ConversionError(
            f"expected an Auto Lights configuration object, got {type(document).__name__}."
        )

    report = []

    def note(text):
        report.append(text)

    plugin_config = document.get("plugin_config") or {}
    periods_by_id = {}
    for raw in document.get("lighting_periods") or []:
        period_id = _int_or_none((raw or {}).get("id"))
        if period_id is not None:
            periods_by_id[period_id] = raw

    presence = dict(presence or {})
    unused = set(presence)
    zones = []

    for raw_zone in document.get("zones") or []:
        name = zone_name((raw_zone or {}).get("name"))
        if not name:
            note("A zone with no name was DROPPED; Lamplighter keys its state by name.")
            continue

        devices = (raw_zone.get("device_settings") or {})
        on_lights = _unique(devices.get("on_lights_dev_ids"))
        off_lights = _unique(devices.get("off_lights_dev_ids"))
        lights = _unique(on_lights, off_lights)
        if not lights:
            note(f"{name}: no on- or off-lights, so there is nothing to drive; zone DROPPED.")
            continue

        if name in presence:
            presence_devices = _unique(presence[name])
            unused.discard(name)
            note(
                f"{name}: presence_devices replaced from --presence with "
                f"{', '.join(str(d) for d in presence_devices)} (was "
                f"{', '.join(str(d) for d in _unique(devices.get('presence_dev_ids'))) or 'nothing'})."
            )
        elif raw_zone.get("name") in presence:
            # The fork's own name accepted too, so an operator copying the
            # name out of the Auto Lights editor is not told it is unknown.
            key = raw_zone["name"]
            presence_devices = _unique(presence[key])
            unused.discard(key)
            note(
                f"{name}: presence_devices replaced from --presence with "
                f"{', '.join(str(d) for d in presence_devices)}."
            )
        else:
            presence_devices = _unique(devices.get("presence_dev_ids"))

        if not presence_devices:
            note(f"{name}: no presence devices, so the zone could never be occupied; DROPPED.")
            continue

        luminance = raw_zone.get("minimum_luminance_settings") or {}
        if luminance.get("adjust_brightness"):
            note(
                f"{name}: adjust_brightness was true and is DROPPED -- adjust_by_lux is "
                "not implemented in Lamplighter v1 (PRD section 5.6), and the loader "
                "rejects the flag on a zone that has a lux block rather than running at "
                "unscaled levels. Lights without an explicit per-period level are "
                "written as 'on' (full); set the levels you want."
            )

        if raw_zone.get("global_behavior_variables_map"):
            note(
                f"{name}: global_behavior_variables_map "
                f"({raw_zone['global_behavior_variables_map']}) is DROPPED -- "
                "Lamplighter has no global behaviour variables. Express it as an Indigo "
                "trigger on the zone's enable, or on the controller device."
            )

        behavior = raw_zone.get("behavior_settings") or {}
        force_off = str(behavior.get("off_lights_behavior") or "").startswith(FORCE_OFF_PREFIX)

        override, zone_extend = _convert_override(
            raw_zone, name, on_lights, plugin_config, note
        )
        periods = _convert_periods(
            raw_zone, name, lights, on_lights, force_off, periods_by_id, zone_extend, note
        )
        if not periods:
            note(f"{name}: no usable lighting periods survived conversion; zone DROPPED.")
            continue

        zones.append(
            {
                "name": name,
                "enabled": False,
                "presence_devices": presence_devices,
                "hold_seconds": hold_seconds,
                "lux": _convert_lux(raw_zone, name, note),
                "lights": lights,
                "override": override,
                "periods": periods,
            }
        )

    if unused:
        known = ", ".join(repr(zone["name"]) for zone in zones) or "none"
        raise ConversionError(
            f"--presence names no such zone: {', '.join(repr(u) for u in sorted(unused))}. "
            f"Known zones (the Auto Lights name with '{FORK_SUFFIX.strip()}' removed): {known}."
        )

    if plugin_config.get("global_behavior_variables"):
        note(
            "plugin_config.global_behavior_variables is DROPPED -- Lamplighter has no "
            "global lights-off variables. The nearest equivalent is a trigger that "
            "turns the lamplighter_controller device off."
        )

    note(
        f"All zones: hold_seconds is {hold_seconds} for every zone (--hold). Auto Lights "
        "had no per-zone presence hold -- it lived in the Occupatum zone device's "
        "off-delay, which is not in this file -- so this is a flat assumption, not a "
        "converted value. Check each zone against its Occupatum device."
    )
    note(
        "All zones: emitted with enabled: false. Enable them one at a time, disabling "
        "the matching Auto Lights zone first (PRD section 6, step 3): Hallway, Study, "
        "Back Garden, Dining, Living Room, Kitchen. Never both engines on the same lights."
    )
    note(
        "All periods: converted as wall-clock times. Sunset- and sunrise-relative edges "
        "are a MANUAL step -- Auto Lights has no sun expressions, so a 'Dusk' band "
        "approximated as a fixed 16:00 stays fixed until it is rewritten as e.g. "
        "'sunset-30m' (R11)."
    )

    return {"version": 1, "zones": zones}, report


def validate(config):
    """Load ``config`` through the plugin's own loader; raise on rejection."""
    load_config, _ = _loader()
    return load_config(config, FixedSun(), dt.date.today())


# --------------------------------------------------------------- the CLI


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="convert_autolights_config.py",
        description="Convert an Auto Lights configuration into a Lamplighter one.",
        epilog="Every zone is emitted disabled. Enable them one at a time.",
    )
    parser.add_argument("source", help="path to auto_lights_conf.json")
    parser.add_argument(
        "-o", "--output", help="where to write lamplighter.json (default: stdout)"
    )
    parser.add_argument(
        "--hold",
        type=int,
        default=DEFAULT_HOLD_SECONDS,
        help=f"presence hold in seconds for every zone (default {DEFAULT_HOLD_SECONDS})",
    )
    parser.add_argument(
        "--presence",
        action="append",
        default=[],
        metavar='"Zone Name=id,id"',
        help="replace one zone's presence devices; repeatable",
    )
    args = parser.parse_args(argv)

    try:
        document = json.loads(Path(args.source).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: {args.source} could not be read: {exc.strerror}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: {args.source} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        config, report = convert(document, hold_seconds=args.hold, presence=parse_presence(args.presence))
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if report:
        print(f"Conversion report ({len(report)} notes):", file=sys.stderr)
        for line in report:
            print(f"  - {line}", file=sys.stderr)

    text = json.dumps(config, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}: {len(config['zones'])} zones, all disabled.", file=sys.stderr)
    else:
        sys.stdout.write(text)

    # Validated last and with the output already in hand, so that a rejected
    # result can still be read and fixed rather than vanishing with the error.
    load_config, config_error = _loader()
    try:
        load_config(config, FixedSun(), dt.date.today())
    except config_error as exc:
        print(
            f"error: the converted configuration does not load: {exc}\n"
            "The output above is what failed; nothing has been installed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
