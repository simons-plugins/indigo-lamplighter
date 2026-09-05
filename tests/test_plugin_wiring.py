"""Unit tests for the plugin bundle around the engine (PRD 5.1, 5.10, 5.11, 5.13).

`plugin.py` has no lighting logic in it, so what these test is the wiring:
does an Indigo event reach the engine, does a zone's state reach its device,
does a bad configuration stop the good one, and does the file handler follow
the configured level. Every one of those fails silently in production -- a
plugin that never subscribes to device changes looks exactly like a quiet
house -- so each is pinned here rather than found on jarvis.

Nothing here sleeps. The fake `PluginBase.sleep` records the delay and raises
`StopThread` after a set number of passes, so `runConcurrentThread` can be
run for exactly one iteration.
"""

import datetime as dt
import json
import logging
import os
import types
import xml.etree.ElementTree as ET

import indigo
import pytest
from helpers import make_device, make_period, make_snapshot, make_zone_document

import plugin as plugin_module
from lamplighter import compare, indigo_sync

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)

SERVER_PLUGIN = os.path.join(
    os.path.dirname(__file__),
    os.pardir,
    "Lamplighter.indigoPlugin",
    "Contents",
    "Server Plugin",
)


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A fake Indigo installation folder the plugin will write into."""
    monkeypatch.setattr(indigo.server, "getInstallFolderPath", lambda: str(tmp_path))
    return tmp_path


def hallway(name="Hallway", **fields):
    fields.setdefault("presence_devices", [101])
    fields.setdefault("lights", [201])
    fields.setdefault("lux", None)
    return make_zone_document(
        name,
        periods=[make_period("Evening", "00:00", "23:59", levels={"201": "on"})],
        **fields,
    )


def a_document(*zones):
    return {"version": 1, "zones": list(zones) or [hallway()]}


def write_config(document):
    """Put a configuration where the plugin will look for it."""
    path = plugin_module.config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


def write_text_config(text):
    path = plugin_module.config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def make_plugin(log_level=logging.INFO):
    return plugin_module.Plugin(
        plugin_module.PLUGIN_ID, "Lamplighter", "2026.0.1", {"log_level": log_level}
    )


def started(document=None):
    """A started plugin, with its lights and presence sensors on the server."""
    if document is not None:
        write_config(document)
    make_device(101, "relay", name="Hallway Motion")
    make_device(201, "relay", name="Hallway Light")
    the_plugin = make_plugin()
    the_plugin.startup()
    return the_plugin


def zone_devices():
    return [
        dev
        for dev in indigo.devices
        if dev.deviceTypeId == plugin_module.ZONE_TYPE_ID
    ]


def controller_device():
    for dev in indigo.devices:
        if dev.deviceTypeId == plugin_module.CONTROLLER_TYPE_ID:
            return dev
    return None


def device_for(zone_name):
    for dev in zone_devices():
        if dev.pluginProps.get("zone_name") == zone_name:
            return dev
    return None


def an_action(**props):
    return types.SimpleNamespace(props=props, pluginTypeId="")


def a_device_action(kind):
    return types.SimpleNamespace(deviceAction=kind)


# --------------------------------------------------------------- the startup


def test_startup_writes_a_starter_configuration_when_there_is_none(install, caplog):
    """A fresh install gets a file it can edit, and no error.

    Kills: treating "zones: []" as a broken configuration. The loader refuses
    an empty zone list (a *configured* file must not have one), so the naive
    reading logs an ERROR on every first start and tells a new user their
    install is broken.
    """
    with caplog.at_level(logging.DEBUG):
        the_plugin = started()

    with open(plugin_module.config_path(), encoding="utf-8") as handle:
        assert json.load(handle) == {"version": 1, "zones": []}
    assert the_plugin.engine.zones == {}
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_startup_creates_one_device_per_zone_and_one_controller(install):
    """Kills: creating the devices but not linking them to their zones, which
    leaves every zone publishing into the first device it finds."""
    started(a_document(hallway("Hallway"), hallway("Study", presence_devices=[102])))

    assert sorted(dev.pluginProps["zone_name"] for dev in zone_devices()) == [
        "Hallway",
        "Study",
    ]
    assert controller_device() is not None


def test_startup_does_not_create_a_second_device_for_a_zone_that_already_has_one(install):
    """A restart must not litter the device list.

    Kills: matching devices by name rather than by the zone_name prop -- a
    user who renames "Lamplighter - Hallway" gets a new device on every
    restart, and the old one stops updating.
    """
    started(a_document(hallway()))
    device_for("Hallway").name = "Hall lights (renamed by the user)"

    second = make_plugin()
    second.startup()

    assert len(zone_devices()) == 1


def test_startup_subscribes_to_device_and_variable_changes(install):
    """Kills: dropping the subscription. Without it no callback ever fires and
    the plugin looks merely like a quiet house."""
    started(a_document())

    assert indigo.devices.subscribed == 1
    assert indigo.variables.subscribed == 1


def test_startup_publishes_the_zone_states_onto_its_device(install):
    started(a_document())

    states = device_for("Hallway").states
    for key in indigo_sync.ZONE_DEVICE_STATE_KEYS:
        assert key in states, key
    assert states["state"] in ("off_duty", "vacant", "occupied", "overridden")


def test_startup_restores_an_override_persisted_on_the_device(install):
    """The point of persisting on the device (R13, section 5.10).

    Kills: creating the zone device and publishing to it without ever reading
    it back, which loses every lock on every plugin restart -- the fork's
    behaviour, and the reason this exists.
    """
    write_config(a_document())
    make_device(101, "relay", name="Hallway Motion")
    make_device(201, "relay", name="Hallway Light")

    dev = indigo.device.create(
        protocol=indigo.kProtocol.Plugin,
        name="Lamplighter - Hallway",
        deviceTypeId=plugin_module.ZONE_TYPE_ID,
        props={"zone_name": "Hallway"},
    )
    dev.updateStatesOnServer(
        [
            {"key": "persist_version", "value": "1"},
            {"key": "persist_override_device", "value": "201"},
            {"key": "persist_override_since", "value": "2026-09-04T19:46:03"},
            {"key": "persist_override_expires", "value": "2099-01-01T00:00:00"},
        ]
    )

    the_plugin = make_plugin()
    the_plugin.startup()

    override = the_plugin.engine.zones["Hallway"].override
    assert override is not None
    assert override.device_id == 201
    assert override.expires_at == dt.datetime(2099, 1, 1, 0, 0, 0)


def test_a_lookup_that_broke_at_startup_does_not_lose_the_override(install, monkeypatch):
    """A lookup that failed is not a zone with no override (R15).

    Kills: publishing the zone's empty persisted state over a device whose
    record could not be read at startup, which turns one unanswered lookup
    into a permanently lost lock -- and it is the *successful* pass that does
    the damage, so nothing in the log would ever point at it.
    """
    write_config(a_document())
    make_device(101, "relay", name="Hallway Motion")
    make_device(201, "relay", name="Hallway Light")
    dev = indigo.device.create(
        protocol=indigo.kProtocol.Plugin,
        name="Lamplighter - Hallway",
        deviceTypeId=plugin_module.ZONE_TYPE_ID,
        props={"zone_name": "Hallway"},
    )
    dev.updateStatesOnServer(
        [
            {"key": "persist_version", "value": "1"},
            {"key": "persist_override_device", "value": "201"},
            {"key": "persist_override_since", "value": "2026-09-04T19:46:03"},
            {"key": "persist_override_expires", "value": "2099-01-01T00:00:00"},
        ]
    )

    real_get_device = plugin_module.device_lookup.get_device
    unanswered = {"still": True}

    def flaky(dev_id):
        if unanswered["still"] and dev_id == dev.id:
            raise plugin_module.device_lookup.LookupFailed(
                dev_id, RuntimeError("the server is not answering")
            )
        return real_get_device(dev_id)

    monkeypatch.setattr(plugin_module.device_lookup, "get_device", flaky)

    the_plugin = make_plugin()
    the_plugin.startup()
    assert the_plugin.engine.zones["Hallway"].override is None

    unanswered["still"] = False
    the_plugin._sync_all()

    override = the_plugin.engine.zones["Hallway"].override
    assert override is not None
    assert override.device_id == 201
    assert dev.states["persist_override_device"] == "201"


def test_startup_in_an_occupied_room_does_not_turn_the_light_off(install):
    """The jarvis defect, through the bundle.

    Kills: starting up and waiting for a device edge. The room is occupied and
    the lamp is on; a zone that has not read its presence device evaluates
    VACANT and the first reconcile pass turns the lamp off on the person
    standing under it.
    """
    write_config(a_document())
    make_device(101, "relay", name="Hallway Motion", onState=True)
    make_device(201, "relay", name="Hallway Light", onState=True)

    the_plugin = make_plugin()
    the_plugin.startup()
    the_plugin.engine.tick(NOW)

    assert the_plugin.engine.zones["Hallway"].state.value == "occupied"
    assert indigo.devices[201].onState is True, "the lamp was on and nobody left the room"


def test_a_reload_that_enables_a_zone_in_an_occupied_room_leaves_the_light_on(install):
    """Exactly what happened on jarvis: the zone arrived enabled by an edit.

    Kills: re-seeding only at startup. `enabled` deliberately comes from the
    file on every reload, so a reload is precisely when a zone can go from off
    to on knowing nothing at all about the room it has just taken over.
    """
    write_config(a_document(hallway(enabled=False)))
    make_device(101, "relay", name="Hallway Motion", onState=True)
    make_device(201, "relay", name="Hallway Light", onState=True)
    the_plugin = make_plugin()
    the_plugin.startup()
    the_plugin.engine.tick(NOW)

    write_config(a_document(hallway(enabled=True)))
    the_plugin._config_checked_at = None
    assert the_plugin._check_config_file(NOW) is True
    the_plugin.engine.tick(NOW)

    assert the_plugin.engine.zones["Hallway"].state.value == "occupied"
    assert indigo.devices[201].onState is True


def test_a_zone_device_switched_back_on_re_reads_the_room(install):
    """The same gap, reached through the device rather than the file.

    Kills: seeding on reload only. A zone that has been off for an hour knows
    nothing about what happened while it was off, and the first thing it does
    on being switched back on is decide.
    """
    write_config(a_document())
    make_device(101, "relay", name="Hallway Motion", onState=False)
    make_device(201, "relay", name="Hallway Light", onState=True)
    the_plugin = make_plugin()
    the_plugin.startup()
    dev = device_for("Hallway")
    the_plugin.actionControlDevice(a_device_action(indigo.kDeviceAction.TurnOff), dev)
    the_plugin.engine.tick(NOW)

    # Somebody walks in while the zone is switched off; no edge reaches a
    # disabled zone that would survive to the moment it is switched back on.
    indigo.devices[101].updateStateOnServer("onOffState", True)

    the_plugin.actionControlDevice(a_device_action(indigo.kDeviceAction.TurnOn), dev)
    the_plugin.engine.tick(NOW)

    assert the_plugin.engine.zones["Hallway"].state.value == "occupied"
    assert indigo.devices[201].onState is True


def test_a_live_presence_reading_beats_the_persisted_timestamp_at_startup(install):
    """Restore first, seed second, and the order is the whole point.

    Kills: seeding before restoring, which lets a persisted timestamp from
    before the restart overwrite the fact that the sensor is reporting right
    now -- and an hour-old `last_seen` is an expired hold, so the zone's first
    act is to turn the lights off on an occupied room.
    """
    write_config(a_document())
    make_device(101, "relay", name="Hallway Motion", onState=True)
    make_device(201, "relay", name="Hallway Light", onState=True)
    dev = indigo.device.create(
        protocol=indigo.kProtocol.Plugin,
        name="Lamplighter - Hallway",
        deviceTypeId=plugin_module.ZONE_TYPE_ID,
        props={"zone_name": "Hallway"},
    )
    stale = "2020-01-01T00:00:00"
    dev.updateStatesOnServer(
        [
            {"key": "persist_version", "value": "1"},
            {"key": "persist_presence_last_seen", "value": stale},
        ]
    )

    the_plugin = make_plugin()
    the_plugin.startup()

    last_seen = the_plugin.engine.zones["Hallway"].presence.last_seen
    assert last_seen > dt.datetime(2020, 1, 2), f"the stale record won: {last_seen}"
    assert (dt.datetime.now() - last_seen).total_seconds() < 60


def test_a_controller_left_off_keeps_every_zone_off(install, caplog):
    """The global enable lives on the device, because nothing else carries it.

    Kills: defaulting plugin_enabled to True at startup, which turns the whole
    house's automation back on after a restart the user did not ask for.
    """
    started(a_document())
    controller_device().updateStateOnServer("onOffState", False)

    with caplog.at_level(logging.WARNING):
        second = make_plugin()
        second.startup()

    assert second.engine.plugin_enabled is False
    assert not second.engine.zones["Hallway"].running
    assert any("controller device is off" in r.message for r in caplog.records)


# ------------------------------------------------------------ the callbacks


def test_a_presence_device_change_reaches_the_engine(install):
    """Kills: never wiring deviceUpdated to the engine at all, which is a
    plugin that runs its timers and ignores the house.

    The zone is ticked clean first, and that is the whole test: startup marks
    every zone dirty, so asserting on a dirty zone without draining it proves
    only that startup ran.
    """
    the_plugin = started(a_document())
    the_plugin.engine.tick(NOW)
    assert the_plugin.engine.dirty == {}

    before = make_snapshot(101, device_cls="relay", name="Hallway Motion", onState=False)
    after = make_device(101, "relay", name="Hallway Motion", onState=True)
    the_plugin.deviceUpdated(before, after)

    assert "presence" in the_plugin.engine.dirty.get("Hallway", "")


def test_our_own_devices_never_reach_the_engine(install):
    """A plugin that feeds its own state updates back into itself is a loop.

    Asserted by making it fatal: the engine raises if it is asked about any
    device at all, so a passing test is proof the call was not made rather
    than proof the result was empty. The exception is a BaseException on
    purpose -- deviceUpdated has a catch-all around the engine call, and an
    ordinary Exception would come back as a log line and a passing test.
    """
    the_plugin = started(a_document())

    class NotAllowed(BaseException):
        pass

    class Fatal:
        zones = {}

        def device_updated(self, *args):
            raise NotAllowed("the engine was asked about one of our own devices")

    the_plugin.engine = Fatal()
    zone_dev = device_for("Hallway")
    the_plugin.deviceUpdated(zone_dev, zone_dev)
    the_plugin.deviceUpdated(controller_device(), controller_device())


def test_an_event_the_engine_cannot_classify_does_not_take_the_callback_down(install, caplog):
    """Kills: letting the exception out. It runs on Indigo's callback thread,
    where an uncaught error is reported against Indigo's own event and this
    plugin is never named."""
    the_plugin = started(a_document())

    class Angry:
        zones = {}

        def device_updated(self, *args):
            raise RuntimeError("the classifier broke")

    the_plugin.engine = Angry()
    with caplog.at_level(logging.ERROR):
        the_plugin.deviceUpdated(None, make_device(101, "relay", onState=True))

    assert any("classifying an update" in r.message for r in caplog.records)


def test_a_variable_change_that_flips_the_verdict_reaches_the_engine(install):
    """A threshold variable is an input too, and only a flip is an edge (R9).

    Kills: never wiring variableUpdated, which leaves a zone gated on a
    variable following whatever the threshold was when the plugin started.
    """
    make_device(302, "sensor", name="Hall Lux", sensorValue=1800)
    indigo.variables[55] = indigo.Variable(55, name="dark_below", value="2200")
    the_plugin = started(
        a_document(hallway(lux={"device": 302, "dark_below": 2200, "dark_below_variable_id": 55}))
    )
    the_plugin.engine.zones["Hallway"].ingest_lux(1800, NOW)
    the_plugin.engine.tick(NOW)
    assert the_plugin.engine.zones["Hallway"].lux.verdict is True
    assert the_plugin.engine.dirty == {}

    indigo.variables[55].value = "1000"
    the_plugin.variableUpdated(None, indigo.variables[55])

    assert "variable" in the_plugin.engine.dirty.get("Hallway", "")
    assert the_plugin.engine.zones["Hallway"].lux.verdict is False


# ------------------------------------------------------------ the on/off pair


def test_turning_a_zone_device_off_disables_that_zone_only(install):
    the_plugin = started(a_document(hallway("Hallway"), hallway("Study", presence_devices=[102])))

    dev = device_for("Hallway")
    the_plugin.actionControlDevice(a_device_action(indigo.kDeviceAction.TurnOff), dev)

    assert the_plugin.engine.zones["Hallway"].enabled is False
    assert the_plugin.engine.zones["Study"].enabled is True
    assert dev.onState is False


def test_turning_the_controller_off_stops_every_zone_writing(install):
    """Kills: wiring the controller to one zone, or to nothing at all. "All
    automation off" is the one control that has to be right."""
    the_plugin = started(a_document(hallway("Hallway"), hallway("Study", presence_devices=[102])))

    the_plugin.actionControlDevice(
        a_device_action(indigo.kDeviceAction.TurnOff), controller_device()
    )

    assert the_plugin.engine.plugin_enabled is False
    assert not any(zone.running for zone in the_plugin.engine.zones.values())


