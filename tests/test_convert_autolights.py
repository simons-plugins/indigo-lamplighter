"""The Auto Lights -> Lamplighter converter (PRD section 6, step 2).

The thing being tested is a translation between two formats that disagree,
so the failures worth catching are not crashes. They are conversions that
produce a *loadable, plausible* file which quietly means something else: a
light mapped to "leave" that the fork used to force off, a threshold variable
dropped so the Kitchen freezes at one lux level, a zone that arrives enabled
and starts driving lights the fork is still driving.

Every test below therefore names, in its docstring, the mutation it kills --
the specific wrong-but-working converter it would catch. Four of those
mutations were applied to the converter and the suite re-run to prove each
test actually fails, then reverted (md5 verified).

The fixture is jarvis's live Auto Lights configuration as of 2026-09-05: six
zones, a global period list referenced by id, and a `device_period_map` per
zone. Synthetic documents cover the combinations the live file happens not
to contain.
"""

import json
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import convert_autolights_config as conv  # noqa: E402  (needs the path above)

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "auto_lights_conf_jarvis_2026-09-05.json"
SCHEMA_PATH = (
    REPO_ROOT
    / "Lamplighter.indigoPlugin"
    / "Contents"
    / "Server Plugin"
    / "lamplighter"
    / "schema.json"
)

FIXTURE = json.loads(FIXTURE_PATH.read_text())
SCHEMA = json.loads(SCHEMA_PATH.read_text())

CONVERTED, REPORT = conv.convert(FIXTURE)


def zone(config, name):
    """The named zone, or fail loudly -- `None` would fail the next line anyway,
    with an AttributeError that says nothing about which zone went missing."""
    for candidate in config["zones"]:
        if candidate["name"] == name:
            return candidate
    raise AssertionError(f"no zone named {name!r}; got {[z['name'] for z in config['zones']]}")


def period(zone_dict, name):
    for candidate in zone_dict["periods"]:
        if candidate["name"] == name:
            return candidate
    raise AssertionError(
        f"no period named {name!r} in {zone_dict['name']}; "
        f"got {[p['name'] for p in zone_dict['periods']]}"
    )


def reported(fragment, report=None):
    return [line for line in (REPORT if report is None else report) if fragment in line]


# ------------------------------------------------------- synthetic documents


def fork_period(period_id, name, start, end, mode="On and Off", **extra):
    """One entry of the fork's global `lighting_periods` list."""
    raw = {
        "id": period_id,
        "name": name,
        "mode": mode,
        "from_time_hour": start[0],
        "from_time_minute": start[1],
        "to_time_hour": end[0],
        "to_time_minute": end[1],
        "lock_duration": -1,
        "limit_brightness": -1,
    }
    raw.update(extra)
    return raw


def fork_zone(
    name,
    period_ids,
    on_lights,
    off_lights=(),
    presence=(101,),
    luminance=(),
    behavior=None,
    luminance_settings=None,
    exclude=(),
    device_period_map=None,
):
    """One entry of the fork's `zones` list, with its defaults filled in."""
    behavior_settings = {
        "lock_duration": 60,
        "extend_lock_when_active": True,
        "lock_extension_duration": 30,
        "unlock_when_no_presence": True,
        "off_lights_behavior": "force off unless zone is locked",
    }
    behavior_settings.update(behavior or {})
    minimum = {
        "minimum_luminance": 2000,
        "minimum_luminance_use_variable": False,
        "minimum_luminance_var_id": None,
        "luminance_hysteresis": 0,
        "adjust_brightness": False,
    }
    minimum.update(luminance_settings or {})
    return {
        "name": name,
        "lighting_period_ids": list(period_ids),
        "device_settings": {
            "on_lights_dev_ids": list(on_lights),
            "off_lights_dev_ids": list(off_lights),
            "luminance_dev_ids": list(luminance),
            "presence_dev_ids": list(presence),
        },
        "minimum_luminance_settings": minimum,
        "behavior_settings": behavior_settings,
        "advanced_settings": {"exclude_from_lock_dev_ids": list(exclude)},
        "device_period_map": device_period_map or {},
        "global_behavior_variables_map": {},
    }


