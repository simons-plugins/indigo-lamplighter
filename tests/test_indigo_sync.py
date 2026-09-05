"""Unit tests for zone state on Indigo devices (PRD sections 5.10, 5.11, 9).

Three sets of keys have to agree or the plugin publishes into a void: the
keys `zone.snapshot()` emits, the keys `indigo_sync` publishes, and the
`<State>` ids `Devices.xml` declares. Nothing at runtime says when they stop
agreeing -- Indigo drops an update naming a state it does not know, and a
state nobody publishes simply sits at its last value looking plausible -- so
they are pinned here, against the PRD's own list.

The rest is the persisted record's round trip through device states, which
PRD section 9 asks for by name, and its tolerance: a garbled value loses the
field, never the record, and never raises on the way through.
"""

import datetime as dt
import logging
import os
import xml.etree.ElementTree as ET

import pytest
from helpers import make_period, make_zone

from lamplighter import compare, indigo_sync, persist

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
LUX = {"device": 302, "dark_below": 2200, "hysteresis": 300}

DEVICES_XML = os.path.join(
    os.path.dirname(__file__),
    os.pardir,
    "Lamplighter.indigoPlugin",
    "Contents",
    "Server Plugin",
    "Devices.xml",
)

#: PRD section 5.10, transcribed. Not imported from the module under test:
#: the point is to fail when the module drifts from the document.
PRD_5_10_STATES = (
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


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def evening(levels=None, **extra):
    return make_period(
        "Evening", "18:00", "23:00", levels=levels or {"201": 60, "202": 30}, **extra
    )


def a_zone(periods=None, **fields):
    fields.setdefault("lights", [201, 202])
    fields.setdefault("lux", dict(LUX))
    return make_zone(
        periods or [evening()], logger=logging.getLogger("test.indigo_sync"), **fields
    )


def an_occupied_zone():
    """A zone that has actually run, so its snapshot is not all defaults."""
    zone = a_zone()
    zone.ingest_presence(101, True, NOW)
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "a person arrived")
    return zone


def a_held_zone():
    """The same zone with an override held by device 201."""
    zone = an_occupied_zone()
    zone.start_override(201, NOW)
    zone.evaluate(NOW, "override started")
    return zone


def keys_of(states):
    return [state["key"] for state in states]


def as_device_states(states):
    """What `dev.states` looks like after `updateStatesOnServer(states)`."""
    return {state["key"]: state["value"] for state in states}


def xml_state_ids(device_type_id):
    root = ET.parse(DEVICES_XML).getroot()
    for device in root.findall("Device"):
        if device.get("id") == device_type_id:
            return [state.get("id") for state in device.find("States").findall("State")]
    raise AssertionError(f"Devices.xml declares no device type {device_type_id!r}")


# ----------------------------------------------------------------- the keys


def test_the_published_keys_are_the_prd_section_5_10_states():
    """The live states are the PRD's list, in its order.

    Kills: quietly dropping a state the PRD promises (`explain` and
    `desired_summary` are the tempting ones -- they are long strings and
    nothing else reads them) or renaming one in passing.
    """
    assert indigo_sync.ZONE_STATE_KEYS == PRD_5_10_STATES


def test_every_key_a_zone_snapshot_emits_is_published():
    """snapshot() and the publisher agree, in both directions.

    Kills: adding a state to `zone.snapshot()` and forgetting to publish it,
    which produces a device state that never moves again and no error
    anywhere; and publishing a key the snapshot does not carry, which Indigo
    rejects -- taking the whole update with it.
    """
    snapshot = an_occupied_zone().snapshot()
    assert set(keys_of(indigo_sync.states_for_zone(snapshot))) == set(snapshot)


def test_devices_xml_declares_exactly_the_states_the_plugin_publishes():
    """The bundle's XML and the module agree, live states and persisted.

    Kills: adding a state to the module and not to Devices.xml. Indigo
    discards an update that names an undeclared state, so the failure mode is
    a zone device that stops updating entirely -- from one added key.
    """
    assert tuple(xml_state_ids("lamplighter_zone")) == indigo_sync.ZONE_DEVICE_STATE_KEYS


def test_devices_xml_declares_exactly_the_controller_states():
    zone = an_occupied_zone()
    engine = _FakeEngine({zone.name: zone})
    published = keys_of(indigo_sync.controller_states(engine))
    assert tuple(xml_state_ids("lamplighter_controller")) == indigo_sync.CONTROLLER_STATE_KEYS
    assert published == list(indigo_sync.CONTROLLER_STATE_KEYS)
    # Named as well as counted: the tuple comparison above passes just as
    # happily if BOTH sides lose a state, and the load record is the one an
    # MCP caller polls, so its absence would be silent at both ends.
    assert "config_loaded_at" in published
    assert "config_zone_count" in published


