"""Manual-override promises (PRD R1-R3, R5-R8, R10; section 7 must-haves).

The fork's numbering is kept in brackets so a promise can be traced back to
the test that proved it in `indigo-auto-lights/tests/`. Every docstring names
the promise in one sentence and then the mutation it must kill -- the wrong
implementation that would still pass a happy-path suite. Attempt 1 at this
rule (fork #15) looked fine against one.

M0 ships these as stubs. M1 replaces each body with the real test AND applies
the named mutation to confirm the test actually fails under it.
"""

import pytest

# Every stub carries this mark. strict=True means the first stub that starts
# passing FAILS the suite: M1 lands promise by promise, and no stub can be
# quietly satisfied by an unrelated change.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)


# ------------------------------------------------------- the transition rule


@promise
def test_override_survives_a_concurrent_revert():
    """[M1] An override created by a device change stands even though the
    plugin's own revert has already put the light back.

    Kills: judge the zone by re-reading live device state. The override erases
    its own evidence -- by the time anything re-reads, the light is back at
    desired -- so only the transition carried in the event can see it.
    """
    raise NotImplementedError


@promise
def test_own_ramp_never_overrides():
    """[M2] The plugin's own ramp toward a desired level never creates an
    override.

    Kills: judge the new state alone. Every intermediate step of a ramp is off
    desired; only the before-state tells them apart from a dial move, and the
    before-state of our own write is always off desired because we only ever
    command a device that is not already there (R2).
    """
    raise NotImplementedError


@promise
def test_late_reporter_never_overrides():
    """[M3] A device reporting from an already-off-desired state cannot create
    an override, however late the report arrives.

    Kills: drop the before-state check. The 50 s round-trip reporter (R6) is
    the reason: its reports arrive against a history that is already off
    desired, so nothing changed hands.
    """
    raise NotImplementedError


@promise
def test_turn_on_flash_never_overrides():
    """[M4] A lamp that flashes to full on its way to a dim level does not
    create an override.

    Kills: treat any jump away from desired as manual. The before-state is 0,
    already off desired, so the flash is not a transition off desired (R7).
    """
    raise NotImplementedError


@promise
def test_sibling_mid_write_cannot_override_this_device():
    """[M5] A sibling light still mid-write must not create an override on the
    device that actually reported.

    Kills: restore the whole-zone live check ("is anything off desired right
    now?"). The rule is per device.
    """
    raise NotImplementedError


@promise
def test_genuine_change_still_overrides_while_sibling_mid_write():
    """[M5 sibling] The per-device rule narrows the question; it does not go
    blind.

    Kills: fixing M5 by suppressing override detection whenever any device in
    the zone is off desired. A real dial move on the reporting device must
    still land.
    """
    raise NotImplementedError


@promise
def test_override_lands_while_our_own_write_is_in_flight():
    """[M6] A manual change that arrives during the plugin's own write burst
    creates an override.

    Kills: keep a `writing -> ignore events` guard. That guard existed to stop
    self-locking, which the transition rule now prevents by construction, but
    it discarded real input at exactly the moment a person is most likely to
    act -- with the lights visibly moving.
    """
    raise NotImplementedError


@promise
def test_relay_switched_off_from_desired_overrides():
    """[M7] Relays go through the same rule against onState.

    Kills: only judge dimmers. Off->on from rest is our own work (before-state
    off desired); on->off from the settled desired state is a person at the
    switch.
    """
    raise NotImplementedError


@promise
def test_relay_with_desired_off_overrides_when_switched_on():
    """[M7 polarity] A relay whose desired level is `off` creates an override
    when a person switches it ON.

    Kills: treat "on" as at-desired and "off" as off-desired. Every other
    relay case has a desired of on, so that mutation passes them all.
    """
    raise NotImplementedError


@promise
def test_readback_inside_the_band_is_not_a_change():
    """[M8] The override rule uses the same proportional band as the send
    path: max(1, ceil(target x 0.10)) (R5).

    Kills: exact equality. zigbee2mqtt truncates 30 to 29 and a group dimmer
    reads back 45..48 for 50; under equality every one of those is a manual
    override.
    """
    raise NotImplementedError


@promise
def test_band_is_exact_at_zero_and_one_hundred_and_a_real_move_overrides():
    """[M8 edges] 0 and 100 compare exactly, and a move outside the band still
    creates an override.

    Kills: widen the band until nothing counts, and: apply the proportional
    band at the ends, where 100 would accept 90 as "on" and 0 would accept 1
    as "off".
    """
    raise NotImplementedError


