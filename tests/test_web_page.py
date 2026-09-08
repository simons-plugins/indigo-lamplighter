"""The bundled status page: install/update it into Web Assets on startup and
on every prefs save with "Manage the status page" ticked.

Mirrors indigo-unifi-protect's cameras.html plumbing (issue #27 there). The
question, per workspace convention: how many distinct ways can this degrade,
and does each one stay non-fatal and say so in the Event Log?
"""

import logging
import os
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
    """Any write to the destination -- by any means -- is fatal here, not
    merely unobserved. A spy that only records `os.replace` calls survives a
    mutant that rewrites the page via `open(dest, "wb")` or `shutil.copyfile`
    instead; this makes every write path fatal so none of them survive."""
    import builtins
    import shutil

    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>same</html>")
    dest = _installed_dest_path(install)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>same</html>")

    forbidden_paths = {str(dest), f"{dest}.tmp"}
    write_modes = ("w", "a", "x", "+")
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if str(file) in forbidden_paths and any(flag in mode for flag in write_modes):
            raise AssertionError(f"page must not be written: open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    def guarded_replace(src, dst, *_a, **_k):
        # Only the page's own destination is forbidden here -- startup also
        # writes the (unrelated) history data file on every run, by design,
        # regardless of "Manage the status page" (see
        # test_plugin_wiring.py::test_managepage_off_does_not_stop_the_history_file_being_written).
        if str(dst) in forbidden_paths:
            raise AssertionError("page must not be written: os.replace")

    def guarded_copy(*_a, **_k):
        raise AssertionError("page must not be written: shutil.copy*")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(plugin_module.os, "replace", guarded_replace)
    monkeypatch.setattr(shutil, "copyfile", guarded_copy)
    monkeypatch.setattr(shutil, "copy", guarded_copy)

    plug = make_plugin(managePage=True)
    with caplog.at_level("DEBUG"):
        plug.startup()  # must not raise

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
    assert "differs from the bundled one" in infos[0].getMessage()


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

    # The old filter here looked for "copy it by hand", which the real
    # message never contains ("Copy the bundled copy ... there by hand") --
    # it matched nothing and always passed. A programming error in the page
    # sync must not produce the friendly filesystem-problem WARNING at all.
    # `_web_page_paths` is also what `_load_history` resolves its own path
    # through (`_history_path`), so the same monkeypatch legitimately trips
    # ITS "could not determine the path" WARNING too -- that one is excluded
    # here rather than asserting no warnings at all, which would make this
    # test fail for a reason it does not claim to care about.
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "history file" not in r.getMessage()
    ]
    assert warnings == []


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


# ------------------------------------------------------- _truthy (mutation review)

@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("false", False),
        ("0", False),
        ("True", True),
        (" yes ", True),
        ("maybe", False),
        (0, False),
        (True, True),
    ],
)
def test_truthy_table(value, expected):
    assert plugin_module._truthy(value) is expected


def test_truthy_logs_a_debug_for_an_unrecognised_string(caplog):
    """Mutant: an unrecognised string coerced to True instead of False."""
    logger = logging.getLogger("lamplighter.test_truthy")
    with caplog.at_level("DEBUG", logger=logger.name):
        result = plugin_module._truthy("maybe", logger=logger)

    assert result is False
    debugs = [
        r for r in caplog.records
        if r.levelname == "DEBUG" and "unrecognised value" in r.getMessage()
    ]
    assert len(debugs) == 1
    assert "'maybe'" in debugs[0].getMessage()


def test_the_string_false_from_an_indigo_checkbox_means_off(install, monkeypatch, caplog):
    """Indigo can hand a checkbox prop back as the STRING "false" rather
    than the bool False. `bool("false")` is True, so a mutant that reads
    the pref with `.get("managePage", True)` instead of going through
    `_truthy` would treat this as "management on" and overwrite a
    hand-edited page."""
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>new</html>")
    dest = _installed_dest_path(install)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>hand-edited</html>")

    plug = make_plugin(managePage="false")
    with caplog.at_level("INFO"):
        plug._sync_web_page()

    assert dest.read_bytes() == b"<html>hand-edited</html>"
    infos = [r for r in caplog.records
             if r.levelname == "INFO" and str(dest) in r.getMessage()]
    assert len(infos) == 1
    assert "differs from the bundled one" in infos[0].getMessage()


# ------------------------------------------------------- cancelled prefs dialog

def test_a_cancelled_preferences_dialog_does_not_touch_the_page(install, monkeypatch, caplog):
    """Mutant: the `user_cancelled` early return loses its effect (the sync
    call moves ahead of it), so a Cancel click would still install/update
    the page from whatever was in the dialog's fields."""
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>new</html>")
    dest = _installed_dest_path(install)
    assert not dest.exists()

    plug = make_plugin(managePage=False)
    with caplog.at_level("INFO"):
        plug.closedPrefsConfigUi({"managePage": True}, user_cancelled=True)

    assert not dest.exists()
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert infos == []


