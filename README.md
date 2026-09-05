# Lamplighter

Presence- and daylight-driven lighting for [Indigo](https://www.indigodomo.com),
built around one idea: every decision comes from the zone's inputs (when
presence was last seen, how bright the room is, which period it is, whether a
person has taken over), never from diffing live device state.

Successor to the `indigo-auto-lights` fork. Design: [`docs/plans/PRD-indigo-lamplighter.md`](docs/plans/PRD-indigo-lamplighter.md).

Status: M2 live on the Hallway zone on jarvis (2026-09-05); M3 (converter
done, MCP tools in progress).

## Install

**First install: double-click `Lamplighter.indigoPlugin` on the Indigo
server.** Indigo has to register the bundle itself; copying the folder into
`Plugins/` works only for *later* updates to a bundle it has already
installed, and a copied first install shows up as a plugin that will not
start.

Updating an installed plugin: copy the changed files into

```
/Library/Application Support/Perceptive Automation/Indigo 2025.2/Plugins/Lamplighter.indigoPlugin/Contents/Server Plugin/
```

and reload the plugin (Plugins → Lamplighter → Reload). Adding or changing a
device *state* needs a plugin restart, not just a reload, because the state
list is refreshed in `deviceStartComm`.

On first start the plugin creates:

- one **Lamplighter Zone** device per zone in the configuration — its on/off
  is that zone's enable, and its states carry everything in PRD section 5.10
  (`state`, `presence_active`, `lux`, `dark`, `period`, `explain`, the day's
  counters, and the persisted override);
- one **Lamplighter Controller** device — its on/off is the global enable, so
  "all automation off" is one switch, and it carries the zone counts, the
  summed counters and the configuration status.

A zone that later disappears from the configuration keeps its device: nothing
is ever deleted for you. The device says so in its `explain` state and the
plugin names it once at WARNING.

## Configuration

Zones live in one JSON file, in one fixed place:

```
<Indigo install folder>/Preferences/Plugins/com.simons-plugins.indigo-lamplighter/lamplighter.json
```

which on a stock 2025.2 server is

```
/Library/Application Support/Perceptive Automation/Indigo 2025.2/Preferences/Plugins/com.simons-plugins.indigo-lamplighter/lamplighter.json
```

The plugin writes `{"version": 1, "zones": []}` there if the file is missing,
watches its modification time, and reloads within five seconds of a save.
Overrides, presence and the dark verdict survive the reload (a zone switched
off in the new file does *not* survive — the file is what `enabled` means).
**A file that does not validate is refused whole**: the error names the
failing path, is logged once per edit, appears on the controller device's
`config_status` state, and the previous configuration keeps running.

See [`examples/lamplighter.example.json`](examples/lamplighter.example.json)
for a worked file and
`Lamplighter.indigoPlugin/Contents/Server Plugin/lamplighter/schema.json`
for the schema.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Everything passes and nothing is `xfailed`: the promises below were written
first as strict `xfail` stubs and have all since been replaced by real tests.

### The acceptance suite was written before the engine, and it is strict

`tests/test_promises_*.py` holds one stub per promise in PRD section 7, each
marked:

```python
@pytest.mark.xfail(strict=True, reason="M1: engine not built", raises=NotImplementedError)
```

Every docstring states the promise in one sentence and then names **the
mutation it must kill** — the plausible wrong implementation that a happy-path
test would not catch. Attempt 1 at the override rule (fork issue #15) passed a
happy-path suite and shipped a bug, which is why the promises are written this
way and why they are written first.

`xfail_strict = true` (in `pyproject.toml`) means a stub that *starts passing*
**fails the suite**. That is deliberate: M1 lands one promise at a time, each
by replacing a stub body with the real test and then applying the named
mutation to confirm the test actually fails under it. A stub can never be
satisfied by accident, and the run's `xfailed` count is the honest measure of
how much of the PRD is still unbuilt.

`tests/test_schema.py` is not a stub: it validates the bundled schema against
the JSON Schema 2020-12 metaschema, validates
`examples/lamplighter.example.json` against the schema, checks that a table of
invalid documents fails at the path a config author would need to see, and
pins the PRD section 11 decisions that are visible in the schema's shape.

Schema: `Lamplighter.indigoPlugin/Contents/Server Plugin/lamplighter/schema.json`.