def test_the_load_record_says_never_rather_than_inventing_a_time():
    """Kills: defaulting config_loaded_at to now, which would tell a caller
    the configuration had just loaded every time the plugin failed to load
    one -- the exact question these two states exist to answer."""
    zone = an_occupied_zone()
    states = as_device_states(indigo_sync.controller_states(_FakeEngine({zone.name: zone})))

    assert states["config_loaded_at"] == ""
    assert states["config_zone_count"] == 0


def test_the_load_record_is_reported_as_it_was_recorded():
    """The count comes from the load, not from the engine.

    Kills: deriving config_zone_count from len(engine.zones). They agree
    today, and would stop agreeing the moment a load is rejected -- which is
    the one time a caller is actually reading them.
    """
    zone = an_occupied_zone()
    engine = _FakeEngine({zone.name: zone})  # one zone live
    states = as_device_states(
        indigo_sync.controller_states(
            engine, "ok", config_loaded_at="2026-09-05T04:05:06", config_zone_count=6
        )
    )

    assert states["config_loaded_at"] == "2026-09-05T04:05:06"
    assert states["config_zone_count"] == 6
    assert states["zones"] == 1


def test_the_manual_lock_device_id_is_the_one_the_engine_records():
    """The two constants are written out separately and must not drift.

    `indigo_sync` deliberately does not import the engine (that would drag
    `indigo` into a module whose whole point is being importable without a
    server), so the id the `lock zone` action records is spelled twice. This
    is what stops the second spelling quietly becoming a device id nobody
    labels.
    """
    from lamplighter.engine import MANUAL_LOCK_DEVICE_ID

    assert indigo_sync._MANUAL_LOCK_DEVICE_ID == MANUAL_LOCK_DEVICE_ID


def test_the_persisted_states_are_the_fields_persist_writes():
    """The `persist_` states cover `to_persisted`, exactly.

    Kills: adding a field to persist.to_persisted() and not to the device,
    which restores a record missing that field on every restart -- silently,
    because apply_persisted treats a missing field as "nothing to restore".
    """
    record = persist.to_persisted(a_held_zone())
    assert set(record) == set(indigo_sync.PERSIST_KEYS)


# --------------------------------------------------------------- the values


def test_every_published_value_is_something_an_indigo_state_can_hold():
    """Strings, numbers and bools only (PRD section 9).

    Kills: publishing a datetime or a dict, which Indigo stores as its repr
    and which no trigger or control page can then read.
    """
    zone = a_held_zone()
    states = indigo_sync.states_for_zone(zone.snapshot())
    states += indigo_sync.persist_to_states(persist.to_persisted(zone))
    for state in states:
        assert isinstance(state["value"], (str, int, float, bool)), state
        assert isinstance(state["uiValue"], str), state


def test_an_unread_lux_sensor_is_not_published_as_zero():
    """No value is "", never 0 (R15).

    Kills: declaring `lux` a number and letting the empty string become 0.0,
    which publishes a reading that looks exactly like a pitch-dark room for a
    sensor that has never answered.
    """
    zone = a_zone()
    zone.evaluate(NOW, "never read")
    published = as_device_states(indigo_sync.states_for_zone(zone.snapshot()))
    assert published["lux"] == ""
    lux_state = next(s for s in indigo_sync.states_for_zone(zone.snapshot()) if s["key"] == "lux")
    assert lux_state["uiValue"] == "unknown"


def test_a_lux_reading_is_published_as_a_readable_number():
    zone = an_occupied_zone()
    states = {s["key"]: s for s in indigo_sync.states_for_zone(zone.snapshot())}
    assert states["lux"]["value"] == "1800"
    assert states["lux"]["uiValue"] == "1800 lux"


def test_a_zone_with_no_override_says_none_rather_than_zero():
    """Kills: publishing device id 0 for "nobody has taken this zone over"."""
    zone = an_occupied_zone()
    states = {s["key"]: s for s in indigo_sync.states_for_zone(zone.snapshot())}
    assert states["override_device"]["value"] == ""
    assert states["override_device"]["uiValue"] == "none"


def test_a_missing_snapshot_key_publishes_nothing_rather_than_raising():
    """A snapshot short of a key still publishes the rest.

    Kills: indexing the snapshot directly, which turns one missing key into a
    KeyError inside the engine's zone-changed callback -- and that callback
    swallowing it means the whole device stops updating, not just the state.
    """
    states = as_device_states(indigo_sync.states_for_zone({"state": "vacant"}))
    assert states["state"] == "vacant"
    assert states["explain"] == ""


def test_the_controller_sums_the_zones_counters():
    """Kills: reporting one zone's counters, or counting zones as enabled
    when the plugin itself is off."""
    first, second = an_occupied_zone(), an_occupied_zone()
    first.evaluations_today, first.writes_today, first.overrides_today = 3, 2, 1
    second.evaluations_today, second.writes_today, second.overrides_today = 4, 0, 0
    second.set_enabled(enabled=False)

    engine = _FakeEngine({"First": first, "Second": second})
    states = as_device_states(indigo_sync.controller_states(engine, "ok"))

    assert states["zones"] == 2
    assert states["zones_enabled"] == 1
    assert states["evaluations_today"] == 7
    assert states["writes_today"] == 2
    assert states["overrides_today"] == 1
    assert states["config_status"] == "ok"


