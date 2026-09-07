"""The bundled status page: install/update it into Web Assets on startup and
on every prefs save with "Manage the status page" ticked.

Mirrors indigo-unifi-protect's cameras.html plumbing (issue #27 there). The
question, per workspace convention: how many distinct ways can this degrade,
and does each one stay non-fatal and say so in the Event Log?
"""

import logging
from pathlib import Path

import indigo
import pytest

import plugin as plugin_module


def make_plugin(managePage=True, **extra_prefs):
    prefs = {"log_level": logging.INFO, "managePage": managePage}
    prefs.update(extra_prefs)
    return plugin_module.Plugin(plugin_module.PLUGIN_ID, "Lamplighter", "2026.3.0", prefs)


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A fake Indigo installation folder the plugin will write into."""
    monkeypatch.setattr(indigo.server, "getInstallFolderPath", lambda: str(tmp_path))
    return tmp_path


def _bundle_source_path(install_dir):
    return (
        Path(install_dir) / "Plugins" / "Lamplighter.indigoPlugin" / "Contents"
        / "Resources" / "pages" / "lamplighter.html"
    )


def _installed_dest_path(install_dir):
    return Path(install_dir) / "Web Assets" / "static" / "pages" / "lamplighter.html"


def test_the_status_page_is_installed_into_web_assets_on_startup(install, monkeypatch, caplog):
    """Drives the real `startup()`, not `_sync_web_page()` directly -- this is
    what pins the call actually being made from startup, not merely that the
    method works when called by hand."""
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>v1</html>")
    dest = _installed_dest_path(install)
    assert not dest.exists()

    plug = make_plugin(managePage=True)
    with caplog.at_level("INFO"):
        plug.startup()

    assert dest.read_bytes() == b"<html>v1</html>"
    infos = [r for r in caplog.records
             if r.levelname == "INFO" and "Installed/updated" in r.getMessage()]
    assert len(infos) == 1


def test_an_up_to_date_page_is_not_rewritten(install, monkeypatch, caplog):
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>same</html>")
    dest = _installed_dest_path(install)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>same</html>")

    calls = []
    real_replace = plugin_module.os.replace
    monkeypatch.setattr(
        plugin_module.os, "replace",
        lambda *a, **k: (calls.append(a) or real_replace(*a, **k)),
    )

    plug = make_plugin(managePage=True)
    with caplog.at_level("DEBUG"):
        plug._sync_web_page()

    assert calls == [], "os.replace must not be called when the page is already current"
    assert dest.read_bytes() == b"<html>same</html>"
    debugs = [r for r in caplog.records
              if r.levelname == "DEBUG" and "already up to date" in r.getMessage()]
    assert len(debugs) == 1


def test_a_changed_bundle_page_replaces_the_installed_one(install, monkeypatch, caplog):
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>new</html>")
    dest = _installed_dest_path(install)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>old, hand-edited</html>")

    plug = make_plugin(managePage=True)
    with caplog.at_level("INFO"):
        plug._sync_web_page()

    assert dest.read_bytes() == b"<html>new</html>"
    infos = [r for r in caplog.records
             if r.levelname == "INFO" and "Installed/updated" in r.getMessage()]
    assert len(infos) == 1


def test_with_the_pref_off_nothing_is_written_but_a_stale_copy_is_noted(install, monkeypatch, caplog):
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>new</html>")
    dest = _installed_dest_path(install)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>old, hand-edited</html>")

    plug = make_plugin(managePage=False)
    with caplog.at_level("INFO"):
        plug._sync_web_page()

    assert dest.read_bytes() == b"<html>old, hand-edited</html>"
    infos = [r for r in caplog.records
             if r.levelname == "INFO" and str(dest) in r.getMessage()]
    assert len(infos) == 1


def test_a_missing_bundle_page_warns_and_does_not_raise(install, monkeypatch, caplog):
    # install has no Plugins/... tree at all -- the bundle is missing.
    dest = _installed_dest_path(install)
    plug = make_plugin(managePage=True)

    with caplog.at_level("WARNING"):
        plug._sync_web_page()  # must not raise

    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and "not found in the plugin bundle" in r.getMessage()]
    assert len(warnings) == 1
    assert str(dest) in warnings[0].getMessage()
    assert not dest.exists()


def test_an_empty_bundle_page_is_refused(install, monkeypatch, caplog):
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"")
    dest = _installed_dest_path(install)

    plug = make_plugin(managePage=True)
    with caplog.at_level("WARNING"):
        plug._sync_web_page()  # must not raise

    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and "empty" in r.getMessage().lower()]
    assert len(warnings) == 1
    assert not dest.exists()


def test_saving_prefs_with_the_box_ticked_resyncs_the_page(install, monkeypatch, caplog):
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>flip-on</html>")

    plug = make_plugin(managePage=False)
    values = {"log_level": logging.INFO, "managePage": True}

    with caplog.at_level("INFO"):
        plug.closedPrefsConfigUi(values, user_cancelled=False)

    dest = _installed_dest_path(install)
    assert dest.read_bytes() == b"<html>flip-on</html>"


def test_an_unreadable_bundle_page_blames_the_bundle_not_the_destination(install, monkeypatch, caplog):
    """M1: a source that exists but can't be opened (permissions, whatever)
    must not produce the generic destination-blaming WARNING -- the bundle
    is the thing broken here, and the message has to say so."""
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>unreadable</html>")
    dest = _installed_dest_path(install)

    import builtins
    real_open = builtins.open

    def flaky_open(path, *args, **kwargs):
        if str(path) == str(source):
            raise PermissionError("permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    plug = make_plugin(managePage=True)
    with caplog.at_level("WARNING"):
        plug._sync_web_page()  # must not raise

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(source) in message
    assert "cannot be read" in message
    assert "reinstalling the plugin restores it" in message
    assert str(dest) not in message
    assert not dest.exists()


def test_a_programming_error_in_the_page_sync_is_logged_with_a_traceback_not_as_a_copy_failure(
    install, monkeypatch, caplog
):
    """M2: a bug in the sync code itself (not a filesystem problem) must not
    be folded into the friendly "copy it by hand" WARNING -- it needs a
    traceback at ERROR, and startup still has to complete either way."""
    def boom(_install):
        raise TypeError("boom")

    monkeypatch.setattr(plugin_module.Plugin, "_web_page_paths", staticmethod(boom))

    plug = make_plugin(managePage=True)
    with caplog.at_level("DEBUG"):
        plug.startup()  # must not raise

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "sync failed unexpectedly" in errors[0].getMessage()

    copy_warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "copy it by hand" in r.getMessage().lower()
    ]
    assert copy_warnings == []


def test_a_leftover_tmp_file_is_named_in_the_warning(install, monkeypatch, caplog):
    """M3: when the failed write's own cleanup (os.remove on the .tmp) also
    fails, the WARNING must name the leftover file rather than silently
    swallowing the second failure."""
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>new</html>")
    dest = _installed_dest_path(install)

    monkeypatch.setattr(
        plugin_module.os, "replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("replace failed")),
    )
    monkeypatch.setattr(
        plugin_module.os, "remove",
        lambda *a, **k: (_ for _ in ()).throw(OSError("remove failed too")),
    )

    plug = make_plugin(managePage=True)
    with caplog.at_level("WARNING"):
        plug._sync_web_page()  # must not raise

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "partial file was left at" in message
    assert f"{dest}.tmp" in message
    assert "delete it by hand" in message


def test_the_stale_check_does_not_recommend_an_empty_bundle_page(install, monkeypatch, caplog):
    """M4: an empty bundled page is a broken install, not a reason to tell
    the user to copy it over an installed page that's actually fine."""
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"")
    dest = _installed_dest_path(install)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>installed, untouched</html>")

    plug = make_plugin(managePage=False)
    with caplog.at_level("DEBUG"):
        plug._sync_web_page()

    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert infos == []
    debugs = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(debugs) == 1


def test_with_the_pref_off_a_missing_bundle_page_is_still_noted(install, monkeypatch, caplog):
    """M5: a missing bundle is a damaged install regardless of the pref --
    it stays an INFO even with 'Manage the status page' unticked. A missing
    *installed* page, by contrast, is nothing to report at all (DEBUG)."""
    source = _bundle_source_path(install)
    dest = _installed_dest_path(install)
    assert not source.exists()

    plug = make_plugin(managePage=False)
    with caplog.at_level("DEBUG"):
        plug._sync_web_page()

    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1
    assert str(source) in infos[0].getMessage()
    assert "reinstalling the plugin restores it" in infos[0].getMessage()
    assert not dest.exists()
