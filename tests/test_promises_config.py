"""Configuration load, validation and hot reload (PRD R13, R15; section 5.11).

The config is a JSON file an agent edits through MCP tools. It is validated
against the bundled schema with the failing path named, and reloaded on an
mtime change: zone objects are rebuilt from the new config and the persisted
state is then re-applied, so an override created at 19:46 is still an override
after an edit at 19:50.
"""

import copy
import datetime as dt
import json

import pytest
from helpers import FixedSun

from lamplighter.config import ConfigError, load_config

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)

TODAY = dt.date(2026, 9, 4)
SUN = FixedSun(sunrise=dt.time(6, 30), sunset=dt.time(19, 45))

# Two zones, because half the promises here are about what happens to the
# zone that was fine when another zone is not.
TWO_ZONES = {
    "version": 1,
    "zones": [
        {
            "name": "Study",
            "presence_devices": [101],
            "hold_seconds": 300,
            "lux": None,
            "lights": [201],
            "periods": [
                {
                    "name": "Evening",
                    "from": "sunset-30m",
                    "to": "23:00",
                    "mode": "on_and_off",
                    "levels": {"201": 60},
                }
            ],
        },
        {
            "name": "Garden",
            "presence_devices": [102],
            "hold_seconds": 120,
            "lux": {"device": 302, "dark_below": 50, "when_unreadable": "bright"},
            "lights": [301],
            "periods": [
                {
                    "name": "Night",
                    "from": "22:00",
                    "to": "06:00",
                    "mode": "off_only",
                    "levels": {"301": "off"},
                }
            ],
        },
    ],
}


def _written(tmp_path, document):
    path = tmp_path / "lamplighter.json"
    path.write_text(json.dumps(document))
    return path


def test_an_invalid_config_is_rejected_naming_the_failing_path(tmp_path):
    """A file that fails validation is refused with the JSON path of the
    offending value, and the previously loaded config keeps running (R15).

    Kills: fall back to defaults for the bad field, or load the zones that did
    parse. A partially applied config is the one state nobody can reason
    about, and an MCP caller must get the validation error verbatim.
    """
    path = _written(tmp_path, TWO_ZONES)
    running = load_config(str(path), SUN, TODAY)
    assert [zone.name for zone in running.zones] == ["Study", "Garden"]

    # Zone 0 is untouched and parses perfectly; the mistake is deep inside
    # zone 1, which is exactly the shape that tempts a loader into keeping
    # the zones it managed to read.
    broken = copy.deepcopy(TWO_ZONES)
    broken["zones"][1]["periods"][0]["levels"]["301"] = 0
    _written(tmp_path, broken)

    with pytest.raises(ConfigError) as caught:
        load_config(str(path), SUN, TODAY)

    assert caught.value.path == "zones/1/periods/0/levels/301", (
        "the path is the whole point: without it an MCP caller is handed "
        "'invalid config' and a 300-line file"
    )
    assert "zones/1/periods/0/levels/301" in str(caught.value)
    assert "minimum of 1" in str(caught.value), "the message must name the rule"

    # Nothing partial escaped, and nothing already loaded was disturbed: the
    # zone that parsed is not a Config anybody can act on, and the config
    # that was running still has both its zones with their original values.
    assert [zone.name for zone in running.zones] == ["Study", "Garden"]
    assert running.zones[1].periods[0].levels == {301: "off"}
    assert running.zones[1].hold_seconds == 120


def test_an_unparseable_file_leaves_the_running_config_in_place(tmp_path):
    """A file that is not JSON at all -- a half-written save -- is refused the
    same way, and the plugin keeps running on what it had.

    Kills: clear the zone list before parsing, which turns a truncated write
    into every light in the house going unmanaged.
    """
    path = _written(tmp_path, TWO_ZONES)
    running = load_config(str(path), SUN, TODAY)

    path.write_text('{"version": 1, "zones": [{"name": "Study",')  # caught mid-save

    with pytest.raises(ConfigError) as caught:
        load_config(str(path), SUN, TODAY)

    assert caught.value.path == ""
    assert "not valid JSON" in str(caught.value)
    assert "line" in str(caught.value), "a syntax error must say where"

    assert [zone.name for zone in running.zones] == ["Study", "Garden"]
    assert running.zones[0].lights == (201,)
    assert running.zones[1].periods[0].levels == {301: "off"}


@promise
def test_hot_reload_preserves_an_active_override():
    """An override held before a reload is still held after it, with its
    original expiry (R13).

    Kills: rebuild zones and let persisted state be re-applied only at plugin
    startup -- the fork's "all locks and zone state has been reset" on every
    single reload.
    """
    raise NotImplementedError


@promise
def test_hot_reload_preserves_presence_last_seen():
    """Presence last-seen survives a reload, so an edit does not turn the
    lights off in an occupied room (R13).

    Kills: re-apply only the override state, which is the half of section 5.10
    that is easy to remember.
    """
    raise NotImplementedError


@promise
def test_an_unknown_device_id_warns_once_and_the_zone_keeps_running():
    """A device id in the config that does not resolve warns once, is dropped
    from that zone's working set, and the zone keeps working for its other
    devices (R8, R15).

    Kills: raise and abort the load, and: skip it silently. A zone quietly
    running on three of its five lights is the failure this promise exists to
    prevent.
    """
    raise NotImplementedError
