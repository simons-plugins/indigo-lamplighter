"""The echo book's own mechanics (PRD R3, section 5.7).

`is_manual_override` is pinned promise by promise in
`test_promises_override.py`. This file is about the book underneath it: what
it keeps, what it matches, what it throws away, and what it does when it is
asked about a snapshot it cannot read. The book is a licence to ignore
evidence of a manual change, so its bounds are worth their own tests.
"""

import datetime as dt

from helpers import make_snapshot

from lamplighter.override import HISTORY_PER_DEVICE, EchoBook

NOW = dt.datetime(2026, 9, 4, 20, 0, 0)
WINDOW = 15


def at(**kwargs):
    return NOW + dt.timedelta(**kwargs)


def test_a_record_matches_the_state_the_transition_started_from():
    book = EchoBook()
    book.note_pre_command(201, 0, NOW)

    # The device reports leaving 0 -- at any value, including one nobody
    # commanded.
    age = book.consume_echo(201, make_snapshot(201, brightness=0), at(seconds=3), WINDOW)
    assert age == 3.0
    assert book.pending(201) == ()


def test_a_record_matches_within_the_same_band_the_rule_uses():
    """The recorded state is a reading, so it wobbles like any other (R5)."""
    book = EchoBook()
    book.note_pre_command(201, 50, NOW)

    assert (
        book.consume_echo(201, make_snapshot(201, brightness=47), at(seconds=1), WINDOW)
        is not None
    )


def test_a_relay_record_matches_on_the_bool():
    book = EchoBook()
    book.note_pre_command(203, False, NOW)

    matched = book.consume_echo(
        203,
        make_snapshot(203, device_cls="relay", onState=False),
        at(seconds=1),
        WINDOW,
    )
    assert matched is not None


def test_a_device_with_no_records_is_never_excused():
    book = EchoBook()
    assert book.consume_echo(201, make_snapshot(201, brightness=0), NOW, WINDOW) is None


def test_only_the_matching_record_is_consumed():
    """Two commands in flight, and the echo of one must not spend the other's
    excuse -- a dimmer and a relay in the same zone move at different speeds."""
    book = EchoBook()
    book.note_pre_command(201, 0, NOW)
    book.note_pre_command(201, 60, at(seconds=1))
    assert book.pending(201) == (0, 60)

    assert book.consume_echo(201, make_snapshot(201, brightness=60), at(seconds=2), WINDOW) == 1.0
    assert book.pending(201) == (0,)


def test_records_older_than_the_window_are_pruned_on_the_way_past():
    """The book must not grow. Every lookup prunes what it walks over."""
    book = EchoBook()
    book.note_pre_command(201, 0, NOW)
    book.note_pre_command(201, 88, at(seconds=1))

    assert (
        book.consume_echo(201, make_snapshot(201, brightness=0), at(seconds=WINDOW + 5), WINDOW)
        is None
    )
    assert book.pending(201) == (), "expired records were left to rot"


def test_the_history_is_capped_per_device():
    """A burst longer than the cap is a ramp, and a ramp's later steps need no
    excuse: their before-state is already off desired."""
    book = EchoBook()
    for step in range(HISTORY_PER_DEVICE + 3):
        book.note_pre_command(201, step, NOW)

    assert len(book.pending(201)) == HISTORY_PER_DEVICE
    assert book.pending(201)[-1] == HISTORY_PER_DEVICE + 2, "the newest are kept"


def test_an_unreadable_snapshot_is_not_excused():
    """The strict direction: a snapshot that cannot be read cannot be shown to
    be ours, so the transition rule alone decides. Excusing it would be a
    licence handed out on no evidence at all (R8)."""
    book = EchoBook()
    book.note_pre_command(201, 0, NOW)

    unreadable = make_snapshot(201, brightness=0)
    unreadable.brightness = None

    assert book.consume_echo(201, unreadable, at(seconds=1), WINDOW) is None
    assert book.pending(201) == (0,), "an unmatched lookup must not consume a record"


def test_forget_drops_one_device_or_all_of_them():
    book = EchoBook()
    book.note_pre_command(201, 0, NOW)
    book.note_pre_command(202, 30, NOW)

    book.forget(201)
    assert book.pending(201) == () and book.pending(202) == (30,)

    book.forget()
    assert book.pending(202) == ()
