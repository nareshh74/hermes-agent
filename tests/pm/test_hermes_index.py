"""HERMES_PYPI_INDEX_URL: one setting that points every PM install at a PyPI mirror.

A frozen ``uv sync`` downloads the absolute artifact URLs recorded in ``uv.lock``,
so ``UV_INDEX_URL`` alone cannot help a network that blocks files.pythonhosted.org.
With the setting, PM re-points its working copy of the lock at the mirror first,
keeping every pin and hash. Unset, nothing changes.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from pm.environment import PythonEnvironment, _base_environment
from pm.package import InstallError

MIRROR = "https://mirror.example/pypi/simple/"

LOCK = """version = 1
revision = 3
requires-python = "==3.14.*"

[options]
exclude-newer = "0001-01-01T00:00:00Z"
exclude-newer-span = "P14D"

[options.exclude-newer-package]
demo = false

[[package]]
name = "demo"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/packages/aa/demo-1.0-py3-none-any.whl", hash = "sha256:%s" },
]
""" % ("a" * 64)


def _relocked(text: str, *, version: str = "1.0", digest: str = "a" * 64) -> str:
    """What ``uv lock --index-url MIRROR --exclude-newer NOW`` writes."""
    return (text.replace('source = { registry = "https://pypi.org/simple" }',
                         f'source = {{ registry = "{MIRROR}" }}')
            .replace("https://files.pythonhosted.org/packages/aa/", "https://feed.example/dl/")
            .replace('version = "1.0"', f'version = "{version}"')
            .replace("a" * 64, digest)
            .replace('exclude-newer = "0001-01-01T00:00:00Z"\nexclude-newer-span = "P14D"\n\n'
                     '[options.exclude-newer-package]\ndemo = false\n',
                     'exclude-newer = "2026-10-08T00:00:00Z"\n'))


@pytest.fixture
def clean_index_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("UV_", "PIP_")) or key == "HERMES_PYPI_INDEX_URL":
            monkeypatch.delenv(key)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)


def _environment(tmp_path: Path, env: dict, **kwargs) -> PythonEnvironment:
    return PythonEnvironment(uv=tmp_path / "uv", python=tmp_path / "python", destination=tmp_path / "venv",
                             cache=tmp_path / "cache", env=env, **kwargs)


def _fake_uv(monkeypatch, rewrite=None):
    """Record uv commands; ``uv lock`` rewrites the lock in its cwd like the real one."""
    seen: list[tuple[list[str], dict]] = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        if command[1] == "lock" and rewrite is not None:
            lock = Path(kwargs["cwd"]) / "uv.lock"
            lock.write_text(rewrite(lock.read_text(encoding="utf-8")), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_setting_selects_the_index_over_ambient_mirrors(clean_index_env, monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://pip.example/simple")
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://uv.example/simple")
    monkeypatch.setenv("HERMES_PYPI_INDEX_URL", MIRROR)

    env = _base_environment()
    assert env["UV_INDEX_URL"] == MIRROR
    assert "UV_DEFAULT_INDEX" not in env
    assert _base_environment({"HERMES_PYPI_INDEX_URL": MIRROR})["UV_INDEX_URL"] == MIRROR


def test_unset_setting_leaves_lock_and_commands_untouched(clean_index_env, tmp_path, monkeypatch):
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    seen = _fake_uv(monkeypatch)
    _environment(tmp_path, _base_environment()).relock_to_index(tmp_path)
    assert seen == []
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == LOCK
    assert "UV_INDEX_URL" not in _base_environment()


def test_relock_repoints_urls_and_keeps_pins_hashes_and_options(clean_index_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PYPI_INDEX_URL", MIRROR)
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    seen = _fake_uv(monkeypatch, _relocked)

    _environment(tmp_path, _base_environment()).relock_to_index(tmp_path)

    (command, kwargs), = seen
    assert command[1] == "lock" and command[command.index("--index-url") + 1] == MIRROR
    assert "--exclude-newer" in command and "demo=" in command[command.index("--exclude-newer-package") + 1]
    assert kwargs["env"]["UV_INDEX_URL"] == MIRROR
    text = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert "files.pythonhosted.org" not in text and f'registry = "{MIRROR}"' in text
    assert 'version = "1.0"' in text and "sha256:" + "a" * 64 in text
    # The committed quarantine policy survives; only this one resolve lifted it.
    assert 'exclude-newer-span = "P14D"\n\n[options.exclude-newer-package]\ndemo = false\n' in text
    assert "2026-10-08" not in text

    # Already on the mirror: nothing to do.
    seen.clear()
    _environment(tmp_path, _base_environment()).relock_to_index(tmp_path)
    assert seen == []


@pytest.mark.parametrize("change", [{"version": "1.1"}, {"digest": "b" * 64}])
def test_relock_refuses_a_mirror_that_changes_what_installs(clean_index_env, tmp_path, monkeypatch, change):
    monkeypatch.setenv("HERMES_PYPI_INDEX_URL", MIRROR)
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    _fake_uv(monkeypatch, lambda text: _relocked(text, **change))

    with pytest.raises(InstallError, match="exact pins"):
        _environment(tmp_path, _base_environment()).relock_to_index(tmp_path)
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == LOCK


def test_isolated_runtime_build_keeps_only_the_hermes_index(clean_index_env, tmp_path, monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://pip.example/simple")
    monkeypatch.setenv("HERMES_PYPI_INDEX_URL", MIRROR)
    seen = _fake_uv(monkeypatch)
    _environment(tmp_path, _base_environment(), no_config=True)._run(["sync"], cwd=tmp_path, timeout=5)
    (_, kwargs), = seen
    assert kwargs["env"]["UV_INDEX_URL"] == MIRROR


def test_frozen_workspace_sync_relocks_its_copy_first(clean_index_env, tmp_path, monkeypatch):
    import pm.workspace as ws

    monkeypatch.setenv("HERMES_PYPI_INDEX_URL", MIRROR)
    core = tmp_path / "core"
    core.mkdir()
    (core / "pyproject.toml").write_text('[project]\nname = "core"\nversion = "0"\n', encoding="utf-8")
    (core / "uv.lock").write_text(LOCK, encoding="utf-8")
    seen = _fake_uv(monkeypatch, _relocked)

    ws.lock_and_sync([], [], root=tmp_path / "workspace", source=core, seed_lock=core / "uv.lock",
                     environment=_environment(tmp_path, _base_environment()), frozen=True)

    assert [command[1] for command, _ in seen] == ["lock", "sync"]
    assert "--frozen" in seen[1][0]
    assert f'registry = "{MIRROR}"' in (tmp_path / "workspace" / "uv.lock").read_text(encoding="utf-8")
    assert (core / "uv.lock").read_text(encoding="utf-8") == LOCK
