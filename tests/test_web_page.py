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
