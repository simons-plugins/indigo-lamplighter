"""The timeline's data source (PRD section 12): a bounded, per-zone event log.

Each test is named for the mutation it kills, per workspace convention. The
module has no Indigo dependency at all, so these run against `History` and
`ZoneHistory` directly -- the engine/plugin wiring is pinned separately in
`test_engine.py` and `test_plugin_wiring.py`.
"""

import datetime as dt
import json
import logging

import pytest

from lamplighter.history import MAX_EVENTS, RETENTION_HOURS, VERSION, History, ZoneHistory

NOW = dt.datetime(2026, 9, 8, 20, 0, 0)
LOG = logging.getLogger("test.history")


@pytest.fixture(autouse=True)
def _log():
    return LOG


# --------------------------------------------------------------- appending


def test_append_records_the_timestamp_and_kind():
    """Kills: dropping `t` or `k`, which the page's parser requires on
    every event to place and classify it."""
    zh = ZoneHistory()
    zh.append(NOW, "state", to="occupied", cause="presence: PIR")
    assert zh.events == [
        {"t": "2026-09-08T20:00:00", "k": "state", "to": "occupied", "cause": "presence: PIR"}
    ]


def test_events_older_than_retention_are_trimmed_on_every_append():
    """Kills: trimming only at load time, which would let a long-running
    plugin's memory grow forever between restarts."""
    zh = ZoneHistory()
    old = NOW - dt.timedelta(hours=RETENTION_HOURS + 1)
    zh.append(old, "state", to="vacant")
    zh.append(NOW, "state", to="occupied")
    assert len(zh.events) == 1
    assert zh.events[0]["to"] == "occupied"


def test_an_event_exactly_at_the_retention_boundary_is_kept():
    """Kills: an off-by-one that trims an event still exactly 48h old."""
    zh = ZoneHistory()
    boundary = NOW - dt.timedelta(hours=RETENTION_HOURS)
    zh.append(boundary, "state", to="vacant")
    zh.append(NOW, "state", to="occupied")
    assert len(zh.events) == 2


def test_the_cap_drops_the_oldest_events_and_marks_a_gap():
    """Kills: dropping the newest events instead of the oldest, and kills
    silently truncating with no marker -- a reader must not mistake a
    dropped run of activity for a quiet one (R15)."""
    zh = ZoneHistory()
    for i in range(MAX_EVENTS + 10):
        zh.append(NOW, "write", id=1, level=i % 100)
    assert len(zh.events) == MAX_EVENTS + 1  # the cap, plus one gap marker
    assert zh.events[0]["k"] == "gap"
    # The newest events survived; the very first ones did not.
    assert zh.events[-1]["level"] == (MAX_EVENTS + 9) % 100


def test_the_cap_does_not_insert_a_second_gap_marker_back_to_back():
    """Kills: a gap marker inserted on every single overflowing append,
    which would fill the lane with gap glyphs instead of one."""
    zh = ZoneHistory()
    for i in range(MAX_EVENTS + 50):
        zh.append(NOW, "write", id=1, level=1)
    gap_count = sum(1 for event in zh.events if event["k"] == "gap")
    assert gap_count == 1


def test_age_trimming_alone_never_inserts_a_gap_marker():
    """Kills: treating every trim as a truncation. A quiet Tuesday aging out
    needs no explanation; only the count-based cap does."""
    zh = ZoneHistory()
    old = NOW - dt.timedelta(hours=RETENTION_HOURS + 1)
    zh.append(old, "state", to="vacant")
    zh.append(NOW, "state", to="occupied")
    assert all(event["k"] != "gap" for event in zh.events)


# ----------------------------------------------------------- light dedupe


def test_a_light_reporting_the_same_level_twice_is_not_recorded_twice():
    """Kills: recording every light report regardless of whether the level
    actually moved, which would fill the lane with noise from link-quality
    and last-seen updates that carry no level change."""
    zh = ZoneHistory()
    assert zh.append_light(NOW, 201, 60) is True
    assert zh.append_light(NOW, 201, 60) is False
    assert len(zh.events) == 1


def test_a_light_reporting_a_different_level_is_recorded():
    """Kills: deduping by device alone, ignoring the level -- a light
    genuinely dimming from 60 to 30 must produce a second point."""
    zh = ZoneHistory()
    zh.append_light(NOW, 201, 60)
    later = NOW + dt.timedelta(minutes=5)
    assert zh.append_light(later, 201, 30) is True
    assert len(zh.events) == 2


def test_two_different_lights_at_the_same_level_both_record():
    """Kills: a dedupe table keyed on level alone rather than on
    (device, level), which would swallow every second light's first report."""
    zh = ZoneHistory()
    zh.append_light(NOW, 201, 60)
    assert zh.append_light(NOW, 202, 60) is True
    assert len(zh.events) == 2


def test_rebuild_last_levels_restores_the_dedupe_table_after_load():
    """Kills: forgetting the dedupe state on load, which would double up the
    very next report for every light after every restart."""
    zh = ZoneHistory()
    zh.events = [
        {"t": "2026-09-08T18:00:00", "k": "light", "id": 201, "level": 60},
        {"t": "2026-09-08T19:00:00", "k": "light", "id": 201, "level": 30},
    ]
    zh.rebuild_last_levels()
    assert zh.append_light(NOW, 201, 30) is False
    assert zh.append_light(NOW, 201, 45) is True


# --------------------------------------------------------- History routing


def test_history_creates_a_zone_on_first_use():
    history = History(logger=LOG)
    history.record_state("Kitchen", NOW, "occupied", "vacant", "presence: PIR")
    assert "Kitchen" in history.zones
    assert history.zones["Kitchen"].events[0]["k"] == "state"