def fork_doc(zones, periods, plugin_config=None):
    return {
        "plugin_config": plugin_config or {},
        "zones": list(zones),
        "lighting_periods": list(periods),
    }


# ------------------------------------------------------------ the whole file


def test_every_zone_arrives_disabled():
    """Kills: emitting the fork's enabled state, or a bare `enabled: true`.

    Migration step 1 requires both plugins installed with Lamplighter driving
    nothing. A zone that arrives enabled drives lights the fork is still
    driving, and "never both engines on the same lights" is the one rule of
    section 6 that cannot be undone by editing a file afterwards.
    """
    names = [z["name"] for z in CONVERTED["zones"]]
    assert names == ["Study", "Kitchen", "Living Room", "Hallway", "Dining Room", "Back Garden"]
    assert [z["enabled"] for z in CONVERTED["zones"]] == [False] * 6


def test_converted_fixture_validates_against_the_bundled_schema():
    """Kills: any emitted shape the schema forbids -- a level of 0, a `to` of
    "23:59" outside the pattern's intent, an unknown key, an empty `levels`.

    jsonschema is a dev dependency and the plugin cannot import it, so this
    is the only place the emitted document meets the published contract
    rather than the loader's re-statement of it.
    """
    errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(CONVERTED))
    assert errors == [], [f"{list(e.absolute_path)}: {e.message}" for e in errors]


def test_converted_fixture_loads_through_the_plugins_own_loader():
    """Kills: emitting periods that overlap, which jsonschema cannot see.

    Auto Lights resolves an overlap by list order; the loader refuses one
    outright (R11), and the fixture contains a real overlap -- Study's
    "All Day Zone" under its "Evening". A converter that copied both bands
    across verbatim would pass the schema test above and be rejected by the
    plugin on jarvis, which is the worst place to find out.
    """
    config = conv.validate(CONVERTED)
    assert len(config.zones) == 6
    assert all(z.enabled is False for z in config.zones)


# ------------------------------------------------------------------ Hallway


def test_hallway_never_locks_because_every_light_is_lock_excluded():
    """Kills: copying `exclude_from_lock_dev_ids` into `override.exclude` and
    leaving `override.enabled` true.

    The fork had no per-zone "never lock" switch, so the Hallway expresses it
    by excluding its only light. Carried across literally, the zone is
    overridable-in-principle with nothing able to override it -- which looks
    identical until someone adds a second light to the hallway and it starts
    locking a zone that is supposed to be a timer.
    """
    hallway = zone(CONVERTED, "Hallway")
    assert hallway["lights"] == [459564566]
    assert hallway["override"]["enabled"] is False
    assert hallway["override"]["exclude"] == []


def test_hallway_ladder_ends_at_midnight_not_at_23_59():
    """Kills: passing the fork's inclusive `to` of 23:59 through unchanged.

    Lamplighter's `to` is exclusive, so a band written "23:00 to 23:59" stops
    a minute before midnight and the last minute of every day is OFF-DUTY --
    a hallway light that stays on from 23:59 to 00:00 because nothing has an
    opinion about it. "00:00" means midnight at the END of the band.
    """
    hallway = zone(CONVERTED, "Hallway")
    assert [(p["from"], p["to"]) for p in hallway["periods"]] == [
        ("00:00", "06:00"),
        ("06:00", "16:00"),
        ("16:00", "23:00"),
        ("23:00", "00:00"),
    ]
    assert [p["levels"]["459564566"] for p in hallway["periods"]] == [30, 100, 80, 30]


# ------------------------------------------------------------------ Kitchen