def test_a_zone_device_toggle_flips_the_enable(install):
    the_plugin = started(a_document())
    dev = device_for("Hallway")

    the_plugin.actionControlDevice(a_device_action(indigo.kDeviceAction.Toggle), dev)

    assert the_plugin.engine.zones["Hallway"].enabled is False


# ---------------------------------------------------------------- the reload


def test_a_configuration_edit_reloads_and_keeps_an_override(install):
    """R13: an unrelated edit at 19:50 must not throw away a lock from 19:46.

    Kills: rebuilding the zones from the new file and calling it done, which
    is the fork's "all locks and zone state has been reset" on every save.
    """
    the_plugin = started(a_document())
    the_plugin.engine.lock_zone("Hallway", NOW)
    assert the_plugin.engine.zones["Hallway"].override is not None

    write_config(a_document(hallway(hold_seconds=900)))
    the_plugin._config_checked_at = None

    assert the_plugin._check_config_file(NOW) is True
    assert the_plugin.engine.config.zones[0].hold_seconds == 900
    assert the_plugin.engine.zones["Hallway"].override is not None


def test_an_invalid_configuration_edit_is_logged_and_the_old_one_keeps_running(install, caplog):
    """Kills: swapping in a half-loaded configuration, or dropping every zone
    because the file stopped parsing. The house keeps running yesterday's."""
    the_plugin = started(a_document())
    the_plugin.engine.lock_zone("Hallway", NOW)

    write_text_config('{"version": 1, "zones": [{"name": "Hallway"}]}')
    the_plugin._config_checked_at = None

    with caplog.at_level(logging.ERROR):
        assert the_plugin._check_config_file(NOW) is False

    assert "Hallway" in the_plugin.engine.zones
    assert the_plugin.engine.zones["Hallway"].override is not None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "still running" in errors[0].message


