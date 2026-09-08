"""Unit tests for the zone state machine (PRD sections 5.3, 5.6, 5.10).

Every transition in section 5.3 against a fixed clock, plus the pieces the
promises lean on and do not each re-prove: the counters rolling at midnight,
next_wake's ordering, explain, and the desired-level table.

The acceptance promises are in tests/test_promises_*.py. These sit under
them, in the same relationship test_compare.py has to the override promises.
"""

import dataclasses
import datetime as dt
import json
import logging

import pytest
from helpers import make_device, make_period, make_zone

from lamplighter import compare
from lamplighter.config import ConfigError
from lamplighter.zone import ZoneState

# 20:00 on a September evening: inside the Evening band below, after the
# fixed sun's 19:45 sunset, and far from any boundary that could move.
NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
LUX = {"device": 302, "dark_below": 2200, "hysteresis": 300}


@pytest.fixture(autouse=True)
def _clean_warnings():
    compare.reset_warnings()
    yield
    compare.reset_warnings()


def at(**kwargs):
    return NOW + dt.timedelta(**kwargs)


def evening(levels=None, **extra):
    return make_period(
        "Evening", "18:00", "23:00", levels=levels or {"201": 60, "202": 30}, **extra
    )


def two_light_zone(periods=None, **fields):
    """A zone with two lights, a lux gate and a five-minute presence hold."""
    fields.setdefault("lights", [201, 202])
    fields.setdefault("lux", dict(LUX))
    return make_zone(periods or [evening()], logger=logging.getLogger("test.zone"), **fields)


def occupy(zone, now=NOW, lux=1800):
    """Put the zone in OCCUPIED with dark lux and presence, and settle it."""
    # A PIR trip: on, then immediately off. The sensor is not left reporting,
    # so the hold is running from `now` and every timing assertion below is
    # about the hold rather than about a sensor that is still on. A LEVEL
    # sensor left on is a different case with its own tests.
    zone.ingest_presence(101, True, now)
    zone.ingest_presence(101, False, now)
    zone.ingest_lux(lux, now)
    zone.evaluate(now, "setup")
    assert zone.state is ZoneState.OCCUPIED
    return zone


# --------------------------------------------------- the state machine table


def test_a_fresh_zone_starts_off_duty():
    assert two_light_zone().state is ZoneState.OFF_DUTY


def test_period_and_dark_with_no_presence_goes_vacant():
    zone = two_light_zone()
    zone.ingest_lux(1800, NOW)
    move = zone.evaluate(NOW, "lux edge")
    assert (move.from_state, move.to_state) == (ZoneState.OFF_DUTY, ZoneState.VACANT)
    assert move.cause == "lux edge"
    assert move.zone == "Study"


def test_period_and_dark_with_presence_goes_occupied():
    zone = two_light_zone()
    zone.ingest_lux(1800, NOW)
    zone.ingest_presence(101, True, NOW)
    move = zone.evaluate(NOW, "presence edge")
    assert move.to_state is ZoneState.OCCUPIED


def test_vacant_to_occupied_on_presence():
    zone = two_light_zone()
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "lux edge")
    zone.ingest_presence(101, True, at(minutes=1))
    move = zone.evaluate(at(minutes=1), "presence edge")
    assert (move.from_state, move.to_state) == (ZoneState.VACANT, ZoneState.OCCUPIED)


def test_occupied_to_vacant_when_the_hold_expires():
    zone = occupy(two_light_zone())
    assert zone.evaluate(at(seconds=299), "hold check") is None
    move = zone.evaluate(at(seconds=300), "hold expiry")
    assert (move.from_state, move.to_state) == (ZoneState.OCCUPIED, ZoneState.VACANT)


def test_a_bright_room_goes_off_duty_because_it_is_bright():
    zone = occupy(two_light_zone())
    zone.ingest_lux(2500, at(minutes=1))  # dark_below + hysteresis
    move = zone.evaluate(at(minutes=1), "lux edge")
    assert move.to_state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "bright"


def test_the_end_of_the_period_goes_off_duty_with_no_period():
    zone = occupy(two_light_zone())
    later = dt.datetime(2026, 9, 4, 23, 0, 0)
    zone.ingest_presence(101, True, later)
    move = zone.evaluate(later, "period boundary")
    assert move.to_state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "no_period"


def test_disabling_the_zone_goes_off_duty_and_enabling_comes_back():
    zone = occupy(two_light_zone())
    assert zone.set_enabled(enabled=False) is True
    assert zone.evaluate(at(minutes=1), "zone disabled").to_state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "disabled"

    assert zone.set_enabled(enabled=True) is True
    zone.ingest_presence(101, True, at(minutes=2))
    assert zone.evaluate(at(minutes=2), "zone enabled").to_state is ZoneState.OCCUPIED
    assert zone.off_duty_cause is None, "a zone that is on duty has no cause"


def test_disabling_the_plugin_goes_off_duty():
    zone = occupy(two_light_zone())
    assert zone.set_enabled(plugin_enabled=False) is True
    assert zone.evaluate(at(minutes=1), "plugin disabled").to_state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "disabled"


def test_being_disabled_outranks_being_bright_and_having_no_period():
    """The causes are ranked, not combined. A disabled zone must write
    nothing whatever else is true, so `disabled` has to win -- and it wins by
    reporting itself, because the cause is what chooses the plan."""
    zone = two_light_zone()
    zone.ingest_lux(30000, NOW)  # bright as well
    zone.set_enabled(enabled=False)
    zone.evaluate(dt.datetime(2026, 9, 4, 23, 30), "everything at once")  # and no period
    assert zone.off_duty_cause == "disabled"


def test_having_no_period_outranks_being_bright():
    """With no period there are no levels to write, so there is nothing for
    `bright` to turn off even if the reading says so."""
    zone = two_light_zone()
    zone.ingest_lux(30000, NOW)
    zone.evaluate(dt.datetime(2026, 9, 4, 23, 30), "after the band")
    assert zone.off_duty_cause == "no_period"