def test_kitchen_lux_carries_the_threshold_variable_and_the_hysteresis():
    """Kills: dropping `minimum_luminance_var_id`, or defaulting hysteresis to 0.

    The Kitchen threshold is tuned from a control page through a variable and
    its sensor sits in the room its own lights light. Lose the variable and
    the threshold freezes at whatever was in the file; lose the 300 band and
    the zone oscillates -- lights on, reading rises, not dark, lights off (R9).
    """
    lux = zone(CONVERTED, "Kitchen")["lux"]
    assert lux["device"] == 1616814762
    assert lux["dark_below_variable_id"] == 293227493
    assert lux["hysteresis"] == 300
    assert lux["when_unreadable"] == "dark"
    # The fork stored no fixed minimum alongside the variable, so the fallback
    # is 0 -- which means "never dark". That is a trap, so it is reported.
    assert lux["dark_below"] == 0
    assert reported("dark_below is 0")


def test_kitchen_override_timings_come_from_the_zone():
    """Kills: reading `lock_extension_duration` without `extend_lock_when_active`.

    The two fields are independent in the fork and a zone can carry a stale
    extension length with extending switched off. Reading the number alone
    turns a 60-minute Kitchen override into one that never ends while anyone
    is in the room.
    """
    override = zone(CONVERTED, "Kitchen")["override"]
    assert override == {
        "enabled": True,
        "duration_minutes": 60,
        "extend_minutes": 30,
        "unlock_on_leave": True,
        "exclude": [],
    }


def test_kitchen_force_off_maps_a_false_cell_to_off():
    """Kills: mapping every `false` cell to "leave".

    `false` means "excluded from this period", and what the fork then does
    depends on the zone: under "force off unless zone is locked" it actively
    drives the light to 0. The Kitchen spots are excluded overnight precisely
    so that they go off, and "leave" would leave them burning all night with
    the config looking correct.
    """
    kitchen = zone(CONVERTED, "Kitchen")
    overnight = period(kitchen, "Kitchen Overnight")
    night = period(kitchen, "Kitchen Night")
    assert overnight["levels"]["772478931"] == "off"
    assert overnight["levels"]["1256902388"] == "off"
    assert night["levels"]["772478931"] == "off"
    assert night["levels"]["1256902388"] == "off"
    # The pendants keep their integer level in the same period, so this is a
    # per-cell decision and not a whole-period one.
    assert overnight["levels"]["144694384"] == 30
    assert overnight["levels"]["1894385558"] == 10


def test_kitchen_night_limit_brightness_becomes_limit():
    """Kills: dropping `limit_brightness`, or writing the fork's -1 sentinel.

    -1 means "no cap" and 50 means 50; writing -1 through would be rejected
    by the schema, and dropping the 50 would let the Kitchen run at 60 at
    22:00. This is the fork's own bug (limit only applied inside the
    adjust-brightness branch) arriving as a converter bug.
    """
    kitchen = zone(CONVERTED, "Kitchen")
    assert period(kitchen, "Kitchen Night")["limit"] == 50
    assert "limit" not in period(kitchen, "Kitchen Overnight")


# -------------------------------------------------------- the other zones


def test_zone_with_no_luminance_device_gets_no_lux_block():
    """Kills: emitting a lux block with `dark_below: 0` for a sensorless zone.

    `lux` is required-but-nullable so that "no daylight gate" is stated. A
    zero-threshold block is not that: it is a gate that is never satisfied,
    so the Study would never be dark and its lights would never come on --
    and the file would load without a word.
    """
    study = zone(CONVERTED, "Study")
    assert "lux" in study
    assert study["lux"] is None


