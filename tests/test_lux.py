"""Unit tests for the daylight gate (PRD R9, R15; section 11, decision 3).

The Schmitt trigger's edges and the unreadable direction. The promises built
on these are in tests/test_promises_degradation.py and
tests/test_promises_replan.py.
"""

import datetime as dt
import logging

import pytest
from helpers import make_device

from lamplighter import compare
from lamplighter.lux import STALE_AFTER_SECONDS, Lux, read_sensor_value

NOW = dt.datetime(2026, 9, 4, 19, 40, 0)
DARK_BELOW = 2200
HYSTERESIS = 300


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def at(seconds):
    return NOW + dt.timedelta(seconds=seconds)


def reading(value, when=NOW, device_id=17):
    lux = Lux(device_id, logging.getLogger("test.lux"))
    lux.update(value, when)
    return lux


# ------------------------------------------------------- the Schmitt trigger


def test_below_the_threshold_is_dark():
    assert reading(1800).dark(DARK_BELOW, HYSTERESIS) is True


def test_at_the_threshold_is_not_dark():
    """`<` not `<=`: the threshold is the first value that is NOT dark."""
    assert reading(2200).dark(DARK_BELOW, HYSTERESIS) is False


def test_the_band_is_one_sided_and_holds_the_verdict():
    """Inside the band the verdict is held. This is the whole trigger: the
    zone's own lights lift the reading 100-200 lux at a sensor in the room,
    and a plain threshold turns that into an oscillator."""
    lux = reading(1800)
    assert lux.dark(DARK_BELOW, HYSTERESIS) is True
    lux.update(2400, at(60))  # lights on: above the threshold, inside the band
    assert lux.dark(DARK_BELOW, HYSTERESIS) is True
    assert lux.changed is False


def test_leaving_dark_needs_the_whole_band():
    lux = reading(1800)
    lux.dark(DARK_BELOW, HYSTERESIS)
    lux.update(2499, at(60))
    assert lux.dark(DARK_BELOW, HYSTERESIS) is True
    lux.update(2500, at(120))  # dark_below + hysteresis exactly
    assert lux.dark(DARK_BELOW, HYSTERESIS) is False
    assert lux.changed is True


def test_entering_dark_does_not_use_the_band():
    """The band exists to stop the zone's own lights ending the dark, not to
    delay the dark starting."""
    lux = reading(3000)
    assert lux.dark(DARK_BELOW, HYSTERESIS) is False
    lux.update(2199, at(60))
    assert lux.dark(DARK_BELOW, HYSTERESIS) is True
    assert lux.changed is True


def test_a_first_reading_inside_the_band_is_not_dark():
    """With no verdict to hold, a reading at or above dark_below is bright:
    the band never invents a dark the threshold did not."""
    lux = reading(2400)
    assert lux.verdict is None
    assert lux.dark(DARK_BELOW, HYSTERESIS) is False


def test_the_first_verdict_is_not_a_flip():
    lux = reading(1800)
    assert lux.dark(DARK_BELOW, HYSTERESIS) is True
    assert lux.changed is False


def test_a_value_change_that_does_not_flip_the_verdict_is_not_a_change():
    lux = reading(1800)
    lux.dark(DARK_BELOW, HYSTERESIS)
    for value in (1700, 1500, 2100):
        lux.update(value, at(60))
        lux.dark(DARK_BELOW, HYSTERESIS)
        assert lux.changed is False


def test_a_seeded_verdict_is_honoured_without_being_a_flip():
    """R13: the trigger's memory is the persisted state. A zone that comes
    back with no verdict decides the first in-band reading the wrong way."""
    lux = Lux(17, logging.getLogger("test.lux"))
    lux.seed(True)
    lux.update(2400, NOW)  # inside the band
    assert lux.dark(DARK_BELOW, HYSTERESIS) is True
    assert lux.changed is False


def test_seeding_ignores_a_non_boolean():
    lux = Lux(17, logging.getLogger("test.lux"))
    lux.seed("yes")
    assert lux.verdict is None


def test_zero_hysteresis_is_a_plain_threshold():
    lux = reading(1800)
    assert lux.dark(DARK_BELOW, 0) is True
    lux.update(2200, at(60))
    assert lux.dark(DARK_BELOW, 0) is False


# ----------------------------------------------------------- the unreadable


def test_unreadable_takes_the_configured_direction():
    lux = reading(None)
    assert lux.unreadable is True
    assert lux.dark(DARK_BELOW, HYSTERESIS, when_unreadable="dark") is True
    assert lux.dark(DARK_BELOW, HYSTERESIS, when_unreadable="bright") is False


