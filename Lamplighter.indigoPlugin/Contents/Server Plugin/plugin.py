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
import re

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


def is_starter_document(document) -> bool:
    """Is this the shape a fresh install is given (and no zones at all)?

    Recognised here, by shape, rather than by reading the loader's error
    message: ``load_config`` refuses an empty ``zones`` because a
    *configured* file must not have one, which is a different statement from
    "this install has not been configured yet". Both ``_read_config`` and the
    ``validate_config`` action ask this, and they must agree -- one of them
    telling a new user their starter file is broken is the failure.
    """
    return isinstance(document, dict) and document.get("zones") == []


#: What Indigo accepts as an XML tag name. Deliberately narrower than the XML
#: spec -- no colons, nothing non-ASCII -- because this is the shape that is
#: known to survive the trip, not the shape a parser would tolerate.
_XML_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")

#: The value types that cross the bridge. Everything else has to be made into
#: one of these before it is returned.
_BRIDGE_TYPES = (bool, int, float, str)


def _bridge_safe(payload, where="the return value"):
    """Return ``payload``, or raise if Indigo could not serialise it.

    Indigo turns an action's return value into **XML**, so every dict key
    becomes a tag name. That makes the obvious shape for "what each light is
    set to" -- a map keyed by device id, ``{"459564566": 60}`` -- not a
    payload at all. It failed on jarvis with ``LowLevelBadParameterError:
    illegal XML tag name character``, which names neither the key nor the
    action, and the caller got nothing back.

    Nothing in the test suite could have caught that: the fake ``indigo``
    hands the dict straight back without serialising it, so the shape looked
    perfect right up until it reached a real server. This is the check that
    moves that failure into the suite. Every return in this file goes through
    it, so the next payload with an id for a key fails here, loudly and by
    name, instead of quietly on the server.

    The rule it enforces: **keys are words, ids are values.** A key must
    start with a letter or underscore and hold only letters, digits, ``-``,
    ``_`` and ``.``; a value must be None, a bool, a number, a string, or a
    list or dict of those.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(key, str) or not _XML_NAME.match(key):
                raise ValueError(
                    f"{where}: {key!r} cannot be an XML tag name, so Indigo would "
                    "refuse this payload at the action bridge. Keys must start with "
                    "a letter or underscore and hold only letters, digits, '-', '_' "
                    "and '.'. Put ids in values -- a list of objects -- not in keys."
                )
            _bridge_safe(value, f"{where}/{key}")
        return payload
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            _bridge_safe(item, f"{where}[{index}]")
        return payload
    if payload is not None and not isinstance(payload, _BRIDGE_TYPES):
        raise ValueError(
            f"{where}: a {type(payload).__name__} does not cross the Indigo action "
            "bridge. Use None, a bool, a number, a string, a list or a dict."
        )
    return payload


def _device_name(dev_id) -> str:
    """The device's name for display, or ``""`` if it cannot be read now.

    A label and only a label. The level beside it is the zone's decision and
    does not depend on this resolving, so ``""`` means "could not be named"
    and makes no claim about whether the device exists -- the zone's own
    explain line is where a light that would not resolve is reported.
    """
    try:
        device = device_lookup.get_device(dev_id)
    except (device_lookup.DeviceGone, device_lookup.LookupFailed):
        return ""
    return str(getattr(device, "name", "") or "")


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
        #: When the RUNNING configuration was last loaded successfully, as a
        #: local ISO-8601 string, and how many zones it declared. "" means
        #: nothing has loaded since this plugin started. Together they are the
        #: reload signal the MCP tools poll: every other controller state also
        #: moves on an ordinary worker pass, so none of them can answer "has
        #: the file been reloaded since I wrote it".
        self._config_loaded_at = ""
        self._config_zone_count = 0

    # ------------------------------------------------------------- lifecycle

    def _record_config_loaded(self, config, now):
        """Note that THIS configuration is now the running one, and say so.

        Called only where a load has actually succeeded and been applied, so
        a rejected edit leaves both values exactly where they were -- which
        is the whole signal: a caller that wrote a file and sees an unmoved
        ``config_loaded_at`` knows the file was refused, and ``config_status``
        says why.

        The publish is *in here* rather than left to the caller so that the
        record and the announcement cannot come apart: a timestamp that only
        reaches the device on the next worker pass is one a poller cannot
        distinguish from a pass that did nothing.

        At startup this publish is a no-op and is meant to be: it runs
        before ``_refresh_device_map``, so there is no controller id yet. The
        ``_sync_all()`` at the end of ``startup`` and the republish in
        ``deviceStartComm`` are what put the record on the device there.
        """
        self._config_loaded_at = now.strftime("%Y-%m-%dT%H:%M:%S")
        self._config_zone_count = len(config.zones)
        self._sync_controller()

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
        # A startup that could not read the file has NOT loaded a
        # configuration, so it must not stamp one: "" is the honest answer
        # and it is what tells a caller the plugin is running on nothing.
        loaded = config is not None
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
        if loaded:
            self._record_config_loaded(config, dt.datetime.now())

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
        """Indigo has (re)learned this device. Refresh its states, then fill them.

        Two halves, and the second is the one an upgrade needs.

        ``stateListOrDisplayStateIdChanged`` is what makes states added to a
        device type *after* its devices exist visible at all. Until it runs,
        Indigo refuses any update naming one of the new keys -- it logs
        ``state key <x> not defined (ignoring update request)`` and drops it
        -- so on the first start after an upgrade every publish from
        ``startup`` is rejected for exactly the states that were just added.

        So the refresh is not enough on its own: the values that were
        rejected have to be written again, here, now that Indigo will accept
        them. Without this republish the new states sit empty until something
        incidental syncs -- observed on jarvis as `config_zone_count` blank
        for twenty seconds after a restart, which for a state whose whole job
        is to be polled is indistinguishable from "no configuration loaded".
        """
        dev.stateListOrDisplayStateIdChanged()
        if self.engine is None:
            # startup() raised; there are no states to write. The refresh
            # above still mattered, and the next successful start republishes.
            return

        if dev.deviceTypeId == CONTROLLER_TYPE_ID:
            self._controller_id = dev.id
            self._sync_controller()
            return

        if dev.deviceTypeId != ZONE_TYPE_ID:
            return

        zone_name = self._zone_name_of(dev)
        zone = self.engine.zones.get(zone_name)
        if zone is None:
            # A device whose zone has left the configuration. It is not ours
            # to fill in -- _mark_orphan_devices owns what it says -- but it
            # has still just had its state list refreshed, and it must not be
            # claimed by the device map.
            self._mark_orphan_devices()
            return

        self._zone_device_ids[zone_name] = dev.id
        self._warned_orphans.discard(dev.id)
        self._sync_zone(zone)

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

    # --------------------------------------------- the actions with an answer
    #
    # These two exist for a caller that is not a person: the `lamplighter_*`
    # MCP tools in indigo-mcp-lite, which is stdlib-only and in another
    # process, so it cannot import this loader and must ask the running
    # plugin instead. Both therefore RETURN rather than log -- the value
    # reaches a caller that used `executeAction(..., waitUntilDone=True)` --
    # and both answer in plain dicts of str/int/bool/list, which is what
    # survives that bridge.
    #
    # Neither raises for a bad *document*: a caller validating an edit an LLM
    # just proposed needs the complaint back as a value it can hand to the
    # author, and an exception out of an action callback is a traceback in
    # Indigo's log and a `None` at the caller. They raise only for a bug in
    # the call itself.

    def validate_config(self, action, dev=None, caller_waiting_for_result=None):
        """Is this document a configuration this plugin would load?

        The one and only validator is :func:`lamplighter.config.load_config`,
        the same call ``_read_config`` makes -- a second implementation of
        "is this valid" is a second opinion, and the one that is wrong is
        always the one the author is holding.

        ``{"ok": True, "zones": [...], "enabled": [...]}`` or
        ``{"ok": False, "path": "zones/0/hold_seconds", "message": "..."}``.
        The path is what turns "invalid" into a value an author can go and
        fix, so it is a field rather than prose inside the message.

        Nothing is applied. A document that validates here is not running
        anywhere until it is written to the configuration file.
        """
        raw = action.props.get("config_json")
        if raw is None:
            # A caller bug, not a bad document: there is nothing to validate
            # and answering "not ok" would report the author's file as broken
            # when the file was never sent.
            raise ValueError(
                "validate_config needs a 'config_json' prop holding the document "
                "to validate; none was sent"
            )

        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            try:
                document = json.loads(raw)
            except ValueError as exc:
                return _bridge_safe(
                    {"ok": False, "path": "", "message": f"not valid JSON: {exc}"}
                )
        else:
            # Already parsed -- an in-process caller handing over the dict.
            document = raw

        if is_starter_document(document):
            return _bridge_safe({"ok": True, "zones": [], "enabled": []})

        try:
            config = load_config(
                document, self.sun or IndigoSun(logger=self.logger), dt.date.today()
            )
        except ConfigError as exc:
            return _bridge_safe(
                {"ok": False, "path": exc.path or "", "message": exc.message}
            )
        except Exception as exc:
            # The loader raised something that is not a ConfigError, which
            # means the document reached a check that was not expecting its
            # shape at all. Still an answer rather than a traceback at the
            # caller, but it is logged in full here because it is a loader
            # bug and not the author's mistake.
            self.logger.exception(
                "Lamplighter: validating a configuration raised something other than "
                "a ConfigError; the document was refused and the traceback above is "
                "a bug in the loader, not in the document"
            )
            return _bridge_safe({
                "ok": False,
                "path": "",
                "message": (
                    f"the loader raised {type(exc).__name__}: {exc}. This is a bug in "
                    "Lamplighter rather than a rule the document broke; the plugin log "
                    "has the traceback."
                ),
            })

        return _bridge_safe({
            "ok": True,
            "zones": [zone.name for zone in config.zones],
            "enabled": [zone.name for zone in config.zones if zone.enabled],
        })

    def explain_zone(self, action, dev=None, caller_waiting_for_result=None):
        """Why one zone is doing what it is doing, or would at another time.

        With no ``at``, this is the zone's live explain line plus the plan it
        is holding right now. With one, it is a **dry run**: the period, the
        state and the levels resolved against that instant from the inputs
        the zone holds now, deciding nothing and writing nothing (see
        :meth:`lamplighter.zone.Zone.dry_run`). That is the question an
        author asks before saving a period edit.

        The line is logged at INFO as well as returned, so that an action
        group can be pointed at this and read in the event log.
        """
        if self.engine is None:
            message = (
                "Lamplighter did not start; there are no zones to explain. See the "
                "startup error in the event log."
            )
            self.logger.warning(f"Lamplighter: {message}")
            return _bridge_safe({"ok": False, "message": message})

        # `zone_name`, the same prop every zone-taking action reads. It is
        # not routed through _action_zone: that logs the complaint and
        # returns, and this one has to hand the complaint back as a value.
        zone_name = str(action.props.get("zone_name", "") or "")
        zone = self.engine.zones.get(zone_name)
        if zone is None:
            # Returned, never raised: Indigo's action layer turns an
            # exception here into a traceback with no hint that the zone name
            # is simply stale, and the caller gets None either way.
            message = f"no zone named {zone_name!r}; zones are: " + (
                ", ".join(sorted(self.engine.zones)) or "none"
            )
            self.logger.warning(f"Lamplighter: {message}")
            return _bridge_safe({"ok": False, "message": message})

        raw_at = str(action.props.get("at", "") or "").strip()
        if raw_at:
            try:
                at = dt.datetime.fromisoformat(raw_at)
            except ValueError:
                message = (
                    f"{raw_at!r} is not a time this action understands; write it as "
                    "YYYY-MM-DDTHH:MM in local time, or leave it blank for now"
                )
                self.logger.warning(f"Lamplighter: {message}")
                return _bridge_safe({"ok": False, "message": message})
            run = zone.dry_run(at)
            text, levels = run.text, run.levels
        else:
            at = dt.datetime.now()
            text = f"{zone.explain(at)} Desired now: {zone.desired_summary(at)}."
            levels = zone.desired_levels(at)

        self.logger.info(text)
        return _bridge_safe({
            "ok": True,
            "zone": zone.name,
            "at": at.strftime("%Y-%m-%dT%H:%M:%S"),
            "explain": text,
            # A LIST of objects, not a map keyed by device id. Indigo
            # serialises the return value as XML, so a key of "459564566"
            # is an illegal tag name -- the shape this originally shipped
            # with, and it failed on jarvis with LowLevelBadParameterError,
            # naming neither the key nor the action. Ids are values here,
            # and _bridge_safe holds that line for every future edit.
            "desired": [
                {
                    "device": light,
                    "name": _device_name(light),
                    "level": levels[light],
                }
                for light in sorted(levels)
            ],
        })

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

        if is_starter_document(document):
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
        self._record_config_loaded(config, now)
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
            indigo_sync.controller_states(
                self.engine,
                self._config_status,
                config_loaded_at=self._config_loaded_at,
                config_zone_count=self._config_zone_count,
            )
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