def test_a_configuration_that_will_not_load_is_reported_once_per_edit(install, caplog):
    """Kills: retrying the load every five seconds, which fills the log with
    the same error until somebody fixes the file."""
    the_plugin = started(a_document())
    write_text_config("{not json at all")
    the_plugin._config_checked_at = None

    with caplog.at_level(logging.ERROR):
        the_plugin._check_config_file(NOW)
        the_plugin._check_config_file(NOW + dt.timedelta(seconds=60))

    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


def test_a_broken_configuration_is_visible_on_the_controller_device(install):
    """The ERROR scrolls away; the device state does not (R15)."""
    the_plugin = started(a_document())
    write_text_config("{not json at all")
    the_plugin._config_checked_at = None
    the_plugin._check_config_file(NOW)
    the_plugin._sync_controller()

    assert "not valid JSON" in controller_device().states["config_status"]


def test_the_file_is_not_stared_at_more_often_than_the_interval(install):
    """Kills: statting the file on every worker pass, which at a pass a second
    is a syscall a second for ever."""
    the_plugin = started(a_document())
    the_plugin._config_checked_at = NOW
    write_config(a_document(hallway(hold_seconds=900)))

    assert the_plugin._check_config_file(NOW + dt.timedelta(seconds=1)) is False
    assert the_plugin.engine.config.zones[0].hold_seconds != 900


