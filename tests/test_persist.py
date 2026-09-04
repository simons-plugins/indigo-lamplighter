"""Unit tests for persisted zone state (PRD R13; sections 5.10, 5.11, 9).

Section 9 names this as a risk: Indigo device states are strings and numbers,
so timestamps need a fixed format and a version key, and the mitigation it
asks for is "a single persist.py with round-trip tests". These are those,
plus the tolerance rules -- a record that is missing, garbled or from a
future version loses the field, never the record.
"""

import datetime as dt
import logging

import pytest
from helpers import make_period, make_zone

from lamplighter import compare, persist
from lamplighter.zone import ZoneState

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
LUX = {"device": 302, "dark_below": 2200, "hysteresis": 300}


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def at(**kwargs):
    return NOW + dt.timedelta(**kwargs)


def evening(levels=None, **extra):
    return make_period(
        "Evening", "18:00", "23:00", levels=levels or {"201": 60, "202": 30}, **extra
    )


def a_zone(periods=None, **fields):
    fields.setdefault("lights", [201, 202])
    fields.setdefault("lux", dict(LUX))
    return make_zone(periods or [evening()], logger=logging.getLogger("test.persist"), **fields)


def a_held_zone():
    """An occupied, dark zone with an override held by device 201."""
    zone = a_zone()
    zone.ingest_presence(101, True, NOW)
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "setup")
    zone.start_override(201, NOW)
    zone.evaluate(NOW, "override started")
    return zone


# ------------------------------------------------------------- the round trip


def test_a_clean_record_round_trips_whole():
    before = a_held_zone()
    record = persist.to_persisted(before)

    after = a_zone()
    assert persist.apply_persisted(after, record, at(minutes=1)) == []

    assert after.presence.last_seen == before.presence.last_seen
    assert after.lux.verdict is True
    assert after.override.device_id == 201
    assert after.override.since == before.override.since
    assert after.override.expires_at == before.override.expires_at
    assert after.override.duration_minutes == before.override.duration_minutes
    assert after.override.extend_minutes == before.override.extend_minutes
    assert after.override.extended_count == before.override.extended_count


def test_the_record_is_strings_numbers_and_booleans_only():
    """Indigo device states hold nothing else (section 9)."""
    record = persist.to_persisted(a_held_zone())
    for key, value in record.items():
        assert isinstance(value, (str, int, float, bool)), key
    assert record["presence_last_seen"] == "2026-09-04T20:00:00"
    assert record["override_expires"] == "2026-09-04T21:00:00"
    assert record["version"] == 1


def test_a_zone_with_nothing_held_persists_nothing_to_restore():
    record = persist.to_persisted(a_zone())
    assert record["presence_last_seen"] == ""
    assert record["override_device"] == ""

    after = a_zone()
    assert persist.apply_persisted(after, record, NOW) == []
    assert after.presence.last_seen is None
    assert after.override is None


def test_the_restored_override_still_expires_on_its_own_clock():
    """The point of R13: a lock made at 19:46 is still a lock, with its
    original expiry, after a restart at 19:50."""
    before = a_held_zone()
    after = a_zone()
    persist.apply_persisted(after, persist.to_persisted(before), at(minutes=4))

    after.ingest_presence(101, True, at(minutes=4))
    assert after.evaluate(at(minutes=4), "startup").to_state is ZoneState.OVERRIDDEN

    # Kept occupied, so what ends the override below is its own expiry and
    # not unlock-on-leave -- two different exits from the same state.
    after.ingest_presence(101, True, at(minutes=59))
    assert after.evaluate(at(minutes=59), "tick") is None
    assert after.evaluate(at(minutes=60), "override expiry").to_state is ZoneState.OCCUPIED


# --------------------------------------------------------------- tolerance


def test_a_record_that_is_not_an_object_loses_the_record_not_the_zone(caplog):
    zone = a_zone()
    with caplog.at_level(logging.WARNING, logger="test.persist"):
        complaints = persist.apply_persisted(zone, "corrupted", NOW)
    assert len(complaints) == 1
    assert "not an object" in complaints[0]
    assert caplog.records


def test_a_missing_version_is_read_as_one_and_the_fields_still_apply():
    zone = a_zone()
    complaints = persist.apply_persisted(
        zone, {"presence_last_seen": "2026-09-04T19:58:00"}, NOW
    )
    assert any("no version key" in c for c in complaints)
    assert zone.presence.last_seen == dt.datetime(2026, 9, 4, 19, 58)


def test_a_future_version_warns_and_keeps_what_it_understands():
    """A downgrade must not empty an occupied room. Every field is validated
    on its own, so an unknown version costs a warning, not the state."""
    record = persist.to_persisted(a_held_zone())
    record["version"] = 4
    record["some_future_field"] = {"not": "a scalar"}

    zone = a_zone()
    complaints = persist.apply_persisted(zone, record, at(minutes=1))
    assert any("version 4" in c for c in complaints)
    assert zone.presence.last_seen == NOW
    assert zone.override.device_id == 201


