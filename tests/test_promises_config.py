"""Configuration load, validation and hot reload (PRD R13, R15; section 5.11).

The config is a JSON file an agent edits through MCP tools. It is validated
against the bundled schema with the failing path named, and reloaded on an
mtime change: zone objects are rebuilt from the new config and the persisted
state is then re-applied, so an override created at 19:46 is still an override
after an edit at 19:50.
"""

import pytest

# strict=True: the first stub that starts passing fails the suite.
promise = pytest.mark.xfail(
    strict=True, reason="M1: engine not built", raises=NotImplementedError
)


@promise
def test_an_invalid_config_is_rejected_naming_the_failing_path():
    """A file that fails validation is refused with the JSON path of the
    offending value, and the previously loaded config keeps running (R15).

    Kills: fall back to defaults for the bad field, or load the zones that did
    parse. A partially applied config is the one state nobody can reason
    about, and an MCP caller must get the validation error verbatim.
    """
    raise NotImplementedError


@promise
def test_an_unparseable_file_leaves_the_running_config_in_place():
    """A file that is not JSON at all -- a half-written save -- is refused the
    same way, and the plugin keeps running on what it had.

    Kills: clear the zone list before parsing, which turns a truncated write
    into every light in the house going unmanaged.
    """
    raise NotImplementedError


@promise
def test_hot_reload_preserves_an_active_override():
    """An override held before a reload is still held after it, with its
    original expiry (R13).

    Kills: rebuild zones and let persisted state be re-applied only at plugin
    startup -- the fork's "all locks and zone state has been reset" on every
    single reload.
    """
    raise NotImplementedError


@promise
def test_hot_reload_preserves_presence_last_seen():
    """Presence last-seen survives a reload, so an edit does not turn the
    lights off in an occupied room (R13).

    Kills: re-apply only the override state, which is the half of section 5.10
    that is easy to remember.
    """
    raise NotImplementedError


@promise
def test_an_unknown_device_id_warns_once_and_the_zone_keeps_running():
    """A device id in the config that does not resolve warns once, is dropped
    from that zone's working set, and the zone keeps working for its other
    devices (R8, R15).

    Kills: raise and abort the load, and: skip it silently. A zone quietly
    running on three of its five lights is the failure this promise exists to
    prevent.
    """
    raise NotImplementedError
