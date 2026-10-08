"""The test harness must never write the real Windows User PATH.

Regression: launcher-publication tests reached ``_register_windows_user_path``
unguarded and prepended ``...\\pytest-of-<user>\\...\\bin`` directories to the
developer's real ``HKCU\\Environment\\Path``, so new shells ran a dead pytest
launcher. ``tests/_fixtures/user_environment_guard.py`` sandboxes the key.
"""
import os

import pytest

if os.name == "nt":
    import winreg

    # Captured at import, before any fixture patches the module: the real API.
    _REAL_OPEN = winreg.OpenKey
    _REAL_QUERY = winreg.QueryValueEx
    _REAL_SET = winreg.SetValueEx


def _real_user_path():
    try:
        with _REAL_OPEN(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            return _REAL_QUERY(key, "Path")
    except FileNotFoundError:
        return None


def _require_guard() -> None:
    # Checked BEFORE exercising the writer, so a missing guard fails without
    # touching the host's PATH.
    assert winreg.SetValueEx is not _REAL_SET, "real winreg.SetValueEx is reachable from tests"


@pytest.mark.platforms("windows")
def test_launcher_user_path_registration_stays_in_the_sandbox(tmp_path):
    from hermes_cli import _launchers

    _require_guard()
    entry = tmp_path / "bin"
    saved = _real_user_path()
    assert _launchers._register_windows_user_path(entry) == "added"
    assert _real_user_path() == saved
    # Code under test still reads back its own write through the sandbox.
    assert _launchers._register_windows_user_path(entry) == "present"


@pytest.mark.platforms("windows")
def test_install_repair_user_path_write_stays_in_the_sandbox(tmp_path):
    from hermes_cli import _install_repair

    _require_guard()
    saved = _real_user_path()
    entries, kind = _install_repair._read_user_path_raw()
    _install_repair._write_user_path_raw([str(tmp_path / "bin"), *entries], kind)
    assert _real_user_path() == saved
    assert _install_repair._read_user_path_raw()[0][0] == str(tmp_path / "bin")


@pytest.mark.platforms("windows")
@pytest.mark.live_system_guard_bypass
def test_generic_live_system_bypass_keeps_the_registry_guard():
    # Signal/process tests use that marker; it is no licence to edit the User PATH.
    _require_guard()


@pytest.mark.platforms("windows")
@pytest.mark.real_user_environment
def test_machine_e2e_marker_reaches_the_real_registry():
    assert winreg.SetValueEx is _REAL_SET


@pytest.mark.platforms("windows")
def test_session_backstop_strips_only_entries_under_basetemp(tmp_path, _user_environment_guard):
    # Runs against the sandboxed key; the hook calls the same function on the real one.
    from tests._fixtures.user_environment_guard import strip_session_user_path_leaks

    basetemp = tmp_path / "basetemp"
    leaked = str(basetemp / "pytest-0" / "test_x0" / "bin")
    keep = [r"%LOCALAPPDATA%\hermes\launch", str(tmp_path / "basetemp-sibling" / "bin")]
    _user_environment_guard["path"] = (";".join([leaked, *keep]), winreg.REG_EXPAND_SZ)

    assert strip_session_user_path_leaks(basetemp) == [leaked]
    assert _user_environment_guard["path"] == (";".join(keep), winreg.REG_EXPAND_SZ)