@pytest.mark.parametrize("garbled", ["yesterday", "2026-09-04 20:00:00", 17, []])
def test_a_garbled_timestamp_loses_that_field_only(garbled):
    record = persist.to_persisted(a_held_zone())
    record["presence_last_seen"] = garbled

    zone = a_zone()
    complaints = persist.apply_persisted(zone, record, at(minutes=1))
    assert any("presence_last_seen" in c for c in complaints)
    assert zone.presence.last_seen is None
    assert zone.override.device_id == 201  # the rest survived
    assert zone.lux.verdict is True


def test_a_garbled_dark_verdict_leaves_the_trigger_unseeded():
    record = persist.to_persisted(a_held_zone())
    record["dark"] = "yes"

    zone = a_zone()
    complaints = persist.apply_persisted(zone, record, at(minutes=1))
    assert any("dark" in c for c in complaints)
    assert zone.lux.verdict is None


def test_an_override_with_no_expiry_is_not_half_restored():
    """Kills: build the Override anyway with a default expiry, which invents a
    lock nobody created and holds a room with it."""
    record = persist.to_persisted(a_held_zone())
    record["override_expires"] = ""

    zone = a_zone()
    complaints = persist.apply_persisted(zone, record, at(minutes=1))
    assert zone.override is None
    assert any("incomplete" in c for c in complaints)


def test_an_override_with_no_start_time_falls_back_to_now_and_says_so():
    """`since` is what unlock-on-leave is judged against, so losing it is not
    cosmetic; now is the conservative replacement."""
    record = persist.to_persisted(a_held_zone())
    record["override_since"] = ""

    zone = a_zone()
    complaints = persist.apply_persisted(zone, record, at(minutes=1))
    assert zone.override.since == at(minutes=1)
    assert any("start time" in c for c in complaints)


def test_a_last_seen_from_the_future_is_clamped_rather_than_believed():
    zone = a_zone()
    complaints = persist.apply_persisted(
        zone, {"version": 1, "presence_last_seen": "2026-09-05T20:00:00"}, NOW
    )
    assert zone.presence.last_seen == NOW
    assert any("future" in c for c in complaints)


def test_a_garbled_counter_uses_the_configured_default():
    record = persist.to_persisted(a_held_zone())
    record["override_extend_minutes"] = "half an hour"

    zone = a_zone(override={"extend_minutes": 30})
    complaints = persist.apply_persisted(zone, record, at(minutes=1))
    assert zone.override.extend_minutes == 30
    assert any("override_extend_minutes" in c for c in complaints)


# ------------------------------------------------------------ rebuild_zone


def test_rebuild_carries_the_override_and_presence_across_a_config_edit():
    before = a_held_zone()
    before.evaluations_today = 12
    before.writes_today = 3

    edited = a_zone([evening(levels={"201": 80, "202": 10})]).config
    after = persist.rebuild_zone(before, edited, at(minutes=4))

    assert after.config is edited
    assert after.override.device_id == 201
    assert after.override.expires_at == before.override.expires_at
    assert after.presence.last_seen == NOW
    assert after.lux.verdict is True
    assert after.state is ZoneState.OVERRIDDEN
    assert (after.evaluations_today, after.writes_today, after.overrides_today) == (12, 3, 1)
    assert after.presence.on_devices == {101}


def test_rebuild_keeps_the_lux_reading_when_the_sensor_is_the_same():
    before = a_held_zone()
    after = persist.rebuild_zone(before, a_zone().config, at(minutes=1))
    assert after.lux.value == 1800
    assert after.lux.read_at == NOW


def test_rebuild_drops_the_lux_reading_when_the_sensor_changed():
    """A reading from a different device is not this zone's reading."""
    before = a_held_zone()
    moved = a_zone(lux={**LUX, "device": 999}).config
    after = persist.rebuild_zone(before, moved, at(minutes=1))
    assert after.lux.value is None


def test_rebuild_takes_enabled_from_the_new_config_not_the_old_zone():
    """A config edit that switches a zone off has to take effect, or the file
    stops meaning anything."""
    before = a_held_zone()
    switched_off = a_zone(enabled=False).config
    after = persist.rebuild_zone(before, switched_off, at(minutes=1))
    assert after.enabled is False
    assert after.evaluate(at(minutes=1), "config reload").to_state is ZoneState.OFF_DUTY


def test_rebuild_carries_the_global_enable_which_is_not_in_the_file():
    before = a_held_zone()
    before.set_enabled(plugin_enabled=False)
    after = persist.rebuild_zone(before, a_zone().config, at(minutes=1))
    assert after.plugin_enabled is False


def test_rebuild_drops_a_presence_device_the_edit_removed():
    before = a_held_zone()
    before.ingest_presence(101, True, NOW)
    narrowed = a_zone(presence_devices=[102]).config
    after = persist.rebuild_zone(before, narrowed, at(minutes=1))
    assert after.presence.on_devices == set()
    assert after.presence.last_seen == NOW  # the hold is a fact about the room
