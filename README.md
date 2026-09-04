# Lamplighter

Presence- and daylight-driven lighting for [Indigo](https://www.indigodomo.com),
built around one idea: every decision comes from the zone's inputs (when
presence was last seen, how bright the room is, which period it is, whether a
person has taken over), never from diffing live device state.

Successor to the `indigo-auto-lights` fork. Design: [`docs/plans/PRD-indigo-lamplighter.md`](docs/plans/PRD-indigo-lamplighter.md).

Status: M0 — schema, example config and the acceptance suite as strict `xfail`
stubs. No engine yet.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Expect **passes from `tests/test_schema.py` and a large block of `xfailed`** —
that is the M0 state, not a broken checkout.

### The acceptance suite is written before the engine, and it is strict

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
