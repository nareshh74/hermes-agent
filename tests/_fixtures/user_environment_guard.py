"""The autouse User-environment guard: tests never write the real HKCU\\Environment.

Imported into ``tests/conftest.py`` so pytest registers the fixture there (same
reason as ``live_system_guard``).

Windows launcher publication (``hermes_cli._launchers._register_windows_user_path``),
the bin migration (``hermes_cli._install_repair``) and uninstall all edit the User
PATH through ``winreg`` on ``HKEY_CURRENT_USER\\Environment``. Tests isolate HOME,
LOCALAPPDATA and HERMES_HOME, but the registry is machine state: a test that reached
the real write prepended its temporary ``...\\pytest-N\\...\\bin`` directory to the
developer's User PATH, ahead of the real launcher, and every new shell then ran a
dead pytest launcher. This guard makes that impossible: every ``winreg`` open of
``HKCU\\Environment`` returns an in-memory key seeded from the real values, and the
``WM_SETTINGCHANGE`` broadcast is dropped. Tests that drive a real machine (the
opt-in ``tests/e2e/core/windows_update`` suite) opt out with
``@pytest.mark.live_system_guard_bypass`` and restore PATH themselves.
"""
from __future__ import annotations

import os

import pytest

_ENVIRONMENT_SUBKEY = "environment"


class SandboxEnvironmentKey:
    """In-memory stand-in for an open ``HKCU\\Environment`` handle."""

    def __init__(self, values: dict[str, tuple[object, int]]):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def Close(self) -> None:  # noqa: N802 - mirrors PyHKEY
        pass

    def Detach(self) -> int:  # noqa: N802 - mirrors PyHKEY
        return 0


def _is_user_environment(winreg, key, sub_key) -> bool:
    return key == winreg.HKEY_CURRENT_USER and str(sub_key or "").strip("\\").casefold() == _ENVIRONMENT_SUBKEY


def _seed(real, winreg) -> dict[str, tuple[object, int]]:
    """Copy the real values (read-only) so code under test sees a realistic PATH."""
    values: dict[str, tuple[object, int]] = {}
    try:
        with real["OpenKey"](winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, data, kind = real["EnumValue"](key, index)
                except OSError:
                    break
                values[name.casefold()] = (data, kind)
                index += 1
    except OSError:
        pass
    return values


@pytest.fixture(autouse=True)
def _user_environment_guard(request, monkeypatch):
    """Route ``HKCU\\Environment`` through an in-memory key; drop the env broadcast.

    Yields the sandbox value map (casefolded name -> ``(data, type)``), or None
    off Windows / when bypassed.
    """
    if os.name != "nt" or request.node.get_closest_marker("live_system_guard_bypass") is not None:
        yield None
        return

    import ctypes
    import winreg

    names = ("OpenKey", "OpenKeyEx", "CreateKey", "CreateKeyEx",
             "QueryValueEx", "SetValueEx", "DeleteValue", "EnumValue", "CloseKey")
    real = {name: getattr(winreg, name) for name in names}
    values: dict[str, tuple[object, int]] | None = None

    def sandbox() -> SandboxEnvironmentKey:
        nonlocal values
        if values is None:
            values = _seed(real, winreg)
        return SandboxEnvironmentKey(values)

    def opener(name):
        def open_key(key, sub_key, *args, **kwargs):
            if _is_user_environment(winreg, key, sub_key):
                return sandbox()
            return real[name](key, sub_key, *args, **kwargs)
        return open_key

    def query_value(key, name):
        if isinstance(key, SandboxEnvironmentKey):
            try:
                return key.values[str(name).casefold()]
            except KeyError:
                raise FileNotFoundError(2, "The system cannot find the file specified") from None
        return real["QueryValueEx"](key, name)

    def set_value(key, name, reserved, kind, data):
        if isinstance(key, SandboxEnvironmentKey):
            key.values[str(name).casefold()] = (data, kind)
            return None
        return real["SetValueEx"](key, name, reserved, kind, data)

    def delete_value(key, name):
        if isinstance(key, SandboxEnvironmentKey):
            if key.values.pop(str(name).casefold(), None) is None:
                raise FileNotFoundError(2, "The system cannot find the file specified")
            return None
        return real["DeleteValue"](key, name)

    def enum_value(key, index):
        if isinstance(key, SandboxEnvironmentKey):
            items = list(key.values.items())
            if index >= len(items):
                raise OSError(259, "No more data is available")
            name, (data, kind) = items[index]
            return name, data, kind
        return real["EnumValue"](key, index)

    def close_key(key):
        if isinstance(key, SandboxEnvironmentKey):
            return None
        return real["CloseKey"](key)

    for name in ("OpenKey", "OpenKeyEx", "CreateKey", "CreateKeyEx"):
        monkeypatch.setattr(winreg, name, opener(name))
    monkeypatch.setattr(winreg, "QueryValueEx", query_value)
    monkeypatch.setattr(winreg, "SetValueEx", set_value)
    monkeypatch.setattr(winreg, "DeleteValue", delete_value)
    monkeypatch.setattr(winreg, "EnumValue", enum_value)
    monkeypatch.setattr(winreg, "CloseKey", close_key)
    monkeypatch.setattr(ctypes.windll.user32, "SendMessageTimeoutW", lambda *_a, **_k: 1, raising=False)

    yield sandbox().values
