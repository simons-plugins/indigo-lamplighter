"""The schema is the M0 deliverable, so these tests are real, not stubs.

Three things are pinned here: the schema is itself a valid JSON Schema, the
shipped example validates against it, and each invalid document fails at the
path a config author would need to see (R15 -- a rejected config names what is
wrong, it does not fall back to a default).

The PRD section 11 decisions that are visible in the shape of the file are
asserted directly, so a later edit cannot quietly reverse one. Decisions 1
(controller device) and 2 (lock-zone action) are not config, so they are
pinned by the acceptance stubs, not here.
"""

import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    REPO_ROOT
    / "Lamplighter.indigoPlugin"
    / "Contents"
    / "Server Plugin"
    / "lamplighter"
    / "schema.json"
)
EXAMPLE_PATH = REPO_ROOT / "examples" / "lamplighter.example.json"

SCHEMA = json.loads(SCHEMA_PATH.read_text())
EXAMPLE = json.loads(EXAMPLE_PATH.read_text())


def _errors(doc):
    return list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc))


# --------------------------------------------------------------- the schema


def test_schema_is_a_valid_2020_12_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_schema_declares_2020_12():
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_example_config_validates():
    assert _errors(EXAMPLE) == []


def test_example_exercises_the_shapes_the_prd_calls_out():
    """The example is documentation as much as a fixture.

    If a later edit trims it back to one plain zone, the features that were
    hardest to get right stop being demonstrated anywhere a config author
    will look.
    """
    zones = {z["name"]: z for z in EXAMPLE["zones"]}
    kitchen, hallway = zones["Kitchen"], zones["Hallway"]

    assert kitchen["lux"]["hysteresis"] > 0, "Schmitt band (R9)"
    assert "dark_below_variable_id" in kitchen["lux"], "variable-driven threshold"
    periods = {p["name"]: p for p in kitchen["periods"]}
    assert "override" in periods["Dusk"], "period-level override (decision 4)"
    assert periods["Daytime"]["limit"] == 80
    assert "adjust_by_lux" not in periods["Daytime"], (
        "the schema still describes adjust_by_lux, but the loader refuses it on "
        "a zone with a lux block (PRD section 5.6, not implemented in v1). The "
        "example is what a config author copies, so it must not demonstrate a "
        "setting that makes the file fail to load."
    )
    assert "leave" in periods["Overnight"]["levels"].values(), "leave (R12)"

    assert hallway["lux"] is None, "a zone with no daylight gate"
    assert hallway["override"]["enabled"] is False, "never-lock zone (R10)"
    hall_periods = {p["name"]: p for p in hallway["periods"]}
    assert hall_periods["Overnight"]["mode"] == "off_only", "off_only (R11)"
    assert hall_periods["Overnight"]["from"] > hall_periods["Overnight"]["to"], (
        "the overnight band must cross midnight"
    )
    assert hall_periods["Evening"]["from"].startswith("sunset"), "sun-relative (R11)"
    assert set(hall_periods["Evening"]["levels"].values()) == {"on"}, "relay level"


def test_vacant_levels_is_accepted_with_the_same_shape_as_levels():
    """A porch light: 25 while vacant, 100 while occupied (R12).

    Kills: a schema edit that forgot to add `vacant_levels` to a period's
    `properties`, which would make this fail on `additionalProperties`
    instead of validating.
    """
    doc = copy.deepcopy(EXAMPLE)
    period = doc["zones"][0]["periods"][0]
    period["vacant_levels"] = {"1894385558": 25}
    assert _errors(doc) == []


def test_vacant_levels_rejects_a_bad_level_word():
    """`vacant_levels` values are levels, not free text (R12).

    Kills: a `vacant_levels` schema that forgot to `$ref` the shared `level`
    def and so accepted any string.
    """
    doc = copy.deepcopy(EXAMPLE)
    period = doc["zones"][0]["periods"][0]
    period["vacant_levels"] = {"1894385558": "dim"}
    errors = _errors(doc)
    assert errors, "'dim' is not one of the level words the schema allows"


def test_presence_variables_is_accepted_as_a_list_of_variable_ids():
    """A zone may list Indigo variable ids as presence inputs (PRD 5.4).

    Kills: a schema edit that forgot to add `presence_variables` to the
    zone's `properties`, which would make this fail on `additionalProperties`
    instead of validating.
    """
    doc = copy.deepcopy(EXAMPLE)
    doc["zones"][0]["presence_variables"] = [1872770829]
    assert _errors(doc) == []


