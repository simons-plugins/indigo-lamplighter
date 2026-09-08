"""Syntax and pure-function smoke tests for the bundled status page's inline
`<script>` blocks.

These need a `node` binary on PATH and are skipped without one -- `test.yml`
runs on `ubuntu-latest`, which has Node preinstalled, so they run in CI even
though nothing else in this suite needs a JS runtime.

Each `<script>` block is pulled out of the real, shipped
`lamplighter.html` with stdlib `html.parser` (not a regex -- the page has
attributes and nested angle brackets inside string literals that a naive
regex would mishandle), written to a temp file, and syntax-checked with
`node --check`. The page's `init()` -- the very last thing either script
block does -- calls itself unconditionally, so a pure-function check evals
*only* the second (page-logic) block: with the first block's `IndigoAPI`
class absent, `init()`'s own `typeof IndigoAPI === "undefined"` guard makes
it bail out through `showErr()` rather than trying to open a real
connection, which is why only a minimal `document` stub is needed.
"""

import datetime
import json
import os
import shutil
import subprocess
import textwrap
from html.parser import HTMLParser

import pytest


def dt_ms(iso_local):
    """`iso_local` (no timezone, "YYYY-MM-DDTHH:MM:SS") as epoch milliseconds
    in the LOCAL timezone -- the same interpretation node's `new Date(iso)`
    (no `Z`/offset) and `new Date(y,m,d,...)` both use, so a Python-computed
    expected value and node's actual one are comparable without pinning a
    timezone into the test itself."""
    naive = datetime.datetime.strptime(iso_local, "%Y-%m-%dT%H:%M:%S")
    local = naive.astimezone()
    return int(local.timestamp() * 1000)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