def test_back_garden_relays_map_true_to_on():
    """Kills: treating a `true` cell as "no explicit level" and omitting the key.

    An absent key is "leave" in Lamplighter (R12), so the garden relays would
    never be commanded at all -- the zone would look configured and do
    nothing. `true` in the fork means "included, use the zone's calculated
    brightness", which with adjust_brightness off is full.
    """
    evening = period(zone(CONVERTED, "Back Garden"), "Back Garden Evening")
    assert evening["levels"]["1445308831"] == "on"
    assert evening["levels"]["1634365746"] == "on"
    assert evening["levels"]["1976227552"] == "on"
    assert evening["levels"]["558991579"] == 20


def test_do_not_adjust_zone_maps_a_false_cell_to_leave():
    """Kills: mapping every `false` cell to "off".

    The mirror of the Kitchen test, and the reason the pairing has to be
    resolved per zone rather than once. Under "do not adjust unless no
    presence" the fork never writes an excluded light while the room is
    occupied; "off" would start switching off a light the fork left alone,
    which is a lamp going dark in an occupied room.
    """
    doc = fork_doc(
        [
            fork_zone(
                "Loft Auto Lights",
                [1],
                on_lights=[11, 22],
                behavior={"off_lights_behavior": "do not adjust unless no presence"},
                device_period_map={"11": {"1": False}, "22": {"1": 40}},
            )
        ],
        [fork_period(1, "All Day", (0, 0), (23, 59))],
    )
    config, _ = conv.convert(doc)
    levels = config["zones"][0]["periods"][0]["levels"]
    assert levels == {"11": "leave", "22": 40}
    conv.validate(config)


def test_off_lights_join_lights_and_are_off_in_every_period():
    """Kills: emitting only `on_lights_dev_ids` as the zone's `lights`.

    A device absent from `lights` is never written by the zone and can never
    create an override for it, so the fork's "off lights" list -- lights this
    zone turns off but never on -- would become inert, and the schema would
    not notice because the file is otherwise valid.
    """
    doc = fork_doc(
        [fork_zone("Porch Auto Lights", [1, 2], on_lights=[11], off_lights=[22])],
        [
            fork_period(1, "Day", (6, 0), (18, 0)),
            fork_period(2, "Night", (18, 0), (23, 59)),
        ],
    )
    config, _ = conv.convert(doc)
    porch = config["zones"][0]
    assert porch["lights"] == [11, 22]
    assert [p["levels"]["22"] for p in porch["periods"]] == ["off", "off"]
    assert [p["levels"]["11"] for p in porch["periods"]] == ["on", "on"]
    conv.validate(config)


def test_a_periods_own_lock_duration_becomes_a_period_override():
    """Kills: dropping a per-period `lock_duration`.

    It is the whole of PRD section 11 decision 4: a Dining evening period
    holds an override longer so a meal that runs late is not reverted
    mid-course. Dropped, the zone's 60 minutes applies and the lights change
    during dessert -- a difference nobody sees until it happens.
    """
    doc = fork_doc(
        [
            fork_zone(
                "Dining Auto Lights",
                [1],
                on_lights=[11],
                behavior={"lock_extension_duration": 45},
            )
        ],
        [fork_period(1, "Evening", (16, 0), (23, 59), lock_duration=120)],
    )
    config, report = conv.convert(doc)
    assert config["zones"][0]["periods"][0]["override"] == {
        "duration_minutes": 120,
        "extend_minutes": 45,
    }
    assert reported("lock_duration of 120", report)
    conv.validate(config)


# ------------------------------------------------------------- --presence


def test_presence_option_replaces_one_zones_devices():
    """Kills: merging `--presence` into the fork's list instead of replacing it.

    The point of the option is migration step 4: swap an Occupatum zone for
    the raw sensors behind it. Merged, the Occupatum device stays in the list
    and keeps ticking the zone -- the exact input that produced hundreds of
    re-plans an hour under the fork (R4).
    """
    config, report = conv.convert(
        FIXTURE, presence=conv.parse_presence(["Hallway=33980440,1107013217"])
    )
    assert zone(config, "Hallway")["presence_devices"] == [33980440, 1107013217]
    # and nothing else moved
    assert zone(config, "Kitchen")["presence_devices"] == [1604547174]
    assert reported("presence_devices replaced", report)
    conv.validate(config)


