"""Time expressions and period resolution (PRD R11; sections 5.5, 5.6).

A period is a band of the day with its own per-device levels. Its edges are
written as ``HH:MM``, ``sunrise``/``sunset``, or either sun word with an
offset, and they are resolved against a date -- so "sunset-30m" is a
different clock time in June than in December, which is the whole reason the
Kitchen's fixed 16:00-19:00 "Dusk" band was an hour wrong for half the year.

Three rules that are easy to get subtly wrong and are therefore stated once,
here:

* ``from`` is inclusive, ``to`` is exclusive, so one period may end at
  "19:00" and the next begin at "19:00" without overlapping.
* ``end <= start`` means the period crosses midnight and the window runs to
  the next day. ``to: "00:00"`` therefore means midnight at the *end* of the
  period, which is what a config author writing "Dusk, sunset-30m to 00:00"
  means, and it falls out of the same rule rather than needing its own.
* Everything here is a naive local ``datetime``, because that is what
  ``indigo.server.calculateSunrise/calculateSunset`` return and what the rest
  of the plugin compares against. Mixing in an aware datetime would raise on
  the first comparison, so :class:`IndigoSun` strips any tzinfo it is handed.

Periods within a zone must not overlap at any minute of the year, and
:func:`check_overlaps` is what proves it. It is a loader-time check with a
named pair, not a first-match-wins tiebreak at runtime (R11).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

import indigo

ONE_DAY = dt.timedelta(days=1)

#: Where sunrise/sunset land when the server cannot be asked (PRD section 9).
#: Deliberately blunt round numbers: they are a stated fallback that comes
#: with a warning, not an estimate anyone should mistake for the real thing.
FALLBACK_SUNRISE = dt.time(6, 0)
FALLBACK_SUNSET = dt.time(18, 0)

# Copied verbatim from the bundled schema's time_expression pattern, and
# applied with re.search() exactly as jsonschema applies it, so that the
# loader accepts precisely the strings the schema accepts and no others.
# tests/test_config_matches_schema.py holds the two to that agreement.
TIME_PATTERN = (
    r"^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]"
    r"|(?:sunrise|sunset)(?:[+-](?:[0-9]{1,3}m|[0-9]{1,2}h(?:[0-5]?[0-9]m)?))?)$"
)
_TIME_RE = re.compile(TIME_PATTERN)
_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?:(?P<hours>[0-9]{1,2})h)?(?:(?P<minutes>[0-9]{1,3})m)?$")

_TIME_EXPR_HELP = (
    "expected HH:MM in 24-hour clock with a leading zero, 'sunrise', 'sunset', "
    "or either with an offset written with its unit ('-30m', '+1h', '-1h30m')"
)


class ConfigError(ValueError):
    """A configuration the plugin refuses to load, with the path that failed.

    Defined here rather than in ``config`` so that :func:`check_overlaps` can
    raise it without importing the loader that calls it; ``config`` re-exports
    it, so ``from lamplighter.config import ConfigError`` also works and there
    is only ever one class.

    ``path`` is a JSON-pointer-like trail through the document
    (``zones/0/periods/1/from``) -- the thing a config author, or an MCP
    caller that just had its edit rejected, needs in order to find the value
    that is wrong (R15).
    """

    def __init__(self, message, path=""):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


# ------------------------------------------------------- time expressions


@dataclass(frozen=True)
class TimeExpr:
    """One edge of a period, parsed but not yet resolved to a datetime.

    ``kind`` is "clock", "sunrise" or "sunset". ``minutes`` is minutes since
    midnight and is meaningful only for "clock"; ``offset_minutes`` is the
    signed offset and is meaningful only for the two sun kinds. ``text`` is
    what the config said, kept for log lines and error messages.
    """

    kind: str
    minutes: int = 0
    offset_minutes: int = 0
    text: str = ""


def parse_time_expr(text) -> TimeExpr:
    """Parse a schema-legal time expression, or raise ``ValueError``.

    The error names the offending text: this is reached from the loader with
    a path attached, and from there it is the only thing that tells an author
    which of thirty period edges they mistyped.
    """
    if not isinstance(text, str) or not _TIME_RE.search(text):
        raise ValueError(f"{text!r} is not a time expression: {_TIME_EXPR_HELP}")

    if ":" in text:
        hours, minutes = text.split(":")
        return TimeExpr("clock", minutes=int(hours) * 60 + int(minutes), text=text)

    kind = "sunrise" if text.startswith("sunrise") else "sunset"
    offset = text[len(kind) :]
    if not offset:
        return TimeExpr(kind, text=text)

    parts = _OFFSET_RE.match(offset)
    total = int(parts["hours"] or 0) * 60 + int(parts["minutes"] or 0)
    return TimeExpr(kind, offset_minutes=-total if parts["sign"] == "-" else total, text=text)


# ------------------------------------------------------------ sun provider


class SunProvider(Protocol):
    """Where sunrise and sunset come from for a given date.

    A protocol so the engine can be driven by a fixed sun in tests without
    a server, and so the one place that can fail -- the Indigo call -- is a
    single implementation with a single fallback.
    """

    def sunrise(self, date: dt.date) -> dt.datetime: ...

    def sunset(self, date: dt.date) -> dt.datetime: ...


class IndigoSun:
    """The real sun: ``indigo.server.calculateSunrise/calculateSunset``.

    If the call fails, or answers with something that is not a datetime, the
    period falls back to a fixed time and says so once per day per kind (PRD
    section 9, R15). Falling back matters more than it looks: swallowing the
    failure and treating the period as absent leaves the zone silently
    OFF-DUTY for a whole evening, which is a dark house and no log line.

    The warned-dates set is per instance rather than in ``compare``'s
    module-level one because this condition is keyed by date, not by device:
    a fresh instance after a config reload should re-warn if the server is
    still broken tomorrow.
    """

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("Plugin")
        self._warned: set = set()

    def sunrise(self, date: dt.date) -> dt.datetime:
        return self._ask("sunrise", indigo.server.calculateSunrise, date, FALLBACK_SUNRISE)

    def sunset(self, date: dt.date) -> dt.datetime:
        return self._ask("sunset", indigo.server.calculateSunset, date, FALLBACK_SUNSET)

    def _ask(self, kind, call, date, fallback):
        try:
            value = call(date)
        except Exception as exc:  # any server-side failure, not just one kind
            return self._fall_back(kind, date, fallback, f"{type(exc).__name__}: {exc}")

        if isinstance(value, dt.datetime):
            if value.tzinfo is not None:
                # PRD section 9 flags the timezone of the returned value as
                # unverified. Naive local is the invariant everything else
                # here relies on, so convert rather than discover it later as
                # a TypeError comparing aware to naive.
                value = value.astimezone().replace(tzinfo=None)
            return value

        return self._fall_back(kind, date, fallback, f"returned {value!r}, not a datetime")

    def _fall_back(self, kind, date, fallback, reason):
        if date not in self._warned:
            self._warned.add(date)
            self.logger.warning(
                f"{kind} for {date} could not be read from the Indigo server "
                f"({reason}); falling back to {fallback.strftime('%H:%M')} local. "
                "Periods relative to this boundary are running on the fallback, "
                "not on the real sun."
            )
        return dt.datetime.combine(date, fallback)


def resolve(expr: TimeExpr, date: dt.date, sun: SunProvider) -> dt.datetime:
    """Turn a parsed edge into a naive local datetime on ``date``."""
    if expr.kind == "clock":
        return dt.datetime.combine(date, dt.time()) + dt.timedelta(minutes=expr.minutes)
    base = sun.sunrise(date) if expr.kind == "sunrise" else sun.sunset(date)
    return base + dt.timedelta(minutes=expr.offset_minutes)


# ------------------------------------------------------------- the period


@dataclass(frozen=True)
class Period:
    """One band of the day with its own per-device levels (R11, R12).

    ``levels`` is keyed by Indigo device id as an int (the config writes them
    as strings because JSON keys are strings; the loader converts). A light
    of the zone that is absent from ``levels`` is left alone, exactly as if
    it were written as "leave".

    ``vacant_levels`` is the same shape, optional, and gives a light that has
    a level in ``levels`` a different level for when the room is VACANT
    instead of the off it would otherwise get -- a porch light that dims
    rather than goes dark. A key here always has a matching, non-"leave" key
    in ``levels``: the loader refuses a ``vacant_levels`` entry for a light
    this period does not manage while occupied.

    ``override`` is a ``config.PeriodOverride`` or None, typed loosely here
    only to keep this module below the loader in the import order.
    """

    name: str
    start: TimeExpr
    end: TimeExpr
    mode: str
    levels: Mapping[int, Any] = field(default_factory=dict)
    vacant_levels: Mapping[int, Any] = field(default_factory=dict)
    limit: int | None = None
    adjust_by_lux: bool = False
    override: Any = None


def period_window(period: Period, date: dt.date, sun: SunProvider):
    """The concrete ``(start, end)`` of ``period``'s instance beginning on ``date``.

    ``start`` is inclusive and ``end`` exclusive. ``end <= start`` means the
    band crosses midnight and the window runs into the next day -- which is
    also how ``to: "00:00"`` comes out as midnight at the end of the period
    rather than midnight at its beginning.
    """
    start = resolve(period.start, date, sun)
    end = resolve(period.end, date, sun)
    if end <= start:
        end += ONE_DAY
    return start, end


def active_period(periods: Sequence[Period], now: dt.datetime, sun: SunProvider):
    """The period covering ``now``, or None if no period does.

    Yesterday's instances are considered as well as today's, because a band
    that starts at 22:00 and ends at 06:00 covers 01:00 through the instance
    that began yesterday. None is a real answer -- a gap between periods is
    legal and means the zone is OFF-DUTY -- so this never falls back to the
    nearest or the last period.
    """
    today = now.date()
    for date in (today - ONE_DAY, today):
        for period in periods:
            start, end = period_window(period, date, sun)
            if start <= now < end:
                return period
    return None


def next_boundary(periods: Sequence[Period], now: dt.datetime, sun: SunProvider):
    """The next moment any period starts or ends after ``now``, or None.

    This is what the worker's timer heap sleeps until, so that a period
    boundary re-plans a zone with no device event at all (R4). Yesterday's
    and tomorrow's instances are included: the next boundary after 23:50 is
    usually tomorrow's, and the end of a band that started yesterday is
    still ahead of us.
    """
    today = now.date()
    soonest = None
    for date in (today - ONE_DAY, today, today + ONE_DAY):
        for period in periods:
            for edge in period_window(period, date, sun):
                if edge > now and (soonest is None or edge < soonest):
                    soonest = edge
    return soonest


def check_overlaps(periods: Sequence[Period], sun: SunProvider, dates, path="periods") -> None:
    """Raise :class:`ConfigError` if any two periods overlap on ``dates`` (R11).

    Overlap is a configuration error naming the pair and the first minute
    they share, not something resolved at runtime by taking whichever period
    is listed first. Ordering hides the mistake; the author never learns that
    their second band has been dead since they wrote it.

    ``dates`` are sampling points, and each one is checked together with the
    day after it, because a band that wraps past midnight has to be compared
    against the *next* day's instances of the others -- "22:00 to 06:00" and
    "00:00 to 06:00" do not overlap on any single day's arithmetic, and
    collide every night. The loader supplies today, tomorrow and the year's
    solstices and equinoxes, since a sun-relative edge can move a period into
    its neighbour at one time of year and nowhere near it at another.
    """
    sample = sorted({d for date in dates for d in (date, date + ONE_DAY)})
    instances = []
    for date in sample:
        for index, period in enumerate(periods):
            start, end = period_window(period, date, sun)
            instances.append((start, end, index, period))
    instances.sort(key=lambda item: item[0])

    first = None  # (overlap_start, period_a, period_b)
    for i, (a_start, a_end, a_index, a_period) in enumerate(instances):
        for b_start, b_end, b_index, b_period in instances[i + 1 :]:
            if a_index == b_index:
                continue  # the same period on two days is not a pair
            if b_start >= a_end:
                break  # sorted by start: nothing later can overlap this one
            overlap = max(a_start, b_start)
            if first is None or overlap < first[0]:
                first = (overlap, a_period, b_period)

    if first is not None:
        when, first_period, second_period = first
        raise ConfigError(
            f"periods {first_period.name!r} ({first_period.start.text} to "
            f"{first_period.end.text}) and {second_period.name!r} "
            f"({second_period.start.text} to {second_period.end.text}) overlap, "
            f"first at {when:%Y-%m-%d %H:%M}. Periods must not overlap at any "
            "minute of the year: the plugin will not pick one for you.",
            path=path,
        )