def test_presence_variables_rejects_a_non_integer():
    """`presence_variables` holds variable ids, not names or strings.

    Kills: a `presence_variables` schema that forgot to constrain its items
    to integers and so accepted anything.
    """
    doc = copy.deepcopy(EXAMPLE)
    doc["zones"][0]["presence_variables"] = ["SimonHome"]
    errors = _errors(doc)
    assert errors, "a variable NAME is not a variable id"


# ------------------------------------------------------- invalid documents
#
# Each mutation returns the path the resulting error must carry. Errors are
# matched on the failing keyword rather than on jsonschema's wording, which
# changes between releases.


def _missing_zones(doc):
    del doc["zones"]
    return ()


def _empty_lights(doc):
    doc["zones"][0]["lights"] = []
    return ("zones", 0, "lights")


def _negative_hold(doc):
    doc["zones"][0]["hold_seconds"] = -1
    return ("zones", 0, "hold_seconds")


def _level_zero(doc):
    doc["zones"][0]["periods"][0]["levels"]["1894385558"] = 0
    return ("zones", 0, "periods", 0, "levels", "1894385558")


def _level_over_one_hundred(doc):
    doc["zones"][0]["periods"][0]["levels"]["1894385558"] = 101
    return ("zones", 0, "periods", 0, "levels", "1894385558")


def _bad_mode(doc):
    doc["zones"][0]["periods"][0]["mode"] = "on_and_of"
    return ("zones", 0, "periods", 0, "mode")


def _impossible_clock_time(doc):
    doc["zones"][0]["periods"][0]["from"] = "25:00"
    return ("zones", 0, "periods", 0, "from")


def _truncated_sun_offset(doc):
    doc["zones"][0]["periods"][0]["from"] = "sunset+"
    return ("zones", 0, "periods", 0, "from")


def _unknown_top_level_key(doc):
    doc["ghost_setting"] = True
    return ()


def _zero_override_duration(doc):
    doc["zones"][0]["override"]["duration_minutes"] = 0
    return ("zones", 0, "override", "duration_minutes")


def _bad_when_unreadable(doc):
    doc["zones"][0]["lux"]["when_unreadable"] = "maybe"
    return ("zones", 0, "lux", "when_unreadable")


def _half_specified_period_override(doc):
    del doc["zones"][0]["periods"][2]["override"]["extend_minutes"]
    return ("zones", 0, "periods", 2, "override")


def _levels_key_not_a_device_id(doc):
    # jsonschema reports a propertyNames failure against the object, not the
    # key, so the path stops at `levels` and the offending key has to come
    # from the message. Pinned here so a config author is never left guessing
    # which key was wrong.
    doc["zones"][0]["periods"][0]["levels"]["kitchen-strip"] = 50
    return ("zones", 0, "periods", 0, "levels")


def _unknown_period_key(doc):
    doc["zones"][0]["periods"][0]["transition_seconds"] = 3
    return ("zones", 0, "periods", 0)


def _wrong_version(doc):
    doc["version"] = 2
    return ("version",)


INVALID = [
    ("missing zones", _missing_zones, "required", "'zones'"),
    ("empty lights", _empty_lights, "minItems", None),
    ("negative hold_seconds", _negative_hold, "minimum", None),
    ("level 0", _level_zero, "anyOf", None),
    ("level 101", _level_over_one_hundred, "anyOf", None),
    ("misspelt mode", _bad_mode, "enum", None),
    ("from 25:00", _impossible_clock_time, "pattern", None),
    ("from sunset+", _truncated_sun_offset, "pattern", None),
    ("unknown top-level key", _unknown_top_level_key, "additionalProperties", "ghost_setting"),
    ("override.duration_minutes 0", _zero_override_duration, "minimum", None),
    ("when_unreadable maybe", _bad_when_unreadable, "enum", None),
    ("half-specified period override", _half_specified_period_override, "required", None),
    ("levels key that is not a device id", _levels_key_not_a_device_id, "pattern", "kitchen-strip"),
    ("unknown period key", _unknown_period_key, "additionalProperties", None),
    ("wrong version", _wrong_version, "const", None),
]


@pytest.mark.parametrize(
    "name,mutate,keyword,fragment", INVALID, ids=[row[0] for row in INVALID]
)
def test_invalid_document_fails_at_the_expected_path(name, mutate, keyword, fragment):
    doc = copy.deepcopy(EXAMPLE)
    path = mutate(doc)

    errors = _errors(doc)
    assert errors, f"{name}: the schema accepted a document it must reject"

    matching = [
        e
        for e in errors
        if tuple(e.absolute_path) == path and e.validator == keyword
    ]
    assert matching, (
        f"{name}: expected a '{keyword}' error at {path or 'the document root'}; "
        f"got {[(tuple(e.absolute_path), e.validator) for e in errors]}"
    )
    if fragment is not None:
        assert any(fragment in e.message for e in matching), (
            f"{name}: the error must name the offending key; "
            f"got {[e.message for e in matching]}"
        )


