import json
import sqlite3
import time

import pytest

from hermes_cli import active_sessions, idle_rollout
from hermes_cli.idle_rollout import decide, eta_seconds, idle_minutes


def test_idle_decision():
    m = 5
    assert decide(1000, None, None, m) == "notice"  # no sessions
    assert decide(1000, 1000 - 299, None, m) == "wait"  # recent prompt
    assert decide(1000, 1000 - 300, None, m) == "notice"  # idle N minutes
    assert decide(1000, 900, 950, m) == "wait"  # notice sent, window running
    assert decide(1300, 900, 1000, m) == "update"  # window elapsed, no prompt
    assert decide(1300, 1001, 1000, m) == "postpone"  # prompt after notice


def test_config_and_eta():
    assert idle_minutes({}) == 5
    assert idle_minutes({"updates": {"idle_minutes": 2}}) == 2
    assert idle_minutes({"updates": {"idle_minutes": "x"}}) == 5
    assert eta_seconds([]) == 300
    assert eta_seconds([100, 200, "bad"]) == 150


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A profile home with one idle-by-messages session holding a live lease."""
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    monkeypatch.setattr(idle_rollout, "broadcast_targets", lambda: [])
    monkeypatch.setattr(active_sessions, "_prune_dead", lambda entries, **_: entries)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "create table sessions (id text, source text, last_activity_at real, last_activity_description text);"
        "create table messages (session_id text, timestamp real);"
    )
    con.execute("insert into sessions values ('s1', 'cli', ?, 'running terminal')", (time.time() - 3600,))
    con.execute("insert into messages values ('s1', ?)", (time.time() - 3600,))
    con.commit()
    con.close()
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "active_sessions.json").write_text(
        json.dumps({"entries": [{"session_id": "s1", "pid": 1, "surface": "cli"}]})
    )
    return db


def run(argv, capsys):
    assert idle_rollout.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_in_flight_turn_blocks_notice(profile, capsys):
    out = run(["0", str(profile)], capsys)
    assert out["action"] == "wait"
    assert out["busy_sessions"] == ["s1"]


def test_in_flight_turn_postpones_and_clears_notice(profile, capsys):
    out = run([str(time.time() - 60), str(profile)], capsys)
    assert out["action"] == "postpone"
    assert out["notice_at"] is None


def test_excluded_source_does_not_block(profile, capsys):
    con = sqlite3.connect(profile)
    con.execute("update sessions set source = 'kanban'")
    con.commit()
    con.close()
    out = run(["0", str(profile)], capsys)
    assert out["action"] == "notice"
    assert out["busy_sessions"] == []


def test_bad_argv_prints_usage(capsys):
    assert idle_rollout.main(["soon"]) == 2
    assert capsys.readouterr().err.startswith("usage:")