def test_setting_an_enable_to_what_it_already_is_is_not_an_edge():
    zone = two_light_zone()
    assert zone.set_enabled(enabled=True, plugin_enabled=True) is False


def test_occupied_to_overridden():
    zone = occupy(two_light_zone())
    zone.start_override(201, at(minutes=1))
    move = zone.evaluate(at(minutes=1), "override started")
    assert (move.from_state, move.to_state) == (ZoneState.OCCUPIED, ZoneState.OVERRIDDEN)


def test_vacant_to_overridden():
    zone = two_light_zone()
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "lux edge")
    zone.start_override(201, at(minutes=1))
    move = zone.evaluate(at(minutes=1), "override started")
    assert (move.from_state, move.to_state) == (ZoneState.VACANT, ZoneState.OVERRIDDEN)


def test_an_override_that_expires_in_an_empty_room_ends():
    zone = two_light_zone(override={"duration_minutes": 60, "unlock_on_leave": False})
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "lux edge")
    zone.start_override(201, NOW)
    zone.evaluate(NOW, "override started")

    assert zone.evaluate(at(minutes=59), "tick") is None
    move = zone.evaluate(at(minutes=60), "override expiry")
    assert (move.from_state, move.to_state) == (ZoneState.OVERRIDDEN, ZoneState.VACANT)
    assert zone.override is None


def test_an_override_that_expires_with_presence_and_no_extension_ends():
    """extend_minutes 0 means an override in an occupied room still ends at
    its duration -- right for a bathroom, wrong for a dining room, which is
    why the number is a per-zone and per-period setting."""
    zone = two_light_zone(
        override={"duration_minutes": 60, "extend_minutes": 0, "unlock_on_leave": False}
    )
    occupy(zone)
    zone.start_override(201, NOW)
    zone.evaluate(NOW, "override started")

    zone.ingest_presence(101, True, at(minutes=59))  # still here
    move = zone.evaluate(at(minutes=60), "override expiry")
    assert (move.from_state, move.to_state) == (ZoneState.OVERRIDDEN, ZoneState.OCCUPIED)


def test_an_override_that_expires_with_presence_extends_instead_of_ending():
    zone = two_light_zone(
        override={"duration_minutes": 60, "extend_minutes": 30, "unlock_on_leave": False}
    )
    occupy(zone)
    zone.start_override(201, NOW)
    zone.evaluate(NOW, "override started")

    zone.ingest_presence(101, True, at(minutes=59))
    assert zone.evaluate(at(minutes=60), "override expiry") is None
    assert zone.state is ZoneState.OVERRIDDEN
    assert zone.override.expires_at == at(minutes=90)
    assert zone.override.extended_count == 1


def test_an_override_created_in_an_empty_room_is_not_released_by_leaving():
    """unlock_on_leave releases an override when the room EMPTIES. A lock
    taken from the app in an already-empty hallway has no leaving to wait
    for, and must run its duration instead of evaporating on the next tick."""
    zone = two_light_zone(override={"duration_minutes": 60, "unlock_on_leave": True})
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "lux edge")
    zone.start_override(201, NOW)

    assert zone.evaluate(at(minutes=1), "tick").to_state is ZoneState.OVERRIDDEN
    assert zone.override is not None
    assert zone.evaluate(at(minutes=30), "tick") is None
    assert zone.override is not None


def test_a_never_lock_zone_refuses_to_start_an_override():
    zone = two_light_zone(override={"enabled": False})
    occupy(zone)
    assert zone.start_override(201, at(minutes=1)) is None
    assert zone.override is None
    assert zone.evaluate(at(minutes=1), "override attempt") is None
    assert zone.state is ZoneState.OCCUPIED


def test_evaluate_returns_none_when_nothing_moved():
    zone = occupy(two_light_zone())
    assert zone.evaluate(at(seconds=30), "tick") is None
    assert zone.evaluate(at(seconds=60), "tick") is None


# ------------------------------------------------------------ override timing


def test_a_period_override_block_supplies_the_timing_at_creation():
    zone = two_light_zone(
        [evening(override={"duration_minutes": 120, "extend_minutes": 45})],
        override={"duration_minutes": 60, "extend_minutes": 30},
    )
    occupy(zone)
    override = zone.start_override(201, NOW)
    assert (override.duration_minutes, override.extend_minutes) == (120, 45)
    assert override.expires_at == at(minutes=120)


def test_an_override_keeps_its_timing_when_the_period_changes_under_it():
    """The timing is copied in at creation, so a lock taken in a long-hold
    band does not have its expiry moved by the clock crossing a boundary."""
    zone = two_light_zone(
        [
            make_period(
                "Evening",
                "18:00",
                "21:00",
                levels={"201": 60, "202": 30},
                override={"duration_minutes": 120, "extend_minutes": 45},
            ),
            make_period("Late", "21:00", "23:00", levels={"201": 10, "202": 10}),
        ],
        override={"duration_minutes": 60, "extend_minutes": 5},
    )
    occupy(zone)
    override = zone.start_override(201, NOW)
    assert override.expires_at == at(minutes=120)  # 22:00, inside "Late"

    zone.ingest_presence(101, True, at(minutes=119))
    zone.evaluate(at(minutes=120), "override expiry")
    assert zone.override.extend_minutes == 45
    assert zone.override.expires_at == at(minutes=165)


# ---------------------------------------------------------- the desired plan