def test_unreadable_is_not_a_reading_of_zero():
    """Kills: fall through to 0.0, which reads as a pitch-dark room and is
    indistinguishable from a working sensor at night."""
    lux = reading(None)
    assert lux.value is None
    assert lux.dark(DARK_BELOW, HYSTERESIS, when_unreadable="bright") is False


def test_a_previous_value_survives_an_unreadable_sample_as_evidence():
    lux = reading(1800)
    lux.update(None, at(60))
    assert lux.value == 1800
    assert lux.unreadable is True
    # ...but the verdict comes from the flag, not from the stale value.
    assert lux.dark(DARK_BELOW, HYSTERESIS, when_unreadable="bright") is False


@pytest.mark.parametrize("value", [None, "", "not a number", True, False, object()])
def test_anything_that_is_not_a_number_is_unreadable(value):
    """`True` included: isinstance(True, int) is true in Python, and an
    on/off state that leaked into a lux field must not read as 1 lux."""
    assert reading(value).unreadable is True


@pytest.mark.parametrize("value,expected", [(0, 0.0), ("1800", 1800.0), (" 12.5 ", 12.5)])
def test_a_number_in_any_shape_indigo_reports_it_is_a_reading(value, expected):
    lux = reading(value)
    assert lux.unreadable is False
    assert lux.value == expected


def test_the_unreadable_warning_names_the_reason(caplog):
    lux = Lux(17, logging.getLogger("test.lux.warn"))
    with caplog.at_level(logging.WARNING, logger="test.lux.warn"):
        lux.update(None, NOW, reason="device 17 does not exist in Indigo")
    message = caplog.records[0].getMessage()
    assert "does not exist" in message
    assert "not a lux of zero" in message


# ---------------------------------------------------------------- staleness


def test_a_reading_that_has_never_arrived_is_stale():
    assert Lux(17).stale(NOW) is True


def test_a_fresh_reading_is_not_stale():
    assert reading(1800).stale(at(60)) is False


def test_a_reading_older_than_the_threshold_is_stale():
    lux = reading(1800)
    assert lux.stale(at(STALE_AFTER_SECONDS)) is False
    assert lux.stale(at(STALE_AFTER_SECONDS + 1)) is True
    assert lux.age(at(600)) == dt.timedelta(seconds=600)


def test_an_unreadable_sample_does_not_refresh_the_age():
    """Kills: stamp read_at on every attempt, which makes a sensor that has
    been dead for an hour look like it reported a second ago."""
    lux = reading(1800)
    lux.update(None, at(STALE_AFTER_SECONDS + 1))
    assert lux.stale(at(STALE_AFTER_SECONDS + 1)) is True


# -------------------------------------------------------------- the reading


def test_read_sensor_value_prefers_sensor_value():
    device = make_device(302, "sensor", sensorValue=1800)
    assert read_sensor_value(device) == 1800.0


def test_read_sensor_value_falls_back_to_the_states_dict():
    device = make_device(302, "sensor", sensorValue=None)
    device.sensorValue = None
    device.states["sensorValue"] = "1750"
    assert read_sensor_value(device) == 1750.0


def test_read_sensor_value_answers_none_rather_than_guessing():
    device = make_device(302, "sensor")
    device.sensorValue = None
    device.states["sensorValue"] = None
    assert read_sensor_value(device) is None


def test_a_restored_verdict_outranks_the_unreadable_direction_before_any_reading():
    """After a restart the zone knows it was dark five minutes ago. The
    when_unreadable direction is for a zone that knows nothing at all.

    Kills: treat "no reading yet" as unreadable, which throws the persisted
    verdict away on every restart of a `when_unreadable: bright` zone.
    """
    lux = Lux(17, logging.getLogger("test.lux"))
    lux.seed(True)
    assert lux.dark(DARK_BELOW, HYSTERESIS, when_unreadable="bright") is True

    fresh = Lux(17, logging.getLogger("test.lux"))
    assert fresh.dark(DARK_BELOW, HYSTERESIS, when_unreadable="bright") is False


def test_an_explicit_unreadable_reading_outranks_a_restored_verdict():
    """A failed read is news; a memory is not. The direction wins here."""
    lux = Lux(17, logging.getLogger("test.lux"))
    lux.seed(True)
    lux.update(None, NOW)
    assert lux.dark(DARK_BELOW, HYSTERESIS, when_unreadable="bright") is False
