"""The marks lane's bright-off-duty span (a stretch, not an instant)."""

import json

import pytest

from test_web_page_js import _run_page_logic


@pytest.mark.parametrize("name,events,expected", [
    ("runs_until_the_next_state_event", [
        {"t": "2026-09-08T08:10:00", "k": "offduty", "cause": "bright"},
        {"t": "2026-09-08T08:10:00", "k": "state", "to": "off_duty"},
        {"t": "2026-09-08T17:40:00", "k": "state", "to": "vacant"},
    ], [34200000]),
    ("still_running_has_a_null_end", [
        {"t": "2026-09-08T08:10:00", "k": "offduty", "cause": "bright"},
    ], [None]),
    ("other_off_duty_causes_are_not_bright", [
        {"t": "2026-09-08T08:10:00", "k": "offduty", "cause": "no_period"},
    ], []),
    ("a_state_event_at_the_same_instant_does_not_end_it", [
        {"t": "2026-09-08T08:10:00", "k": "state", "to": "off_duty"},
        {"t": "2026-09-08T08:10:00", "k": "offduty", "cause": "bright"},
        {"t": "2026-09-08T09:00:00", "k": "state", "to": "occupied"},
    ], [3000000]),
])
def test_brightspans(tmp_path, name, events, expected):
    """A bright off-duty stretch is drawn from the offduty event to the next
    state change, or to now. Kills: ending it at the SAME-instant state event
    that accompanies it (zero-width span), and treating every offduty cause
    as bright."""
    driver = f"""
        const spans = brightSpans({json.dumps(events)}).map(s => s.end === null ? null : s.end - s.start);
        console.log(JSON.stringify({{ spans }}));
    """
    output = _run_page_logic(tmp_path, driver, name=f"brightspans_{name}.js")
    # Durations in ms rather than clock strings, so the test does not depend
    # on the machine's timezone: 08:10 to 17:40 is 34,200,000 ms anywhere.
    assert output["spans"] == expected