def test_a_zone_that_leaves_the_configuration_keeps_its_device_and_says_why(install, caplog):
    """Never delete a user's device silently (section 5.10).

    Kills: deleting the device, and the quieter failure of leaving it in place
    with stale states and no explanation of why it stopped moving.
    """
    the_plugin = started(a_document(hallway("Hallway"), hallway("Study", presence_devices=[102])))
    study = device_for("Study")

    write_config(a_document(hallway("Hallway")))
    the_plugin._config_checked_at = None
    with caplog.at_level(logging.WARNING):
        the_plugin._check_config_file(NOW)
        warnings_after_one_reload = [
            r for r in caplog.records if "is for zone" in r.message
        ]
        the_plugin._sync_all()

    assert study.id in [dev.id for dev in zone_devices()]
    assert "not in" in study.states["explain"]
    assert "Study" in study.states["explain"]
    assert len(warnings_after_one_reload) == 1
    # ...and the second pass does not say it again (section 10: once per
    # condition, not once per pass).
    assert len([r for r in caplog.records if "is for zone" in r.message]) == 1


def test_a_zone_disabled_in_the_configuration_turns_its_device_off(install):
    """The file is the source of truth for `enabled` across a reload, so the
    device follows it (the rule persist.rebuild_zone documents).

    Kills: publishing the states and never the on/off, which leaves a device
    reading "on" for a zone the configuration switched off -- the first thing
    a person checks before deciding the plugin is broken.
    """
    the_plugin = started(a_document())
    assert device_for("Hallway").onState is True

    write_config(a_document(hallway(enabled=False)))
    the_plugin._config_checked_at = None
    the_plugin._check_config_file(NOW)

    assert the_plugin.engine.zones["Hallway"].enabled is False
    assert device_for("Hallway").onState is False


