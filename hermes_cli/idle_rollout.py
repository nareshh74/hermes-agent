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


def idle_minutes(config: Optional[dict] = None) -> float:
    """``updates.idle_minutes``; invalid or missing values fall back to the default."""
    try:
        value = float(((config or {}).get("updates") or {}).get("idle_minutes", DEFAULT_IDLE_MINUTES))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_IDLE_MINUTES
    return value if value > 0 else DEFAULT_IDLE_MINUTES


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
    """Newest message timestamp across state DBs, excluding cron/oneshot runs. Read-only."""
    newest = None
    for path in dbs:
        if not Path(path).exists():
            continue
        try:
            con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
            try:
                (ts,) = con.execute(
                    "select max(m.timestamp) from messages m join sessions s on s.id = m.session_id "
                    "where s.source not in ('cron', 'oneshot')"
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if ts is not None and (newest is None or ts > newest):
            newest = float(ts)
    return newest


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

    notice_at = (float(argv[0]) or None) if argv else None
    now = time.time()
    last = last_prompt_at(argv[1:])
    minutes = idle_minutes(load_config_readonly())
    print(json.dumps({
        "action": decide(now, last, notice_at, minutes),
        "idle_minutes": minutes,
        "last_prompt_at": last,
        "targets": broadcast_targets(),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