def test_presence_option_rejects_an_unknown_zone():
    """Kills: ignoring a `--presence` name that matches no zone.

    A typo is otherwise a silent no-op: the operator believes they expanded a
    zone to raw sensors, the Occupatum device is still there, and the only
    evidence is a device id that is not in the file they did not re-read.
    """
    with pytest.raises(conv.ConversionError) as exc:
        conv.convert(FIXTURE, presence={"Hallwy": [1]})
    assert "Hallwy" in str(exc.value)
    assert "Hallway" in str(exc.value)  # the known names are offered


def test_presence_option_accepts_the_forks_own_zone_name():
    """Kills: matching only the trimmed name, with no error and no effect.

    An operator copying a name out of the Auto Lights editor gets "Hallway
    Auto Lights". Rejecting it would be honest; silently accepting the
    argument and doing nothing would not, and this pins the third option --
    accept it and apply it -- so it cannot regress into the second.
    """
    config, _ = conv.convert(
        FIXTURE, presence=conv.parse_presence(["Hallway Auto Lights=42"])
    )
    assert zone(config, "Hallway")["presence_devices"] == [42]


# --------------------------------------------------------------- the report


def test_report_names_the_dropped_adjust_by_lux():
    """Kills: dropping `adjust_brightness` without saying so.

    The loader refuses `adjust_by_lux` on a zone with a lux block precisely
    so it cannot run at unscaled levels in silence (PRD 5.6). The converter
    has to make the same noise: every light that relied on the lux-scaled
    level now comes on at full, and nothing in the emitted file records that
    a setting was there at all.
    """
    doc = fork_doc(
        [
            fork_zone(
                "Snug Auto Lights",
                [1],
                on_lights=[11],
                luminance=[77],
                luminance_settings={"minimum_luminance": 500, "adjust_brightness": True},
            )
        ],
        [fork_period(1, "All Day", (0, 0), (23, 59))],
    )
    config, report = conv.convert(doc)
    assert reported("adjust_by_lux", report)
    assert "adjust_by_lux" not in json.dumps(config)
    conv.validate(config)


def test_report_names_the_fixed_hold_seconds():
    """Kills: writing the flat `--hold` value as though it were converted.

    Auto Lights has no per-zone presence hold -- it lived in the Occupatum
    device's off-delay, which is not in this file. A silent 300 on six zones
    reads as six migrated settings, and the one zone that needed 900 is
    discovered as a room going dark on someone.
    """
    lines = reported("hold_seconds")
    assert lines, REPORT
    assert "300" in lines[0]
    assert all(z["hold_seconds"] == 300 for z in CONVERTED["zones"])

    other, _ = conv.convert(FIXTURE, hold_seconds=900)
    assert all(z["hold_seconds"] == 900 for z in other["zones"])


def test_report_names_every_approximation_the_fixture_contains():
    """Kills: trimming the report to the interesting cases.

    The report is the product as much as the JSON is. These are the six
    things the live file cannot express faithfully, and each one is a
    decision the operator has to check by hand.
    """
    for fragment in (
        "overlapped",  # Study's All Day Zone under its Evening
        "never fired under Auto Lights",  # Study's Evening, 19:00 to 00:00
        "dark_below is 0",  # Kitchen's variable-backed threshold
        "no usable lock duration",  # Hallway
        "hold_seconds",
        "enabled: false",
        "MANUAL step",  # sunset conversion
    ):
        assert reported(fragment), f"{fragment!r} missing from:\n" + "\n".join(REPORT)