def test_a_zone_device_deleted_under_the_plugin_does_not_stop_the_others(install, caplog):
    """One missing device is one zone's states, not the pass.

    Kills: letting the KeyError out of the lookup, which would stop every
    zone after the deleted one publishing anything at all.
    """
    the_plugin = started(a_document(hallway("Hallway"), hallway("Study", presence_devices=[102])))
    del indigo.devices[device_for("Hallway").id]

    with caplog.at_level(logging.WARNING):
        the_plugin._sync_all()

    assert any("no longer exists" in r.message for r in caplog.records)
    assert device_for("Study").states["explain"]


# ---------------------------------------------------------------- the worker


def test_one_worker_pass_publishes_states_and_the_controller_counters(install):
    the_plugin = started(a_document())
    the_plugin.stop_after_sleeps = 1

    the_plugin.runConcurrentThread()

    assert controller_device().states["zones"] == 1
    assert controller_device().states["zones_enabled"] == 1
    assert device_for("Hallway").states["explain"]


def test_the_worker_sleep_is_bounded_at_both_ends(install):
    """Kills: sleeping until the engine's next wake-up (which can be hours,
    and leaves a dirty zone waiting), and sleeping zero (which spins)."""
    the_plugin = started(a_document())
    the_plugin.stop_after_sleeps = 1

    the_plugin.runConcurrentThread()

    assert the_plugin.slept
    for delay in the_plugin.slept:
        assert plugin_module.MIN_LOOP_SECONDS <= delay <= plugin_module.MAX_LOOP_SECONDS


