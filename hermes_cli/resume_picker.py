"""Machine-wide session candidates for a bare ``/resume``.

Reads every profile's ``state.db`` (default root plus ``profiles/<name>/``) read-only and ranks
rows against the current context: same cwd > same git repo root > same branch > recency.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

_INTERNAL_SOURCES = ("kanban", "tool", "oneshot")
_PER_DB_LIMIT = 200
logger = logging.getLogger(__name__)


def _norm(path: str | None) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def rank_key(row: dict[str, Any], ctx: dict[str, str]) -> tuple:
    """Sort key (ascending = most relevant first) for *row* against the current *ctx*."""
    cwd, root = _norm(ctx.get("cwd")), _norm(ctx.get("git_repo_root"))
    branch = ctx.get("git_branch") or ""
    same_cwd = bool(cwd) and _norm(row.get("cwd")) == cwd
    same_root = bool(root) and _norm(row.get("git_repo_root")) == root
    same_branch = bool(branch) and (row.get("git_branch") or "") == branch
    return (not same_cwd, not same_root, not same_branch, -(row.get("last_active") or 0))


def current_context(cwd: str | None = None) -> dict[str, str]:
    """cwd plus git repo root/branch via ``git rev-parse`` (empty strings outside a repo)."""
    from hermes_cli.worktree_ops import _normalize_git_bash_path

    cwd = cwd or os.getenv("TERMINAL_CWD") or os.getcwd()

    def _git(*args: str) -> str:
        try:
            out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
            return out.stdout.strip() if out.returncode == 0 else ""
        except Exception:
            return ""

    return {"cwd": cwd, "git_repo_root": _normalize_git_bash_path(_git("rev-parse", "--show-toplevel")) or "",
            "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD")}


def profile_dbs(root: Path) -> list[tuple[str, Path]]:
    """``(profile_name, state.db)`` for the default root and every named profile that has one."""
    dbs = [("default", root / "state.db")]
    profiles = root / "profiles"
    if profiles.is_dir():
        dbs += [(p.name, p / "state.db") for p in sorted(profiles.iterdir()) if p.is_dir()]
    return [(name, db) for name, db in dbs if db.is_file()]


def _read_db(profile: str, db: Path) -> list[dict[str, Any]]:
    # mode=ro: never write in another profile's DB. A read-only open fails when the -shm/-wal
    # side files can't be created (read-only dir); immutable=1 skips them as a last resort.
    try:
        return _query_db(profile, f"{db.resolve().as_uri()}?mode=ro")
    except sqlite3.Error:
        return _query_db(profile, f"{db.resolve().as_uri()}?mode=ro&immutable=1")


def _query_db(profile: str, uri: str) -> list[dict[str, Any]]:
    from hermes_cli.cli_agent_setup_mixin import _user_display_text
    from hermes_state import SessionDB

    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" * len(_INTERNAL_SOURCES))
        rows = conn.execute(
            "SELECT s.id, s.title, s.source, s.cwd, s.git_branch, s.git_repo_root, "
            "COALESCE(s.last_activity_at, s.ended_at, s.started_at) AS last_active, "
            "(SELECT m.content FROM messages m WHERE m.session_id = s.id "
            " AND m.role = 'user' ORDER BY m.id LIMIT 1) AS preview "
            f"FROM sessions s WHERE COALESCE(s.source, '') NOT IN ({marks}) "
            "AND COALESCE(s.archived, 0) = 0 AND s.parent_session_id IS NULL "
            "AND (s.message_count > 0 OR s.title IS NOT NULL) "
            "ORDER BY last_active DESC LIMIT ?",
            (*_INTERNAL_SOURCES, _PER_DB_LIMIT),
        ).fetchall()
    finally:
        conn.close()
    return [{**dict(r), "profile": profile,
             "preview": _user_display_text(SessionDB._decode_content(r["preview"]))[:60] or None}
            for r in rows]


def machine_sessions(root: Path, ctx: dict[str, str], *, exclude_id: str | None = None,
                     limit: int = 10) -> list[dict[str, Any]]:
    """Top *limit* sessions across all profiles under *root*, ranked by :func:`rank_key`."""
    rows: list[dict[str, Any]] = []
    for profile, db in profile_dbs(root):
        try:
            rows += _read_db(profile, db)
        except sqlite3.Error as exc:
            logger.debug("resume picker: skipping %s (%s): %s", profile, db, exc)
            continue  # locked/old-schema DB: skip that profile rather than fail the picker
    rows = [r for r in rows if r["id"] != exclude_id]
    return sorted(rows, key=lambda r: rank_key(r, ctx))[:limit]