def test_overlapping_periods_are_trimmed_to_the_band_the_fork_actually_ran():
    """Kills: dropping the later period, or keeping both and letting the loader fail.

    Study lists "Evening" (19:00 to midnight) ahead of "All Day Zone", so the
    fork ran All Day only up to 19:00. Dropping it would leave the Study dark
    all morning; keeping both would produce a file the plugin refuses. The
    trim has to be the fork's own behaviour, written down.
    """
    study = zone(CONVERTED, "Study")
    assert [(p["name"], p["from"], p["to"]) for p in study["periods"]] == [
        ("Evening", "19:00", "00:00"),
        ("All Day Zone", "00:00", "19:00"),
    ]
    assert reported("trimmed to 00:00-19:00")


def test_a_period_wholly_covered_by_an_earlier_one_is_dropped_and_reported():
    """Kills: emitting a zero-length band, or a band that survives the trim empty.

    The general case of the Study trim. A period the fork could never reach
    has to leave the file, and it has to leave it loudly -- an author who
    wrote it believes it is doing something.
    """
    doc = fork_doc(
        [fork_zone("Cellar Auto Lights", [1, 2], on_lights=[11])],
        [
            fork_period(1, "All Day", (0, 0), (23, 59)),
            fork_period(2, "Shadowed", (9, 0), (17, 0)),
        ],
    )
    config, report = conv.convert(doc)
    assert [p["name"] for p in config["zones"][0]["periods"]] == ["All Day"]
    assert reported("entirely covered", report)
    conv.validate(config)


# ------------------------------------------------------------------- the CLI


def test_cli_writes_the_file_reports_to_stderr_and_exits_zero(tmp_path, capsys):
    """Kills: writing the JSON and skipping the load_config check.

    The converter's one hard promise is that what it emits is a file the
    plugin will actually load. Without the check a rejected config leaves
    with exit 0 and is discovered on jarvis; the report going to stderr
    rather than stdout is what lets `-o -`-style piping stay usable.
    """
    out = tmp_path / "lamplighter.json"
    code = conv.main([str(FIXTURE_PATH), "-o", str(out)])
    assert code == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Conversion report" in captured.err
    assert "hold_seconds" in captured.err

    written = json.loads(out.read_text())
    assert written == CONVERTED


def test_cli_prints_to_stdout_when_no_output_is_given(tmp_path, capsys):
    """Kills: requiring -o, or printing the JSON to stderr along with the report.

    Mixed into stderr the document cannot be piped anywhere, and the report
    cannot be read without the document scrolling past it.
    """
    code = conv.main([str(FIXTURE_PATH)])
    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == CONVERTED
    assert "Conversion report" in captured.err


def test_cli_exits_two_on_an_unknown_presence_zone(capsys):
    """Kills: letting a bad argument through as an exception traceback.

    Exit 2 is "you gave me something unusable", distinct from exit 1, "what I
    produced will not load" -- and a traceback is neither.
    """
    code = conv.main([str(FIXTURE_PATH), "--presence", "Nowhere=1"])
    assert code == 2
    assert "Nowhere" in capsys.readouterr().err


def test_cli_exits_one_when_the_result_would_not_load(tmp_path, capsys):
    """Kills: exiting 0 on a conversion that produced nothing loadable.

    A document whose every zone was dropped still emits valid-looking JSON --
    `{"version": 1, "zones": []}` -- and the loader is the only thing that
    calls it: a zone list with no zones is not a configuration. The output is
    written anyway so the operator can read what failed rather than being
    handed an error and an empty hand.
    """
    doc = fork_doc(
        [fork_zone("Empty Auto Lights", [1], on_lights=[])],
        [fork_period(1, "All Day", (0, 0), (23, 59))],
    )
    source = tmp_path / "auto_lights_conf.json"
    source.write_text(json.dumps(doc))
    out = tmp_path / "lamplighter.json"

    assert conv.main([str(source), "-o", str(out)]) == 1

    err = capsys.readouterr().err
    assert "does not load" in err
    assert "nothing to drive" in err  # why the zone went, not just that it did
    assert json.loads(out.read_text())["zones"] == []