def test_a_disabled_zone_and_a_period_gap_both_leave_every_light_alone():
    """`no_period` and `disabled` write nothing: the plugin has no opinion
    about a time it was not configured for, or a zone it was told to leave
    alone (section 5.3, decided 2026-09-04).

    Kills: treat every OFF-DUTY cause like `bright`, which turns "this zone
    is switched off" into "turn this zone's lights off" -- the one thing a
    disabled zone must never do -- and makes a deliberate gap between two
    bands into a hard-off band nobody wrote.

    Mutation applied: Zone.desired_levels's `if self._off_duty ==
    OFF_DUTY_BRIGHT:` -> `if True:`.
    """
    # Disabled, with the room dark and occupied so that nothing else could
    # be keeping the lights alone.
    disabled = occupy(two_light_zone())
    disabled.set_enabled(enabled=False)
    assert disabled.evaluate(at(minutes=1), "zone disabled").to_state is ZoneState.OFF_DUTY
    assert disabled.off_duty_cause == "disabled"
    assert disabled.desired_levels(at(minutes=1)) == {201: "leave", 202: "leave"}

    # The plugin's global switch, same answer.
    plugin_off = occupy(two_light_zone())
    plugin_off.set_enabled(plugin_enabled=False)
    plugin_off.evaluate(at(minutes=1), "plugin disabled")
    assert plugin_off.desired_levels(at(minutes=1)) == {201: "leave", 202: "leave"}

    # A gap between two bands, with the room dark and occupied.
    gapped = two_light_zone(
        [
            make_period("Dusk", "18:00", "20:00", levels={"201": 60, "202": 30}),
            make_period("Night", "21:00", "23:00", levels={"201": 10, "202": 10}),
        ]
    )
    inside, gap = dt.datetime(2026, 9, 4, 19, 0), dt.datetime(2026, 9, 4, 20, 30)
    gapped.ingest_lux(1800, inside)
    gapped.ingest_presence(101, True, inside)
    assert gapped.evaluate(inside, "presence edge").to_state is ZoneState.OCCUPIED
    gapped.ingest_presence(101, True, gap)
    assert gapped.evaluate(gap, "period boundary").to_state is ZoneState.OFF_DUTY
    assert gapped.off_duty_cause == "no_period"
    assert gapped.desired_levels(gap) == {201: "leave", 202: "leave"}

    # ...and a zone that has never been evaluated at all is the same: no
    # cause, no opinion.
    fresh = two_light_zone()
    assert fresh.state is ZoneState.OFF_DUTY
    assert fresh.off_duty_cause is None
    assert fresh.desired_levels(NOW) == {201: "leave", 202: "leave"}


def test_a_bright_room_turns_its_lights_off_like_a_vacant_one():
    """`bright` is VACANT's plan, presence or no presence: daylight makes the
    lights unnecessary and this house relies on them going off when a room
    brightens (section 5.3).
    """
    zone = occupy(two_light_zone([evening(levels={"201": 60, "202": "leave"})]))
    zone.ingest_lux(2500, at(minutes=1))
    assert zone.evaluate(at(minutes=1), "lux edge").to_state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "bright"

    # Somebody is still standing in the room, and the lights still go off.
    assert zone.presence.active(at(minutes=1), 300) is True
    assert zone.desired_levels(at(minutes=1)) == {201: "off", 202: "leave"}

    vacant = two_light_zone([evening(levels={"201": 60, "202": "leave"})])
    vacant.ingest_lux(1800, NOW)
    assert vacant.evaluate(NOW, "lux edge").to_state is ZoneState.VACANT
    assert vacant.desired_levels(NOW) == zone.desired_levels(at(minutes=1))


def test_an_override_outlasts_the_room_going_bright():
    """A person took the lights over; daylight does not take them back
    (section 5.3). The zone stays OVERRIDDEN and writes nothing until the
    override ends, and only then does `bright` get its way.

    Kills: let the OFF-DUTY cause outrank the override, which reverts a
    manual change the moment the sun comes out -- the fork's revert bug with
    a different trigger.
    """
    zone = occupy(two_light_zone())
    zone.start_override(201, at(minutes=1))
    assert zone.evaluate(at(minutes=1), "override started").to_state is ZoneState.OVERRIDDEN

    # The room brightens. The override wins.
    zone.ingest_lux(2500, at(minutes=2))
    assert zone.is_dark() is False
    assert zone.evaluate(at(minutes=2), "lux edge") is None
    assert zone.state is ZoneState.OVERRIDDEN
    assert zone.off_duty_cause is None
    assert zone.desired_levels(at(minutes=2)) == {201: "leave", 202: "leave"}
    assert "outlasts the room going bright" in zone.explain(at(minutes=2))

    # ...and when it ends, the daylight gets its way after all.
    zone.end_override("test", at(minutes=3))
    move = zone.evaluate(at(minutes=3), "override released")
    assert move.to_state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "bright"
    assert zone.desired_levels(at(minutes=3)) == {201: "off", 202: "off"}


def test_being_switched_off_while_overridden_still_reads_as_off_duty():
    """Only `bright` is outranked. A disabled zone reporting OVERRIDDEN would
    lie about what it is doing, and both write nothing anyway."""
    zone = occupy(two_light_zone())
    zone.start_override(201, at(minutes=1))
    zone.evaluate(at(minutes=1), "override started")

    zone.set_enabled(enabled=False)
    assert zone.evaluate(at(minutes=2), "zone disabled").to_state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "disabled"
    assert zone.override is not None, "the override is held, just not reported as the state"
    assert zone.desired_levels(at(minutes=2)) == {201: "leave", 202: "leave"}


def test_vacant_turns_off_every_light_with_a_level():
    zone = two_light_zone([evening(levels={"201": 60, "202": "leave"})])
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "lux edge")
    assert zone.desired_levels(NOW) == {201: "off", 202: "leave"}


def test_vacant_uses_the_period_vacant_levels_and_off_for_the_rest():
    """A porch light dims rather than goes dark; a light with no
    `vacant_levels` entry still goes off, exactly as before (R12).

    Kills: the VACANT branch still calling `_all_off`, which ignores
    `vacant_levels` entirely.
    """
    zone = two_light_zone(
        [evening(levels={"201": 60, "202": 30}, vacant_levels={"201": 25})]
    )
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "lux edge")
    assert zone.desired_levels(NOW) == {201: 25, 202: "off"}


