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
import logging

import pytest
from helpers import FixedSun, make_device

from lamplighter import compare, persist
from lamplighter.config import ConfigError, load_config
from lamplighter.zone import Zone, ZoneState

TODAY = dt.date(2026, 9, 4)
SUN = FixedSun(sunrise=dt.time(6, 30), sunset=dt.time(19, 45))
LOGGER = "test.promises.config"

# The two moments the PRD names for R13: a lock created at 19:46 is still a
# lock after a config reload at 19:50. Both are inside the Study's Evening
# band, which starts at sunset-30m = 19:15.
LOCKED_AT = dt.datetime(2026, 9, 4, 19, 46, 35)
EDITED_AT = dt.datetime(2026, 9, 4, 19, 50, 0)


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


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

# One zone, two lights, for the reload promises. Separate from TWO_ZONES
# because those are about what happens to the zone that was fine when
# another zone is not, and these are about what happens to the zone itself.
ONE_ZONE = {
    "version": 1,
    "zones": [
        {
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
                    "levels": {"201": 60, "202": 30},
                }
            ],
        }
    ],
}


def zone_config(document, index=0):
    """One validated ZoneConfig out of a whole document."""
    return load_config(document, SUN, TODAY).zones[index]


def a_zone(document=None):
    """A running Zone on the ONE_ZONE config, with a real logger."""
    return Zone(
        zone_config(document or ONE_ZONE), SUN, logger=logging.getLogger(LOGGER)
    )


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


def test_hot_reload_preserves_an_active_override():
    """An override held before a reload is still held after it, with its
    original expiry (R13).

    Kills: rebuild zones and let persisted state be re-applied only at plugin
    startup -- the fork's "all locks and zone state has been reset" on every
    single reload.

    Mutation applied: persist.rebuild_zone's `apply_persisted(fresh,
    to_persisted(old_zone), now, logger=old_zone.logger)` -> `pass`.
    """
    zone = a_zone()
    zone.ingest_presence(101, True, LOCKED_AT)
    assert zone.evaluate(LOCKED_AT, "presence edge").to_state is ZoneState.OCCUPIED

    override = zone.start_override(201, LOCKED_AT)
    assert zone.evaluate(LOCKED_AT, "override started").to_state is ZoneState.OVERRIDDEN
    assert override.expires_at == LOCKED_AT + dt.timedelta(minutes=60)

    # 19:50: somebody edits the file. The zone is rebuilt from the new
    # config, then the state is put back.
    edited = copy.deepcopy(ONE_ZONE)
    edited["zones"][0]["periods"][0]["levels"]["201"] = 80
    reloaded = persist.rebuild_zone(zone, zone_config(edited), EDITED_AT)

    assert reloaded is not zone
    assert reloaded.override is not None, "the lock must survive the edit"
    assert reloaded.override.device_id == 201
    assert reloaded.override.since == LOCKED_AT
    assert reloaded.override.expires_at == override.expires_at
    assert reloaded.override.duration_minutes == 60
    assert reloaded.state is ZoneState.OVERRIDDEN

    # Nothing moved, so the reload is not itself a re-plan, and the zone
    # still writes nothing while the override stands.
    assert reloaded.evaluate(EDITED_AT, "config reload") is None
    assert reloaded.desired_levels(EDITED_AT) == {201: "leave", 202: "leave"}

    # ...and the edit did take effect, so this is a preserved override on a
    # genuinely reloaded config rather than the old object under a new name.
    reloaded.end_override("test", EDITED_AT)
    reloaded.ingest_presence(101, True, EDITED_AT)
    assert reloaded.evaluate(EDITED_AT, "override released").to_state is ZoneState.OCCUPIED
    assert reloaded.desired_levels(EDITED_AT) == {201: 80, 202: 30}


def test_hot_reload_preserves_presence_last_seen():
    """Presence last-seen survives a reload, so an edit does not turn the
    lights off in an occupied room (R13).

    Kills: re-apply only the override state, which is the half of section 5.10
    that is easy to remember.

    Mutation applied: persist.apply_persisted's `last_seen = _read_time(data,
    "presence_last_seen", complain)` -> `last_seen = None`.
    """
    zone = a_zone()
    # A PIR: it trips and drops again, and the hold runs from the drop.
    zone.ingest_presence(101, True, LOCKED_AT)
    zone.ingest_presence(101, False, LOCKED_AT)
    assert zone.evaluate(LOCKED_AT, "presence edge").to_state is ZoneState.OCCUPIED

    # No override at all: this promise is the other half of the state, and a
    # reload that carries only the lock passes every test written about locks.
    assert zone.override is None

    edited = copy.deepcopy(ONE_ZONE)
    edited["zones"][0]["periods"][0]["levels"]["201"] = 80
    reloaded = persist.rebuild_zone(zone, zone_config(edited), EDITED_AT)

    assert reloaded.presence.last_seen == LOCKED_AT
    assert reloaded.presence.active(EDITED_AT, 300) is True
    assert reloaded.state is ZoneState.OCCUPIED

    # The room stays lit. Under the mutation the zone has never seen anybody,
    # goes VACANT on the next evaluation, and turns the lights off on the
    # person who was editing the file.
    assert reloaded.evaluate(EDITED_AT, "config reload") is None
    assert reloaded.desired_levels(EDITED_AT) == {201: 80, 202: 30}

    # And the hold still runs from the original sighting, not from the edit.
    assert reloaded.presence.expiry(300) == LOCKED_AT + dt.timedelta(seconds=300)
    expired = LOCKED_AT + dt.timedelta(seconds=300)
    assert reloaded.evaluate(expired, "presence hold expired").to_state is ZoneState.VACANT


def test_an_unknown_device_id_warns_once_and_the_zone_keeps_running(caplog):
    """A device id in the config that does not resolve warns once, is dropped
    from that zone's working set, and the zone keeps working for its other
    devices (R8, R15).

    Kills: raise and abort the load, and: skip it silently. A zone quietly
    running on three of its five lights is the failure this promise exists to
    prevent.

    Mutation applied: Zone.resolve_lights's `devices.warn_gone_once(
    self.logger, dev_id, self.name)` -> `pass`. The other half -- refusing the
    load -- is pinned by the first assertion: the loader never resolves ids,
    so an id that no longer exists cannot reach it.
    """
    # 201 exists; 202 does not. The load must not care.
    lamp = make_device(201, "dimmer", name="Desk Lamp")
    config = zone_config(ONE_ZONE)
    assert config.lights == (201, 202), "an unknown id is not a config error"

    zone = Zone(config, SUN, logger=logging.getLogger(LOGGER))
    zone.ingest_presence(101, True, LOCKED_AT)
    assert zone.evaluate(LOCKED_AT, "presence edge").to_state is ZoneState.OCCUPIED

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(5):
            found = zone.resolve_lights()

    # Dropped from the working set, and named: not silently absent.
    assert found.live == {201: lamp}
    assert found.gone == (202,)
    assert len(caplog.records) == 1, "once per condition, not once per pass"
    message = caplog.records[0].getMessage()
    assert "202" in message and "Study" in message
    assert "does not exist" in message

    # The zone keeps running for the light it still has, and says out loud
    # that it is one light short rather than looking complete.
    assert zone.desired_levels(LOCKED_AT) == {201: 60, 202: 30}
    assert "unavailable lights=202" in zone.explain(LOCKED_AT)

    # And when the device comes back it is simply there again, with no
    # config change and no restart.
    returned = make_device(202, "dimmer", name="Wall Light")
    assert zone.resolve_lights().live == {201: lamp, 202: returned}
    assert "unavailable" not in zone.explain(LOCKED_AT)