def test_the_controller_carries_the_configuration_status():
    """The one place a person sees "yesterday's config is what is running"."""
    engine = _FakeEngine({})
    states = as_device_states(indigo_sync.controller_states(engine, "invalid at zones/0/name"))
    assert states["config_status"] == "invalid at zones/0/name"


class _FakeEngine:
    """Just enough engine for `controller_states`: it only reads zones."""

    def __init__(self, zones):
        self.zones = zones


# ----------------------------------------------------------- the round trip


def test_a_held_override_round_trips_through_device_states():
    """The point of persisting on the device (R13).

    Kills: writing the record but decoding it back as strings, so the
    restored override's `expires_at` never parses and the lock is lost on
    every restart -- which is exactly the fork's behaviour this replaces.
    """
    zone = a_held_zone()
    record = persist.to_persisted(zone)

    states = as_device_states(indigo_sync.persist_to_states(record))
    assert indigo_sync.states_to_persisted(states) == record


def test_a_zone_with_no_override_and_no_verdict_round_trips():
    """The empty case, which is what a fresh zone device carries.

    Kills: encoding an unset `dark` as False. A zone that comes back with a
    verdict it never took decides the first reading inside the hysteresis
    band on the wrong side of it (see lux.py), and a kitchen with its lights
    on reads inside that band all evening.
    """
    zone = a_zone()
    record = persist.to_persisted(zone)
    assert record["dark"] is None

    states = as_device_states(indigo_sync.persist_to_states(record))
    assert states["persist_dark"] == ""

    restored = indigo_sync.states_to_persisted(states)
    assert "dark" not in restored
    assert restored["version"] == persist.VERSION


def test_a_false_dark_verdict_survives_as_false():
    """False and unset are different answers and must stay different."""
    zone = a_zone()
    zone.ingest_lux(9000, NOW)
    zone.is_dark()
    record = persist.to_persisted(zone)
    assert record["dark"] is False

    states = as_device_states(indigo_sync.persist_to_states(record))
    assert indigo_sync.states_to_persisted(states)["dark"] is False


def test_a_device_that_has_never_been_written_restores_nothing():
    """An empty record, not a record full of empties.

    Kills: returning `{"version": "", ...}`, which makes apply_persisted
    complain about a missing version on the first start of every zone -- a
    warning that means nothing and trains the reader to ignore the real ones.
    """
    assert indigo_sync.states_to_persisted({}) == {}
    assert indigo_sync.states_to_persisted({key: "" for key in indigo_sync.PERSIST_STATE_KEYS}) == {}


def test_the_manual_lock_device_id_survives_the_round_trip():
    """-1 is the lock-zone action's device id; 0 would read as "no override"."""
    zone = a_zone()
    zone.ingest_presence(101, True, NOW)
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "setup")
    zone.start_override(-1, NOW)

    states = as_device_states(indigo_sync.persist_to_states(persist.to_persisted(zone)))
    assert indigo_sync.states_to_persisted(states)["override_device"] == -1


# ------------------------------------------------------------- the garbling


def test_a_garbled_state_is_passed_through_rather_than_guessed_at():
    """Decoding never raises and never invents a value.

    Kills: `int(raw)` without a guard (one hand-edited state takes the whole
    restore down at startup) and `bool(raw)` on `dark` (the string "banana"
    is truthy, so a garbled verdict would silently become "dark").
    """
    states = {
        "persist_version": "1",
        "persist_dark": "banana",
        "persist_override_device": "not a number",
        "persist_override_expires": "yesterday",
    }
    record = indigo_sync.states_to_persisted(states)
    assert record["dark"] == "banana"
    assert record["override_device"] == "not a number"
    assert record["override_expires"] == "yesterday"


def test_a_garbled_record_loses_the_field_and_keeps_the_zone(caplog):
    """Applying it complains per field and leaves the zone usable (R15).

    Kills: refusing the whole record over one bad value, and accepting it
    silently. The complaints are returned as well as logged so a caller can
    put them in a payload.
    """
    zone = a_zone()
    states = {
        "persist_version": "1",
        "persist_dark": "banana",
        "persist_presence_last_seen": "2026-09-04T19:46:03",
    }
    record = indigo_sync.states_to_persisted(states)

    with caplog.at_level(logging.WARNING):
        complaints = persist.apply_persisted(zone, record, NOW)

    assert any("dark" in complaint for complaint in complaints)
    assert zone.presence.last_seen == dt.datetime(2026, 9, 4, 19, 46, 3)
    assert zone.lux.verdict is None


def test_a_states_mapping_that_will_not_answer_restores_nothing():
    """A lookup that breaks is not a record that says "no override".

    Kills: letting an exception out of the read, which would take the whole
    startup restore down for every zone because one device would not answer.
    """

    class Hostile:
        def get(self, key):
            raise RuntimeError("the device states are not readable")

    assert indigo_sync.states_to_persisted(Hostile()) == {}