def test_a_worker_pass_that_raises_does_not_end_the_worker(install, caplog):
    """Kills: letting one bad pass kill runConcurrentThread, which stops every
    zone in the house until somebody restarts the plugin."""
    the_plugin = started(a_document())
    the_plugin.stop_after_sleeps = 2

    class Angry:
        zones = {}
        plugin_enabled = True

        def tick(self, now):
            raise RuntimeError("the pass broke")

        def next_wake(self, now):
            return None

    the_plugin.engine = Angry()
    with caplog.at_level(logging.ERROR):
        the_plugin.runConcurrentThread()

    assert len(the_plugin.slept) == 2


# ------------------------------------------------------------- the log level


def test_the_file_handler_follows_the_configured_level_from_startup(install):
    """Auto Lights pinned this to DEBUG and wrote 2.3 GB a day, which put a
    97-second backlog in front of every zone. Both handlers, from __init__."""
    the_plugin = make_plugin(log_level=logging.WARNING)

    assert the_plugin.plugin_file_handler.level == logging.WARNING
    assert the_plugin.indigo_log_handler.level == logging.WARNING


def test_saving_the_preferences_moves_both_handlers(install):
    the_plugin = make_plugin(log_level=logging.WARNING)

    the_plugin.closedPrefsConfigUi({"log_level": logging.DEBUG}, False)

    assert the_plugin.plugin_file_handler.level == logging.DEBUG
    assert the_plugin.indigo_log_handler.level == logging.DEBUG


def test_cancelling_the_preferences_changes_nothing(install):
    the_plugin = make_plugin(log_level=logging.WARNING)

    the_plugin.closedPrefsConfigUi({"log_level": logging.DEBUG}, True)

    assert the_plugin.plugin_file_handler.level == logging.WARNING