@promise
def test_no_before_state_means_no_override_and_a_warning():
    """[M9] An event with no before-state is not judged at all, and says so.

    Kills: fall back to judging the new value alone when the before-state is
    missing -- which is exactly the M2 mutation. The skipped judgement must
    reach the log; a silent skip is the fork's level-5 fall-through (R8, R15).
    """
    raise NotImplementedError


# ------------------------------------------------------- who cannot override


@promise
def test_excluded_device_never_overrides():
    """[M10a] A device in the zone's `override.exclude` never creates an
    override, but is still commanded normally (R6).

    Kills: implement exclusion by dropping the device from the plan, which
    would also stop the plugin driving it.
    """
    raise NotImplementedError


@promise
def test_zone_with_override_disabled_never_locks():
    """[M10b] A zone with `override.enabled: false` never locks -- the Hallway
    (R10).

    Kills: honour the flag only on the timing path, so the zone still enters
    OVERRIDDEN and simply expires quickly.
    """
    raise NotImplementedError


@promise
def test_device_with_no_level_in_this_period_never_overrides():
    """[M10c] A light the active period gives no level -- absent, or `leave`
    -- has nothing to compare against and cannot create an override.

    Kills: default a missing level to off, which makes every such device
    permanently off desired.
    """
    raise NotImplementedError


@promise
def test_disabled_zone_never_overrides():
    """[M10d] A disabled zone, or one under a disabled controller, records no
    override.

    Kills: check the enable flag only at write time, so a zone accumulates
    overrides while off and acts on them the moment it is re-enabled.
    """
    raise NotImplementedError


# --------------------------------------------------------- the echo window


@promise
def test_echo_after_the_desired_level_reverts_is_excused_once():
    """[M12a] An echo of our own command that arrives after the desired level
    has moved back onto the device's pre-command state is still ours, and is
    excused exactly once (R3).

    Kills: match echoes on the value we commanded rather than the value we
    commanded the device AWAY from. In-room lux rises, the zone stops being
    dark, the lights are commanded off, and the queued on-echo then reads as a
    manual override.
    """
    raise NotImplementedError


@promise
def test_ramp_out_of_the_recorded_state_needs_no_excuse():
    """[M12b] A dimmer ramping out of the recorded pre-command state reports
    an intermediate value, not the commanded one, and needs no excuse.

    Kills: consume the recorded state on any event from that device, which
    spends the one excuse on a step that never needed it and leaves the real
    echo unexcused.
    """
    raise NotImplementedError


@promise
def test_manual_move_onto_the_recorded_state_still_overrides():
    """[M12c] The excuse covers transitions OUT of the recorded state, not
    transitions onto it.

    Kills: excuse any event that touches the recorded value. A person dialling
    a light back to exactly where it was before our command is still a person.
    """
    raise NotImplementedError


@promise
def test_the_echo_excuse_expires_with_the_window():
    """[M12d] The excuse is time-boxed by `echo_window_seconds`: an old
    command cannot cover a new change.

    Kills: keep pre-command states until they are used. The documented cost of
    this rule is that one manual change inside the window is swallowed; the
    window and the single-use rule are the two things that bound it.
    """
    raise NotImplementedError


# --------------------------------------------------- holding and releasing


@promise
def test_override_extends_while_presence_is_active():
    """An override that expires with the room still occupied extends by
    `extend_minutes` instead of ending (R10).

    Kills: read presence from a cached value captured when the override was
    created, so a room still in use loses its override at the first expiry.
    """
    raise NotImplementedError


@promise
def test_lock_zone_action_creates_an_override_without_a_device_change():
    """The `lock zone` action creates an override with no device event behind
    it (PRD section 5.13, decision 2) -- what scripts wanted from the fork.

    Kills: implement it by writing a light and letting the transition rule
    notice, which both moves the lights and depends on the write landing.
    """
    raise NotImplementedError


@promise
def test_an_overridden_zone_writes_nothing():
    """In OVERRIDDEN the desired level IS whatever the devices are: the zone
    issues no commands at all, including at the reconcile tick (section 5.3).

    Kills: keep planning in OVERRIDDEN and merely suppress the write, which
    leaves the reconcile tick free to "fix" the person's level.
    """
    raise NotImplementedError