# ------------------------------------------------------- time expressions

TIME_EXPR = SCHEMA["$defs"]["time_expression"]
TIME_RE = re.compile(TIME_EXPR["pattern"])

BAD_TIME_EXPRESSIONS = [
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
]

# Forms the pattern must accept that the schema's own examples do not all
# spell out: minutes only, hours only, and hours with minutes.
EXTRA_GOOD_TIME_EXPRESSIONS = [
    "sunset-1h",
    "sunrise+2h",
    "sunset-1h30m",
]


@pytest.mark.parametrize("expr", TIME_EXPR["examples"])
def test_documented_time_expression_examples_match_the_pattern(expr):
    assert TIME_RE.search(expr), (
        f"{expr!r} is advertised in the schema's examples but the pattern "
        "rejects it -- the documentation and the rule must agree"
    )


@pytest.mark.parametrize("expr", EXTRA_GOOD_TIME_EXPRESSIONS, ids=repr)
def test_every_offset_form_is_accepted(expr):
    assert TIME_RE.search(expr), (
        f"{expr!r} is one of the three offset forms the schema documents "
        "(minutes only, hours only, hours and minutes)"
    )


@pytest.mark.parametrize("expr", BAD_TIME_EXPRESSIONS, ids=repr)
def test_bad_time_expressions_are_rejected(expr):
    assert not TIME_RE.search(expr)


def test_time_expression_examples_cover_all_three_forms():
    examples = TIME_EXPR["examples"]
    assert any(":" in e for e in examples), "clock form"
    assert any(e in ("sunrise", "sunset") for e in examples), "bare sun form"
    assert any(e.startswith("sun") and ("+" in e or "-" in e) for e in examples), (
        "offset form"
    )


# --------------------------------------------------------- PRD decisions


def test_decision_3_unreadable_lux_defaults_to_dark():
    """PRD section 11, decision 3: dark, with a per-zone flag for the garden."""
    field = SCHEMA["$defs"]["lux"]["properties"]["when_unreadable"]
    assert field["default"] == "dark"
    assert set(field["enum"]) == {"dark", "bright"}


def test_decision_4_a_period_may_carry_its_own_override_timing():
    """PRD section 11, decision 4: kept, and it REPLACES rather than merges."""
    period_override = SCHEMA["$defs"]["period"]["properties"]["override"]
    assert period_override["$ref"] == "#/$defs/period_override"

    block = SCHEMA["$defs"]["period_override"]
    assert set(block["properties"]) == {"duration_minutes", "extend_minutes"}
    assert set(block["required"]) == {"duration_minutes", "extend_minutes"}
    assert block["additionalProperties"] is False


def test_a_zone_can_declare_that_it_never_locks():
    """R10 / the Hallway: override.enabled false, defaulting to true."""
    enabled = SCHEMA["$defs"]["zone_override"]["properties"]["enabled"]
    assert enabled["type"] == "boolean"
    assert enabled["default"] is True


def test_lux_is_required_but_nullable():
    """'No daylight gate' must be stated, not acquired by omission.

    Nullable in one schema rather than a oneOf, so that a bad field inside a
    lux block is still reported at that field's own path (R15).
    """
    zone = SCHEMA["$defs"]["zone"]
    assert "lux" in zone["required"]
    assert set(SCHEMA["$defs"]["lux"]["type"]) == {"object", "null"}


def test_every_object_refuses_unknown_keys():
    """A typo in a config an agent edits must fail loudly, everywhere."""
    def walk(node, where):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False or isinstance(
                    node.get("additionalProperties"), dict
                ), f"{where} accepts unknown keys"
            for key, value in node.items():
                walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{where}[{i}]")

    walk(SCHEMA, "$")


def test_defaults_documented_here_are_the_ones_the_prd_states():
    assert SCHEMA["properties"]["reconcile_seconds"]["default"] == 60
    assert SCHEMA["properties"]["echo_window_seconds"]["default"] == 15
    zone_override = SCHEMA["$defs"]["zone_override"]["properties"]
    assert zone_override["duration_minutes"]["default"] == 60
    assert zone_override["extend_minutes"]["default"] == 0
    assert zone_override["unlock_on_leave"]["default"] is True
    assert SCHEMA["$defs"]["lux"]["properties"]["hysteresis"]["default"] == 0
    assert SCHEMA["$defs"]["period"]["properties"]["adjust_by_lux"]["default"] is False
