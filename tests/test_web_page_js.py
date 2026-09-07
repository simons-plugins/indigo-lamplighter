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
