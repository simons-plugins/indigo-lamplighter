"""Unit tests for the loader's shapes and defaults (PRD R15; section 5.11).

What the loader must get right beyond "rejects bad files": the defaults are
the ones the schema documents, the cross-field rules the schema cannot state
are enforced here, and an unresolvable device id is deliberately NOT one of
them.
"""

import datetime as dt
import json

import pytest
from helpers import FixedSun

from lamplighter.config import (
    Config,
    ConfigError,
    LuxConfig,
    OverrideConfig,
    PeriodOverride,
    ZoneConfig,
    load_config,
    validation_dates,
)

TODAY = dt.date(2026, 9, 4)
SUN = FixedSun(sunrise=dt.time(6, 30), sunset=dt.time(19, 45))


def doc(**zone_overrides):
    """A minimal valid document, with one zone the caller can bend."""
    zone = {
        "name": "Study",
        "presence_devices": [101],
        "hold_seconds": 300,
        "lux": None,
        "lights": [201, 202],
        "periods": [
            {
                "name": "Evening",
                "from": "sunset-30m",
                "to": "23:00",
                "mode": "on_and_off",
                "levels": {"201": 60, "202": "leave"},
            }
        ],
    }
    zone.update(zone_overrides)
    return {"version": 1, "zones": [zone]}


def load(document=None, **zone_overrides):
    return load_config(document or doc(**zone_overrides), SUN, TODAY)


# ----------------------------------------------------------------- shapes


def test_a_minimal_document_loads_into_dataclasses():
    config = load()
    assert isinstance(config, Config)
    assert isinstance(config.zones[0], ZoneConfig)
    assert config.zones[0].name == "Study"
    assert config.zones[0].lights == (201, 202)
    assert config.zones[0].periods[0].levels == {201: 60, 202: "leave"}


def test_levels_are_keyed_by_int_device_id():
    """JSON keys are strings; the rest of the plugin works in device ids."""
    levels = load().zones[0].periods[0].levels
    assert set(levels) == {201, 202}


def test_the_documented_defaults_are_the_ones_applied():
    config = load()
    zone = config.zones[0]
    assert (config.reconcile_seconds, config.echo_window_seconds) == (60, 15)
    assert zone.enabled is True
    assert zone.override == OverrideConfig(
        enabled=True,
        duration_minutes=60,
        extend_minutes=0,
        unlock_on_leave=True,
        exclude=(),
    )
    assert zone.periods[0].adjust_by_lux is False
    assert zone.periods[0].limit is None
    assert zone.periods[0].override is None


def test_a_lux_block_takes_its_own_defaults():
    zone = load(lux={"device": 301, "dark_below": 2200}).zones[0]
    assert zone.lux == LuxConfig(
        device=301, dark_below=2200, dark_below_variable_id=None, hysteresis=0, when_unreadable="dark"
    )


def test_a_null_lux_block_means_no_daylight_gate():
    assert load().zones[0].lux is None


def test_a_period_override_is_a_replacement_block():
    zone = load(
        periods=[
            {
                "name": "Evening",
                "from": "19:00",
                "to": "23:00",
                "mode": "on_and_off",
                "override": {"duration_minutes": 120, "extend_minutes": 30},
                "levels": {"201": 60},
            }
        ]
    ).zones[0]
    assert zone.periods[0].override == PeriodOverride(duration_minutes=120, extend_minutes=30)


def test_a_file_path_and_a_dict_load_the_same_way(tmp_path):
    path = tmp_path / "lamplighter.json"
    path.write_text(json.dumps(doc()))
    assert load_config(str(path), SUN, TODAY) == load_config(doc(), SUN, TODAY)


# ------------------------------------------------- rules the schema states