def test_a_vacant_level_is_capped_by_the_period_limit():
    """The period's `limit` applies to a vacant level exactly as it applies
    to an occupied one (R12, section 5.6).

    Kills: forgetting to run a vacant level through `_capped`.
    """
    zone = two_light_zone(
        [evening(levels={"201": 60, "202": "leave"}, vacant_levels={"201": 80}, limit=60)]
    )
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "lux edge")
    assert zone.desired_levels(NOW) == {201: 60, 202: "leave"}


def test_bright_off_duty_ignores_vacant_levels_and_turns_everything_off():
    """`bright` is not VACANT's plan any more, and it must not become one:
    daylight makes the lights unnecessary outright, dim level included
    (section 5.3, decided 2026-09-04).

    Kills: routing OFF-DUTY's `bright` cause through `_vacant_plan` instead
    of `_all_off`.
    """
    zone = occupy(
        two_light_zone([evening(levels={"201": 60, "202": "leave"}, vacant_levels={"201": 25})])
    )
    zone.ingest_lux(2500, at(minutes=1))
    assert zone.evaluate(at(minutes=1), "lux edge").to_state is ZoneState.OFF_DUTY
    assert zone.off_duty_cause == "bright"
    assert zone.desired_levels(at(minutes=1)) == {201: "off", 202: "leave"}


def test_a_leave_vacant_level_is_not_written_when_vacant():
    """`vacant_levels: "leave"` means the plugin does not write this device
    when vacant, the same guarantee `levels: "leave"` gives in any state
    (R12).

    Kills: mapping a "leave" vacant level to off.
    """
    zone = two_light_zone(
        [evening(levels={"201": 100, "202": "leave"}, vacant_levels={"201": "leave"})]
    )
    zone.ingest_lux(1800, NOW)
    zone.evaluate(NOW, "lux edge")
    assert zone.desired_levels(NOW) == {201: "leave", 202: "leave"}


def test_occupied_is_the_periods_levels():
    zone = occupy(two_light_zone())
    assert zone.desired_levels(NOW) == {201: 60, 202: 30}


def test_overridden_writes_nothing_at_all():
    zone = occupy(two_light_zone())
    zone.start_override(201, at(minutes=1))
    zone.evaluate(at(minutes=1), "override started")
    assert zone.desired_levels(at(minutes=1)) == {201: "leave", 202: "leave"}


def test_on_is_a_level_and_off_is_a_force_off():
    zone = two_light_zone([evening(levels={"201": "on", "202": "off"})])
    occupy(zone)
    assert zone.desired_levels(NOW) == {201: "on", 202: "off"}


def test_the_limit_caps_an_explicit_level_and_on():
    zone = two_light_zone([evening(levels={"201": 100, "202": "on"}, limit=60)])
    occupy(zone)
    assert zone.desired_levels(NOW) == {201: 60, 202: 60}


def test_the_limit_does_not_raise_a_level_below_it():
    zone = two_light_zone([evening(levels={"201": 20, "202": 30}, limit=60)])
    occupy(zone)
    assert zone.desired_levels(NOW) == {201: 20, 202: 30}


def test_a_zone_with_a_lux_sensor_can_never_be_built_with_adjust_by_lux():
    """The unimplemented path is unreachable rather than defended: the loader
    refuses the file (PRD section 5.6), so no Zone with a sensor to scale
    against gets as far as planning.

    Kills: putting back a runtime branch, which is a decision taken at the
    wrong end -- once the zone exists, every answer it can give is wrong.
    """
    with pytest.raises(ConfigError, match="not implemented in this version"):
        two_light_zone([evening(adjust_by_lux=True)])


def test_adjust_by_lux_without_a_lux_block_warns_and_is_treated_as_false(caplog):
    zone = two_light_zone([evening(adjust_by_lux=True)], lux=None)
    occupy(zone)
    with caplog.at_level(logging.WARNING, logger="test.zone"):
        assert zone.desired_levels(NOW) == {201: 60, 202: 30}
        zone.desired_levels(NOW)
    assert len(caplog.records) == 1
    assert "adjust_by_lux" in caplog.records[0].getMessage()


def test_the_summary_lists_every_light_in_id_order():
    zone = occupy(two_light_zone())
    assert zone.desired_summary(NOW) == "201=60, 202=30"


# ---------------------------------------------------------------- the timers


def test_next_wake_takes_the_earliest_of_the_four():
    zone = occupy(two_light_zone())  # hold expiry 20:05
    assert zone.next_wake(NOW) == at(minutes=5)


def test_next_wake_falls_through_to_the_period_boundary():
    zone = two_light_zone()  # nothing seen, no override: 23:00 ends Evening
    assert zone.next_wake(NOW) == dt.datetime(2026, 9, 4, 23, 0)


def test_next_wake_falls_through_to_midnight_for_the_counters():
    zone = make_zone(
        [make_period("Overnight", "00:00", "23:00")], logger=logging.getLogger("test.zone")
    )
    late = dt.datetime(2026, 9, 4, 23, 30)
    assert zone.next_wake(late) == dt.datetime(2026, 9, 5, 0, 0)


def test_next_wake_prefers_an_override_expiry_when_it_is_soonest():
    zone = occupy(two_light_zone())
    zone.ingest_presence(101, True, at(minutes=10))  # hold now expires 20:15
    zone.start_override(201, NOW)
    zone.override.expires_at = at(minutes=2)
    assert zone.next_wake(at(minutes=1)) == at(minutes=2)


def test_next_wake_never_answers_with_a_moment_that_has_passed():
    zone = occupy(two_light_zone())
    assert zone.next_wake(at(minutes=30)) > at(minutes=30)


# -------------------------------------------------------------- the counters


def test_evaluations_are_counted_and_the_trigger_recorded():
    zone = occupy(two_light_zone())
    zone.evaluate(at(seconds=10), "reconcile tick")
    assert zone.evaluations_today == 2
    assert zone.last_trigger == "reconcile tick"


def test_overrides_are_counted():
    zone = occupy(two_light_zone())
    zone.start_override(201, at(minutes=1))
    assert zone.overrides_today == 1


