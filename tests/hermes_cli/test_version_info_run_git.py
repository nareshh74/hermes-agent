"""``_run_git`` must return on its own timeout even when git leaves a child holding the output.

On Windows ``git.exe`` is a launcher; killing it at the timeout leaves its child alive, and a pipe-based
``subprocess.run`` then blocks until that child exits (an ACP/TUI start stalled behind it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli.version_info import _run_git


def _init_repo(tmp_path: Path) -> Path:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    git = [real_git, "-c", "commit.gpgsign=false", "-C", str(repo)]
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "r"]):
        subprocess.run([*git, *args], check=True, env=env)
    return repo


def test_run_git_returns_stripped_stdout_and_none_on_failure(tmp_path):
    repo = _init_repo(tmp_path)
    head = _run_git(repo, "rev-parse", "HEAD")
    assert head is not None and len(head) == 40 and head == head.strip()
    assert _run_git(repo, "rev-parse", "--verify", "no-such-ref") is None


@pytest.mark.platforms("windows")
def test_run_git_returns_at_timeout_when_git_leaves_a_child_holding_stdout(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    slow = tmp_path / "slow.py"
    slow.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        "time.sleep(20)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.slow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f'!"{Path(sys.executable).as_posix()}" "{slow.as_posix()}"')
    started = time.monotonic()
    assert _run_git(repo, "slow") is None
    assert time.monotonic() - started < 10
