"""Idle-gated update rollout: decide when an external updater may promote a new install.

An update waits until every session has been idle for ``updates.idle_minutes`` (default 5).
It then broadcasts a notice to active sessions and promotes after another idle window,
unless a prompt arrives in between, which postpones it.

``python -m hermes_cli.idle_rollout <notice_at|0> <state.db> [...]`` prints the activity
snapshot and the decision as JSON. The updater (for example the blue/green slot tick)
calls it every few minutes and acts on ``action``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

DEFAULT_IDLE_MINUTES = 5
DEFAULT_ETA_SECONDS = 300
# Sessions that are not a person at a prompt: never block or postpone an update.
EXCLUDED_SOURCES = ("cron", "oneshot", "kanban", "tool")
_NOT_EXCLUDED = f"source not in ({', '.join('?' * len(EXCLUDED_SOURCES))})"
USAGE = "usage: python -m hermes_cli.idle_rollout <notice_at|0> <state.db> [...]"


def idle_minutes(config: Optional[dict] = None) -> float:
    """``updates.idle_minutes``; invalid or missing values fall back to the default."""
    try:
        value = float(((config or {}).get("updates") or {}).get("idle_minutes", DEFAULT_IDLE_MINUTES))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_IDLE_MINUTES
    if value <= 0:
        return DEFAULT_IDLE_MINUTES
    # Whole minutes stay ints so notices read "in 5 min", not "in 5.0 min".
    return int(value) if value.is_integer() else value


def decide(now: float, last_prompt_at: Optional[float], notice_at: Optional[float], idle_min: float) -> str:
    """Return ``wait``, ``notice``, ``postpone`` or ``update``.

    ``last_prompt_at`` is the newest message in any session (None = no sessions).
    ``notice_at`` is when the pending notice was broadcast (None = no notice yet).
    """
    window = idle_min * 60
    if notice_at is not None:
        if last_prompt_at is not None and last_prompt_at > notice_at:
            return "postpone"
        return "update" if now - notice_at >= window else "wait"
    if last_prompt_at is None or now - last_prompt_at >= window:
        return "notice"
    return "wait"


def eta_seconds(durations: Optional[list]) -> int:
    """Mean of recorded update durations in seconds, else a fixed default."""
    vals = [float(d) for d in durations or [] if isinstance(d, (int, float)) and d > 0]
    return round(sum(vals) / len(vals)) if vals else DEFAULT_ETA_SECONDS


def last_prompt_at(dbs: list) -> Optional[float]:
    """Newest message timestamp across state DBs, excluding ``EXCLUDED_SOURCES``. Read-only."""
    newest = None
    for path in dbs:
        if not Path(path).exists():
            continue
        try:
            con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
            try:
                (ts,) = con.execute(
                    "select max(m.timestamp) from messages m join sessions s on s.id = m.session_id "
                    f"where s.{_NOT_EXCLUDED}",
                    EXCLUDED_SOURCES,
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if ts is not None and (newest is None or ts > newest):
            newest = float(ts)
    return newest


def busy_sessions(dbs: list, now: float, idle_min: float) -> list:
    """Session ids holding a live lease with a turn in flight or activity inside the window.

    Each ``state.db`` is paired with the lease registry of its own profile home
    (``<home>/runtime/active_sessions.json``), so in-flight turns that have not yet
    written a message still block the rollout.
    """
    from hermes_cli import active_sessions

    window = idle_min * 60
    busy = []
    for path in dbs:
        home = Path(path).parent
        try:
            entries = active_sessions._prune_dead(active_sessions._read_entries(active_sessions._state_path(home)))
        except Exception:
            continue
        ids = [str(e["session_id"]) for e in entries if e.get("session_id")]
        if not ids or not Path(path).exists():
            continue
        try:
            con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    f"select id, last_activity_description, last_activity_at from sessions "
                    f"where id in ({', '.join('?' * len(ids))}) and {_NOT_EXCLUDED}",
                    (*ids, *EXCLUDED_SOURCES),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        for sid, desc, at in rows:
            if (desc or "").strip() or (at is not None and now - float(at) < window):
                busy.append(sid)
    return busy


def broadcast_targets() -> list:
    """``platform:chat_id`` for every live gateway session lease (the notice recipients)."""
    from hermes_cli import active_sessions

    try:
        entries = active_sessions._prune_dead(active_sessions._read_entries(active_sessions._state_path()))
    except Exception:
        return []
    out = []
    for e in entries:
        meta = e.get("metadata") or {}
        if meta.get("platform") and meta.get("chat_id"):
            target = f"{meta['platform']}:{meta['chat_id']}"
            if target not in out:
                out.append(target)
    return out


def main(argv: list) -> int:
    from hermes_cli.config import load_config_readonly

    try:
        notice_at = (float(argv[0]) or None) if argv else None
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    now = time.time()
    dbs = argv[1:]
    last = last_prompt_at(dbs)
    minutes = idle_minutes(load_config_readonly())
    busy = busy_sessions(dbs, now, minutes)
    # A turn in flight counts as activity now: it blocks a notice and postpones a pending one.
    action = decide(now, now if busy else last, notice_at, minutes)
    print(json.dumps({
        "action": action,
        "idle_minutes": minutes,
        "last_prompt_at": last,
        "busy_sessions": busy,
        # Postpone clears the pending notice; the caller stores this value back.
        "notice_at": None if action == "postpone" else notice_at,
        "targets": broadcast_targets(),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
