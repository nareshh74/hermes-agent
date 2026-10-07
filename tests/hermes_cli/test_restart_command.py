"""/restart in CLI/TUI: leave the session and resume the same session id in place."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli import HermesCLI
from hermes_cli import main_tui_launch
from hermes_cli.commands import resolve_command
from hermes_cli.relaunch import build_relaunch_argv


def test_restart_is_offered_outside_the_gateway():
    cmd = resolve_command("restart")
    assert cmd is not None and not cmd.gateway_only and not cmd.cli_only
    assert HermesCLI._slash_handler("restart") == ("_cmd_restart", True)


@pytest.mark.parametrize("history, expected", [([{"role": "user"}], ["--resume", "abc"]), ([], [])])
def test_cli_restart_queues_resume_and_leaves_repl(history, expected):
    self_ = SimpleNamespace(session_id="abc", conversation_history=history, _pending_relaunch=None,
                            _agent_running=False)
    assert HermesCLI._cmd_restart(self_, "/restart") is False  # False = exit REPL
    assert self_._pending_relaunch == expected
    assert self_._relaunch_preserve_inherited is True


def test_cli_restart_refuses_mid_turn():
    self_ = SimpleNamespace(session_id="abc", conversation_history=[{"role": "user"}],
                            _pending_relaunch=None, _agent_running=True, _console_print=print)
    assert HermesCLI._cmd_restart(self_, "/restart") is not False  # stay in the REPL
    assert self_._pending_relaunch is None


def test_restart_relaunch_keeps_inherited_mode_and_profile():
    argv = build_relaunch_argv(["--resume", "abc"],
                               original_argv=["--tui", "-p", "work", "-m", "x", "--resume", "old"])
    assert "--tui" in argv and argv[argv.index("-p") + 1] == "work"
    assert argv[-2:] == ["--resume", "abc"]
    assert argv.count("--resume") == 1 and "old" not in argv  # --resume is not inherit_on_relaunch


_WAIT_SRC = """
import sys
sys.path.insert(0, {repo!r})
import cli
from hermes_cli import relaunch as r
sys.platform = "win32"  # exercise the spawn-and-wait relaunch path on any OS
cli._arm_exit_watchdog(timeout_s=0.5)  # what _run_cleanup arms before run() relaunches
r.build_relaunch_argv = lambda *a, **k: [sys.executable, "-c", "import time, sys; time.sleep(2); sys.exit(7)"]
r.relaunch([])
"""


def test_windows_relaunch_wait_survives_exit_watchdog():
    # The watchdog os._exit(0)s after its timeout; it must not kill the parent waiting on the child.
    import os, subprocess, sys
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    p = subprocess.run([sys.executable, "-c", _WAIT_SRC.format(repo=repo)], env=env, timeout=60)
    assert p.returncode == 7


def test_tui_exit_code_43_resumes_active_session(tmp_path, monkeypatch):
    monkeypatch.setattr(main_tui_launch, "_make_tui_argv", lambda *_a: (["node"], tmp_path))
    monkeypatch.setattr(main_tui_launch, "_read_tui_active_session_file", lambda _p: "live-id")
    calls = []
    with patch("subprocess.call", return_value=43), \
            patch("hermes_cli.relaunch.relaunch", side_effect=lambda a, **k: calls.append(a)), \
            pytest.raises(SystemExit):
        main_tui_launch._launch_tui(resume_session_id="old-id")
    assert calls == [["--resume", "live-id"]]