PAGE_PATH = os.path.join(
    REPO_ROOT, "Lamplighter.indigoPlugin", "Contents", "Resources", "pages",
    "lamplighter.html",
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


class _ScriptExtractor(HTMLParser):
    """Collects the text content of every inline (no `src=`) <script> tag."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocks = []
        self._in_inline_script = False
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            has_src = any(name == "src" for name, _value in attrs)
            self._in_inline_script = not has_src
            self._buffer = []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_inline_script:
            self.blocks.append("".join(self._buffer))
        self._in_inline_script = False

    def handle_data(self, data):
        if self._in_inline_script:
            self._buffer.append(data)


def _extract_script_blocks():
    with open(PAGE_PATH, "r", encoding="utf-8") as handle:
        html = handle.read()
    extractor = _ScriptExtractor()
    extractor.feed(html)
    return extractor.blocks


def test_the_page_has_the_two_expected_script_blocks():
    blocks = _extract_script_blocks()
    assert len(blocks) == 2
    assert "class IndigoAPI" in blocks[0]
    assert "function parseDesired" in blocks[1]
    assert "function levelLabel" in blocks[1]


@pytest.mark.parametrize("index", [0, 1])
def test_each_script_block_is_valid_javascript(tmp_path, index):
    blocks = _extract_script_blocks()
    script_path = tmp_path / f"block_{index}.js"
    script_path.write_text(blocks[index], encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node --check failed on script block {index}:\n{result.stderr}"
    )


def test_parsedesired_and_levellabel_behave_as_documented(tmp_path):
    """Evaluate the page-logic block (script index 1) under node with a
    minimal `document` stub. `IndigoAPI` (defined only in block 0) is
    deliberately left undefined, so `init()`'s own guard makes it bail
    through `showErr()` instead of trying to reach a real Indigo server."""
    blocks = _extract_script_blocks()
    page_logic = blocks[1]

    stub = textwrap.dedent(
        """
        const document = {
            getElementById: () => ({ textContent: "", classList: { toggle: () => {} } }),
        };
        """
    )
    driver = textwrap.dedent(
        """
        const parsed = parseDesired("1=100, 2=leave, junk, 3=off");
        console.log(JSON.stringify({
            itemCount: parsed.items.length,
            unparsedCount: parsed.unparsedCount,
            leaveLabel: levelLabel("leave"),
        }));
        """
    )

    script_path = tmp_path / "page_logic.js"
    script_path.write_text(stub + page_logic + driver, encoding="utf-8")

    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"node failed evaluating the page-logic block:\n{result.stderr}"

    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output["itemCount"] == 3
    assert output["unparsedCount"] == 1
    assert output["leaveLabel"] == "left alone"


_DOCUMENT_STUB = textwrap.dedent(
    """
    const document = {
        getElementById: () => ({ textContent: "", classList: { toggle: () => {} } }),
    };
    """
)


def _run_page_logic(tmp_path, driver, name="driver.js"):
    """Evaluate the real page-logic block under node, plus `driver`, and
    return the JSON object its last `console.log` line printed.

    Same shape as `test_parsedesired_and_levellabel_behave_as_documented`
    above: `IndigoAPI` is left undefined so `init()`'s own guard bails
    through `showErr()` rather than reaching for a real connection.
    """
    blocks = _extract_script_blocks()
    page_logic = blocks[1]
    script_path = tmp_path / name
    script_path.write_text(_DOCUMENT_STUB + page_logic + driver, encoding="utf-8")

    result = subprocess.run(
        ["node", str(script_path)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node failed evaluating {name}:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# --------------------------------------------------- verdictFor (table-driven)

_VERDICT_CASES = [
    (
        "occupied_with_a_level_to_switch",
        {"state": "occupied", "period": "Evening", "desired_summary": "1=60"},
        False,
        "Someone is here — lights at Evening levels.",
    ),
    (
        "occupied_nothing_to_switch",
        {"state": "occupied", "period": "Evening", "desired_summary": "1=leave"},
        False,
        "Someone is here — nothing to switch in Evening.",
    ),
    (
        # No presence_last_seen -> no "since HH:MM" clause. The clause is
        # covered separately below, without pinning node's locale-dependent
        # time formatting into this table.
        "vacant_lights_off",
        {"state": "vacant", "desired_summary": "1=off"},
        False,
        "Empty — lights off.",
    ),
    (
        "vacant_dimmed_to_vacant_levels",
        {"state": "vacant", "desired_summary": "1=25"},
        False,
        "Empty — lights dimmed to vacant levels.",
    ),
    (
        # No override_expires -> the "it expires" fallback, same reason.
        "overridden_names_the_expiry",
        {"state": "overridden"},
        False,
        "Changed by hand — holding whatever the lights are now until it expires.",
    ),
    (
        "off_duty_bright_names_the_lux",
        {"state": "off_duty", "off_duty_cause": "bright", "lux": "291"},
        False,
        "Bright enough (291 lx) — lights left off.",
    ),
    (
        "off_duty_no_period",
        {"state": "off_duty", "off_duty_cause": "no_period"},
        False,
        "No period covers now — lights left alone.",
    ),
    (
        "off_duty_disabled_cause",
        {"state": "off_duty", "off_duty_cause": "disabled"},
        False,
        "Zone disabled — lights left alone.",
    ),
    (
        "the_device_itself_is_off",
        {"state": "occupied", "desired_summary": "1=60"},
        True,
        "Zone switched off in Indigo.",
    ),
]


@pytest.mark.parametrize("name,states,off_flag,expected", _VERDICT_CASES, ids=[c[0] for c in _VERDICT_CASES])
def test_verdictfor_table(tmp_path, name, states, off_flag, expected):
    driver = f"""
        console.log(JSON.stringify({{
            verdict: verdictFor({json.dumps(states)}, {json.dumps(off_flag)}),
        }}));
    """
    output = _run_page_logic(tmp_path, driver, name=f"verdict_{name}.js")
    assert output["verdict"] == expected


def test_verdictfor_names_the_since_and_until_clock_times(tmp_path):
    """The "since"/"until" clauses use fmtTime, whose format is node's local
    default -- so the expected value is computed the same way, inside node,
    rather than a hardcoded string that would pin a locale."""
    driver = """
        const seen = "2026-09-04T20:00:00";
        const expires = "2026-09-04T21:00:00";
        console.log(JSON.stringify({
            vacant: verdictFor({ state: "vacant", presence_last_seen: seen, desired_summary: "1=off" }, false),
            overridden: verdictFor({ state: "overridden", override_expires: expires }, false),
            expectedSince: fmtTime(seen),
            expectedUntil: fmtTime(expires),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="verdict_clock_times.js")
    assert output["vacant"] == f"Empty since {output['expectedSince']} — lights off."
    assert output["overridden"] == (
        f"Changed by hand — holding whatever the lights are now until {output['expectedUntil']}."
    )


# ---------------------------------------------------- actualState (table-driven)

_ACTUAL_STATE_CASES = [
    ("no_device_at_all", None, {"kind": "unknown"}),
    ("device_with_no_states_at_all", {}, {"kind": "unknown"}),
    ("device_with_onstate_explicitly_null", {"onState": None}, {"kind": "unknown"}),
    (
        # `states.onOffState` alone keeps this out of the "unknown" branch
        # (the guard's second half is false), but `isOff`/`on` still read
        # only `onState` for a relay's "on" -- unaffected by this change.
        "onoffstate_present_but_not_onstate_reads_relay_off",
        {"onState": None, "states": {"onOffState": True}},
        {"kind": "relay", "on": False},
    ),
    ("relay_off", {"onState": False}, {"kind": "relay", "on": False}),
    ("relay_on", {"onState": True}, {"kind": "relay", "on": True}),
    (
        "dimmer_off_reads_zero_brightness",
        {"onState": False, "brightness": 40},
        {"kind": "dimmer", "on": False, "brightness": 0},
    ),
    (
        "dimmer_on_reads_its_brightness",
        {"onState": True, "brightness": 40},
        {"kind": "dimmer", "on": True, "brightness": 40},
    ),
]


@pytest.mark.parametrize(
    "name,dev,expected", _ACTUAL_STATE_CASES, ids=[c[0] for c in _ACTUAL_STATE_CASES]
)
def test_actualstate_table(tmp_path, name, dev, expected):
    """A device with no on/off reading anywhere (`onState` absent/null AND no
    `states.onOffState`) must read as unknown, not guessed at as "off" --
    the whole point being that an unresolved light must never render, or
    compare, as though it were dark."""
    driver = f"""
        console.log(JSON.stringify({{
            state: actualState({json.dumps(dev)}),
        }}));
    """
    output = _run_page_logic(tmp_path, driver, name=f"actualstate_{name}.js")
    assert output["state"] == expected


@pytest.mark.parametrize("desired", ["on", "off"])
def test_actualstate_unknown_never_mismatches_either_desired_polarity(tmp_path, desired):
    """Kills: a stateless device reading as "off" by default, which would
    flag a mismatch against a desired "on" light that was never actually
    resolved -- a false red dot on a light this card cannot see at all."""
    for index, dev in enumerate(({}, {"onState": None})):
        driver = f"""
            console.log(JSON.stringify({{
                mismatch: mismatch({json.dumps(desired)}, {json.dumps(dev)}),
                label: actualLabel({json.dumps(dev)}),
            }}));
        """
        output = _run_page_logic(
            tmp_path, driver, name=f"actualstate_unknown_{desired}_{index}.js"
        )
        assert output["mismatch"] is False
        assert output["label"] == "unknown"


# ------------------------------------------------------- mismatch (table-driven)

_MISMATCH_CASES = [
    ("leave_never_mismatches", "leave", {"onState": True, "brightness": 80}, False),
    ("desired_off_actual_on_relay", "off", {"onState": True}, True),
    ("desired_off_actual_off_relay", "off", {"onState": False}, False),
    ("desired_on_actual_off_relay", "on", {"onState": False}, True),
    ("desired_on_actual_on_relay", "on", {"onState": True}, False),
    ("numeric_within_band_matches", "50", {"onState": True, "brightness": 52}, False),
    ("numeric_outside_band_mismatches", "50", {"onState": True, "brightness": 60}, True),
    # The band is the engine's (compare.level_matches), not a flat ±3: 46 for
    # 50 is inside the 5-point band and must NOT flag, 13 for 10 is outside
    # the 1-point band and must, and 100 is exact so 98 must.
    ("numeric_inside_engine_band_does_not_mismatch", "50", {"onState": True, "brightness": 46}, False),
    ("numeric_low_target_has_a_one_point_band", "10", {"onState": True, "brightness": 13}, True),
    ("numeric_full_is_exact", "100", {"onState": True, "brightness": 98}, True),
    ("numeric_dimmer_off_mismatches", "50", {"onState": False, "brightness": 0}, True),
    ("numeric_on_a_relay_uses_onish", "50", {"onState": True}, False),
    ("numeric_zero_on_a_relay_that_is_on_mismatches", "0", {"onState": True}, True),
    ("unresolved_device_never_mismatches", "50", None, False),
]


@pytest.mark.parametrize("name,level,dev,expected", _MISMATCH_CASES, ids=[c[0] for c in _MISMATCH_CASES])
def test_mismatch_table(tmp_path, name, level, dev, expected):
    driver = f"""
        console.log(JSON.stringify({{
            mismatch: mismatch({json.dumps(level)}, {json.dumps(dev)}),
        }}));
    """
    output = _run_page_logic(tmp_path, driver, name=f"mismatch_{name}.js")
    assert output["mismatch"] is expected


# ---------------------------------------------------- bandSegments (table-driven)

def test_bandsegments_a_same_day_period_is_one_segment(tmp_path):
    driver = """
        console.log(JSON.stringify({
            segments: bandSegments({ from: "18:00", to: "23:00" }),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="band_same_day.js")
    assert output["segments"] == [{"startPct": 75, "widthPct": pytest.approx(20.833333, rel=1e-4)}]


def test_bandsegments_a_midnight_crossing_period_is_two_segments(tmp_path):
    driver = """
        console.log(JSON.stringify({
            segments: bandSegments({ from: "22:00", to: "06:00" }),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="band_wrap.js")
    segments = output["segments"]
    assert len(segments) == 2
    # 22:00 -> 24:00 (2h = 8.333%), then 00:00 -> 06:00 (6h = 25%)
    assert segments[0]["startPct"] == pytest.approx(91.6667, rel=1e-3)
    assert segments[0]["widthPct"] == pytest.approx(8.3333, rel=1e-3)
    assert segments[1] == {"startPct": 0, "widthPct": 25}


# -------------------------------------------------------- sortRank (table-driven)

_SORTRANK_CASES = [
    ("overridden_first", {"state": "overridden"}, False, 0),
    ("occupied_second", {"state": "occupied"}, False, 1),
    ("vacant_third", {"state": "vacant"}, False, 2),
    ("off_duty_fourth", {"state": "off_duty"}, False, 3),
    ("disabled_device_is_last_of_the_named_states", {"state": "occupied"}, True, 4),
    ("no_states_yet_sorts_after_disabled", {}, False, 5),
]


@pytest.mark.parametrize("name,states,off_flag,expected", _SORTRANK_CASES, ids=[c[0] for c in _SORTRANK_CASES])
def test_sortrank_table(tmp_path, name, states, off_flag, expected):
    driver = f"""
        console.log(JSON.stringify({{
            rank: sortRank({json.dumps(states)}, {json.dumps(off_flag)}),
        }}));
    """
    output = _run_page_logic(tmp_path, driver, name=f"sortrank_{name}.js")
    assert output["rank"] == expected


# ------------------------------------------------- parsePeriodsToday (table-driven)

def test_parseperiodstoday_the_literal_sentinel_sets_unavailable(tmp_path):
    """The literal string "unavailable" (zone.py's `_periods_today_json`
    sentinel for "the sun failed") must set `unavailable: true` with no
    periods. Kills `unavailable: false` hardcoded in place of the
    `raw === "unavailable"` comparison."""
    driver = """
        console.log(JSON.stringify({
            result: parsePeriodsToday("unavailable"),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="ppt_unavailable.js")
    assert output["result"] == {"periods": [], "unavailable": True, "malformed": False}


def test_parseperiodstoday_an_empty_array_is_not_unavailable(tmp_path):
    """A genuine, successfully-fetched empty period list ("[]") must read as
    `unavailable: false` with an empty `periods` array -- distinct from the
    "unavailable" sentinel above. Kills `unavailable: raw === ""` (or any
    variant that treats a parseable empty array as unavailable)."""
    driver = """
        console.log(JSON.stringify({
            result: parsePeriodsToday("[]"),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="ppt_empty_array.js")
    assert output["result"] == {"periods": [], "unavailable": False, "malformed": False}


def test_parseperiodstoday_a_valid_array_is_parsed_through(tmp_path):
    """A well-formed JSON array of periods comes back parsed, untouched, with
    both flags clear. Kills a mutation that drops the parsed value in favour
    of an empty array even on success."""
    driver = """
        console.log(JSON.stringify({
            result: parsePeriodsToday(JSON.stringify([{ name: "Evening", from: "18:00", to: "23:00", mode: "on_and_off" }])),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="ppt_valid_array.js")
    assert output["result"] == {
        "periods": [{"name": "Evening", "from": "18:00", "to": "23:00", "mode": "on_and_off"}],
        "unavailable": False,
        "malformed": False,
    }


def test_parseperiodstoday_garbage_is_flagged_malformed_not_silently_empty(tmp_path):
    """Unparseable JSON (a truncated/corrupt `periods_today` value) must set
    `malformed: true` and stay distinguishable from both the "unavailable"
    sentinel and a genuine empty list -- an empty strip that looks identical
    to "no periods today" would hide a real read failure from the viewer.
    Kills collapsing the catch branch into the same `{ periods: [],
    unavailable: false }` shape as the empty-array case."""
    driver = """
        console.log(JSON.stringify({
            result: parsePeriodsToday("{not json"),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="ppt_malformed.js")
    result = output["result"]
    assert result == {"periods": [], "unavailable": False, "malformed": True}
    # And it must actually be distinguishable from the genuine-empty-list case.
    assert result != {"periods": [], "unavailable": False, "malformed": False}


def test_periodsfortimeline_notes_malformed_period_data(tmp_path):
    """`periodsForTimeline` must surface `malformed` the same way it already
    surfaces `unavailable` -- a note, not a silently empty periods lane --
    so a corrupt `periods_today` value is visible on the card, not
    indistinguishable from an ordinary "no periods configured" zone."""
    driver = """
        const r = periodsForTimeline({ period: "Evening", periods_today: "{not json" });
        console.log(JSON.stringify({ periods: r.periods, note: r.note }));
    """
    output = _run_page_logic(tmp_path, driver, name="periodsfortimeline_malformed.js")
    assert output["periods"] == []
    assert output["note"] == "period data unreadable"


def test_periodsfortimeline_notes_unavailable_period_times(tmp_path):
    """Kills: the `unavailable` branch dropped from `periodsForTimeline`
    (falling through to the `malformed`/generic path instead) -- the literal
    "unavailable" sentinel from `periods_today` needs its OWN note, distinct
    from "period data unreadable" (malformed) and from the separate "sun
    times unavailable" note `stripNoteFor` adds for approximate bands."""
    driver = """
        const r = periodsForTimeline({ period: "Evening", periods_today: "unavailable" });
        console.log(JSON.stringify({ periods: r.periods, note: r.note }));
    """
    output = _run_page_logic(tmp_path, driver, name="periodsfortimeline_unavailable_note.js")
    assert output == {"periods": [], "note": "period times unavailable"}


# --------------------------------------------- validPeriodEntry / splitValidPeriods

_VALID_PERIOD_ENTRY_CASES = [
    ("a_well_formed_entry", {"name": "Dusk", "from": "18:00", "to": "23:00"}, True),
    ("single_digit_hour_is_accepted", {"name": "Dusk", "from": "6:00", "to": "9:05"}, True),
    ("name_not_a_string", {"name": 5, "from": "18:00", "to": "23:00"}, False),
    ("from_not_hhmm", {"name": "Dusk", "from": "sunset-30m", "to": "23:00"}, False),
    ("to_not_a_string", {"name": "Dusk", "from": "18:00", "to": 2300}, False),
    ("not_an_object_at_all", "Dusk", False),
    ("null_entry", None, False),
]


@pytest.mark.parametrize(
    "name,entry,expected", _VALID_PERIOD_ENTRY_CASES, ids=[c[0] for c in _VALID_PERIOD_ENTRY_CASES]
)
def test_validperiodentry_table(tmp_path, name, entry, expected):
    driver = f"""
        console.log(JSON.stringify({{
            valid: validPeriodEntry({json.dumps(entry)}),
        }}));
    """
    output = _run_page_logic(tmp_path, driver, name=f"validperiodentry_{name}.js")
    assert output["valid"] is expected


def test_splitvalidperiods_keeps_good_entries_in_order_and_counts_the_rest(tmp_path):
    """Kills: dropping a good entry along with the bad ones, or counting a
    good entry as invalid (or vice versa)."""
    driver = """
        const periods = [
            { name: "Overnight", from: "00:00", to: "06:00" },
            { name: "Broken", from: "sunset-30m", to: "19:00" },
            { name: "Dusk", from: "18:00", to: "23:00" },
            "not even an object",
        ];
        console.log(JSON.stringify(splitValidPeriods(periods)));
    """
    output = _run_page_logic(tmp_path, driver, name="splitvalidperiods.js")
    assert [p["name"] for p in output["valid"]] == ["Overnight", "Dusk"]
    assert output["invalidCount"] == 2


# -------------------------------------------------------------- stripNoteFor

def test_stripnotefor_is_empty_when_nothing_is_degraded(tmp_path):
    driver = """
        console.log(JSON.stringify({
            note: stripNoteFor([{ name: "Dusk", from: "18:00", to: "23:00" }], 0),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="stripnotefor_clean.js")
    assert output["note"] == ""


def test_stripnotefor_names_an_approximate_entry(tmp_path):
    driver = """
        console.log(JSON.stringify({
            note: stripNoteFor(
                [{ name: "Dusk", from: "17:30", to: "23:00", approximate: true }], 0
            ),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="stripnotefor_approximate.js")
    assert output["note"] == "sun times unavailable — periods drawn at the fixed fallback"


def test_stripnotefor_names_unreadable_entries_singular_and_plural(tmp_path):
    driver = """
        console.log(JSON.stringify({
            one: stripNoteFor([], 1),
            two: stripNoteFor([], 2),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="stripnotefor_counts.js")
    assert output["one"] == "1 period entry unreadable"
    assert output["two"] == "2 period entries unreadable"


def test_stripnotefor_joins_both_degradations_when_both_apply(tmp_path):
    """Kills: only ever reporting one of the two reasons, silently dropping
    whichever ran second."""
    driver = """
        console.log(JSON.stringify({
            note: stripNoteFor(
                [{ name: "Dusk", from: "17:30", to: "23:00", approximate: true }], 3
            ),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="stripnotefor_both.js")
    assert output["note"] == (
        "sun times unavailable — periods drawn at the fixed fallback · "
        "3 period entries unreadable"
    )


# --------------------------------- periodsForTimeline: approximate bands, mixed validity

def test_periodsfortimeline_marks_an_approximate_entry_and_notes_it(tmp_path):
    """An `approximate: true` entry survives into `periods` (the caller draws
    the `.approximate` class from it) and the sun-fallback note appears
    (A3). Kills: dropping the flag, or dropping the note."""
    driver = """
        const s = {
            period: "Dusk",
            periods_today: JSON.stringify([
                { name: "Dusk", from: "17:30", to: "23:00", mode: "on_and_off", approximate: true },
            ]),
        };
        const r = periodsForTimeline(s);
        console.log(JSON.stringify({
            approximate: r.periods[0].approximate,
            note: r.note,
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="periodsfortimeline_approximate.js")
    assert output["approximate"] is True
    assert output["note"] == "sun times unavailable — periods drawn at the fixed fallback"


def test_periodsfortimeline_drops_only_the_unreadable_entries_and_notes_the_count(tmp_path):
    """One good period and one malformed one in the same payload: the good
    one is kept, the bad one is counted in the note rather than silently
    vanishing or taking the whole lane down."""
    driver = """
        const s = {
            period: "Dusk",
            periods_today: JSON.stringify([
                { name: "Dusk", from: "18:00", to: "23:00", mode: "on_and_off" },
                { name: "Broken", from: "sunset-30m", to: "19:00", mode: "on_and_off" },
            ]),
        };
        const r = periodsForTimeline(s);
        console.log(JSON.stringify({
            names: r.periods.map(p => p.name),
            note: r.note,
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="periodsfortimeline_mixed_validity.js")
    assert output["names"] == ["Dusk"]
    assert output["note"] == "1 period entry unreadable"


# ---------------------------------------------------- parsePresenceInputs (table-driven)

def test_parsepresenceinputs_null_is_absent_not_malformed(tmp_path):
    """`presence_inputs == null` means an older plugin never published the
    state at all -- the section is simply not there, not a read failure.
    Kills: treating a missing key the same as a garbled one."""
    driver = """
        console.log(JSON.stringify({
            result: parsePresenceInputs(null),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="ppi_absent.js")
    assert output["result"] == {"inputs": [], "malformed": False, "absent": True}


def test_parsepresenceinputs_garbage_is_malformed_not_absent_or_empty(tmp_path):
    """A present-but-unparseable string is a real degradation, distinct from
    both `absent` (never published) and a genuine "[]" (no inputs
    configured). Kills: falling back to an empty array on a parse failure,
    which reads exactly like "this zone has no presence inputs"."""
    driver = """
        console.log(JSON.stringify({
            result: parsePresenceInputs("{not json"),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="ppi_malformed.js")
    assert output["result"] == {"inputs": [], "malformed": True, "absent": False}


def test_parsepresenceinputs_a_valid_array_parses_through(tmp_path):
    driver = """
        console.log(JSON.stringify({
            result: parsePresenceInputs(JSON.stringify([{ id: 1, kind: "device", on: true, last: true }])),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="ppi_valid.js")
    assert output["result"] == {
        "inputs": [{"id": 1, "kind": "device", "on": True, "last": True}],
        "malformed": False,
        "absent": False,
    }


def test_buildpresencechips_returns_null_for_an_absent_state(tmp_path):
    """Kills: rendering an (empty) presence-inputs section even though the
    plugin never published the state at all."""
    driver = """
        console.log(JSON.stringify({
            result: buildPresenceChips({ presence_inputs: null }),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="bpc_absent.js")
    assert output["result"] is None


def test_buildpresencechips_renders_a_note_for_malformed_presence_inputs(tmp_path):
    driver = """
        document.createElement = () => ({ className: "", textContent: "" });
        const el = buildPresenceChips({ presence_inputs: "{not json" });
        console.log(JSON.stringify({
            className: el.className,
            text: el.textContent,
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="bpc_malformed.js")
    assert output["className"] == "strip-note"
    assert output["text"] == "presence inputs unreadable"


_FAKE_DOM_STUB = """
function makeEl() {
    return {
        className: "", style: {}, textContent: "", title: "",
        children: [],
        appendChild(child) { this.children.push(child); },
    };
}
document.createElement = () => makeEl();
"""


# --------------------------------------------- buildLightsSection: unknownCount

def test_buildlightssection_counts_unresolved_lights_separately_from_mismatches(tmp_path):
    """A light this card could not resolve at all (`actualState` "unknown")
    must be counted as `unknownCount`, not silently folded into 0
    mismatches -- an all-unknown zone would otherwise report "0 off target"
    and look clean. Kills: `unknownCount` always 0, or counting an unknown
    light as a mismatch instead."""
    driver = (
        _FAKE_DOM_STUB
        + """
        deviceById = { "1": {}, "2": { onState: true } };
        deviceNames = {};
        const { mismatchCount, unknownCount } = buildLightsSection({
            desired_summary: "1=60, 2=on",
        });
        console.log(JSON.stringify({ mismatchCount, unknownCount }));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="buildlightssection_unknown.js")
    assert output["unknownCount"] == 1
    assert output["mismatchCount"] == 0


# ------------------------------------------- timeline pure helpers (table-driven)


def test_pctofday_clamps_to_0_100(tmp_path):
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        console.log(JSON.stringify({
            before: pctOfDay(new Date(2026, 8, 7, 23, 0, 0), dayStart),
            noon: pctOfDay(new Date(2026, 8, 8, 12, 0, 0), dayStart),
            after: pctOfDay(new Date(2026, 8, 9, 1, 0, 0), dayStart),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="pctofday.js")
    assert output["before"] == 0
    assert abs(output["noon"] - 50) < 0.01
    assert output["after"] == 100


def test_intervalsfromstates_carries_in_the_state_at_midnight(tmp_path):
    """A state event from YESTERDAY (before dayStart) still puts the zone
    in that state from 00:00 -- the whole reason history keeps 48h."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 6, 0, 0);
        const events = [
            { t: "2026-09-07T23:40:00", k: "state", to: "occupied", from: "vacant" },
            { t: "2026-09-08T05:00:00", k: "state", to: "vacant", from: "occupied" },
        ];
        const { occupied } = intervalsFromStates(events, dayStart, now);
        console.log(JSON.stringify({
            start: occupied[0][0] === dayStart.getTime(),
            count: occupied.length,
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="intervalsfromstates_carryin.js")
    assert output["start"] is True
    assert output["count"] == 1


def test_intervalsfromstates_an_event_exactly_at_now_is_included(tmp_path):
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 6, 0, 0);
        const events = [
            { t: "2026-09-08T06:00:00", k: "state", to: "occupied", from: "vacant" },
        ];
        const { occupied } = intervalsFromStates(events, dayStart, now);
        console.log(JSON.stringify({ occupied }));
    """
    output = _run_page_logic(tmp_path, driver, name="intervalsfromstates_at_now.js")
    # The state starts exactly at `now`, so the interval is zero-width and
    # closeSeg() drops it -- nothing to draw, and that is correct: there is
    # no elapsed time in "occupied" yet.
    assert output["occupied"] == []


def test_intervalsfromstates_empty_events_is_no_intervals_not_a_crash(tmp_path):
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 6, 0, 0);
        const { occupied, overridden } = intervalsFromStates([], dayStart, now);
        console.log(JSON.stringify({ occupied, overridden }));
    """
    output = _run_page_logic(tmp_path, driver, name="intervalsfromstates_empty.js")
    assert output == {"occupied": [], "overridden": []}


def test_levelintervals_with_no_prior_event_draws_nothing_until_the_first_report(tmp_path):
    """Kills: defaulting an unknown starting level to 0 (which would draw
    nothing, indistinguishable in the data from a genuinely-off light) or to
    some positive guess (which would draw a fill nobody reported)."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 6, 0, 0);
        const events = [
            { t: "2026-09-08T05:00:00", k: "light", id: 201, level: 60 },
        ];
        const before = levelIntervals(events, 201, dayStart, new Date(2026, 8, 8, 4, 0, 0));
        const after = levelIntervals(events, 201, dayStart, now);
        console.log(JSON.stringify({ before, after }));
    """
    output = _run_page_logic(tmp_path, driver, name="levelintervals_unknown_start.js")
    assert output["before"] == []
    assert len(output["after"]) == 1
    assert output["after"][0][2] == 60


def test_levelintervals_carries_in_a_level_from_before_midnight(tmp_path):
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 1, 0, 0);
        const events = [
            { t: "2026-09-07T22:00:00", k: "light", id: 201, level: 40 },
        ];
        const intervals = levelIntervals(events, 201, dayStart, now);
        console.log(JSON.stringify({ intervals }));
    """
    output = _run_page_logic(tmp_path, driver, name="levelintervals_carryin.js")
    assert output["intervals"] == [[dt_ms("2026-09-08T00:00:00"), dt_ms("2026-09-08T01:00:00"), 40]]


def test_levelintervals_a_level_0_report_ends_the_interval_and_draws_nothing(tmp_path):
    """Kills: `typeof level === "number" && level > 0` weakened to just
    `level` (0 is falsy but still `typeof === "number"`, so a naive
    `!!level` check happens to work here too -- this pins the actual `> 0`
    comparison) -- a light reporting 0 must close its lit interval rather
    than draw a zero-height (or worse, on-looking) segment."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const events = [
          { t: "2026-09-08T01:00:00", k: "light", id: 201, level: 60 },
          { t: "2026-09-08T02:00:00", k: "light", id: 201, level: 0 },
        ];
        const lit = levelIntervals(events, 201, dayStart, now);
        console.log(JSON.stringify({ n: lit.length, level: lit[0][2], mins: minutesOf(lit) }));
    """
    output = _run_page_logic(tmp_path, driver, name="levelintervals_zero.js")
    assert output == {"n": 1, "level": 60, "mins": 60}


def test_levelintervals_ignores_another_devices_events(tmp_path):
    """Kills: the `e.id === deviceId` filter dropped or loosened -- a light
    lane would otherwise draw a neighbour's report as if it were its own."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const events = [
          { t: "2026-09-08T00:30:00", k: "light", id: 202, level: 100 },
        ];
        const lit = levelIntervals(events, 201, dayStart, now);
        console.log(JSON.stringify({ lit }));
    """
    output = _run_page_logic(tmp_path, driver, name="levelintervals_other_device.js")
    assert output["lit"] == []


def test_intervalsfromstates_and_levelintervals_ignore_events_after_now(tmp_path):
    """Kills: the `ev.t > nowMs` break dropped from either function's scan --
    an event that has not happened yet (a clock skew, or simply data past
    the moment the page asked for) must not be drawn as if it already had."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const events = [
          { t: "2026-09-08T01:00:00", k: "state", to: "occupied", from: "vacant" },
          { t: "2026-09-08T04:00:00", k: "state", to: "vacant", from: "occupied" },
          { t: "2026-09-08T01:00:00", k: "light", id: 201, level: 60 },
          { t: "2026-09-08T04:00:00", k: "light", id: 201, level: 0 },
        ];
        const { occupied } = intervalsFromStates(events, dayStart, now);
        const lit = levelIntervals(events, 201, dayStart, now);
        console.log(JSON.stringify({
          occEnd: occupied[0][1] === now.getTime(),
          litEnd: lit[0][1] === now.getTime(),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="ignore_events_after_now.js")
    assert output == {"occEnd": True, "litEnd": True}


def test_intervalsfromstates_an_overridden_state_lands_in_the_overridden_list(tmp_path):
    """Kills: `else if (state === "overridden")` dropped or merged into the
    `occupied` branch -- an overridden period must be drawn on its own
    lane, not counted (or silently discarded) as ordinary occupied time."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const events = [
          { t: "2026-09-08T01:00:00", k: "state", to: "overridden", from: "occupied" },
        ];
        const r = intervalsFromStates(events, dayStart, now);
        console.log(JSON.stringify({ n: r.overridden.length, occ: r.occupied.length }));
    """
    output = _run_page_logic(tmp_path, driver, name="intervalsfromstates_overridden.js")
    assert output == {"n": 1, "occ": 0}


def test_unionintervals_merges_overlapping_and_closes_adjacent_gaps(tmp_path):
    """Kills: a `<` where the adjacency check needs `<=` (`iv[0] <= last[1]`)
    -- two intervals that exactly touch ([0,10] and [10,20]) must merge into
    one, not sit side by side as if there were a gap between them."""
    driver = """
        console.log(JSON.stringify({
            overlapping: unionIntervals([[0, 10], [5, 15], [20, 30]]),
            adjacent: unionIntervals([[0, 10], [10, 20]]),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="unionintervals.js")
    assert output["overlapping"] == [[0, 15], [20, 30]]
    assert output["adjacent"] == [[0, 20]]


def test_intersectintervals_returns_only_the_overlap(tmp_path):
    driver = """
        console.log(JSON.stringify({
            x: intersectIntervals([[0, 10]], [[5, 20]]),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="intersectintervals.js")
    assert output["x"] == [[5, 10]]


def test_subtractintervals_removes_the_overlap(tmp_path):
    driver = """
        console.log(JSON.stringify({
            x: subtractIntervals([[0, 10]], [[3, 6]]),
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="subtractintervals.js")
    assert output["x"] == [[0, 3], [6, 10]]


def test_minutesof_empty_is_zero_not_nan(tmp_path):
    driver = """
        console.log(JSON.stringify({ m: minutesOf([]) }));
    """
    output = _run_page_logic(tmp_path, driver, name="minutesof_empty.js")
    assert output["m"] == 0


def test_minutesof_sums_interval_durations(tmp_path):
    driver = """
        console.log(JSON.stringify({ m: minutesOf([[0, 60000], [0, 120000]]) }));
    """
    output = _run_page_logic(tmp_path, driver, name="minutesof.js")
    assert output["m"] == 3  # 1 min + 2 min


def test_statsfor_with_events_but_no_lights_configured_is_zeros_not_nan(tmp_path):
    """Kills: any stat computed as a division or ratio that produces NaN on
    an empty zone rather than an honest 0 -- pinned here with a real
    (non-empty) `lightIds` list, since an empty one now takes a different
    path entirely (see the null test right below)."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 6, 0, 0);
        console.log(JSON.stringify(statsFor([], [201, 202], dayStart, now)));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_empty.js")
    assert output == {
        "occupiedMinutes": 0,
        "lightsOnMinutes": 0,
        "onWhileEmptyMinutes": 0,
        "hereLightsOffMinutes": 0,
        "overrideCount": 0,
        "cycles": 0,
        "unknownLights": [201, 202],
    }


def test_statsfor_with_no_lights_configured_reports_null_not_zero(tmp_path):
    """Kills: reporting 0 for every light-dependent figure when `lightIds`
    is empty -- "no lights configured" and "lights configured but never
    on" are different facts, and the old zeros-test above blessed the
    wrong one of the two ("nothing known" read exactly like "definitely
    off"). `occupiedMinutes` and `overrideCount` are NOT light-dependent
    (they come from `state`/`override` events alone) and must stay
    numeric even here."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 6, 0, 0);
        const events = [
            { t: "2026-09-08T01:00:00", k: "state", to: "occupied", from: "vacant" },
        ];
        console.log(JSON.stringify(statsFor(events, [], dayStart, now)));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_no_lights.js")
    assert output["lightsOnMinutes"] is None
    assert output["onWhileEmptyMinutes"] is None
    assert output["hereLightsOffMinutes"] is None
    assert output["cycles"] is None
    assert output["unknownLights"] == []
    assert output["occupiedMinutes"] == 300  # 01:00-06:00, unaffected by lightIds
    assert output["overrideCount"] == 0


def test_statsfor_flags_lights_on_while_the_zone_was_not_occupied(tmp_path):
    """The 'on while empty' stat is lights-on MINUS occupied MINUS
    overridden -- this pins the subtraction, not just that some number comes
    out non-zero."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const events = [
            { t: "2026-09-08T01:00:00", k: "light", id: 201, level: 60 },
        ];
        console.log(JSON.stringify(statsFor(events, [201], dayStart, now)));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_waste.js")
    assert output["lightsOnMinutes"] == 120  # 01:00-03:00
    assert output["occupiedMinutes"] == 0
    assert output["onWhileEmptyMinutes"] == 120
    assert output["hereLightsOffMinutes"] == 0
    assert output["cycles"] == 1


def test_statsfor_unknownlights_names_ids_with_no_light_event_at_all(tmp_path):
    """Kills: reporting 0 mismatches/writes for a light this zone has never
    once heard report -- `unknownLights` must name it separately from a
    light genuinely known to be off, so "0 lights on" cannot be mistaken
    for "nothing has ever reported"."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 6, 0, 0);
        const events = [
            { t: "2026-09-08T01:00:00", k: "light", id: 201, level: 60 },
        ];
        console.log(JSON.stringify(statsFor(events, [201, 202], dayStart, now)));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_unknown.js")
    assert output["unknownLights"] == [202]


def test_statsfor_a_gap_marker_event_does_not_break_the_computation(tmp_path):
    """Kills: assuming every event has the fields a `state`/`light`/`write`
    event carries, which a bare `{"k": "gap"}` does not -- a crash here
    would take the whole card's stats row down over a truncation marker."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 6, 0, 0);
        const events = [{ t: "2026-09-08T02:00:00", k: "gap" }];
        console.log(JSON.stringify(statsFor(events, [201], dayStart, now)));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_gap.js")
    assert output["occupiedMinutes"] == 0
    assert output["cycles"] == 0


def test_statsfor_cycles_counts_one_dimmed_light_and_two_overlapping_lights_as_one_cycle_each(tmp_path):
    """`cycles` is `unionIntervals(...).length` across every configured
    light -- kills counting per-light intervals instead of the union, which
    would double-count a light that only dimmed (never truly off) and a
    second light overlapping the first."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const dim = [
          { t: "2026-09-08T01:00:00", k: "light", id: 201, level: 60 },
          { t: "2026-09-08T02:00:00", k: "light", id: 201, level: 30 },
        ];
        const two = [
          { t: "2026-09-08T01:00:00", k: "light", id: 201, level: 60 },
          { t: "2026-09-08T01:30:00", k: "light", id: 202, level: 60 },
        ];
        console.log(JSON.stringify({
          dim: statsFor(dim, [201], dayStart, now).cycles,
          two: statsFor(two, [201, 202], dayStart, now).cycles,
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_cycles.js")
    assert output == {"dim": 1, "two": 1}


def test_statsfor_subtracts_overridden_time_from_on_while_empty(tmp_path):
    """Kills: `onWhileEmpty` computed as only `anyLight` minus `occUnion`
    (dropping the second `subtractIntervals(..., overUnion)`) -- a light on
    while a person has overridden it by hand must not be counted as
    wasted/on-while-nobody's-there."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const events = [
          { t: "2026-09-08T00:30:00", k: "state", to: "overridden", from: "vacant" },
          { t: "2026-09-08T01:00:00", k: "light", id: 201, level: 60 },
        ];
        console.log(JSON.stringify(statsFor(events, [201], dayStart, now)));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_override_subtraction.js")
    assert output["lightsOnMinutes"] == 120
    assert output["onWhileEmptyMinutes"] == 0


def test_statsfor_here_with_lights_off_is_occupied_minus_any_light(tmp_path):
    """Kills: `hereLightsOff` computed from `anyLight` instead of
    `subtractIntervals(occUnion, anyLight)` (or the arguments swapped) --
    the figure is specifically "occupied AND no light on", not "not
    occupied"."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const events = [
          { t: "2026-09-08T01:00:00", k: "state", to: "occupied", from: "vacant" },
          { t: "2026-09-08T02:00:00", k: "light", id: 201, level: 60 },
        ];
        console.log(JSON.stringify(statsFor(events, [201], dayStart, now)));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_here_lights_off.js")
    assert output["occupiedMinutes"] == 120
    assert output["hereLightsOffMinutes"] == 60


def test_statsfor_overridecount_ignores_yesterdays_start_and_a_later_end(tmp_path):
    """Kills: the override-start filter's `t >= dayStartMs` (or the paired
    `t <= nowMs`) dropped -- an override taken yesterday, ended and
    restarted today must count exactly the one start that actually falls
    inside today's window, and an `end` event must never be mistaken for a
    `start` and counted at all."""
    driver = """
        const dayStart = new Date(2026, 8, 8, 0, 0, 0);
        const now = new Date(2026, 8, 8, 3, 0, 0);
        const events = [
          { t: "2026-09-07T22:00:00", k: "override", phase: "start", device: 201 },
          { t: "2026-09-07T23:00:00", k: "override", phase: "end", device: 201 },
          { t: "2026-09-08T01:00:00", k: "override", phase: "start", device: 201 },
          { t: "2026-09-08T02:00:00", k: "override", phase: "end", device: 201 },
        ];
        console.log(JSON.stringify({ n: statsFor(events, [201], dayStart, now).overrideCount }));
    """
    output = _run_page_logic(tmp_path, driver, name="statsfor_overridecount.js")
    assert output["n"] == 1


@pytest.mark.parametrize("name,zone,expected", [
    ("Kitchen Pendants", "Kitchen", "Pendants"),
    ("Kitchen - LED Strip", "Kitchen", "LED Strip"),
    ("DiningRoom - Shelves", "Dining Room", "Shelves"),
    ("Kitchen", "Kitchen", "Kitchen"),
    ("Kitchenette Lamp", "Kitchen", "Kitchenette Lamp"),
    ("A very long light name indeed here", "", "A very long light…"),
])
def test_lanelabel_drops_the_zone_prefix_then_truncates(tmp_path, name, zone, expected):
    """The lane label column is narrow; a light named after its zone must show
    the distinguishing part. Kills: truncating before stripping (every kitchen
    light reads "Kitchen Pen…"), and blanking a light whose whole name IS the
    zone name."""
    import json
    driver = f"""
        const out = {{ label: laneLabel({json.dumps(name)}, {json.dumps(zone)}) }};
        console.log(JSON.stringify(out));
    """
    output = _run_page_logic(tmp_path, driver, name="lanelabel.js")
    assert output["label"] == expected


# ---------------------------------------------------------- buildTimeline

_FIND_BY_CLASS = """
function findByClass(el, cls) {
    let out = [];
    if (el && el.className && String(el.className).split(" ").includes(cls)) out.push(el);
    for (const c of (el && el.children) || []) out = out.concat(findByClass(c, cls));
    return out;
}
document.createTextNode = text => ({ textContent: text });
"""

_ZONE_STATES = '{ state: "vacant", desired_summary: "" }'


def test_buildtimeline_shows_history_status_when_not_ok(tmp_path):
    """Kills: never reading the controller's `history_status` into the card
    -- a user staring at an empty-looking timeline has no way to tell
    "nothing has happened" from "the plugin stopped writing the file"."""
    driver = (
        _FAKE_DOM_STUB
        + _FIND_BY_CLASS
        + f"""
        const wrap = buildTimeline({_ZONE_STATES}, [], null, [], "Kitchen", "write failed: disk full", null, false);
        const notes = findByClass(wrap, "tl-unavailable").map(e => e.textContent);
        console.log(JSON.stringify({{ notes }}));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="timeline_history_status.js")
    assert any("history not being written" in n and "disk full" in n for n in output["notes"])


def test_buildtimeline_says_nothing_extra_when_history_status_is_ok(tmp_path):
    driver = (
        _FAKE_DOM_STUB
        + _FIND_BY_CLASS
        + f"""
        const wrap = buildTimeline({_ZONE_STATES}, [], null, [], "Kitchen", "ok", null, false);
        const notes = findByClass(wrap, "tl-unavailable").map(e => e.textContent);
        console.log(JSON.stringify({{ notes }}));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="timeline_history_status_ok.js")
    assert output["notes"] == []


def test_buildtimeline_flags_a_stale_generated_at(tmp_path):
    """Kills: never checking the history file's own `generated_at` against
    the current time -- a file that stopped updating an hour ago but is
    still readable must not look current."""
    driver = (
        _FAKE_DOM_STUB
        + _FIND_BY_CLASS
        + f"""
        const stale = new Date(Date.now() - 30 * 60 * 1000).toISOString();
        const wrap = buildTimeline({_ZONE_STATES}, [], null, [], "Kitchen", "ok", stale, false);
        const notes = findByClass(wrap, "tl-unavailable").map(e => e.textContent);
        console.log(JSON.stringify({{ notes }}));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="timeline_stale.js")
    assert any("history last written" in n for n in output["notes"])


def test_buildtimeline_says_nothing_for_a_recent_generated_at(tmp_path):
    driver = (
        _FAKE_DOM_STUB
        + _FIND_BY_CLASS
        + f"""
        const recent = new Date(Date.now() - 30 * 1000).toISOString();
        const wrap = buildTimeline({_ZONE_STATES}, [], null, [], "Kitchen", "ok", recent, false);
        const notes = findByClass(wrap, "tl-note").map(e => e.textContent);
        console.log(JSON.stringify({{ notes }}));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="timeline_fresh.js")
    assert not any("history last written" in n for n in output["notes"])


def test_buildtimeline_notes_a_zone_with_no_history_entry_yet(tmp_path):
    """Kills: treating "the file loaded fine but has no entry for this
    zone" the same as "nothing has happened" -- the missing-entry case must
    say so rather than drawing silent empty lanes indistinguishable from a
    genuinely quiet zone."""
    driver = (
        _FAKE_DOM_STUB
        + _FIND_BY_CLASS
        + f"""
        const wrap = buildTimeline({_ZONE_STATES}, [], null, [], "Kitchen", "ok", null, true);
        const notes = findByClass(wrap, "tl-note").map(e => e.textContent);
        console.log(JSON.stringify({{ notes }}));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="timeline_no_entry.js")
    assert any("no history for this zone yet" in n for n in output["notes"])


def test_buildtimeline_counts_events_it_could_not_use(tmp_path):
    """Kills: silently dropping unreadable events with no visible trace --
    an unparseable timestamp, an unknown `k`, and a non-number `level`
    must each be counted, not just quietly excluded from the lanes."""
    driver = (
        _FAKE_DOM_STUB
        + _FIND_BY_CLASS
        + f"""
        const events = [
            {{ t: "not-a-timestamp", k: "state", to: "occupied" }},
            {{ t: "2026-09-08T01:00:00", k: "made-up-kind" }},
            {{ t: "2026-09-08T01:00:00", k: "light", id: 201, level: "sixty" }},
            {{ t: "2026-09-08T01:00:00", k: "light", id: 201, level: 60 }},
            // Writes legitimately carry "on"/"off" (the reconciler's own
            // levels) and must NOT be counted -- the seed from a real day had
            // every one of these flagged before this row existed.
            {{ t: "2026-09-08T01:00:00", k: "write", id: 201, level: "off" }},
            {{ t: "2026-09-08T01:00:00", k: "write", id: 201, level: "on" }},
            {{ t: "2026-09-08T01:00:00", k: "write", id: 201, level: "dim-ish" }},
        ];
        const wrap = buildTimeline({_ZONE_STATES}, events, null, [], "Kitchen", "ok", null, false);
        const notes = findByClass(wrap, "tl-note").map(e => e.textContent);
        console.log(JSON.stringify({{ notes }}));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="timeline_unusable.js")
    assert any("4 history events could not be read" in n for n in output["notes"])


def test_buildtimeline_counts_an_override_end_with_no_matching_start(tmp_path):
    """Kills: silently discarding an unpaired override `end` event instead
    of counting it as something the marks lane could not use."""
    driver = (
        _FAKE_DOM_STUB
        + _FIND_BY_CLASS
        + f"""
        const events = [
            {{ t: "2026-09-08T01:00:00", k: "override", phase: "end", device: 201 }},
        ];
        const wrap = buildTimeline({_ZONE_STATES}, events, null, [], "Kitchen", "ok", null, false);
        const notes = findByClass(wrap, "tl-note").map(e => e.textContent);
        console.log(JSON.stringify({{ notes }}));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="timeline_unpaired_override.js")
    assert any("1 history event could not be read" in n for n in output["notes"])


def test_buildtimeline_says_nothing_when_every_event_is_usable(tmp_path):
    driver = (
        _FAKE_DOM_STUB
        + _FIND_BY_CLASS
        + f"""
        const events = [
            {{ t: "2026-09-08T01:00:00", k: "state", to: "occupied", from: "vacant" }},
        ];
        const wrap = buildTimeline({_ZONE_STATES}, events, null, [], "Kitchen", "ok", null, false);
        const notes = findByClass(wrap, "tl-note").map(e => e.textContent);
        console.log(JSON.stringify({{ notes }}));
    """
    )
    output = _run_page_logic(tmp_path, driver, name="timeline_all_usable.js")
    assert not any("could not be read" in n for n in output["notes"])


# ---------------------------------------------------------- pollHistoryOnce

def test_pollhistoryonce_only_rerenders_when_the_history_payload_changes(tmp_path):
    """Kills: forcing `lastRenderedJson = null` (and a re-render) on every
    single poll regardless of whether anything changed -- a quiet house
    would otherwise rebuild the whole DOM every 5 seconds forever."""
    driver = """
        let renderCalls = 0;
        render = () => { renderCalls++; };
        lastDevicesPayload = [{ id: 1 }];
        historyApi = { getHistory: async () => ({ version: 1, generated_at: "2026-09-08T12:00:00", zones: { Kitchen: { events: [] } } }) };

        (async () => {
            await pollHistoryOnce();
            await pollHistoryOnce();
            await pollHistoryOnce();
            console.log(JSON.stringify({ renderCalls }));
        })();
    """
    output = _run_page_logic(tmp_path, driver, name="poll_dedupe.js")
    assert output["renderCalls"] == 1


def test_pollhistoryonce_rerenders_when_the_history_payload_actually_changes(tmp_path):
    driver = """
        let renderCalls = 0;
        render = () => { renderCalls++; };
        lastDevicesPayload = [{ id: 1 }];
        let call = 0;
        historyApi = {
            getHistory: async () => {
                call++;
                return {
                    version: 1,
                    generated_at: "2026-09-08T12:00:00",
                    zones: { Kitchen: { events: call === 1 ? [] : [{ t: "2026-09-08T12:00:00", k: "gap" }] } },
                };
            },
        };

        (async () => {
            await pollHistoryOnce();
            await pollHistoryOnce();
            console.log(JSON.stringify({ renderCalls }));
        })();
    """
    output = _run_page_logic(tmp_path, driver, name="poll_change.js")
    assert output["renderCalls"] == 2


def test_pollhistoryonce_names_a_404_with_a_friendly_message(tmp_path):
    """Kills: showing the raw 'HTTP 404: ...' transport error for the
    completely normal case of the plugin not having written the file yet
    (within 30 s of the first event, per the plugin's own write interval)."""
    driver = """
        historyApi = {
            getHistory: async () => {
                const e = new Error("HTTP 404: Not Found");
                e.status = 404;
                throw e;
            },
        };

        (async () => {
            await pollHistoryOnce();
            console.log(JSON.stringify({ historyZones, historyErrorMsg }));
        })();
    """
    output = _run_page_logic(tmp_path, driver, name="poll_404.js")
    assert output["historyZones"] is None
    assert "writes it within 30 s" in output["historyErrorMsg"]


def test_pollhistoryonce_rejects_the_wrong_version(tmp_path):
    """Kills: reading a future/incompatible version's `zones` object as if
    it were shape-compatible with what this page understands."""
    driver = """
        historyApi = {
            getHistory: async () => ({ version: 2, generated_at: "2026-09-08T12:00:00", zones: { Kitchen: { events: [] } } }),
        };

        (async () => {
            await pollHistoryOnce();
            console.log(JSON.stringify({ historyZones, historyErrorMsg }));
        })();
    """
    output = _run_page_logic(tmp_path, driver, name="poll_version.js")
    assert output["historyZones"] is None
    assert "version 2" in output["historyErrorMsg"]
    assert "version 1" in output["historyErrorMsg"]


def test_pollhistoryonce_catches_a_render_failure_without_raising(tmp_path):
    """Kills: leaving the `render(...)` call inside the poll's `finally`
    unguarded -- a bug in rendering one bad payload must not stop every
    future poll's error handling from running cleanly, and must surface
    somewhere a user can see it."""
    driver = """
        const errs = [];
        showErr = msg => errs.push(msg);
        render = () => { throw new Error("boom"); };
        lastDevicesPayload = [{ id: 1 }];
        historyApi = { getHistory: async () => ({ version: 1, generated_at: "2026-09-08T12:00:00", zones: {} }) };

        (async () => {
            await pollHistoryOnce();  // must not raise
            console.log(JSON.stringify({ errs }));
        })();
    """
    output = _run_page_logic(tmp_path, driver, name="poll_render_raises.js")
    assert any("timeline render failed" in e and "boom" in e for e in output["errs"])