def test_the_counters_reset_at_local_midnight():
    zone = occupy(two_light_zone())
    zone.start_override(201, at(minutes=1))
    zone.writes_today = 7
    assert (zone.evaluations_today, zone.writes_today, zone.overrides_today) == (1, 7, 1)

    tomorrow = dt.datetime(2026, 9, 5, 0, 0, 1)
    zone.evaluate(tomorrow, "midnight")
    assert (zone.evaluations_today, zone.writes_today, zone.overrides_today) == (1, 0, 0)


def test_the_counters_do_not_reset_inside_a_day():
    zone = occupy(two_light_zone())
    zone.evaluate(dt.datetime(2026, 9, 4, 23, 59, 59), "late tick")
    assert zone.evaluations_today == 2


# ------------------------------------------------------- explain and snapshot


def test_explain_names_the_state_the_reason_and_the_inputs():
    zone = occupy(two_light_zone())
    line = zone.explain(NOW)
    assert line.count("\n") == 0
    assert "Study is occupied" in line
    assert "someone is here" in line
    assert "period=Evening" in line
    assert "presence=active" in line
    assert "lux=1800" in line
    assert "Last trigger: setup" in line


def test_explain_names_the_off_duty_cause_as_well_as_describing_it():
    """"Off duty" is not an answer anybody can act on: one cause turns the
    lights off and two leave them alone, so the line has to say which."""
    zone = two_light_zone()
    zone.ingest_lux(3000, NOW)
    zone.evaluate(NOW, "lux edge")
    line = zone.explain(NOW)
    assert "the room is bright" in line
    assert "off-duty cause: bright" in line

    zone.set_enabled(enabled=False)
    zone.evaluate(at(minutes=1), "disabled")
    line = zone.explain(at(minutes=1))
    assert "the zone is off" in line
    assert "off-duty cause: disabled" in line

    zone.set_enabled(enabled=True)
    zone.ingest_lux(1800, at(minutes=2))
    after_hours = dt.datetime(2026, 9, 4, 23, 30)
    zone.evaluate(after_hours, "period boundary")
    line = zone.explain(after_hours)
    assert "no period covers this time" in line
    assert "off-duty cause: no_period" in line


def test_explain_names_the_override_and_who_took_it():
    zone = occupy(two_light_zone())
    zone.start_override(201, at(minutes=1))
    zone.evaluate(at(minutes=1), "override started")
    line = zone.explain(at(minutes=1))
    assert "device 201 took it over" in line
    assert "override=201" in line


def test_explain_says_unreadable_rather_than_printing_a_number():
    zone = two_light_zone()
    zone.ingest_lux(None, NOW)
    zone.evaluate(NOW, "lux unreadable")
    assert "UNREADABLE" in zone.explain(NOW)


def test_explain_says_stale_rather_than_looking_like_a_live_sensor():
    zone = occupy(two_light_zone())
    much_later = at(hours=2)
    zone.evaluate(much_later, "tick")
    assert "STALE" in zone.explain(much_later)


def test_explain_names_lights_that_could_not_be_resolved():
    zone = occupy(two_light_zone())
    make_device(201, "dimmer")  # 202 is not in indigo.devices
    zone.resolve_lights()
    assert "unavailable lights=202" in zone.explain(NOW)


def test_the_snapshot_is_only_strings_numbers_and_booleans():
    zone = occupy(two_light_zone())
    zone.start_override(201, NOW)
    zone.evaluate(NOW, "override started")
    states = zone.snapshot()

    assert set(states) == {
        "state",
        "presence_active",
        "presence_last_seen",
        "lux",
        "dark",
        "period",
        "override_device",
        "override_expires",
        "desired_summary",
        "explain",
        "evaluations_today",
        "writes_today",
        "overrides_today",
        "last_trigger",
        "off_duty_cause",
        "periods_today",
        "presence_inputs",
    }
    for key, value in states.items():
        assert isinstance(value, (str, int, float, bool)), key

    assert states["state"] == "overridden"
    assert states["presence_last_seen"] == "2026-09-04T20:00:00"
    assert states["override_device"] == 201
    assert states["dark"] is True
    assert states["period"] == "Evening"
    assert states["off_duty_cause"] == ""  # overridden, not off-duty


def test_an_absent_value_is_the_empty_string_never_a_zero():
    """R15: a lux of 0.0 published for a sensor that was never read is a
    pitch-dark room to anything that reads the state."""
    zone = two_light_zone()
    zone.evaluate(NOW, "startup")
    states = zone.snapshot()
    assert states["lux"] == ""
    assert states["presence_last_seen"] == ""
    assert states["override_device"] == ""
    assert states["override_expires"] == ""


# --------------------------------------------------------------- off_duty_cause


def test_off_duty_cause_is_empty_when_not_off_duty():
    """Mutant: publishing the last-known cause instead of clearing it once
    the zone leaves OFF-DUTY -- the status page would show a stale badge."""
    zone = occupy(two_light_zone())
    assert zone.state is ZoneState.OCCUPIED
    assert zone.snapshot()["off_duty_cause"] == ""


def test_off_duty_cause_names_bright_no_period_and_disabled():
    zone = two_light_zone()
    zone.ingest_lux(9000, NOW)  # above dark_below (2200) + hysteresis (300): bright
    zone.evaluate(NOW, "daylight")
    assert zone.snapshot()["off_duty_cause"] == "bright"

    zone = two_light_zone(periods=[evening()])
    zone.ingest_lux(1800, NOW)
    zone.evaluate(dt.datetime(2026, 9, 4, 2, 0, 0), "no period covers 02:00")
    assert zone.snapshot()["off_duty_cause"] == "no_period"

    zone = two_light_zone()
    zone.set_enabled(enabled=False)
    zone.evaluate(NOW, "disabled")
    assert zone.snapshot()["off_duty_cause"] == "disabled"


# --------------------------------------------------------------- periods_today