# ------------------------------------------------------- the actions and menu


def test_every_callback_the_xml_names_exists_on_the_plugin():
    """Kills: renaming a callback and leaving the XML behind, which Indigo
    reports as a bare AttributeError when a user runs the action."""
    named = []
    for filename in ("Actions.xml", "MenuItems.xml"):
        root = ET.parse(os.path.join(SERVER_PLUGIN, filename)).getroot()
        named.extend(element.text for element in root.iter("CallbackMethod"))

    assert named
    for callback in named:
        assert callable(getattr(plugin_module.Plugin, callback, None)), callback


def test_the_zone_picker_offers_every_zone_and_all_only_where_it_should(install):
    the_plugin = started(a_document(hallway("Hallway"), hallway("Study", presence_devices=[102])))

    assert the_plugin.get_zone_list("") == [("Hallway", "Hallway"), ("Study", "Study")]
    assert the_plugin.get_zone_list("all")[0] == (plugin_module.ALL_ZONES, "All zones")


def test_reset_override_with_no_zone_releases_every_lock(install):
    the_plugin = started(a_document(hallway("Hallway"), hallway("Study", presence_devices=[102])))
    the_plugin.engine.lock_zone("Hallway", NOW)
    the_plugin.engine.lock_zone("Study", NOW)

    the_plugin.reset_override(an_action(zone_name=plugin_module.ALL_ZONES))

    assert all(zone.override is None for zone in the_plugin.engine.zones.values())


def test_lock_zone_takes_a_lock_with_no_device_behind_it(install):
    """Section 5.13, decision 2: what scripts wanted from the fork."""
    the_plugin = started(a_document())

    the_plugin.lock_zone(an_action(zone_name="Hallway"))

    assert the_plugin.engine.zones["Hallway"].override is not None


def test_set_zone_enabled_reads_the_menu_value(install):
    the_plugin = started(a_document())

    the_plugin.set_zone_enabled(an_action(zone_name="Hallway", enabled="off"))
    assert the_plugin.engine.zones["Hallway"].enabled is False

    the_plugin.set_zone_enabled(an_action(zone_name="Hallway", enabled="on"))
    assert the_plugin.engine.zones["Hallway"].enabled is True


def test_an_action_naming_a_zone_that_is_gone_says_so_and_does_nothing(install, caplog):
    """Kills: a KeyError out of the action callback, which Indigo shows as a
    traceback with no hint that the zone name is stale."""
    the_plugin = started(a_document())

    with caplog.at_level(logging.WARNING):
        the_plugin.lock_zone(an_action(zone_name="Kitchen"))

    assert the_plugin.engine.zones["Hallway"].override is None
    assert any("Kitchen" in r.message for r in caplog.records)


def test_reconcile_now_marks_every_zone_for_the_next_pass(install):
    the_plugin = started(a_document(hallway("Hallway"), hallway("Study", presence_devices=[102])))
    the_plugin.engine.tick(NOW)

    the_plugin.reconcile_now(an_action())

    assert set(the_plugin.engine.dirty) == {"Hallway", "Study"}


def test_the_menu_items_run_against_a_plugin_with_no_zones(install, caplog):
    """Kills: iterating zones that are not there. A menu item that raises on a
    fresh install is the first thing a new user sees."""
    the_plugin = started()

    with caplog.at_level(logging.INFO):
        the_plugin.print_zone_states()
        the_plugin.explain_all_zones()

    assert len([r for r in caplog.records if "no zones are configured" in r.message]) == 2


def test_reload_configuration_now_picks_up_a_file_that_looks_unchanged(install):
    """The menu item exists precisely because mtime is not always enough."""
    the_plugin = started(a_document())
    write_config(a_document(hallway(hold_seconds=900)))
    the_plugin._config_checked_at = NOW
    the_plugin._config_mtime = the_plugin._mtime()

    the_plugin.reload_config_now()

    assert the_plugin.engine.config.zones[0].hold_seconds == 900


# ------------------------------------------------------------ the state list


def test_device_start_comm_refreshes_the_state_list(install):
    """New states on an existing device type are invisible until this is
    called, and nothing in the log says so."""
    the_plugin = started(a_document())
    dev = device_for("Hallway")
    before = dev.state_list_refreshed

    the_plugin.deviceStartComm(dev)

    assert dev.state_list_refreshed == before + 1
