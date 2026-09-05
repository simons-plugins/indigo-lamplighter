"""The Indigo plugin around the engine (PRD 5.1, 5.10, 5.11, 5.13).

Deliberately thin, and thin in a specific way: **nothing here decides
anything about a light.** Indigo's callbacks hand events to
:class:`lamplighter.engine.Engine`, the worker loop asks it to tick, and the
mapping between what a zone knows and what an Indigo device carries lives in
:mod:`lamplighter.indigo_sync`, where it can be tested without a server. What
is left in this file is the plumbing that cannot be tested any other way:
the bundle's callbacks, the device bookkeeping and the file watcher.

Three things here are worth reading before changing them.

**The file handler follows the configured level from ``__init__``.** Auto
Lights pinned its file handler to DEBUG at startup and wrote 2.3 GB a day
into it, which put a 97-second backlog in front of every zone it ran. The
level is set in ``__init__`` *and* in ``closedPrefsConfigUi``, and neither
call is optional.

**A zone device is never deleted.** A zone that leaves the configuration
leaves its device in place, marked in ``explain`` and named once at WARNING.
The device may be carrying an override the person who owns the house wants
back tomorrow, and a plugin that silently deletes a user's devices is a
plugin nobody can trust with the rest of the house.

**A configuration that does not load does not stop the one that does.** The
watcher records the file's mtime whether the load succeeded or not, so a
broken save is reported once per edit rather than every five seconds, and
the previous configuration keeps running until a good one replaces it.
"""

import datetime as dt
import json
import logging
import os

try:
    import indigo
except ImportError:  # pragma: no cover - only true outside the Indigo host
    pass

from lamplighter import devices as device_lookup
from lamplighter import indigo_sync, persist
from lamplighter.config import Config, ConfigError, load_config
from lamplighter.engine import Engine
from lamplighter.periods import IndigoSun
from lamplighter.reconcile import IndigoCommander

PLUGIN_ID = "com.simons-plugins.indigo-lamplighter"
ZONE_TYPE_ID = "lamplighter_zone"
CONTROLLER_TYPE_ID = "lamplighter_controller"

CONFIG_FILENAME = "lamplighter.json"

#: What a fresh install gets written for it. It is deliberately a document the
#: loader *refuses* -- ``zones`` may not be empty in a configured file -- so
#: the "no zones yet" case is recognised here by shape rather than by reading
#: an error message, and says something a new user can act on.
DEFAULT_DOCUMENT = {"version": 1, "zones": []}

#: A configuration with no zones. The engine runs perfectly well with none:
#: the worker finds nothing to do and the plugin stays up so that the first
#: save of a real file is picked up without a restart.
EMPTY_CONFIG = Config(version=1, zones=())

#: How often the worker stats the configuration file.
CONFIG_CHECK_SECONDS = 5.0

#: The longest the worker loop sleeps. The engine's own timers can be much
#: further out, but a dirty zone marked on the callback thread is only picked
#: up when the worker next wakes, so the loop caps its sleep at a second and
#: the cost of that is one no-op pass per second per plugin.
MAX_LOOP_SECONDS = 1.0

#: The shortest. Without a floor, a zone that re-dirties itself would spin the
#: worker at whatever rate the CPU allows.
MIN_LOOP_SECONDS = 0.1

#: The value the zone pickers use for "every zone" (Actions.xml).
ALL_ZONES = "__all__"


def config_dir() -> str:
    """The plugin's own Preferences folder (PRD 5.11)."""
    return os.path.join(
        indigo.server.getInstallFolderPath(), "Preferences", "Plugins", PLUGIN_ID
    )


def config_path() -> str:
    """Where ``lamplighter.json`` lives. One fixed place, never configurable."""
    return os.path.join(config_dir(), CONFIG_FILENAME)


