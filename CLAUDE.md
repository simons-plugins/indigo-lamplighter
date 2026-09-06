# CLAUDE.md — Lamplighter

> **Part of the [Indigo workspace](../CLAUDE.md)** — see root for cross-project map, standards, and tooling.

## Project Identity

- **Name**: Lamplighter
- **Type**: Indigo plugin
- **Shortcut**: `lamplighter`
- **Plugin id**: `com.simons-plugins.indigo-lamplighter`
- **GitHub**: https://github.com/simons-plugins/indigo-lamplighter
- **Language**: Python 3.10+ (stdlib only — no bundled deps)

## Role in the workspace

Presence- and daylight-driven lighting engine: zones of lights with per-device
levels per period, presence hold inside the zone, lux gating with hysteresis,
and manual-override locks detected from device-change transitions. Successor
to the `indigo-auto-lights` fork, which it replaces zone by zone.

**Read the PRD first**: [`docs/plans/PRD-indigo-lamplighter.md`](docs/plans/PRD-indigo-lamplighter.md).
It encodes everything learned from three weeks of running the fork on this
house: the transition rule for manual overrides, the echo window, the
tolerance band, the slow-device and turn-on-flash quirks, and why decisions
must never be made from live device state.

## Related projects

- [`../indigo-auto-lights/`](../indigo-auto-lights/) — the fork being replaced; its
  `tests/` are the acceptance suite to port, its `auto_lights_conf.json` the
  migration source.
- [`../indigo-mcp-lite/`](../indigo-mcp-lite/) — hosts the `lamplighter_*` MCP
  tools (config edits, explain, reset lock) once the plugin exists.
- [`../indigo-scripts/`](../indigo-scripts/) — the wall-dimmer mirror scripts
  that create manual overrides today (v2 candidate: bind controls to zones).

## Standards

Inherits workspace standards from [root CLAUDE.md](../CLAUDE.md#common-standards-apply-to-every-project-unless-its-claudemd-overrides). Key points:

- **Version bump per PR**: `Info.plist` `PluginVersion` (`YYYY.R.P`); `CFBundleVersion` stays `1.0.0`.
  **Pre-release rule (Simon, 2026-09-05, applied until the first release, v2026.1.12 on 2026-09-06): the build up to the first release was `2026.1.x` — PATCH bumps only, so the first shipped version was not `2026.17`.** Post-release bump policy: not yet decided by Simon; until it is, keep patch bumps on `2026.1.x`.
- **Testing**: pytest with a fake `indigo` in `tests/conftest.py`; one test per promise, each named for the mutation it kills, verified by applying the mutation; degradation paths must say so, never return a quiet empty result.
- **Merge**: GitHub PR only, never `--admin`, never squash, wait for CI green, wait for Simon's go-ahead. Enforced since 2026-09-06 (the workflows landed in PR #1; the ruleset was set up alongside it in repo Settings → Rules) by the "Protect main" ruleset: PRs required, squash merge blocked (merge/rebase only), `check-version` + `pytest (3.10)` + `pytest (3.13)` required, no force-push or branch deletion. Convention, not enforced: Simon's go-ahead per PR, and `/pr-review-toolkit:review-pr` before the merge ask.
- **Releases**: on EVERY merge to main (`create-release.yml`, same as mcp-lite) -- NOT the workspace's opt-in `[release]` marker; Simon's explicit call for this repo. The version a merge ships is whatever the PR bumped to, so a bump is never optional -- `check-version` (required) rejects a PR whose version is already tagged, and a merge that slipped through without one would silently produce no release.
- **Before writing Indigo plugin code**: invoke `/indigo:dev`.
- **Live testing**: on jarvis. The Auto Lights fork is retired (all six zones migrated by 2026-09-06, fork config `zones: []`); never re-enable it against lights Lamplighter owns.
