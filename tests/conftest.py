"""A fake `indigo` module, installed before any plugin import.

Ported from the Auto Lights fork's conftest with two deliberate changes.

1. `indigo.devices` and `indigo.variables` do NOT auto-create on a missing
   key. The fork's `__missing__` handed back a freshly minted stub for any id
   you asked about, which meant a test could never see the difference between
   "this device exists" and "this device does not", and neither could the code
   under test. A missing key raises `KeyError` here, exactly as Indigo does --
   and `KeyError` means *gone*, while any other exception from a lookup means
   *the lookup itself broke*. Lamplighter must never collapse those two into
   one handler (R15).

2. `indigo.server.calculateSunrise` / `calculateSunset` are present and
   return fixed times, so period resolution is testable without a server.

3. `indigo.device.turnOn/turnOff` and `indigo.dimmer.setBrightness` exist and
   actually move the stub device, because M1's `IndigoCommander` calls them.
   They are the ONLY verbs the plugin has: there is no settle poll to fake, no
   confirm call and no status request, so this is the whole write surface.
   Most tests drive `helpers.RecordingCommander` instead and never reach here.
"""

import datetime
import logging
import os
import sys
import types

# The fixed sun times this fake server reports. Tests that care about a
# specific offset import these rather than hard-coding a clock.
FAKE_SUNRISE_TIME = datetime.time(6, 12)
FAKE_SUNSET_TIME = datetime.time(19, 47)

indigo_stub = types.SimpleNamespace()


class _Collection(dict):
    """An Indigo collection: iterates values, raises KeyError on a miss."""

    def __iter__(self):
        return iter(self.values())


class Device:
    def __init__(self, id, name="", onState=False, brightness=0, sensorValue=None):
        self.id = id
        self.name = name or f"Dev-{id}"
        self.onState = onState
        self.onOffState = onState
        self.brightness = brightness
        self.sensorValue = sensorValue if sensorValue is not None else brightness
        self.states = {
            "onState": self.onState,
            "onOffState": self.onOffState,
            "brightness": self.brightness,
            "sensorValue": self.sensorValue,
        }
        self.pluginId = ""
        self.deviceTypeId = ""
        self.pluginProps = {}
        self.lastChanged = datetime.datetime.now()

    def __iter__(self):
        return iter(self.states.items())

    def replaceOnServer(self):
        pass

    def updateStatesOnServer(self, state_list):
        pass

    def updateStateOnServer(self, key, value=None, **kwargs):
        self.states[key] = value


class DimmerDevice(Device):
    pass


class RelayDevice(Device):
    pass


class SensorDevice(Device):
    pass


class Variable:
    def __init__(self, id, name="", value=None):
        self.id = id
        self.name = name
        self.value = value


class _DummyHandler(logging.Handler):
    def __init__(self, baseFilename="/tmp/Logs/plugin.log"):
        super().__init__()
        self.baseFilename = baseFilename

    def emit(self, record):
        pass


class PluginBase:
    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs):
        self.pluginId = plugin_id
        self.pluginDisplayName = plugin_display_name
        self.pluginVersion = plugin_version
        self.pluginPrefs = plugin_prefs
        self.logger = logging.getLogger("Plugin")
        self.indigo_log_handler = _DummyHandler()
        self.plugin_file_handler = _DummyHandler()

    def deviceUpdated(self, orig_dev, new_dev):
        return None

    def variableUpdated(self, orig_var, new_var):
        return None


def _calculate_sunrise(date=None):
    """indigo.server.calculateSunrise(date) -> datetime, fixed for tests."""
    day = date or datetime.date.today()
    if isinstance(day, datetime.datetime):
        day = day.date()
    return datetime.datetime.combine(day, FAKE_SUNRISE_TIME)


def _calculate_sunset(date=None):
    """indigo.server.calculateSunset(date) -> datetime, fixed for tests."""
    day = date or datetime.date.today()
    if isinstance(day, datetime.datetime):
        day = day.date()
    return datetime.datetime.combine(day, FAKE_SUNSET_TIME)


def _apply_on_off(dev, on):
    """Move a stub device's on/off, attribute and states together.

    Real Indigo keeps `dev.onState` and `dev.states["onOffState"]` in step;
    a stub that moved only one of them would let a bug through in whichever
    one the code under test does not read.
    """
    dev.onState = bool(on)
    dev.onOffState = bool(on)
    dev.states["onState"] = bool(on)
    dev.states["onOffState"] = bool(on)


def _turn_on(dev_id, **kwargs):
    dev = indigo_stub.devices[dev_id]
    _apply_on_off(dev, True)
    if isinstance(dev, DimmerDevice) and not dev.brightness:
        dev.brightness = 100
        dev.states["brightness"] = 100
    return dev


def _turn_off(dev_id, **kwargs):
    dev = indigo_stub.devices[dev_id]
    _apply_on_off(dev, False)
    if isinstance(dev, DimmerDevice):
        dev.brightness = 0
        dev.states["brightness"] = 0
    return dev


def _set_brightness(dev_id, value=None, **kwargs):
    dev = indigo_stub.devices[dev_id]
    dev.brightness = int(value)
    dev.states["brightness"] = dev.brightness
    _apply_on_off(dev, dev.brightness > 0)
    return dev


indigo_stub.device = types.SimpleNamespace(turnOn=_turn_on, turnOff=_turn_off)
indigo_stub.dimmer = types.SimpleNamespace(setBrightness=_set_brightness)

indigo_stub.devices = _Collection()
indigo_stub.variables = _Collection()
indigo_stub.Device = Device
indigo_stub.DimmerDevice = DimmerDevice
indigo_stub.RelayDevice = RelayDevice
indigo_stub.SensorDevice = SensorDevice
indigo_stub.Variable = Variable
indigo_stub.PluginBase = PluginBase
indigo_stub.kProtocol = types.SimpleNamespace(Plugin="Plugin")
indigo_stub.server = types.SimpleNamespace(
    calculateSunrise=_calculate_sunrise,
    calculateSunset=_calculate_sunset,
    log=lambda *a, **k: None,
)

sys.modules["indigo"] = indigo_stub

import pytest  # noqa: E402  (must follow the stub install)

# Make the plugin's Python importable: `import lamplighter.<module>`.
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            os.pardir,
            "Lamplighter.indigoPlugin",
            "Contents",
            "Server Plugin",
        )
    ),
)


@pytest.fixture(autouse=True)
def fake_indigo():
    """Reset the stub indigo module before each test."""
    indigo_stub.devices.clear()
    indigo_stub.variables.clear()
    yield indigo_stub