# ------------------------------------------------------- the real bundled page

def test_the_bundled_status_page_is_real(tmp_path):
    """The page installed by `_sync_web_page` is the real, shipped one --
    not a placeholder that happens to satisfy the other tests' fixtures.
    Mutant: replace the bundled page with a tiny placeholder (verified
    separately, against a temp copy, never the real file)."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    real_page = os.path.join(
        repo_root, "Lamplighter.indigoPlugin", "Contents", "Resources", "pages",
        "lamplighter.html",
    )
    assert os.path.isfile(real_page)
    assert os.path.getsize(real_page) > 5000

    content = Path(real_page).read_text(encoding="utf-8")
    for needle in (
        "indigo-page-name",
        plugin_module.PLUGIN_ID,
        "lamplighter_zone",
        "lamplighter_controller",
        "api-key",
    ):
        assert needle in content, f"expected {needle!r} in the bundled status page"

    # `_web_page_paths` resolves WEB_PAGE_BUNDLE_DIR/WEB_PAGE_FILENAME under
    # <install>/Plugins/... -- mirror the real repo layout with a symlink so
    # the computed path actually points at the on-disk file above, rather
    # than merely looking plausible as a string.
    install = tmp_path / "install"
    install.mkdir()
    (install / "Plugins").symlink_to(repo_root, target_is_directory=True)

    computed_source, _dest_dir, _dest = plugin_module.Plugin._web_page_paths(str(install))
    assert os.path.isfile(computed_source)
    assert os.path.samefile(computed_source, real_page)


# ------------------------------------------------------- install folder lookup

def test_getinstallfolderpath_raising_is_reported_but_does_not_escape_startup(
    install, monkeypatch, caplog
):
    """`startup()` calls `indigo.server.getInstallFolderPath()` once for its
    own config path before `_sync_web_page` calls it again at the end --
    only the second of those is inside the try/except this test pins.
    Mutant: remove that inner try/except, which lets the RuntimeError fall
    through to the method's outer `except Exception`, turning the intended
    WARNING naming the install folder into an ERROR with a traceback
    instead."""
    install_dir = str(install)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return install_dir
        raise RuntimeError("no install folder")

    monkeypatch.setattr(indigo.server, "getInstallFolderPath", flaky)

    plug = make_plugin(managePage=True)
    with caplog.at_level("DEBUG"):
        plug.startup()  # must not raise

    # `_load_history` resolves its own path through the same
    # `getInstallFolderPath()`, and a later call from it also lands on the
    # "raise" branch of `flaky()` -- so it legitimately logs its own WARNING
    # too (`_load_history` path failures are WARNING, not DEBUG). That one is
    # excluded here; this test's own claim is about the page-sync WARNING.
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "history file" not in r.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "install folder" in message.lower()
    assert "Web Assets/static/pages/lamplighter.html" in message

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors == []


# ------------------------------------------------------- stale check, tightened

def test_the_stale_check_says_nothing_when_the_pages_are_identical(install, monkeypatch, caplog):
    """Mutant: the byte comparison in `_warn_if_managed_page_is_stale` is
    replaced with something that always considers the pages different."""
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>identical</html>")
    dest = _installed_dest_path(install)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>identical</html>")

    plug = make_plugin(managePage=False)
    with caplog.at_level("DEBUG"):
        plug._sync_web_page()

    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert infos == []


def test_the_stale_check_swallows_an_unreadable_installed_page_at_debug(
    install, monkeypatch, caplog
):
    """A filesystem problem while reading the *installed* page for the
    staleness check is DEBUG-only, not a WARNING -- an opted-out user must
    not get WARNINGs about a file the plugin isn't managing. Mutant: log the
    OSError at WARNING instead."""
    import builtins

    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>new</html>")
    dest = _installed_dest_path(install)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>old</html>")

    real_open = builtins.open

    def flaky_open(path, *args, **kwargs):
        if str(path) == str(dest):
            raise PermissionError("permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    plug = make_plugin(managePage=False)
    with caplog.at_level("DEBUG"):
        plug._sync_web_page()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == []
    debugs = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(debugs) == 1
    assert "permission denied" in debugs[0].getMessage()


# ------------------------------------------------------- _cleanup_tmp success path

def test_cleanup_tmp_success_leaves_no_partial_file_note(install, monkeypatch, caplog):
    """When `os.replace` fails but the fallback `os.remove` on the `.tmp`
    file actually succeeds, the WARNING must not claim a partial file was
    left behind, and no `.tmp` file remains on disk. Mutant: always append
    the "partial file was left" note regardless of whether cleanup worked."""
    source = _bundle_source_path(install)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"<html>new</html>")
    dest = _installed_dest_path(install)

    monkeypatch.setattr(
        plugin_module.os, "replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("replace failed")),
    )

    plug = make_plugin(managePage=True)
    with caplog.at_level("WARNING"):
        plug._sync_web_page()  # must not raise

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "partial file" not in message
    assert not os.path.exists(f"{dest}.tmp")
