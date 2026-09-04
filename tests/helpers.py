"""Test helpers for the fake `indigo` in conftest.py.

Import as `from helpers import make_device` -- pytest puts `tests/` on
sys.path (there is no `tests/__init__.py`).
"""

import datetime


class FixedSun:
    """A `periods.SunProvider` whose sky never moves unless a test says so.

    `sunrise` and `sunset` are each either a `datetime.time` or a callable
    taking a date and returning one -- the callable form is how a test makes
    sunset swing through the year, which is the only way to catch a period
    that overlaps its neighbour in December and clears it in September.

    Every question is recorded in `asked`, so a test can make "the code never
    consulted the sun at all" fatal rather than inferring it from a value
    that happened to look right.
    """

    def __init__(self, sunrise=datetime.time(6, 30), sunset=datetime.time(19, 45)):
        self._sunrise = sunrise
        self._sunset = sunset
        self.asked = []

    def sunrise(self, date):
        return self._at("sunrise", self._sunrise, date)

    def sunset(self, date):
        return self._at("sunset", self._sunset, date)

    def _at(self, kind, spec, date):
        self.asked.append((kind, date))
        return datetime.datetime.combine(date, spec(date) if callable(spec) else spec)


def make_device(dev_id, device_cls="dimmer", **kwargs):
    """Create a stub indigo device and install it in `indigo.devices`.

    device_cls: "dimmer" (default), "relay", "sensor", "device", or a concrete
    stub class. Recognised kwargs: name, onState, brightness, sensorValue; any
    other kwarg is written straight into the device's `states` dict.
    """
    import indigo

    if isinstance(device_cls, str):
        cls_map = {
            "device": indigo.Device,
            "dimmer": indigo.DimmerDevice,
            "relay": indigo.RelayDevice,
            "sensor": indigo.SensorDevice,
        }
        cls = cls_map[device_cls]
    else:
        cls = device_cls

    d = cls(
        dev_id,
        name=kwargs.get("name", ""),
        onState=kwargs.get("onState", False),
        brightness=kwargs.get("brightness", 0),
        sensorValue=kwargs.get("sensorValue", None),
    )
    for k, v in kwargs.items():
        if k not in ("name", "onState", "brightness", "sensorValue"):
            d.states[k] = v
    d.onOffState = d.onState
    d.states["onState"] = d.onState
    d.states["onOffState"] = d.onOffState
    d.states["brightness"] = d.brightness
    d.states["sensorValue"] = d.sensorValue
    indigo.devices[dev_id] = d
    return d


def make_snapshot(dev_id, **kwargs):
    """Build a DETACHED device object standing in for one event's snapshot.

    Indigo hands `deviceUpdated` two separate objects, origDev and newDev, and
    the override rule (R1) is only meaningful because they are separate: it
    judges the transition between them, never a live re-read. make_device()
    installs into indigo.devices, so capture whatever is live first and put it
    back, leaving the returned object genuinely detached.
    """
    import indigo

    live = indigo.devices.get(dev_id)
    snapshot = make_device(dev_id, **kwargs)
    if live is not None:
        indigo.devices[dev_id] = live
    else:
        del indigo.devices[dev_id]
    return snapshot


def make_period(name, start, end, mode="on_and_off", levels=None, **extra):
    """One period as the config file writes it -- for make_zone() below."""
    return {
        "name": name,
        "from": start,
        "to": end,
        "mode": mode,
        "levels": levels if levels is not None else {"201": 60},
        **extra,
    }


def make_zone(periods, sun=None, today=None, logger=None, clock=None, **zone_fields):
    """A live `zone.Zone` built through `load_config`, not by hand.

    Going through the loader is the point: a Zone assembled from
    hand-written dataclasses can be given a shape the config file could never
    produce, and then the test proves something about a zone that cannot
    exist. `zone_fields` overrides any zone key -- lights, lux, hold_seconds,
    override, enabled -- and `periods` is a list of make_period() dicts.
    """
    import datetime as _dt

    from lamplighter.config import load_config
    from lamplighter.zone import Zone

    sun = sun or FixedSun()
    document = {
        "version": 1,
        "zones": [
            {
                "name": "Study",
                "presence_devices": [101],
                "hold_seconds": 300,
                "lux": None,
                "lights": [201],
                "periods": periods,
                **zone_fields,
            }
        ],
    }
    config = load_config(document, sun, today or _dt.date(2026, 9, 4)).zones[0]
    return Zone(config, sun, clock=clock, logger=logger)
