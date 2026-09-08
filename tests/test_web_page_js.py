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

import json
import os
import shutil
import subprocess
import textwrap
from html.parser import HTMLParser

import pytest

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


def test_builddaystrip_renders_a_note_for_malformed_period_data(tmp_path):
    """`buildDayStrip` must surface `malformed` the same way it already
    surfaces `unavailable` -- a `.strip-note` element, not a silently empty
    strip -- so a corrupt `periods_today` value is visible on the card, not
    indistinguishable from an ordinary "no periods configured" zone."""
    driver = """
        document.createElement = () => ({ className: "", textContent: "" });
        const el = buildDayStrip({ period: "Evening", periods_today: "{not json" });
        console.log(JSON.stringify({
            className: el.className,
            text: el.textContent,
        }));
    """
    output = _run_page_logic(tmp_path, driver, name="builddaystrip_malformed.js")
    assert output["className"] == "strip-note"
    assert output["text"] == "period data unreadable"