def test_periods_today_resolves_a_sunset_relative_period_to_todays_clock_time():
    """The whole reason this state exists: `period_window` does the sun math
    the 2026.3.0 page could not, so a caller sees an actual HH:MM rather than
    the "sunset-30m" expression. FixedSun's default sunset is 19:45."""
    zone = two_light_zone(
        periods=[make_period("Dusk", "sunset-30m", "22:00", levels={"201": 50, "202": 50})]
    )
    zone.evaluate(NOW, "setup")
    periods = json.loads(zone.snapshot()["periods_today"])

    assert periods == [{"name": "Dusk", "from": "19:15", "to": "22:00", "mode": "on_and_off"}]


def test_periods_today_emits_a_midnight_crossing_period_with_to_before_from():
    """Mutant: normalising the wrap so `to` reads later than `from` -- the
    page's own wrap-handling (bandSegments) would then draw a backwards band
    that never triggers, or two full-width ones."""
    zone = two_light_zone(
        periods=[make_period("Night", "22:00", "06:00", levels={"201": 10, "202": 10})]
    )
    zone.evaluate(dt.datetime(2026, 9, 4, 23, 0, 0), "setup")
    periods = json.loads(zone.snapshot()["periods_today"])

    assert periods == [{"name": "Night", "from": "22:00", "to": "06:00", "mode": "on_and_off"}]
    assert periods[0]["to"] < periods[0]["from"]


def test_periods_today_includes_hold_seconds_and_limit_only_when_the_period_sets_them():
    zone = two_light_zone(
        periods=[
            make_period(
                "Evening", "18:00", "23:00", levels={"201": 60, "202": 30},
                hold_seconds=900, limit=80,
            )
        ]
    )
    zone.evaluate(NOW, "setup")
    period = json.loads(zone.snapshot()["periods_today"])[0]
    assert period["hold_seconds"] == 900
    assert period["limit"] == 80

    plain = two_light_zone()  # evening() sets neither
    plain.evaluate(NOW, "setup")
    plain_period = json.loads(plain.snapshot()["periods_today"])[0]
    assert "hold_seconds" not in plain_period
    assert "limit" not in plain_period


def test_periods_today_is_the_empty_list_for_a_zone_with_no_periods():
    # The schema requires at least one period on load (minItems: 1), so the
    # empty case is built by hand: ZoneConfig is frozen, and this is the
    # only way to reach it without going through the loader.
    zone = two_light_zone()
    zone.config = dataclasses.replace(zone.config, periods=())
    zone.evaluate(NOW, "setup")
    assert zone.snapshot()["periods_today"] == "[]"


def test_periods_today_publishes_unavailable_and_warns_once_when_the_sun_fails(caplog):
    """A failed sun lookup must not read as "this zone has no periods" (R15):
    that is a real, different answer and the two must not collapse into the
    same "[]" on the wire. Mutant: swallowing the exception into "[]".

    The zone is built and evaluated with a working sun first -- `evaluate`
    itself resolves the active period through the same `self.sun`, and a
    broken sun from the start would take that down too, which is not what
    this test is about. The sun is only broken afterwards, so only
    `periods_today`'s own resolution -- memoised per date, but `evaluate()`
    never fills that memo, so the first direct call here computes fresh --
    sees the failure.
    """

    class BrokenSun:
        def sunrise(self, date):
            raise RuntimeError("indigo.server.calculateSunrise failed")

        def sunset(self, date):
            raise RuntimeError("indigo.server.calculateSunrise failed")

    # A clock-only period (like evening()) never asks the sun anything --
    # resolve() only calls it for a sunrise/sunset expression -- so this
    # needs a period that actually depends on it.
    zone = two_light_zone(
        periods=[make_period("Dusk", "sunset-30m", "22:00", levels={"201": 50, "202": 50})]
    )
    zone.evaluate(NOW, "setup")
    working_sun = zone.sun
    zone.sun = BrokenSun()

    # `_periods_today_json` directly: `snapshot()` also resolves the active
    # period through the same `self.sun` for `period`/`desired_summary`, and
    # in production those never see a raw exception -- the real sun
    # (`periods.IndigoSun`) already catches every failure itself and falls
    # back to a fixed time. This method's own try/except is what still has
    # to hold if something else is ever handed in as the sun.
    with caplog.at_level(logging.WARNING):
        first = zone._periods_today_json(NOW)
        second = zone._periods_today_json(NOW)

    assert first == "unavailable"
    assert second == "unavailable"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    # Two records on the *first* occurrence -- the summary line and a
    # `exc_info=True` traceback, so a raised exception here (a programming
    # error; the real sun never raises -- see the docstring) is not a single
    # trace-less line -- and neither repeats on the second call.
    assert len(warnings) == 2
    assert warnings[1].exc_info is not None

    # A failure must never be memoised: the same-day cache is only ever
    # written on the success path (see `_periods_today_cache = (today, text)`
    # above the exception), so restoring a working sun on the SAME date has
    # to produce real JSON on the very next ask, not a cached "unavailable"
    # ridden out until midnight. Mutant: caching `(today, "unavailable")` in
    # the except branch.
    zone.sun = working_sun
    third = zone._periods_today_json(NOW)
    assert third != "unavailable"
    parsed = json.loads(third)
    assert parsed and parsed[0]["name"] == "Dusk"


def test_periods_today_marks_every_entry_approximate_and_excludes_the_day_from_the_memo():
    """A sun that fell back (`fell_back_on` True) must not publish its
    resolved times as though they were real: every entry gets
    `"approximate": true`, and the day must not be memoised, so a server
    that recovers mid-day is re-asked on the very next call instead of
    riding out the fallback until midnight (A2).

    Kills: publishing the resolved times unmarked, and caching the
    approximate answer in `_periods_today_cache`.
    """
    from helpers import FixedSun

    class FellBackSun(FixedSun):
        def __init__(self):
            super().__init__()
            self.sunset_calls = 0

        def sunset(self, date):
            self.sunset_calls += 1
            return super().sunset(date)

        def fell_back_on(self, date):
            return True

    sun = FellBackSun()
    zone = two_light_zone(
        periods=[make_period("Dusk", "sunset-30m", "22:00", levels={"201": 50, "202": 50})],
        sun=sun,
    )
    zone.evaluate(NOW, "setup")

    first = json.loads(zone._periods_today_json(NOW))
    assert first and all(entry["approximate"] is True for entry in first)

    calls_after_first = sun.sunset_calls
    zone._periods_today_json(NOW)
    # Not memoised: a second call on the same date asks the sun again.
    assert sun.sunset_calls > calls_after_first