def test_recording_marks_the_store_dirty():
    """Kills: forgetting to set `dirty`, which is what tells the plugin's
    periodic flush there is anything worth writing."""
    history = History(logger=LOG)
    assert history.dirty is False
    history.record_write("Kitchen", NOW, 201, 60)
    assert history.dirty is True


def test_a_duplicate_light_report_does_not_mark_the_store_dirty():
    """Kills: marking dirty unconditionally, which would make the periodic
    flush write every WRITE_INTERVAL_SECONDS even in a house with nothing
    happening beyond link-quality noise."""
    history = History(logger=LOG)
    history.record_light("Kitchen", NOW, 201, 60)
    history.dirty = False
    history.record_light("Kitchen", NOW, 201, 60)
    assert history.dirty is False


def test_record_state_omits_the_from_field_when_there_is_none():
    """Kills: always writing a `from` key, which would put `"from": null`
    (or a stringified None) on the very first transition a zone ever makes."""
    history = History(logger=LOG)
    history.record_state("Kitchen", NOW, "occupied", None, "plugin startup")
    assert "from" not in history.zones["Kitchen"].events[0]


def test_record_override_carries_phase_device_and_reason():
    history = History(logger=LOG)
    history.record_override("Hallway", NOW, "start", 1445308831, "override: Hallway Lamp")
    event = history.zones["Hallway"].events[0]
    assert event == {
        "t": "2026-09-08T20:00:00",
        "k": "override",
        "phase": "start",
        "device": 1445308831,
        "reason": "override: Hallway Lamp",
    }


def test_record_offduty_carries_the_cause():
    history = History(logger=LOG)
    history.record_offduty("Kitchen", NOW, "bright")
    assert history.zones["Kitchen"].events[0]["cause"] == "bright"


# --------------------------------------------------------- to_json / load


def test_to_json_round_trips_through_load():
    """Kills: any asymmetry between the writer and the reader -- a field
    renamed on one side only would pass every test that checks each in
    isolation."""
    history = History(logger=LOG)
    history.record_state("Kitchen", NOW, "occupied", "vacant", "presence: PIR")
    history.record_light("Kitchen", NOW, 201, 60)
    text = history.to_json(NOW)

    restored = History(logger=LOG)
    restored.load(text, now=NOW)
    assert restored.zones["Kitchen"].events == history.zones["Kitchen"].events


def test_to_json_envelope_carries_version_and_retention():
    history = History(logger=LOG)
    payload = json.loads(history.to_json(NOW))
    assert payload["version"] == VERSION
    assert payload["retention_hours"] == RETENTION_HOURS
    assert payload["generated_at"] == "2026-09-08T20:00:00"
    assert payload["zones"] == {}


def test_load_of_malformed_json_warns_once_and_starts_empty(caplog):
    """Kills: raising out of load() on bad input, which would take startup
    down over a corrupted data file (R15 -- this must degrade, not crash)."""
    history = History(logger=LOG)
    with caplog.at_level(logging.WARNING):
        history.load("{not json", now=NOW)
    assert history.zones == {}
    assert any("could not be parsed" in r.getMessage() for r in caplog.records)


def test_load_of_the_wrong_version_warns_and_starts_empty(caplog):
    history = History(logger=LOG)
    text = json.dumps({"version": 999, "zones": {}})
    with caplog.at_level(logging.WARNING):
        history.load(text, now=NOW)
    assert history.zones == {}
    assert any("version" in r.getMessage() for r in caplog.records)


def test_load_of_a_non_object_warns_and_starts_empty(caplog):
    history = History(logger=LOG)
    with caplog.at_level(logging.WARNING):
        history.load("[1, 2, 3]", now=NOW)
    assert history.zones == {}
    assert caplog.records


def test_load_with_no_zones_object_warns_and_starts_empty(caplog):
    history = History(logger=LOG)
    text = json.dumps({"version": VERSION})
    with caplog.at_level(logging.WARNING):
        history.load(text, now=NOW)
    assert history.zones == {}
    assert caplog.records


def test_load_skips_one_zone_whose_events_are_not_a_list_without_losing_others():
    history = History(logger=LOG)
    text = json.dumps(
        {
            "version": VERSION,
            "zones": {
                "Broken": {"events": "not a list"},
                "Kitchen": {"events": [{"t": "2026-09-08T19:00:00", "k": "state", "to": "vacant"}]},
            },
        }
    )
    history.load(text, now=NOW)
    assert "Broken" not in history.zones
    assert "Kitchen" in history.zones


def test_load_drops_individual_events_missing_t_or_k_without_losing_the_zone():
    history = History(logger=LOG)
    text = json.dumps(
        {
            "version": VERSION,
            "zones": {
                "Kitchen": {
                    "events": [
                        {"t": "2026-09-08T19:00:00", "k": "state", "to": "vacant"},
                        {"k": "state", "to": "occupied"},  # no t
                        {"t": "2026-09-08T19:30:00"},  # no k
                        "not even a dict",
                    ]
                }
            },
        }
    )
    history.load(text, now=NOW)
    assert len(history.zones["Kitchen"].events) == 1


def test_load_trims_stale_events_the_same_way_append_does():
    """Kills: loading events verbatim with no age check, which would let a
    history file from a plugin that was down for a week resurrect events
    far outside the retention window on the next startup."""
    history = History(logger=LOG)
    old = NOW - dt.timedelta(hours=RETENTION_HOURS + 5)
    text = json.dumps(
        {
            "version": VERSION,
            "zones": {"Kitchen": {"events": [{"t": old.strftime("%Y-%m-%dT%H:%M:%S"), "k": "state", "to": "vacant"}]}},
        }
    )
    history.load(text, now=NOW)
    assert history.zones["Kitchen"].events == []
