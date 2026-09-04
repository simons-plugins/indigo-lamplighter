"""The loader and the bundled schema must agree (PRD R15; section 5.11).

The plugin is stdlib only, so `config.load_config` re-states by hand every
constraint `schema.json` declares. Two copies of one rule is exactly the
arrangement that drifts: the schema gets a new field, the loader keeps
accepting the old shape, and the MCP tool that validates against the schema
starts writing files the plugin then refuses -- or worse, quietly reshapes.

So this file holds them together from both ends. Every invalid document
tests/test_schema.py rejects is fed through the loader and must be rejected
at the same path, the shipped example must load, and the time-expression
pattern must accept and reject the same strings on both sides.

Only one direction is asserted for documents -- everything the schema rejects,
the loader rejects too. The loader is allowed to be stricter, because it also
enforces the three cross-field rules the schema cannot express (unique zone
names, levels keys being members of the zone's lights, and periods that do
not overlap), and one v1 limitation: `adjust_by_lux` is described by the
schema and refused by the loader. Every place the loader is stricter is
listed at the bottom of this file, so the list is a decision anyone can read
rather than a difference someone discovers.
"""

import copy
import datetime as dt

import jsonschema
import pytest
from helpers import FixedSun
from test_schema import EXAMPLE, INVALID, SCHEMA

from lamplighter.config import ConfigError, load_config
from lamplighter.periods import parse_time_expr

TODAY = dt.date(2026, 9, 4)
SUN = FixedSun(sunrise=dt.time(6, 30), sunset=dt.time(19, 45))

TIME_EXPR = SCHEMA["$defs"]["time_expression"]
_TIME_VALIDATOR = jsonschema.Draft202012Validator(
    {"type": "string", "pattern": TIME_EXPR["pattern"]}
)


def _pointer(path):
    """The schema's tuple path as the loader writes it: zones/0/periods/1."""
    return "/".join(str(part) for part in path)


def test_the_shipped_example_loads():
    """The file a config author copies from must survive the real loader,
    not just the schema."""
    config = load_config(copy.deepcopy(EXAMPLE), SUN, TODAY)
    assert [zone.name for zone in config.zones] == ["Kitchen", "Hallway"]
    assert config.zones[0].lux.hysteresis == 300
    assert config.zones[1].lux is None
    assert config.zones[1].override.enabled is False


@pytest.mark.parametrize(
    "name,mutate,keyword,fragment", INVALID, ids=[row[0] for row in INVALID]
)
def test_every_document_the_schema_rejects_the_loader_rejects_too(
    name, mutate, keyword, fragment
):
    """Kills: a loader that validates a subset of the schema.

    The gap is invisible from either side alone -- the schema tests pass, the
    loader tests pass, and the one document that exercises the missing rule
    is the one nobody wrote a test for. Reusing the schema suite's own table
    means a rule added there is automatically demanded here.
    """
    document = copy.deepcopy(EXAMPLE)
    path = mutate(document)

    with pytest.raises(ConfigError) as caught:
        load_config(document, SUN, TODAY)

    expected = _pointer(path)
    assert caught.value.path.startswith(expected), (
        f"{name}: the schema reports this at {expected!r}; the loader said "
        f"{caught.value.path!r}, which sends a config author to the wrong place"
    )
    if fragment is not None:
        assert fragment.strip("'") in str(caught.value), (
            f"{name}: the loader's message must name the offending key; "
            f"got {str(caught.value)!r}"
        )


@pytest.mark.parametrize(
    "expr",
    list(TIME_EXPR["examples"])
    + ["sunset-1h", "sunrise+2h", "sunset-1h30m", "sunset-120m", "00:00", "23:59"],
    ids=repr,
)
def test_a_time_expression_the_schema_accepts_the_parser_parses(expr):
    assert _TIME_VALIDATOR.is_valid(expr), "fixture error: the schema rejects this"
    assert parse_time_expr(expr).text == expr


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "25:00",
        "24:00",
        "7:30",
        "19:60",
        "noon",
        "sunset+",
        "sunset+30",
        "sunset-30s",
        "sunset -30m",
        "SUNSET-30m",
        "sunset-h",
        "sunset-1h5",
        "sunset+1h30",
        "sunrise+90",
        "06:30:00",
        "sunset-1000m",
    ],
    ids=repr,
)
def test_a_time_expression_the_schema_rejects_the_parser_rejects(expr):
    assert not _TIME_VALIDATOR.is_valid(expr), "fixture error: the schema accepts this"
    with pytest.raises(ValueError):
        parse_time_expr(expr)


# ---------------------------------------------- where the loader is stricter
#
# The schema describes the file FORMAT; the loader describes what THIS
# release can honour. Where they differ the difference is written down here
# rather than left to be discovered by a config author whose schema-valid
# file will not load.


def test_adjust_by_lux_is_the_documented_place_the_loader_is_stricter():
    """The schema accepts `adjust_by_lux: true`; v1's loader refuses it on a
    zone that has a lux block (PRD section 5.6).

    This is a documented v1 limitation, not drift: the flag is unimplemented,
    and the alternative to refusing the file is a zone that runs every light
    at its unscaled level with nothing to say the setting was ignored. When
    it is implemented, this test is what says which side has to change.

    Kills: quietly widening the loader to match the schema (the flag then
    does nothing, silently), and: narrowing the schema instead, which would
    make every file written for a later version invalid today.
    """
    document = copy.deepcopy(EXAMPLE)
    kitchen = document["zones"][0]
    assert kitchen["lux"] is not None, "fixture: the rule is scoped to a lux zone"
    kitchen["periods"][1]["adjust_by_lux"] = True

    assert jsonschema.Draft202012Validator(SCHEMA).is_valid(document), (
        "the schema must keep describing the flag: it describes the format, "
        "not this release"
    )

    with pytest.raises(ConfigError) as caught:
        load_config(document, SUN, TODAY)
    assert caught.value.path == "zones/0/periods/1/adjust_by_lux"
    assert "not implemented in this version" in str(caught.value)


def test_the_shipped_example_does_not_use_anything_the_loader_refuses():
    """Belt and braces on the pair above: whatever the exclusion list grows
    to, the file a config author copies must still load."""
    load_config(copy.deepcopy(EXAMPLE), SUN, TODAY)