def test_periods_today_treats_a_sun_without_fell_back_on_as_never_falling_back():
    """Older/test suns -- `helpers.FixedSun` among them -- do not implement
    `fell_back_on` at all. Its absence must read as "no fallback happened",
    not raise and not mark every entry approximate by accident.

    Kills: calling `self.sun.fell_back_on(...)` unconditionally, which would
    raise `AttributeError` against a sun that predates this attribute.
    """
    zone = two_light_zone(
        periods=[make_period("Dusk", "sunset-30m", "22:00", levels={"201": 50, "202": 50})]
    )
    zone.evaluate(NOW, "setup")
    entries = json.loads(zone._periods_today_json(NOW))
    assert entries and all("approximate" not in entry for entry in entries)


# ------------------------------------------------------------ presence_inputs


def test_presence_inputs_lists_devices_and_variables_with_on_and_last():
    import indigo

    indigo.variables[9001] = indigo.Variable(9001, name="SimonHome", value="true")
    zone = two_light_zone(presence_devices=[101, 102], presence_variables=[9001])

    # 9001 activates first (nothing else on: an ACTIVATED edge), then 101
    # merely refreshes an already-occupied room -- an any-of REFRESHED edge,
    # not a fresh activation -- so 9001 is the input that "last" mattered.
    zone.ingest_presence(9001, True, NOW)
    zone.ingest_presence(101, True, NOW)
    # 102 never reports at all: it must stay unknown, not read as "off".
    zone.evaluate(NOW, "setup")

    inputs = {entry["id"]: entry for entry in json.loads(zone.snapshot()["presence_inputs"])}

    assert inputs[101] == {"id": 101, "kind": "device", "on": True, "last": False}
    assert inputs[102] == {"id": 102, "kind": "device", "on": None, "last": False}
    assert inputs[9001] == {
        "id": 9001, "kind": "variable", "on": True, "last": True, "name": "variable SimonHome",
    }


def test_presence_inputs_on_is_none_until_an_input_is_actually_ingested():
    """Mutant: defaulting an un-ingested input's `on` to False, which reads
    identically to "we asked and it said off" on the status page."""
    zone = two_light_zone(presence_devices=[101, 102])
    zone.evaluate(NOW, "setup")
    inputs = {entry["id"]: entry for entry in json.loads(zone.snapshot()["presence_inputs"])}
    assert inputs[101]["on"] is None
    assert inputs[102]["on"] is None


# ---------------------------------------------------------- resolving lights


def test_resolve_lights_separates_gone_from_broken(monkeypatch):
    import indigo

    class Broken(dict):
        def __getitem__(self, key):
            if key == 202:
                raise RuntimeError("Indigo server is not responding")
            return super().__getitem__(key)

    live = make_device(201, "dimmer")
    monkeypatch.setattr(indigo, "devices", Broken({201: live}))

    zone = two_light_zone(lights=[201, 202, 203])
    found = zone.resolve_lights()

    assert found.live == {201: live}
    assert found.gone == (203,)
    assert found.failed == (202,)


def test_resolve_lights_forgets_the_warning_when_a_device_comes_back(caplog):
    zone = two_light_zone()
    make_device(201, "dimmer")

    with caplog.at_level(logging.WARNING, logger="test.zone"):
        zone.resolve_lights()
        zone.resolve_lights()
        assert len(caplog.records) == 1  # 202 is gone, warned once
        make_device(202, "dimmer")
        zone.resolve_lights()
        del __import__("indigo").devices[202]
        zone.resolve_lights()
        assert len(caplog.records) == 2  # ...and warned again after it returned


# ---------------------------------------------------- the dark_below variable


def test_the_dark_below_variable_overrides_the_file():
    import indigo

    indigo.variables[55] = indigo.Variable(55, value="1000")
    zone = two_light_zone(lux={**LUX, "dark_below_variable_id": 55})
    assert zone.dark_below() == 1000.0
    zone.ingest_lux(1800, NOW)
    assert zone.is_dark() is False  # 1800 is above the variable's 1000


@pytest.mark.parametrize("stored", ["", "bright", None])
def test_an_unparseable_variable_warns_once_and_uses_the_configured_number(stored, caplog):
    import indigo

    indigo.variables[55] = indigo.Variable(55, value=stored)
    zone = two_light_zone(lux={**LUX, "dark_below_variable_id": 55})

    with caplog.at_level(logging.WARNING, logger="test.zone"):
        assert zone.dark_below() == 2200
        assert zone.dark_below() == 2200
    assert len(caplog.records) == 1
    assert "2200" in caplog.records[0].getMessage()


def test_a_missing_variable_and_a_broken_lookup_say_different_things(caplog, monkeypatch):
    zone = two_light_zone(lux={**LUX, "dark_below_variable_id": 55})
    with caplog.at_level(logging.WARNING, logger="test.zone"):
        assert zone.dark_below() == 2200
    assert "does not exist" in caplog.records[0].getMessage()

    compare.reset_warnings()
    caplog.clear()

    import indigo

    class Broken(dict):
        def __getitem__(self, key):
            raise RuntimeError("Indigo server is not responding")

    monkeypatch.setattr(indigo, "variables", Broken())
    with caplog.at_level(logging.WARNING, logger="test.zone"):
        assert zone.dark_below() == 2200
    message = caplog.records[0].getMessage()
    assert "not the variable being gone" in message


def test_a_zone_with_no_lux_block_is_always_dark_enough():
    zone = make_zone([evening()], lights=[201, 202], lux=None)
    assert zone.is_dark() is True
    assert zone.ingest_lux(50_000, NOW) is False


