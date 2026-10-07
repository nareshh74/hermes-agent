import sqlite3

from hermes_cli.resume_picker import machine_sessions, rank_key


def test_rank_key_orders_cwd_then_repo_then_branch_then_recency():
    ctx = {"cwd": "/w/repo/sub", "git_repo_root": "/w/repo", "git_branch": "feat"}
    rows = [
        {"id": "old_other", "last_active": 1},
        {"id": "new_other", "last_active": 9},
        {"id": "branch", "git_branch": "feat", "last_active": 2},
        {"id": "repo", "git_repo_root": "/w/repo", "last_active": 3},
        {"id": "cwd", "cwd": "/w/repo/sub", "last_active": 0},
    ]
    ranked = [r["id"] for r in sorted(rows, key=lambda r: rank_key(r, ctx))]
    assert ranked == ["cwd", "repo", "branch", "new_other", "old_other"]


def test_rank_key_empty_context_never_matches_missing_metadata():
    assert rank_key({"last_active": 5}, {"cwd": "", "git_repo_root": "", "git_branch": ""})[:3] == (True, True, True)


def _db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sessions (id TEXT, title TEXT, source TEXT, cwd TEXT, git_branch TEXT, git_repo_root TEXT,"
        " last_activity_at REAL, ended_at REAL, started_at REAL, archived INT, parent_session_id TEXT,"
        " message_count INT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);")
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,0,NULL,1)", rows)
    conn.commit()
    conn.close()


def test_machine_sessions_spans_profiles_and_ranks(tmp_path):
    (tmp_path / "profiles" / "work").mkdir(parents=True)
    _db(tmp_path / "state.db", [("d1", "def", "cli", "/x", None, None, 50, None, 1)])
    _db(tmp_path / "profiles" / "work" / "state.db", [
        ("w1", "here", "cli", "/proj", "main", "/proj", 10, None, 1),
        ("w2", "kanban", "kanban", "/proj", "main", "/proj", 99, None, 1),
    ])
    out = machine_sessions(tmp_path, {"cwd": "/proj", "git_repo_root": "/proj", "git_branch": "main"},
                           exclude_id="none")
    assert [(r["id"], r["profile"]) for r in out] == [("w1", "work"), ("d1", "default")]


def test_machine_sessions_preview_decodes_multimodal_content(tmp_path):
    import json
    import sqlite3
    _db(tmp_path / "state.db", [("m1", None, "cli", "/x", None, None, 5, None, 1)])
    conn = sqlite3.connect(tmp_path / "state.db")
    parts = [{"type": "text", "text": "look at this"}, {"type": "image_url", "image_url": {"url": "x"}}]
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES ('m1', 'user', ?)",
                 ("\x00json:" + json.dumps(parts),))
    conn.commit()
    conn.close()
    out = machine_sessions(tmp_path, {})
    assert out[0]["preview"] == "look at this [image]"
