"""Looking an Indigo object up by id -- the only module that does (R8, R15).

Every ``indigo.devices[...]`` and ``indigo.variables[...]`` in the plugin goes
through here, for one reason: **a lookup has two different failures and they
must never share a handler.**

``KeyError`` means there is no such object. The device was deleted, or the
config names an id from another Indigo database. That is
:class:`DeviceGone`, and the honest response is to drop it from the zone's
working set and say so once.

*Anything else* means the lookup itself failed -- the server is not
answering, a plugin is mid-reload, something is wrong that has nothing to do
with this device. That is :class:`LookupFailed`, and the honest response is
to skip it for this pass and try again, because the device is very probably
still there. Collapsing the two into one ``except Exception`` turns a
transient server problem into a confident "device gone" and quietly shrinks
a zone to the lights that happened to answer.

The two exceptions therefore carry different names, different messages and
different warning keys, so that neither the log nor the code can confuse
them.

Warnings go through :func:`compare.warn_once`, which is keyed and resettable:
one WARNING per condition per object (section 10), and a fresh one after
:func:`forget_warnings` is called because the object came back. A warning
that latches forever hides the second outage as thoroughly as no warning
hides the first.
"""

from __future__ import annotations

import indigo

from . import compare


class DeviceGone(Exception):
    """There is no such object: ``indigo.devices[id]`` raised ``KeyError``.

    ``kind`` is "device" or "variable" -- the two collections this module
    reaches into. The id is carried because every caller's warning has to
    name the thing a person would go and look for.
    """

    def __init__(self, object_id, kind="device"):
        super().__init__(
            f"{kind} {object_id} does not exist in Indigo (the lookup answered, "
            "and the answer was 'no such object')"
        )
        self.object_id = object_id
        self.device_id = object_id
        self.kind = kind


class LookupFailed(Exception):
    """The lookup broke: it raised something that was not ``KeyError``.

    This is emphatically *not* :class:`DeviceGone`. Nothing has been learned
    about whether the object exists, so a caller must treat it as unknown --
    skip this pass, keep the id -- rather than dropping the object.
    """

    def __init__(self, object_id, cause, kind="device"):
        super().__init__(
            f"looking up {kind} {object_id} failed: {type(cause).__name__}: {cause}. "
            "This says nothing about whether it exists."
        )
        self.object_id = object_id
        self.device_id = object_id
        self.kind = kind
        self.cause = cause


def get_device(dev_id):
    """The Indigo device with this id.

    Raises :class:`DeviceGone` if there is no such device and
    :class:`LookupFailed` if the lookup itself broke. ``KeyError`` is caught
    first and deliberately: it is a subclass of ``Exception``, so the order
    of these two clauses *is* the rule this module exists to enforce.
    """
    try:
        return indigo.devices[dev_id]
    except KeyError:
        raise DeviceGone(dev_id) from None
    except Exception as exc:
        raise LookupFailed(dev_id, exc) from exc


def get_variable_value(var_id):
    """The value of the Indigo variable with this id, as Indigo stores it.

    Indigo variable values are strings; parsing one into the number a caller
    wanted is the caller's job, because "this variable does not hold a
    number" is a different condition from "this variable is not there" and
    the two want different messages.
    """
    try:
        variable = indigo.variables[var_id]
    except KeyError:
        raise DeviceGone(var_id, kind="variable") from None
    except Exception as exc:
        raise LookupFailed(var_id, exc, kind="variable") from exc
    return getattr(variable, "value", None)


# ------------------------------------------------------------- the warnings


def gone_key(object_id, kind="device"):
    """The :func:`compare.warn_once` key for "this object does not exist"."""
    return ("gone", kind, object_id)


def lookup_failed_key(object_id, kind="device"):
    """The :func:`compare.warn_once` key for "the lookup itself broke"."""
    return ("lookup-failed", kind, object_id)


def warn_gone_once(logger, dev_id, zone_name, kind="device") -> bool:
    """Warn once that ``dev_id`` no longer exists. True if it logged."""
    return compare.warn_once(
        logger,
        gone_key(dev_id, kind),
        f"{zone_name}: {kind} {dev_id} does not exist in Indigo. It is dropped "
        f"from this zone's working set; the zone keeps running for its other "
        f"{kind}s. Remove the id from the configuration, or restore the {kind}.",
    )


def warn_lookup_failed_once(logger, dev_id, zone_name, cause, kind="device") -> bool:
    """Warn once that looking ``dev_id`` up broke. True if it logged.

    The message says what happened and what it does *not* mean, because the
    whole hazard here is a reader -- human or model -- reading a lookup
    failure as a deleted device and going to look for a device that is fine.
    """
    return compare.warn_once(
        logger,
        lookup_failed_key(dev_id, kind),
        f"{zone_name}: looking up {kind} {dev_id} failed "
        f"({type(cause).__name__}: {cause}). This is NOT the {kind} being gone: "
        f"nothing was learned about whether it exists, so it is skipped this "
        f"pass and kept in the zone.",
    )


def forget_warnings(object_id, kind="device") -> None:
    """Clear both warnings for this object, so a later failure warns afresh.

    Called when the object answers again. Both keys are cleared together
    because either condition clearing means the object is readable now.
    """
    compare.reset_warnings(gone_key(object_id, kind))
    compare.reset_warnings(lookup_failed_key(object_id, kind))