# ------------------------------------------------------------- the dry run
#
# `dry_run` answers "what would this zone decide at 22:00" from the inputs it
# holds now. The whole value of it is that asking does not change the answer,
# and three of the pieces it needs are hazardous asked the ordinary way: the
# Schmitt trigger advances when you read it, `_age_override` releases a lock
# when you age it, and `desired_levels` reads the state the zone is in. These
# pin the read-only twins to the originals.


def test_a_dry_run_does_not_seed_the_schmitt_trigger():
    """Kills: `would_be_dark()` -> `is_dark()` inside Zone.dry_run.

    `Lux.dark()` writes the verdict it reaches, so a dry run that called it
    would leave the band held against a verdict no evaluation ever made --
    and the trigger is one-sided, so the very next reading inside the band
    would then hold a dark this zone never entered. Asking a question would
    have changed the answer to the next one.
    """
    zone = two_light_zone()  # a lux gate, and nothing read yet
    assert zone.lux.verdict is None

    zone.dry_run(NOW)

    assert zone.lux.verdict is None, "the dry run moved the trigger"

    # And the consequence, in case the verdict is ever seeded some other way:
    # 2400 is inside the 2200-2500 band, so with no verdict yet it is not
    # dark. A seeded True would hold, and the room would be dark all evening.
    zone.ingest_lux(2400, NOW)
    zone.evaluate(NOW, "first reading")
    assert zone.is_dark() is False


def test_a_dry_run_neither_evaluates_nor_writes_nor_counts():
    """Kills: implementing dry_run as `evaluate()` and reading the result.

    That would move the state machine, the counters and the last trigger --
    and reconcile writes from `desired_levels`, so a zone put into VACANT by
    somebody *asking* about midnight turns the lights off now.
    """
    zone = occupy(two_light_zone())
    state, evaluations, trigger = zone.state, zone.evaluations_today, zone.last_trigger

    zone.dry_run(dt.datetime(2026, 9, 5, 2, 0))

    assert zone.state is state
    assert zone.evaluations_today == evaluations
    assert zone.last_trigger == trigger
    assert zone._evaluated_at == NOW


def test_a_dry_run_resolves_the_period_and_the_levels_at_the_instant_asked():
    """Kills: resolving the period against `now` and ignoring `at`."""
    zone = occupy(
        two_light_zone(
            periods=[
                evening(),
                make_period("Overnight", "23:00", "06:00", levels={"201": 5, "202": 5}),
            ]
        )
    )

    inside_evening = zone.dry_run(NOW)
    assert inside_evening.period == "Evening"
    assert inside_evening.levels == {201: 60, 202: 30}

    # Two hours after the presence hold expired, in the next band along.
    overnight = zone.dry_run(dt.datetime(2026, 9, 4, 23, 30))
    assert overnight.period == "Overnight"
    assert overnight.state is ZoneState.VACANT
    assert overnight.levels == {201: "off", 202: "off"}
    assert "would be vacant at 2026-09-04T23:30" in overnight.text
    assert "Overnight" in overnight.text


@pytest.mark.parametrize(
    "when,presence_active,unlock_on_leave,extend_minutes",
    [
        (dt.timedelta(minutes=1), True, True, 0),  # inside the hold
        (dt.timedelta(minutes=61), False, False, 0),  # expired, room empty
        (dt.timedelta(minutes=61), True, False, 30),  # expired, extends
        (dt.timedelta(minutes=61), True, False, 0),  # expired, no extension
        (dt.timedelta(minutes=6), False, True, 0),  # unlock on leave
        (dt.timedelta(minutes=6), False, False, 0),  # unlock on leave, off
    ],
)
def test_asking_whether_an_override_holds_agrees_with_ageing_it(
    when, presence_active, unlock_on_leave, extend_minutes
):
    """The two must never disagree, or a dry run describes a different zone.

    `override_holds_at` is `_age_override` with the writes taken out, so it
    is pinned against the real thing rather than against a table of expected
    answers: a table would have to be re-derived every time R10 moves, and
    the failure mode is silent.

    Kills: any edge of `_age_override` that `override_holds_at` gets wrong --
    the unlock-on-leave release, the expiry, or the extension.
    """
    override = {
        "unlock_on_leave": unlock_on_leave,
        "duration_minutes": 60,
        "extend_minutes": extend_minutes,
    }
    asked, aged = (occupy(two_light_zone(override=override)) for _ in range(2))
    for zone in (asked, aged):
        zone.start_override(201, NOW)

    answer = asked.override_holds_at(NOW + when, presence_active)
    aged._age_override(NOW + when, presence_active)

    assert answer is (aged.override is not None)
    assert asked.override is not None, "asking released the lock"


def test_periods_today_asks_the_sun_once_per_date_not_once_per_snapshot():
    """snapshot() runs on every evaluation and the sun answer cannot move
    before midnight, so a second snapshot on the same date must come from the
    memo. Kills the mutation that drops the cache: with it gone the sun is
    asked again and this count doubles."""
    import datetime as _dt

    from helpers import FixedSun

    class CountingSun(FixedSun):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def sunset(self, date):
            self.calls += 1
            return super().sunset(date)

    sun = CountingSun()
    zone = two_light_zone(
        periods=[make_period("Dusk", "sunset-30m", "22:00", levels={"201": 50, "202": 50})],
        sun=sun,
    )
    zone.evaluate(NOW, "setup")
    # `_periods_today_json` directly rather than snapshot(): snapshot() also
    # resolves the active period and the plan, which ask the sun on their
    # own account and would hide the memo behind their calls.
    first = zone._periods_today_json(NOW)
    calls_after_first = sun.calls
    assert calls_after_first >= 1
    second = zone._periods_today_json(NOW)

    assert second == first
    assert sun.calls == calls_after_first

    # A new date is a new answer: the memo is keyed by date, not "forever".
    zone._periods_today_json(NOW + _dt.timedelta(days=1))
    assert sun.calls > calls_after_first