@pytest.mark.parametrize(
    "mutation,path",
    [
        ({"hold_seconds": 86401}, "zones/0/hold_seconds"),
        ({"hold_seconds": True}, "zones/0/hold_seconds"),
        ({"presence_devices": []}, "zones/0/presence_devices"),
        ({"presence_devices": [0]}, "zones/0/presence_devices/0"),
        ({"lights": [201, 201]}, "zones/0/lights"),
        ({"name": ""}, "zones/0/name"),
        ({"name": "x" * 65}, "zones/0/name"),
        ({"enabled": "yes"}, "zones/0/enabled"),
        ({"periods": []}, "zones/0/periods"),
        ({"lux": {"dark_below": 10}}, "zones/0/lux"),
        ({"lux": {"device": 1, "dark_below": -1}}, "zones/0/lux/dark_below"),
        ({"lux": {"device": 1, "dark_below": 1, "hysteresis": -1}}, "zones/0/lux/hysteresis"),
        ({"override": {"extend_minutes": -1}}, "zones/0/override/extend_minutes"),
        ({"override": {"ghost": 1}}, "zones/0/override"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_broken_value_is_rejected_at_its_own_path(mutation, path):
    with pytest.raises(ConfigError) as caught:
        load(**mutation)
    assert caught.value.path == path


@pytest.mark.parametrize("version", [2, "1", True, None])
def test_only_version_one_is_read(version):
    document = doc()
    document["version"] = version
    with pytest.raises(ConfigError) as caught:
        load_config(document, SUN, TODAY)
    assert caught.value.path == "version"


def test_a_whole_number_written_as_a_float_is_still_an_integer():
    """JSON Schema's `integer` and `const` compare numbers mathematically, so
    60.0 is 60 there and must be here too: anything the schema blesses and
    the loader refuses is a config an MCP edit can validate and then fail to
    load."""
    document = doc(hold_seconds=300.0)
    document["version"] = 1.0
    assert load_config(document, SUN, TODAY).zones[0].hold_seconds == 300


def test_reconcile_seconds_below_the_floor_is_refused():
    document = doc()
    document["reconcile_seconds"] = 5
    with pytest.raises(ConfigError) as caught:
        load_config(document, SUN, TODAY)
    assert caught.value.path == "reconcile_seconds"
    assert "10" in str(caught.value)


def test_a_document_that_is_not_an_object_is_refused():
    with pytest.raises(ConfigError) as caught:
        load_config([{"version": 1}], SUN, TODAY)
    assert caught.value.path == ""


def test_a_source_that_is_neither_a_path_nor_a_dict_is_refused():
    with pytest.raises(ConfigError):
        load_config(17, SUN, TODAY)


def test_a_missing_file_is_refused_with_a_readable_reason(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(str(tmp_path / "nope.json"), SUN, TODAY)
    assert "nope.json" in str(caught.value)


# ------------------------------------------ rules only the loader can state


def test_zone_names_must_be_unique():
    """State is persisted under the name, so two Kitchens are one Kitchen."""
    document = doc()
    document["zones"].append(json.loads(json.dumps(document["zones"][0])))
    with pytest.raises(ConfigError) as caught:
        load_config(document, SUN, TODAY)
    assert caught.value.path == "zones/1/name"
    assert "Study" in str(caught.value)


def test_a_levels_key_must_be_one_of_the_zones_lights():
    """Kills: write to a device the zone does not own, which shows up only as
    a light in another room changing."""
    with pytest.raises(ConfigError) as caught:
        load(
            periods=[
                {
                    "name": "Evening",
                    "from": "19:00",
                    "to": "23:00",
                    "mode": "on_and_off",
                    "levels": {"999": 60},
                }
            ]
        )
    assert caught.value.path == "zones/0/periods/0/levels/999"
    assert "999" in str(caught.value)


def test_an_unknown_device_id_is_not_a_configuration_error():
    """Ids are not resolved at load time (R15).

    A deleted bulb, or an Indigo lookup that is failing right now, must not
    take out five working zones. The engine warns once and keeps the zone
    running; refusing the file here would be the louder wrong answer.
    """
    import indigo

    assert indigo.devices == {}  # nothing in this config resolves at all
    config = load()
    assert config.zones[0].lights == (201, 202)


def test_the_validation_dates_reach_beyond_today_and_tomorrow():
    """A sun-relative edge can collide with its neighbour only in December.

    Kills: sample today and tomorrow only, which is every wall-clock overlap
    and none of the seasonal ones.
    """
    dates = validation_dates(TODAY)
    assert TODAY in dates and TODAY + dt.timedelta(days=1) in dates
    months = {date.month for date in dates}
    assert {3, 6, 9, 12} <= months
