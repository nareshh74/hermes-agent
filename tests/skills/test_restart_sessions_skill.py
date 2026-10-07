"""Behavior tests for skills/autonomous-ai-agents/restart-sessions/scripts/restart_sessions.py."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "autonomous-ai-agents"
          / "restart-sessions" / "scripts" / "restart_sessions.py")
_spec = importlib.util.spec_from_file_location("restart_sessions", SCRIPT)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def _profile(root: Path, name: str, session_id: str, *, surface: str = "tui", pid: int | None = None) -> Path:
    home = root / "profiles" / name
    (home / "runtime").mkdir(parents=True)
    me = psutil.Process(pid)
    entry = {"pid": me.pid, "process_start_time": me.create_time(), "session_id": session_id, "surface": surface}
    (home / "runtime" / "active_sessions.json").write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
    db = sqlite3.connect(home / "state.db")
    db.executescript("""
        CREATE TABLE sessions (id TEXT, parent_session_id TEXT, started_at REAL, ended_at REAL, cwd TEXT,
                               last_activity_at REAL, last_activity_description TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL);
    """)
    db.commit()
    db.close()
    return home / "state.db"


def _sql(db_path: Path, sql: str, *args) -> None:
    db = sqlite3.connect(db_path)
    db.execute(sql, args)
    db.commit()
    db.close()


def test_busy_reason_idle_recent_and_in_turn():
    now = 10_000.0
    assert rs.busy_reason(now, now - 3600, now - 3600, "", 5) == ""
    assert "active" in rs.busy_reason(now, now - 60, None, None, 5)
    assert "turn in progress" in rs.busy_reason(now, now - 3600, None, "tool running: terminal", 5)


def test_collect_follows_compression_child_for_busy_and_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    old = time.time() - 7200
    db = _profile(tmp_path, "work", "root_sess")
    _sql(db, "INSERT INTO sessions VALUES ('root_sess', NULL, ?, ?, 'C:/a', ?, '')", old, old, old)
    _sql(db, "INSERT INTO sessions VALUES ('child', 'root_sess', ?, NULL, 'C:/b', ?, '')", old + 1, old)
    _sql(db, "INSERT INTO messages (session_id, timestamp) VALUES ('child', ?)", old)

    [row] = rs.collect(tmp_path, busy_minutes=5)
    assert (row["profile"], row["session_id"], row["cwd"], row["busy"]) == ("work", "root_sess", "C:/b", "")

    # A turn running on the continuation makes the leased root busy.
    _sql(db, "UPDATE sessions SET last_activity_description = 'receiving stream response' WHERE id = 'child'")
    [row] = rs.collect(tmp_path, busy_minutes=5)
    assert "turn in progress" in row["busy"]


def test_restart_refuses_busy_session_without_touching_it(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    db = _profile(tmp_path, "work", "s1")
    _sql(db, "INSERT INTO messages (session_id, timestamp) VALUES ('s1', ?)", time.time())
    assert rs.main(["--root", str(tmp_path), "--restart", "s1", "--yes"]) == 2
    assert "busy" in capsys.readouterr().err
    assert psutil.pid_exists(os.getpid())


def test_current_session_sorts_last(tmp_path, monkeypatch):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _profile(tmp_path, "a", "cur")
        _profile(tmp_path, "b", "other", pid=other.pid)
        monkeypatch.setenv("HERMES_SESSION_ID", "cur")
        rows = rs.collect(tmp_path, busy_minutes=0)
    finally:
        other.kill()
    assert [r["session_id"] for r in rows] == ["other", "cur"]
    assert rows[-1]["current"]


def test_relaunch_waits_for_old_tree_then_resumes():
    resume = rs.resume_argv("hermes", "sid", "work", "tui")
    assert resume == ["hermes", "--resume", "sid", "-p", "work", "--tui"]
    argv = rs.relaunch_argv(42, "/w d", resume, windows=False, wt=None)
    assert "kill -0 42" in argv[-1] and argv[-1].endswith("exec hermes --resume sid -p work --tui")
    argv = rs.relaunch_argv(42, "C:/w", resume, windows=True, wt="wt")
    assert argv[:6] == ["wt", "-w", "0", "nt", "-d", "C:/w"]
    import base64
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert script.startswith("Wait-Process -Id 42") and "'--resume' 'sid'" in script
