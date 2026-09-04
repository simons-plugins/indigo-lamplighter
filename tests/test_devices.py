"""Unit tests for the lookup boundary (PRD R15; section 5.2).

One rule, tested from both sides: `KeyError` from an Indigo collection means
the object is gone, and anything else means the lookup broke. The promise
that builds on this is
tests/test_promises_degradation.py::test_an_indigo_lookup_failure_is_not_reported_as_device_gone;
these are the mechanics underneath it.
"""

import logging

import pytest
from helpers import make_device

from lamplighter import compare, devices
from lamplighter.devices import DeviceGone, LookupFailed


@pytest.fixture(autouse=True)
def _clean_warnings():
    """warn_once keys are module level and outlive any one object."""
    compare.reset_warnings()
    yield
    compare.reset_warnings()


class Broken(dict):
    """A collection whose lookups fail for one id, the way a server does."""

    def __init__(self, contents=None, bad_id=None, error=None):
        super().__init__(contents or {})
        self.bad_id = bad_id
        self.error = error or RuntimeError("Indigo server is not responding")

    def __getitem__(self, key):
        if key == self.bad_id:
            raise self.error
        return super().__getitem__(key)


# ----------------------------------------------------------------- devices


def test_a_missing_device_is_device_gone():
    with pytest.raises(DeviceGone) as caught:
        devices.get_device(404)
    assert caught.value.object_id == 404
    assert caught.value.kind == "device"
    assert "does not exist" in str(caught.value)


def test_a_present_device_comes_back():
    lamp = make_device(201, "dimmer", name="Desk Lamp", brightness=40)
    assert devices.get_device(201) is lamp


def test_a_broken_lookup_is_not_device_gone(monkeypatch):
    """The whole point of the module: two failures, two classes.

    Kills: one `except Exception` around the lookup. Under it this raises
    DeviceGone and a transient server fault becomes a device the zone drops.
    """
    import indigo

    monkeypatch.setattr(indigo, "devices", Broken({}, bad_id=201))

    with pytest.raises(LookupFailed) as caught:
        devices.get_device(201)
    assert not isinstance(caught.value, DeviceGone)
    assert caught.value.object_id == 201
    assert isinstance(caught.value.cause, RuntimeError)
    assert "not responding" in str(caught.value)


def test_a_broken_lookup_does_not_hide_a_real_miss(monkeypatch):
    """The two live side by side in one collection and stay distinguishable."""
    import indigo

    monkeypatch.setattr(indigo, "devices", Broken({}, bad_id=201))

    with pytest.raises(LookupFailed):
        devices.get_device(201)
    with pytest.raises(DeviceGone):
        devices.get_device(999)


# --------------------------------------------------------------- variables


def test_a_variable_value_comes_back_as_indigo_stores_it():
    import indigo

    indigo.variables[55] = indigo.Variable(55, name="kitchen_dark_below", value="1800")
    assert devices.get_variable_value(55) == "1800"


def test_a_missing_variable_is_gone_and_says_variable():
    with pytest.raises(DeviceGone) as caught:
        devices.get_variable_value(55)
    assert caught.value.kind == "variable"
    assert "variable 55" in str(caught.value)


def test_a_broken_variable_lookup_is_lookup_failed(monkeypatch):
    import indigo

    monkeypatch.setattr(indigo, "variables", Broken({}, bad_id=55))
    with pytest.raises(LookupFailed) as caught:
        devices.get_variable_value(55)
    assert caught.value.kind == "variable"


# ---------------------------------------------------------------- warnings


def test_the_two_warnings_are_different_conditions_with_different_keys(caplog):
    """Gone and lookup-failed have separate keys, so one does not silence the
    other, and the lookup-failed message says what it is NOT."""
    logger = logging.getLogger("test.devices.warn")

    with caplog.at_level(logging.WARNING, logger="test.devices.warn"):
        assert devices.warn_gone_once(logger, 201, "Study") is True
        assert devices.warn_gone_once(logger, 201, "Study") is False
        assert (
            devices.warn_lookup_failed_once(logger, 201, "Study", RuntimeError("boom"))
            is True
        )

    assert len(caplog.records) == 2
    gone, failed = (record.getMessage() for record in caplog.records)
    assert "does not exist" in gone
    assert "NOT" in failed and "gone" in failed
    assert "RuntimeError" in failed


def test_forgetting_a_warning_lets_the_next_outage_speak(caplog):
    """Section 10: a warning latched forever hides the second outage as
    completely as no warning hides the first."""
    logger = logging.getLogger("test.devices.forget")

    with caplog.at_level(logging.WARNING, logger="test.devices.forget"):
        assert devices.warn_gone_once(logger, 201, "Study") is True
        assert devices.warn_gone_once(logger, 201, "Study") is False
        devices.forget_warnings(201)
        assert devices.warn_gone_once(logger, 201, "Study") is True

    # Two records for three calls: the middle one is the silence.
    assert len(caplog.records) == 2
