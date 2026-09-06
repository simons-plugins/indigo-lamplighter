# PRD — Lamplighter (`indigo-lamplighter`)

Presence- and daylight-driven lighting for Indigo. Successor to the
`indigo-auto-lights` fork, which it replaces one zone at a time.

- **Plugin id**: `com.simons-plugins.indigo-lamplighter`
- **Status**: PRD, 2026-09-04. No code.
- **Author of the requirements**: three weeks of running the Auto Lights fork on
  six zones of this house, and the 238 tests that came out of it.

---

## 1. Summary

A lighting zone is a small state machine with four inputs and one output.
Inputs: when presence was last seen, how bright the room is, which period it
is, and whether a person has taken over. Output: a desired level per device.
Every decision comes from those inputs. **No decision is ever made by reading
live device state and comparing it to a target.**

That single rule is the reason the plugin exists. Auto Lights re-plans a zone
on every event and decides by diffing live state against target, and every
serious bug found in it this month was a consequence: a manual change reverted
before it could be seen (#15), self-locks whenever another light was still
ramping (#10), a zone re-planned once a second because its presence device
ticked (#16), and a write machinery of settle polls, retries, suppression and
rate limits built to make the diff safe. Lamplighter does not have those parts
because it does not have that rule.

## 2. Goals

1. Lights follow presence, gated by darkness, at per-device levels that change
   through the day, including relative to sunset and across midnight.
2. A person always wins. A wall dimmer, a wireless button, an app or a voice
   command holds its level for a configurable time, extends while the room is
   in use, and releases when the room empties. Detection is from the device
   change itself and lands within a second, before anything can revert it.
3. The plugin never fights its own writes, its own echoes, a slow device, a
   dimmer that flashes to its old level on the way up, or a group that reads
   back a few points low.
4. Presence hold lives in the zone. Raw sensors (PIR, mmWave radar, door
   contacts, Occupatum zones during migration) feed a per-zone last-seen
   timestamp and hold time. No second plugin is needed to say "still here".
5. State survives restarts and configuration changes. A lock created at 19:46
   is still a lock after a config reload at 19:50.
6. Configuration is a JSON file an agent edits through MCP tools, validated
   against a schema, hot-reloaded. The plugin explains itself: every zone can
   say why it is doing what it is doing, and publishes counters.
7. Stdlib only. Small enough to read in an evening.

## 3. Non-goals (v1)

- Colour temperature. Driven from Apple Home over Matter and by existing CCT
  triggers today; stays there.
- A configuration web page. A read-only status page is a v2 candidate.
- Scenes, schedules, holiday modes, energy accounting.
- Replacing Occupatum for anything other than lighting zones.
- Binding wall controls to zones (so the plugin knows a dimmer *is* the
  kitchen strips' control). v2 candidate; see §12.
- Upstream compatibility with Auto Lights configuration. A one-way converter
  is provided; the file formats are different on purpose.

## 4. What the fork taught us (requirements by evidence)

Each item below is a behaviour the fork got wrong, or got right only after a
fix, with the evidence. These are the acceptance criteria, not background.

| # | Requirement | Evidence |
|---|---|---|
| R1 | Manual override is judged from the device-change **transition** (before-state at desired, after-state off desired), per device, never from a live re-read. | Fork #15: revert landed first, live read saw nothing. 2026-09-03 19:46:35, transition rule locked 112 ms after the command. |
| R2 | Our own writes never create an override: we only command devices that are off desired, so every echo starts off desired. | Fork attempt 1 (194e6af) locked on every ramp step. |
| R3 | An echo of our command that arrives after the desired level moved back onto the device's pre-command state is still ours: remember the state each device was commanded away from, 15 s, consumed once. | Review of #16: in-room lux rises → not dark → lights off → delayed on-echo read as override. |
| R4 | A zone re-plans only when an **input** changes: presence on/off, presence last-seen crossing the hold, lux crossing the (hysteresis-widened) threshold, a period boundary, an override starting or ending. Not on any device update. | Occupatum ticked every 1.2 s → hundreds of re-plans an hour → reverts within a second, 10 s callback lag. |
| R5 | Brightness comparisons use a proportional band: `max(1, ceil(10 % of target))`, with 0 and 100 exact. | zigbee2mqtt truncates both ways (30 → 29); a group dimmer reads back 45..48 for 50. |
| R6 | A late reporter (a device whose state arrives seconds or more after the command) is neither retried nor suppressed nor treated as an override; it is reconciled when it finally reports. | Under the fork's 2 s confirm-and-suppress machinery, any light that had not reported by the re-check could read as a manual override; the workaround was `exclude_from_lock_dev_ids`. |
| R7 | A dimmer that flashes to its previous level before settling must not lock. | Tuya TS0502B turn-on behaviour; covered by R1 (previous state off desired). |
| R8 | A device whose state cannot be read warns once and is excluded from override detection, and the zone keeps working for its other devices. | Fork `_check_confirm` fall-through was a level-5 log. |
| R9 | Lux gating is a Schmitt trigger: the band applies only when leaving dark, and must be wider than the zone's own lights add at the sensor. | Kitchen sensor is in the kitchen; +100..200 lux from the lights. |
| R10 | Locks: duration, extend-while-active, unlock-on-leave (from the zone's own presence hold, so it works for locks created while occupied), per-zone "never lock", per-device exclusion. | Fork #17: unlock-on-leave only armed when the lock was created with the room already empty. |
| R11 | Periods are sunset/sunrise-relative when asked, may cross midnight, and must not overlap (validation error, not first-match-wins). | Kitchen "Dusk" band is a fixed 16:00–19:00 approximation; Garden needs two hard-off bands to express one. |
| R12 | Per-device level per period: an int 1–100, `on`, `off` (force off), or `leave` (don't touch). "Leave" plus force-off must not be two settings that only work in one combination. A period may carry `vacant_levels`; absent means off when vacant (unchanged). | Fork `device_period_map` false + `off_lights_behavior` pairing. |
| R13 | State (lock expiry, override device, presence last-seen, counters) is persisted on the zone device and restored on startup. | Every fork reload: "all locks and zone state has been reset". |
| R14 | Log the real trigger of a re-plan, and publish per-zone counters. | Fork's "Triggered by" is the most recently changed sensor, not the cause. |
| R15 | Every degradation path says so: unreadable sensor, missing device, stale lux, unparseable config. No quiet zero. | Workspace convention; fork's `_luminance_unreadable_warned` pattern. |

## 5. Architecture

### 5.1 Process model

One Indigo plugin process. Indigo callbacks (`deviceUpdated`, `variableUpdated`)
do the minimum: classify the event, update the zone's **inputs**, and mark the
zone dirty. One worker thread (`runConcurrentThread`) drains dirty zones,
runs the state machine, and reconciles devices. No per-write threads. Period
boundaries and hold expiries are scheduled on the same worker via a small
timer heap, not `threading.Timer` per zone.

Consequence: the callback thread is never blocked by a write, and a burst of
device events costs a few dictionary updates.

### 5.2 Zone inputs

```
presence:  last_seen (monotonic + wall clock), active (bool, derived: now - last_seen < hold)
lux:       value, read_at, dark (bool, Schmitt), unreadable (bool, warned once)
period:    the active LightingPeriod or None; next boundary time
override:  None | {device_id, since, expires_at, extended_count}
enabled:   plugin-level and zone-level
```

`active` and `dark` are *derived* inputs with their own transitions; the state
machine reacts to their edges, not to the raw readings.

### 5.3 Zone state machine

```
            ┌──────────┐  period ends / not dark / disabled   ┌──────────┐
            │ OFF-DUTY │ <────────────────────────────────────┤          │
            └────┬─────┘                                       │          │
   period + dark │                                             │          │
                 v                                             │          │
            ┌──────────┐  presence active   ┌──────────┐       │          │
            │  VACANT  │ ─────────────────> │ OCCUPIED │       │          │
            └────┬─────┘ <───────────────── └────┬─────┘       │          │
                 │        hold expired            │            │          │
                 │  override detected             │ override   │          │
                 └──────────────┐   ┌─────────────┘ detected   │          │
                                v   v                          │          │
                           ┌────────────┐  expiry with room    │          │
                           │ OVERRIDDEN │  empty, or left+grace │          │
                           └────────────┘ ─────────────────────┘          │
                                  ^  expiry with presence: extend         │
                                  └───────────────────────────────────────┘
```

- **OFF-DUTY**: no active period, or the room is bright, or the plugin/zone is
  disabled. Desired depends on the cause (decided 2026-09-04, resolving a
  contradiction with §5.6): **bright** → the same as VACANT (every device
  with a level → off, `leave` → leave), because daylight makes the lights
  unnecessary and the house relies on lights going off when a room brightens;
  **no active period** or **disabled** → `leave` for every device, the plugin
  has no opinion. Periods in "off only" mode never turn lights on in
  OCCUPIED and turn them off in VACANT.
- **VACANT**: desired = off for every device with a level in this period;
  `leave` devices untouched.
- **OCCUPIED**: desired = the period's level per device.
- **OVERRIDDEN**: desired = *whatever the devices are*. Nothing is written.
  Entered by R1/R3. Exit rules from R10.

Transitions are logged with the input edge that caused them and the values
that fed the decision (R14).

### 5.4 Presence

- `presence_devices`: any Indigo device with an on/off state, any-of.
  Occupatum zone devices are accepted as one such device during migration.
- **The rule is Occupatum's: the zone is occupied while ANY presence device is
  on, and the off-delay starts only when the LAST one clears.** Not
  `now - last_seen < hold`, which is a different rule that happens to agree
  for edge sensors and fails for level ones.
- `hold_seconds`: how long the zone stays occupied *after the last sensor
  clears*. A raw PIR with a 10 s hardware hold and `hold_seconds: 300` behaves
  like an Occupatum zone with a 300 s off-delay, without the ticking device.
  `hold_seconds: 0` means "follow the sensors exactly": occupied while one is
  on, empty the instant the last goes off.
- **Level sensors are why this matters.** An mmWave radar (Aqara FP1) reports
  "on" once when somebody enters and then says nothing until they leave. A
  hold measured from the last "on" ages that single reading out and turns the
  lights off with the person sitting still — which is what the Study did. A
  PIR re-reports on movement and hides the bug; a radar does not.
- While any sensor is on there is **no hold running**, so no hold wake-up is
  scheduled. The wake is scheduled when the last sensor clears.
- Two pieces of state, and only one is persisted:
  - the set of devices currently reporting — rebuilt at startup by reading the
    devices themselves, because who is on *now* is a fact about the room, not
    about what the plugin believed when it stopped. A radar that has been on
    since before a restart is picked up by seeding and holds the zone.
  - `last_seen` — stamped on an "on" reading *and on an "off"* (the clear is
    when the delay starts), and persisted (R13) so a restart does not turn the
    lights off on an occupied room.
- An "off" reading is therefore an input edge: it is the only notice the
  worker gets that a hold has begun and a wake-up is now due. It is not a
  state edge — the room stays occupied for `hold_seconds` more.

### 5.5 Periods

```json
{"name": "Dusk", "from": "sunset-30m", "to": "20:00", "mode": "on_and_off"}
{"name": "Night", "from": "22:00", "to": "06:00", "mode": "on_and_off"}
```

- `from`/`to` accept `HH:MM`, `sunrise±offset`, `sunset±offset`. Sunrise and
  sunset come from Indigo's server (`indigo.server.calculateSunrise/Sunset`).
- A period may cross midnight. Periods within a zone must not overlap at any
  minute of the year; the loader computes today's and tomorrow's instances
  and rejects overlaps with the offending pair named.
- Modes: `on_and_off`, `off_only`. (Fork parity. No "on only".)

### 5.6 Levels

Per device per period: `int 1..100 | "on" | "off" | "leave"`. Optional per
period: `limit` (cap for every device), `adjust_by_lux` (level = f(lux) for
devices without an explicit int), and `override` (`duration_minutes`,
`extend_minutes`) replacing the zone's override timing while the period is
active. `leave` means the plugin never writes the
device in that period, in any state; `off` means it is turned off in VACANT
(and in OFF-DUTY when the cause is bright) and held off in OCCUPIED.
`adjust_by_lux` is **not implemented in v1**: the loader rejects
`adjust_by_lux: true` on a zone that has a lux block, with a message saying
so, rather than letting a runtime path raise. `hold_seconds: 0` means
presence is never active (the zone behaves as off-only).

A period may also carry `vacant_levels`, the same shape as `levels`, for the
lights that should not simply go dark when the room is empty -- a porch light
at 25% all evening, jumping to 100% on motion and dropping back to 25% when
the hold lapses, rather than off. A light absent from `vacant_levels` still
goes off when vacant, exactly as before this key existed; a light present
here must also have a non-`leave` level in `levels` (the loader refuses
otherwise -- a vacant level for a light the period does not manage is a
mistake, not a default), and its value is mapped exactly as an occupied level
is (capped by `limit`, `"leave"` meaning "don't write it"). `bright` (OFF-DUTY)
still turns every one of these lights off outright, regardless of the
period's mode: daylight makes the light unnecessary, dim level included.

### 5.7 Override detection

The rule from the fork, unchanged, because it is proven:

1. The device has a desired level in the current period, is not excluded, and
   the zone is not `never_lock`.
2. The before-state is at the desired level (R5 band).
3. The after-state is not.
4. The transition's starting state is not one the plugin commanded the device
   away from inside `echo_window_seconds` (15). Each command excuses one
   transition.

Applied on every `deviceUpdated` for a zone light, on the callback thread,
before anything else can run.

`unlock_on_leave` is edge-shaped: the override is released when the zone's
presence hold expires **after** the override began, i.e. the room was
occupied when the person took over and has since emptied. A lock taken in an
already-empty room (the `lock zone` action from an app) is not released by
the next tick; it runs to its expiry. This closes fork #17 in both
directions and survives persistence, since both timestamps are persisted. Creating the override marks the zone dirty; the
worker records it, persists it, and logs the device and the before/after.

### 5.8 Reconcile

The worker computes desired levels for a dirty zone and, for each device whose
actual level is outside the band from desired, sends **one** command and notes
the pre-command state (R3). No settle poll. The zone is re-checked at the next
input edge or at the reconcile tick (`reconcile_seconds`, default 60): devices
still off desired are commanded again, with backoff per device (1, 2, 4, 8
ticks) and a single warning at the first backoff step naming the device and
its actual vs desired. A device commanded less than `COMMAND_RECHECK_SECONDS`
ago is in flight and is neither re-commanded nor warned about by an earlier
pass, whatever woke the zone. Once that ladder is walked the device is parked and
retried every `PARKED_RETRY_SECONDS` (default 600) by the wall clock instead
of by counting passes, since passes are one counter shared by every zone in
the house. A device that later reports at desired clears its backoff
silently, and so does a parked device whose report changes anything at all
(a link quality counts; an update that changes nothing does not) -- a changed
report is evidence it is alive again, and the zone is woken to try it at the
very next pass.

This replaces settle-and-confirm, consecutive-failure counting, suppression,
recovery scans, writer re-evaluation and the re-evaluation rate limit.

### 5.9 Device quirks carried over

- Band: `max(1, ceil(target × 0.10))`, 0 and 100 exact (R5).
- Relays: desired `on`/`off` compare `onState`; dimmers compare `brightness`;
  a device with neither warns once and is excluded from override detection
  (R8) but still receives commands.
- Turn-on flash (R7) and late reporters (R6) need no special handling under
  R1 and §5.8; they are acceptance tests, not code paths.

### 5.10 State on the zone device

Indigo device type `lamplighter_zone`, one per zone. States are the inputs and
the machine state, so a control page, Domio or a trigger can read them:

`state` (off_duty/vacant/occupied/overridden), `presence_active`,
`presence_last_seen`, `lux`, `dark`, `period`, `override_device`,
`override_expires`, `desired_summary`, `explain` (one line: why the zone is in
its state), `evaluations_today`, `writes_today`, `overrides_today`,
`last_trigger`.

Persisted across restarts: `presence_last_seen`, `override_*`, `dark`.

A plugin-level `lamplighter_controller` device carries the global enable and
the counters summed, so "all automation off" is one device.

### 5.11 Configuration

`Preferences/com.simons-plugins.indigo-lamplighter/lamplighter.json`, schema
in the bundle, validated on load with the failing path named. The plugin
watches the file's mtime and hot-reloads: zone objects are rebuilt from the
new config, then the persisted state (§5.10) is re-applied, so locks and
presence survive an edit.

```json
{
  "version": 1,
  "reconcile_seconds": 60,
  "echo_window_seconds": 15,
  "zones": [
    {
      "name": "Kitchen",
      "enabled": true,
      "presence_devices": [1465867145, 735515977, 710473944, 1544029753],
      "hold_seconds": 300,
      "lux": {"device": 1616814762, "dark_below": 2200, "hysteresis": 300},
      "lights": [772478931, 1256902388, 1894385558, 1990903005, 144694384],
      "override": {"duration_minutes": 60, "extend_minutes": 30,
                   "unlock_on_leave": true, "exclude": []},
      "periods": [
        {"name": "Overnight", "from": "00:00", "to": "06:00", "mode": "on_and_off",
         "levels": {"772478931": "leave", "1256902388": "leave",
                    "1894385558": 10, "1990903005": 10, "144694384": 30}},
        {"name": "Dusk", "from": "sunset-30m", "to": "19:00", "mode": "on_and_off",
         "override": {"duration_minutes": 120, "extend_minutes": 30},
         "levels": {"772478931": 50, "1256902388": 50, "1894385558": 60,
                    "1990903005": 60, "144694384": 100}}
      ]
    }
  ]
}
```

### 5.12 MCP surface (in `indigo-mcp-lite`)

`lamplighter_list_zones`, `lamplighter_get_zone` (config + live state +
`explain`), `lamplighter_update_zone` (JSON-merge patch, schema-validated,
written and reloaded, returns the validation errors verbatim on failure),
`lamplighter_reset_override` (zone or all), `lamplighter_set_enabled`,
`lamplighter_explain` (dry-run the state machine for a zone now, or at a given
time, without writing). The last one is the tool that replaces reading the
plugin's mind from the event log.

### 5.13 Actions and menu

Actions: reset override (zone/all), **lock zone** (create an override without a
device change: the thing scripts wanted in the fork), set zone enabled,
reconcile now. Menu: print zone states, dump explain for all zones.

Two more actions exist for a caller that is not a person, and both **return**
their answer rather than only logging it (an `executeAction(...,
waitUntilDone=True)` caller reads the value):

- **`validate_config`** (hidden) — the single validation path. Takes the whole
  document as JSON, runs it through `config.load_config` and nothing else, and
  answers `{ok, zones, enabled}` or `{ok: false, path, message}` with the JSON
  pointer to the value that is wrong. A bad document is a *result*, never an
  exception. It exists because the `lamplighter_*` MCP tools live in
  indigo-mcp-lite, which is stdlib-only and in another process: it cannot
  import the loader, and a second implementation of "is this valid" is a
  second opinion.
- **`explain_zone`** (visible, so an action group can log it) — one zone's
  reasoning. With no time, the live explain line plus the current plan; with
  an optional local `at` (`YYYY-MM-DDTHH:MM`), a **dry run** at that instant:
  which period covers it, what state the machine would be in, and what each
  light would be told, resolved from the inputs the zone holds now. Side
  effect free by construction, which is the hard part — the Schmitt trigger
  advances when read and `_age_override` releases locks when aged, so the dry
  run uses read-only twins of both (R9, R10).

## 6. Migration

1. Install Lamplighter alongside the fork. Both enabled; no zone configured
   in Lamplighter yet, so nothing conflicts.
2. `tools/convert_autolights_config.py` reads `auto_lights_conf.json` and
   emits `lamplighter.json` with every zone `enabled: false`, Occupatum zones
   as the presence devices, `hold_seconds` from the Occupatum off-delay, and
   the period ladders copied (wall-clock; sunset conversion is a manual step).
3. Per zone, in this order: **Hallway** (one light, never-lock), **Study**,
   **Back Garden**, **Dining**, **Living Room**, **Kitchen**. For each: disable
   the zone in the fork, enable it in Lamplighter, live-test the two checks
   (own write → no override; one dial move → override within a second and the
   level holds), then leave it a day.
4. Replace Occupatum with raw sensors per zone once the zone is stable, using
   the same hold. Occupatum stays for non-lighting uses.
5. When the Kitchen has run a week, disable the fork plugin. Delete it a month
   later.

Never both engines on the same lights.

## 7. Acceptance suite

Ported from the fork's `tests/` as promises, not as code. Same discipline:
one test per promise, named for the mutation it kills, verified by applying
the mutation; "not called" made fatal; degradation paths assert the warning.

Must-have promises (the fork's numbering in brackets):

- override survives a concurrent revert [M1]; own ramp never locks [M2]; late
  reporter never locks [M3]; turn-on flash never locks [M4]; sibling mid-write
  cannot lock this device [M5]; override lands while a write is in flight
  [M6]; relays both polarities [M7]; band edges [M8]; no before-state → no
  override and a warning [M9]; excluded, never-lock, no level, disabled [M10];
  echo after target revert, single-jump and ramping, consumed once, window
  bound [M12].
- re-plan only on input edges: a presence device re-reporting "on" with no
  change does nothing; a display-string or timer update does nothing; a
  genuine off→on re-plans once.
- presence hold: last-seen persists across restart; hold expiry turns lights
  off exactly once; unlock-on-leave fires from hold expiry with a lock held.
- periods: sunset-relative resolves against the server; midnight wrap; overlap
  rejected with the pair named; "off only" mode.
- reconcile: one command per device per pass; backoff sequence; warning once;
  a device reporting at desired clears backoff; late reporter reconciled
  without retry storms.
- config: invalid file rejected with the path; hot reload preserves override
  and presence state; unknown device id warns and the zone keeps running.
- degradation: unreadable lux sensor → zone treats as dark per config flag and
  warns once; unreadable light → excluded from override detection with a
  warning; Indigo lookup failure is not "device gone".

## 8. Milestones

| M | Deliverable | Done when |
|---|---|---|
| M0 | This PRD, JSON schema, `tests/` skeleton with the promise list as `xfail` stubs | Simon signs off the schema |
| M1 | Engine (inputs, state machine, override rule, reconcile) against a fake `indigo` | all M0 promises pass, mutations verified |
| M2 | Plugin bundle: devices, actions, config load/reload/persist, logging | installs on jarvis, Hallway live |
| M3 | Converter + MCP tools | Study and Back Garden live via MCP-edited config |
| M4 | Dining and Living Room live; raw sensors replace Occupatum on one zone | a week clean |
| M5 | Kitchen live; fork disabled | a week clean; fork removed a month later |

## 9. Risks

- **The long tail of hardware.** The rules in §5.9 are the ones we know. New
  quirks will appear on zones that have not been under the fork's microscope
  (Garden relays, Dining Skydance dimmer). Mitigation: per-zone rollout, one
  at a time, with a day of soak each.
- **Persistence on device states.** Indigo device states are strings/numbers;
  timestamps need a fixed ISO format and a version key. Mitigation: a single
  `persist.py` with round-trip tests.
- **Sunrise/sunset API shape.** `indigo.server.calculateSunrise(date)` /
  `calculateSunset(date)` take a `datetime.date` and return a `datetime`
  (verified against the 2025.2 IOM reference). Timezone of the returned
  value still to be checked on jarvis before M2; fall back to a fixed time
  with a warning if the call fails.
- **The temptation to add a settle poll back.** If a device does not confirm,
  the answer is the reconcile tick, not a thread. Written down here so the
  first flaky device does not reintroduce §5.8's predecessor.

## 10. Logging

One INFO line per state transition with the cause and the inputs; one INFO
block per reconcile pass that writes anything, listing device, actual,
desired; WARNING once per condition per device (unreadable, backoff, no
before-state, invalid config); DEBUG for everything else, and the file
handler follows the configured level from startup.

## 11. Open questions for Simon

1. **Decided 2026-09-04: controller device.** Global enable is a
   `lamplighter_controller` device (controllable from Domio, control pages,
   triggers and action groups), with the per-zone devices carrying their own
   enable underneath it.
2. **Decided 2026-09-04: yes.** `lock zone` action ships in v1.
3. **Decided 2026-09-04: dark.** An unreadable lux sensor makes the zone
   dark (lights follow presence) and warns once; per-zone flag kept for the
   garden-style zones where bright would be the safer failure.
4. **Decided 2026-09-04: kept.** A period may carry its own `override`
   block (`duration_minutes`, `extend_minutes`) that replaces the zone's while
   that period is active — for example a longer hold during a Dining evening
   period so a meal that runs late is not reverted mid-course. Absent, the
   zone's values apply.

## 12. v2 candidates

- **Controls bound to zones.** Declare that the Samotech dimmer at
  `1949753199` *is* the control for the strips group. The plugin then mirrors
  press/dial itself (retiring the `indigo-scripts` wall_mirror triggers), and
  an override is known from the control, not inferred from the light.
- **Read-only status page** through IWS: zones, states, explain, counters.
- **Adaptive hold**: learn `hold_seconds` per zone from presence gap
  statistics (the Dining study: 5 min bridges 69 % of gaps, 55 min needed).
- **Scenes** as named level sets a period or an action can reference.

## 13. Out of scope

Colour temperature, energy, schedules unrelated to presence, anything the
Home Intelligence plugin does with the same data.