class Plugin(indigo.PluginBase):
    """Indigo's half of Lamplighter: callbacks in, device states out."""

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs)

        self.log_level = int(plugin_prefs.get("log_level", logging.INFO))
        self.indigo_log_handler.setLevel(self.log_level)
        # From startup, not from the first time somebody opens the prefs.
        self.plugin_file_handler.setLevel(self.log_level)

        self.engine = None
        self.sun = None
        self.config_file = None
        self._config_mtime = None
        self._config_checked_at = None
        self._config_status = "not loaded yet"
        #: Zone name -> Indigo device id. Rebuilt from the server rather than
        #: remembered across restarts, because the user may have deleted one.
        self._zone_device_ids = {}
        self._controller_id = None
        #: Device ids already named at WARNING for having no zone (section 10:
        #: one WARNING per condition, not one per pass).
        self._warned_orphans = set()
        #: Zones whose device could not be read at startup, so their persisted
        #: state was never applied. Publishing over one of these would turn a
        #: transient lookup failure into a permanently lost override, so the
        #: next successful pass restores before it publishes.
        self._unrestored = set()

    # ------------------------------------------------------------- lifecycle

    def startup(self):
        # No super().startup() -- the base class does not define one.
        self.sun = IndigoSun(logger=self.logger)
        self.config_file = config_path()
        self._ensure_config_file()

        # Stat before reading, never after: a save that lands in between must
        # look like an edit at the next check, and recording the newer mtime
        # against the older content would lose it until somebody saved again.
        self._config_mtime = self._mtime()
        self._config_checked_at = dt.datetime.now()

        config, complaint = self._read_config()
        if config is None:
            self.logger.error(
                f"Lamplighter: {complaint}. No zones are running; fix the file and it "
                f"will be picked up within {CONFIG_CHECK_SECONDS:g} seconds."
            )
            self._config_status = complaint
            config = EMPTY_CONFIG
        else:
            self._config_status = complaint or "ok"
            if complaint:
                self.logger.info(f"Lamplighter: {complaint} ({self.config_file}).")

        self.engine = Engine(
            config,
            self.sun,
            IndigoCommander(logger=self.logger),
            logger=self.logger,
            on_zone_changed=self._zone_changed,
        )

        self._refresh_device_map()
        self._create_missing_devices()
        self._restore_persisted()
        self._apply_controller_enable()
        # After restore, never before: the persisted record is what the zone
        # knew when it stopped, and seeding is what the room is doing now. A
        # presence device that is on right now refreshes the restored
        # timestamp; one that is off leaves it alone.
        self.engine.seed_inputs(dt.datetime.now())

        indigo.devices.subscribeToChanges()
        indigo.variables.subscribeToChanges()

        self.engine.mark_all_dirty("plugin startup")
        self._sync_all()
        self.logger.info(
            f"Lamplighter: started with {len(self.engine.zones)} zone(s) from "
            f"{self.config_file}."
        )

    def shutdown(self):
        # No super().shutdown() -- the base class does not define one.
        if self.engine is not None:
            # Last chance to put the override and the presence timestamp onto
            # the devices: they are what a restart restores from.
            self._sync_all()
        self.logger.debug("Lamplighter: shutdown")

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if user_cancelled:
            return
        self.log_level = int(values_dict.get("log_level", logging.INFO))
        self.indigo_log_handler.setLevel(self.log_level)
        self.plugin_file_handler.setLevel(self.log_level)
        self.logger.debug(f"Lamplighter: log level is now {self.log_level}")

    # ------------------------------------------------------- Indigo callbacks

    def deviceUpdated(self, orig_dev, new_dev):
        # The base implementation drives the comm-property-change machinery.
        indigo.PluginBase.deviceUpdated(self, orig_dev, new_dev)
        if self.engine is None:
            return
        if getattr(new_dev, "pluginId", "") == PLUGIN_ID:
            # Our own zone and controller devices. Feeding our own state
            # updates back into the engine is how a plugin makes itself a
            # loop, and none of them is an input to any zone.
            return
        try:
            self.engine.device_updated(orig_dev, new_dev, dt.datetime.now())
        except Exception:
            # On the Indigo callback thread. An exception here would be
            # reported against Indigo's own event, so it is caught, named and
            # left behind rather than allowed to take the thread down.
            self.logger.exception(
                f"Lamplighter: classifying an update from device "
                f"{getattr(new_dev, 'id', '?')} raised; that event was lost, and "
                "the zones it belongs to will re-plan at their next wake-up"
            )

    def variableUpdated(self, orig_var, new_var):
        indigo.PluginBase.variableUpdated(self, orig_var, new_var)
        if self.engine is None:
            return
        try:
            self.engine.variable_updated(new_var.id, dt.datetime.now())
        except Exception:
            self.logger.exception(
                f"Lamplighter: reading variable {getattr(new_var, 'id', '?')} raised; "
                "any zone gated on it keeps its previous dark verdict"
            )

    def deviceStartComm(self, dev):
        # New states added to a device type after the devices exist are not
        # visible until this is called. Without it, an upgrade leaves every
        # zone device stuck on the states it had at creation.
        dev.stateListOrDisplayStateIdChanged()
        if self.engine is None:
            return
        zone_name = self._zone_name_of(dev)
        if dev.deviceTypeId == ZONE_TYPE_ID and zone_name in self.engine.zones:
            self._zone_device_ids[zone_name] = dev.id
            self._warned_orphans.discard(dev.id)
            self._sync_zone(self.engine.zones[zone_name])
        elif dev.deviceTypeId == CONTROLLER_TYPE_ID:
            self._controller_id = dev.id
            self._sync_controller()

    def runConcurrentThread(self):
        try:
            while True:
                if self.engine is None:
                    # startup() raised. Indigo will report that; this loop
                    # just declines to raise once a second on top of it.
                    self.sleep(MAX_LOOP_SECONDS)
                    continue
                now = dt.datetime.now()
                try:
                    self.engine.tick(now)
                except Exception:
                    # One bad pass must not end the worker: the next tick is a
                    # second chance and the traceback says what broke.
                    self.logger.exception("Lamplighter: the worker pass raised")
                self._sync_controller()
                self._check_config_file(dt.datetime.now())
                self.sleep(self._loop_delay(dt.datetime.now()))
        except self.StopThread:
            pass

    def _loop_delay(self, now) -> float:
        """How long to sleep: the engine's next wake, capped and floored."""
        try:
            wake = self.engine.next_wake(now)
        except Exception:
            self.logger.exception("Lamplighter: asking for the next wake-up raised")
            return MAX_LOOP_SECONDS
        if wake is None:
            return MAX_LOOP_SECONDS
        delay = (wake - now).total_seconds()
        return min(max(delay, MIN_LOOP_SECONDS), MAX_LOOP_SECONDS)

    def actionControlDevice(self, action, dev):
        """A zone device's on/off is its enable; the controller's is global."""
        if self.engine is None:
            return
        if dev.deviceTypeId not in (ZONE_TYPE_ID, CONTROLLER_TYPE_ID):
            return

        device_action = action.deviceAction
        if device_action == indigo.kDeviceAction.RequestStatus:
            self._sync_all()
            return
        if device_action == indigo.kDeviceAction.Toggle:
            wanted = not dev.onState
        elif device_action in (indigo.kDeviceAction.TurnOn, indigo.kDeviceAction.TurnOff):
            wanted = device_action == indigo.kDeviceAction.TurnOn
        else:
            self.logger.warning(
                f"Lamplighter: {dev.name} does not know what to do with device action "
                f"{device_action!r}; nothing was changed"
            )
            return

        if dev.deviceTypeId == CONTROLLER_TYPE_ID:
            self.engine.set_plugin_enabled(wanted)
            dev.updateStateOnServer("onOffState", wanted)
            self.logger.info(
                f"Lamplighter: the plugin is {'enabled' if wanted else 'disabled'}; "
                f"{len(self.engine.zones)} zone(s) "
                f"{'resume' if wanted else 'stop'} writing to lights"
            )
            self._sync_all()
            return

        zone_name = self._zone_name_of(dev)
        if zone_name not in self.engine.zones:
            self.logger.warning(
                f"Lamplighter: {dev.name} is for zone {zone_name!r}, which is not in "
                f"{self.config_file}; its on/off changes nothing"
            )
            dev.updateStateOnServer("onOffState", wanted)
            return
        self.engine.set_zone_enabled(zone_name, wanted)
        dev.updateStateOnServer("onOffState", wanted)
        self.logger.info(
            f"Lamplighter: zone {zone_name!r} is {'enabled' if wanted else 'disabled'}"
        )

    # ------------------------------------------------------------ the actions

    def get_zone_list(self, filter="", values_dict=None, type_id="", target_id=0):
        """The zone picker's options. ``filter="all"`` prepends "All zones"."""
        zones = [] if self.engine is None else sorted(self.engine.zones)
        options = [(name, name) for name in zones]
        if filter == "all":
            return [(ALL_ZONES, "All zones")] + options
        return options

    def reset_override(self, action, dev=None, caller_waiting_for_result=None):
        zone_name = self._action_zone(action, allow_all=True)
        if zone_name is False:
            return
        released = self.engine.reset_override(zone_name, dt.datetime.now())
        if released:
            self.logger.info(f"Lamplighter: released the override on {', '.join(released)}")
        else:
            self.logger.info(
                "Lamplighter: no override to release on "
                + ("any zone" if zone_name is None else f"zone {zone_name!r}")
            )

    def lock_zone(self, action, dev=None, caller_waiting_for_result=None):
        zone_name = self._action_zone(action)
        if not zone_name:
            return
        created = self.engine.lock_zone(zone_name, dt.datetime.now())
        if created is None:
            self.logger.warning(
                f"Lamplighter: zone {zone_name!r} does not take overrides "
                "(override.enabled is false in its configuration); it was not locked"
            )
            return
        self.logger.info(
            f"Lamplighter: locked zone {zone_name!r} until "
            f"{created.expires_at:%H:%M:%S}"
        )

    def set_zone_enabled(self, action, dev=None, caller_waiting_for_result=None):
        zone_name = self._action_zone(action)
        if not zone_name:
            return
        wanted = str(action.props.get("enabled", "on")).lower() in ("on", "true", "1", "yes")
        self.engine.set_zone_enabled(zone_name, wanted)
        self.logger.info(
            f"Lamplighter: zone {zone_name!r} is {'enabled' if wanted else 'disabled'}"
        )

    def reconcile_now(self, action=None, dev=None, caller_waiting_for_result=None):
        if self.engine is None:
            return
        self.engine.mark_all_dirty("reconcile now action")
        self.logger.info(
            f"Lamplighter: {len(self.engine.zones)} zone(s) will re-plan and reconcile "
            "on the next worker pass"
        )

    def _action_zone(self, action, allow_all=False):
        """The zone an action names. ``None`` means all; ``False`` means stop.

        Three answers rather than two because "every zone" and "the zone you
        picked has gone" both look like an empty string in ``action.props``,
        and they want opposite things done.
        """
        if self.engine is None:
            return False
        zone_name = str(action.props.get("zone_name", "") or "")
        if allow_all and zone_name in ("", ALL_ZONES):
            return None
        if zone_name not in self.engine.zones:
            self.logger.warning(
                f"Lamplighter: this action names zone {zone_name!r}, which is not in "
                f"{self.config_file}; nothing was done. Configured zones: "
                + (", ".join(sorted(self.engine.zones)) or "none")
            )
            return False
        return zone_name

    # -------------------------------------------------------------- the menus

    def print_zone_states(self):
        if self.engine is None or not self.engine.zones:
            self.logger.info("Lamplighter: no zones are configured")
            return
        for name, snapshot in sorted(self.engine.snapshot().items()):
            self.logger.info(
                f"{name}: {snapshot['state']}, presence="
                f"{'active' if snapshot['presence_active'] else 'inactive'}, "
                f"period={snapshot['period'] or 'none'}, "
                f"dark={'yes' if snapshot['dark'] else 'no'}, "
                f"lux={snapshot['lux'] if snapshot['lux'] != '' else 'unknown'}, "
                f"override={snapshot['override_device'] or 'none'}, "
                f"desired: {snapshot['desired_summary']}"
            )

    def explain_all_zones(self):
        if self.engine is None or not self.engine.zones:
            self.logger.info("Lamplighter: no zones are configured")
            return
        for name, line in sorted(self.engine.explain().items()):
            self.logger.info(line if line else f"{name}: nothing to say")

    def reload_config_now(self):
        if self.engine is None:
            return
        # Forget the mtime rather than compare it: the point of the menu item
        # is to reload a file that looks unchanged.
        self._config_mtime = None
        self._config_checked_at = None
        if not self._check_config_file(dt.datetime.now()):
            self.logger.info(
                f"Lamplighter: {self.config_file} was not reloaded; see the error above"
            )

    # ------------------------------------------------------- the config file

    def _ensure_config_file(self):
        """Write a starter file if there is none. Never overwrites one."""
        if os.path.exists(self.config_file):
            return
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as handle:
                json.dump(DEFAULT_DOCUMENT, handle, indent=2)
                handle.write("\n")
        except OSError as exc:
            self.logger.error(
                f"Lamplighter: could not write a starter configuration to "
                f"{self.config_file} ({exc}). The plugin will run with no zones."
            )
            return
        self.logger.info(
            f"Lamplighter: wrote a starter configuration to {self.config_file}. "
            "Add zones to it and save; the plugin picks the file up within "
            f"{CONFIG_CHECK_SECONDS:g} seconds."
        )

    def _read_config(self):
        """``(config, complaint)``. Three outcomes, deliberately.

        * ``(Config, None)`` -- it loaded.
        * ``(EMPTY_CONFIG, "no zones ...")`` -- a fresh install. Not an error:
          the loader refuses an empty ``zones`` because a *configured* file
          must not have one, which is a different statement.
        * ``(None, why)`` -- it did not load, and the caller decides what to
          keep running.
        """
        try:
            with open(self.config_file, encoding="utf-8") as handle:
                document = json.load(handle)
        except OSError as exc:
            return None, f"{self.config_file} could not be read ({exc})"
        except ValueError as exc:
            return None, f"{self.config_file} is not valid JSON ({exc})"

        if isinstance(document, dict) and document.get("zones") == []:
            return EMPTY_CONFIG, "no zones are configured yet"

        try:
            return load_config(document, self.sun, dt.date.today()), None
        except ConfigError as exc:
            where = exc.path or "the top level"
            return None, f"{self.config_file} is invalid at {where}: {exc.message}"

    def _mtime(self):
        try:
            return os.path.getmtime(self.config_file)
        except OSError:
            return None

    def _check_config_file(self, now) -> bool:
        """Stat the file every few seconds; reload it when it moves."""
        if self._config_checked_at is not None:
            if (now - self._config_checked_at).total_seconds() < CONFIG_CHECK_SECONDS:
                return False
        self._config_checked_at = now

        mtime = self._mtime()
        if mtime == self._config_mtime:
            return False
        # Recorded before the load is attempted, and whether or not it works:
        # a file that will not parse must be complained about once per edit,
        # not once every five seconds until somebody fixes it.
        self._config_mtime = mtime

        config, complaint = self._read_config()
        if config is None:
            self._config_status = complaint
            self.logger.error(
                f"Lamplighter: {complaint}. The previous configuration is still "
                "running; nothing about any zone has changed."
            )
            return False

        self._config_status = complaint or "ok"
        if complaint:
            self.logger.info(f"Lamplighter: {complaint} ({self.config_file}).")

        self.engine.reload(config, now)
        # Before anything is published or reconciled. A zone the edit has just
        # switched on has never looked at its room, and an unseeded zone reads
        # as an empty one.
        self.engine.seed_inputs(now)
        self._refresh_device_map()
        self._create_missing_devices()
        self._sync_all()
        self.logger.info(
            f"Lamplighter: reloaded {self.config_file}; {len(self.engine.zones)} zone(s). "
            "Overrides and presence carried across the reload."
        )
        return True

    # ---------------------------------------------------------- the devices

    def _zone_name_of(self, dev) -> str:
        props = getattr(dev, "pluginProps", None) or {}
        try:
            return str(props.get("zone_name", "") or "")
        except Exception:
            return ""

    def _refresh_device_map(self):
        """Rebuild "which device is which zone" from the server."""
        self._zone_device_ids = {}
        self._controller_id = None
        for dev in indigo.devices:
            if getattr(dev, "pluginId", "") != PLUGIN_ID:
                continue
            if dev.deviceTypeId == ZONE_TYPE_ID:
                zone_name = self._zone_name_of(dev)
                if not zone_name:
                    continue
                existing = self._zone_device_ids.get(zone_name)
                if existing is not None and existing != dev.id:
                    self.logger.warning(
                        f"Lamplighter: devices {existing} and {dev.id} both claim zone "
                        f"{zone_name!r}; {dev.id} is the one being updated. Delete or "
                        "rename one of them."
                    )
                self._zone_device_ids[zone_name] = dev.id
                self._warned_orphans.discard(dev.id)
            elif dev.deviceTypeId == CONTROLLER_TYPE_ID and self._controller_id is None:
                self._controller_id = dev.id

    def _create_missing_devices(self):
        """One zone device per configured zone, and one controller."""
        for zone_name, zone in self.engine.zones.items():
            if zone_name in self._zone_device_ids:
                continue
            dev = self._create_device(
                f"Lamplighter - {zone_name}", ZONE_TYPE_ID, {"zone_name": zone_name}
            )
            if dev is None:
                continue
            self._zone_device_ids[zone_name] = dev.id
            dev.updateStateOnServer("onOffState", bool(zone.enabled))
            self.logger.info(
                f"Lamplighter: created device {dev.name!r} (id {dev.id}) for zone "
                f"{zone_name!r}"
            )

        if self._controller_id is None:
            dev = self._create_device("Lamplighter Controller", CONTROLLER_TYPE_ID, {})
            if dev is not None:
                self._controller_id = dev.id
                dev.updateStateOnServer("onOffState", True)
                self.logger.info(
                    f"Lamplighter: created the controller device (id {dev.id}); "
                    "turning it off stops every zone writing to a light"
                )

    def _create_device(self, name, device_type_id, props):
        try:
            return indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                name=name,
                deviceTypeId=device_type_id,
                props=props,
            )
        except Exception as exc:
            self.logger.error(
                f"Lamplighter: could not create the Indigo device {name!r} ({exc}). "
                "The zone still runs; it just has nowhere to publish its state. A "
                "device of that name may already exist."
            )
            return None

    def _device(self, dev_id, what):
        """``(device, gone)`` -- the two lookup failures kept apart (R15).

        ``gone`` is True only for ``DeviceGone``: the lookup answered, and the
        answer was "no such device", so the caller may forget the id. A
        ``LookupFailed`` teaches nothing about whether the device exists, so
        the id is kept and the pass is skipped -- collapsing the two would let
        one unanswered lookup drop a device that is still there.
        """
        if dev_id is None:
            return None, False
        try:
            return device_lookup.get_device(dev_id), False
        except device_lookup.DeviceGone:
            self.logger.warning(
                f"Lamplighter: the Indigo device for {what} (id {dev_id}) no longer "
                "exists; it will be recreated at the next configuration reload"
            )
            return None, True
        except device_lookup.LookupFailed as exc:
            self.logger.warning(
                f"Lamplighter: looking up the Indigo device for {what} (id {dev_id}) "
                f"failed ({exc.cause}); its states are behind by one pass"
            )
            return None, False

    def _zone_device(self, zone_name):
        dev, gone = self._device(self._zone_device_ids.get(zone_name), f"zone {zone_name!r}")
        if gone:
            self._zone_device_ids.pop(zone_name, None)
        return dev

    def _controller_device(self):
        dev, gone = self._device(self._controller_id, "the controller")
        if gone:
            self._controller_id = None
        return dev

    # ------------------------------------------------------------ publishing

    def _zone_changed(self, zone):
        """The engine's ``on_zone_changed``: publish and persist, worker side."""
        self._sync_zone(zone)

    def _sync_zone(self, zone):
        dev = self._zone_device(zone.name)
        if dev is None:
            return
        if zone.name in self._unrestored:
            # This device could not be read when the plugin started, so its
            # persisted record was never applied -- and publishing now would
            # write the zone's empty state straight over a perfectly good
            # override. Restore first, then publish.
            self._restore_zone(zone, dev)
        states = indigo_sync.states_for_zone(zone.snapshot())
        states.extend(indigo_sync.persist_to_states(persist.to_persisted(zone)))
        dev.updateStatesOnServer(states)
        if bool(dev.onState) != bool(zone.enabled):
            # The file is the source of truth for `enabled` across a reload
            # (see persist.rebuild_zone), so the device follows the zone here
            # rather than the other way round. A person toggling the device
            # goes through actionControlDevice, which moves the zone first.
            dev.updateStateOnServer("onOffState", bool(zone.enabled))

    def _sync_controller(self):
        dev = self._controller_device()
        if dev is None:
            return
        dev.updateStatesOnServer(
            indigo_sync.controller_states(self.engine, self._config_status)
        )
        if bool(dev.onState) != bool(self.engine.plugin_enabled):
            dev.updateStateOnServer("onOffState", bool(self.engine.plugin_enabled))

    def _sync_all(self):
        for zone in self.engine.zones.values():
            self._sync_zone(zone)
        self._mark_orphan_devices()
        self._sync_controller()

    def _mark_orphan_devices(self):
        """A device whose zone left the configuration. Kept, and said so.

        Never deleted: it is the user's device, and it may be the only record
        that the zone existed. What it gets instead is an ``explain`` line
        that says why it stopped moving, and one WARNING naming it.
        """
        for dev in indigo.devices:
            if getattr(dev, "pluginId", "") != PLUGIN_ID:
                continue
            if dev.deviceTypeId != ZONE_TYPE_ID:
                continue
            zone_name = self._zone_name_of(dev)
            if zone_name and zone_name in self.engine.zones:
                continue
            named = zone_name or "(no zone name set)"
            if dev.id not in self._warned_orphans:
                self._warned_orphans.add(dev.id)
                self.logger.warning(
                    f"Lamplighter: device {dev.name!r} (id {dev.id}) is for zone "
                    f"{named}, which is not in {self.config_file}. It has been left "
                    "in place and nothing has been deleted, but its states are no "
                    "longer updated. Delete it yourself if the zone is gone for good."
                )
            dev.updateStateOnServer(
                "explain",
                f"zone {named} is not in {CONFIG_FILENAME}; this device is no longer "
                "driven by anything. Its other states are whatever they were when the "
                "zone was last configured.",
            )

    def _restore_persisted(self):
        """Put the persisted record on each zone device back onto its zone."""
        for zone_name, zone in self.engine.zones.items():
            dev = self._zone_device(zone_name)
            if dev is None:
                # The device could not be read, which is not the same as a
                # zone with nothing to restore. Remembered, so that the first
                # successful publish restores before it overwrites.
                self._unrestored.add(zone_name)
                self.logger.warning(
                    f"Lamplighter: zone {zone_name!r} could not be restored from its "
                    "Indigo device; any override it was holding is not lost, it is "
                    "simply not applied yet, and the next successful pass will try again"
                )
                continue
            self._restore_zone(zone, dev)

    def _restore_zone(self, zone, dev):
        self._unrestored.discard(zone.name)
        record = indigo_sync.states_to_persisted(getattr(dev, "states", {}) or {})
        if not record:
            return
        complaints = self.engine.restore({zone.name: record}, dt.datetime.now())
        for message in complaints.get(zone.name, []):
            self.logger.warning(
                f"Lamplighter: restoring {zone.name!r} from its device -- {message}"
            )

    def _apply_controller_enable(self):
        """The controller device's on/off is the global enable, and it is the
        only piece of state with nowhere else to live: the configuration file
        does not carry it, so the device is where it survives a restart."""
        dev = self._controller_device()
        if dev is None:
            return
        self.engine.set_plugin_enabled(bool(dev.onState))
        if not dev.onState:
            self.logger.warning(
                "Lamplighter: the controller device is off, so no zone will write to a "
                "light. Turn it on to resume."
            )
