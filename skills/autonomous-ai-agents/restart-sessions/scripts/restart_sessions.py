#!/usr/bin/env python3
"""List live Hermes CLI/TUI sessions and restart idle ones on the installed Hermes.

Read-only by default: prints one row per live session with its profile, cwd, last
message time, busy verdict and the resume command. ``--restart ID --yes`` restarts
one idle session: it opens a relaunch (a new Windows Terminal tab on Windows) that
waits for the old process tree to exit, then ends that tree. History survives
because every turn is already in the profile's state.db.

Sources: each profile's ``runtime/active_sessions.json`` (live leases, pid-checked)
and ``state.db`` opened read-only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil

INTERACTIVE_SURFACES = {"cli", "tui"}

# A session and every descendant (compression continuations, /new forks): the lease keeps
# the id the process opened with, while messages land on the newest child.
_FAMILY_SQL = """
WITH RECURSIVE fam(id) AS (
  SELECT ? UNION SELECT s.id FROM sessions s JOIN fam ON s.parent_session_id = fam.id
)
SELECT
  (SELECT MAX(timestamp) FROM messages WHERE session_id IN fam),
  (SELECT MAX(last_activity_at) FROM sessions WHERE id IN fam),
  (SELECT group_concat(last_activity_description, ' | ') FROM sessions
     WHERE id IN fam AND ended_at IS NULL AND COALESCE(last_activity_description, '') != ''),
  (SELECT cwd FROM sessions WHERE id IN fam AND cwd IS NOT NULL ORDER BY started_at DESC LIMIT 1),
  (SELECT group_concat(id, ' ') FROM fam)
"""


def hermes_root(home: str | None = None) -> Path:
    """Hermes root (holds ``profiles/``) from a profile or root HERMES_HOME."""
    path = Path(home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return path.parent.parent if path.parent.name == "profiles" else path


def profile_homes(root: Path) -> dict[str, Path]:
    homes = {"default": root}
    profiles = root / "profiles"
    if profiles.is_dir():
        homes.update({p.name: p for p in sorted(profiles.iterdir()) if p.is_dir()})
    return homes


def busy_reason(now: float, last_message: float | None, last_activity: float | None,
                activity: str | None, busy_minutes: float) -> str:
    """Why a session must not be restarted, or '' when idle."""
    if activity:
        return f"turn in progress: {activity}"
    window = busy_minutes * 60
    latest = max(t for t in (last_message, last_activity, 0.0) if t is not None)
    if now - latest < window:
        return f"active {int((now - latest) // 60)}m ago (< {busy_minutes:g}m)"
    return ""


def root_process(pid: int) -> psutil.Process | None:
    """Topmost Hermes launcher process above the lease owner (the tree to end)."""
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return None
    top = proc
    for parent in proc.parents():
        name = parent.name().lower()
        if not (name.startswith(("hermes", "python", "node"))):
            break
        top = parent
    return top


def resume_argv(hermes_exe: str, session_id: str, profile: str, surface: str) -> list[str]:
    argv = [hermes_exe, "--resume", session_id, "-p", profile]
    return argv + ["--tui"] if surface == "tui" else argv


def relaunch_argv(old_root_pid: int, cwd: str, resume: list[str], *, windows: bool,
                  wt: str | None) -> list[str]:
    """Command that waits for the old tree to exit, then resumes in ``cwd``."""
    if windows:
        ps_cmd = "& " + " ".join("'" + a.replace("'", "''") + "'" for a in resume)
        ps = f"Wait-Process -Id {old_root_pid} -ErrorAction SilentlyContinue; {ps_cmd}"
        # Encoded: wt treats a bare ';' in its argv as its own command separator.
        encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
        shell = ["powershell", "-NoExit", "-NoProfile", "-EncodedCommand", encoded]
        return [wt, "-w", "0", "nt", "-d", cwd, *shell] if wt else shell
    return ["sh", "-c", f"while kill -0 {old_root_pid} 2>/dev/null; do sleep 0.5; done; "
            f"cd {shlex.quote(cwd)} && exec {shlex.join(resume)}"]


def collect(root: Path, busy_minutes: float, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    own = {p.pid for p in psutil.Process().parents()} | {os.getpid()}
    current_id = os.environ.get("HERMES_SESSION_ID", "")
    rows = []
    for profile, home in profile_homes(root).items():
        registry = home / "runtime" / "active_sessions.json"
        try:
            entries = json.loads(registry.read_text(encoding="utf-8-sig")).get("entries", [])
        except (OSError, ValueError):
            continue
        db_path = home / "state.db"
        db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) if db_path.exists() else None
        for e in entries:
            if e.get("surface") not in INTERACTIVE_SURFACES or not psutil.pid_exists(int(e.get("pid", 0))):
                continue
            pid, sid = int(e["pid"]), str(e["session_id"])
            try:
                if abs(psutil.Process(pid).create_time() - float(e.get("process_start_time") or 0)) > 0.01:
                    continue  # pid recycled
            except psutil.Error:
                continue
            last_msg = last_act = activity = db_cwd = None
            family = sid
            if db is not None:
                last_msg, last_act, activity, db_cwd, family = db.execute(_FAMILY_SQL, (sid,)).fetchone()
            top = root_process(pid)
            try:
                cwd = db_cwd or psutil.Process(pid).cwd()
            except psutil.Error:
                cwd = db_cwd or str(Path.home())
            rows.append({
                "profile": profile, "session_id": sid, "surface": e["surface"], "pid": pid,
                "root_pid": top.pid if top else pid, "exe": top.exe() if top else "hermes",
                "cwd": cwd, "last_message_at": last_msg,
                "busy": busy_reason(now, last_msg, last_act, activity, busy_minutes),
                "current": pid in own or current_id in (family or "").split(),
            })
        if db is not None:
            db.close()
    # Current session last: restarting it ends the process running this script.
    return sorted(rows, key=lambda r: (r["current"], r["profile"], r["session_id"]))


def fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def print_table(rows: list[dict]) -> None:
    for r in rows:
        state = f"BUSY ({r['busy']})" if r["busy"] else "idle"
        tag = " [current]" if r["current"] else ""
        print(f"{r['profile']:<12} {r['session_id']:<24} {r['surface']:<4} pid={r['root_pid']:<6} "
              f"last={fmt_time(r['last_message_at'])}  {state}{tag}\n    cwd={r['cwd']}\n    "
              f"resume: {shlex.join(resume_argv('hermes', r['session_id'], r['profile'], r['surface']))}")
    print(f"\n{len(rows)} live session(s); {sum(1 for r in rows if not r['busy'])} idle.")


def restart(row: dict) -> int:
    if row["busy"]:
        print(f"refused: {row['session_id']} is busy ({row['busy']})", file=sys.stderr)
        return 2
    windows = os.name == "nt"
    argv = relaunch_argv(row["root_pid"], row["cwd"],
                         resume_argv(row["exe"], row["session_id"], row["profile"], row["surface"]),
                         windows=windows, wt=shutil.which("wt") if windows else None)
    flags = 0 if not windows or argv[0].lower().endswith(("wt", "wt.exe")) else subprocess.CREATE_NEW_CONSOLE
    # Drop this session's HERMES_* identity so the relaunch resolves only from -p/--resume.
    env = {k: v for k, v in os.environ.items() if not k.lstrip("_").startswith("HERMES")}
    subprocess.Popen(argv, cwd=row["cwd"], env=env, creationflags=flags, start_new_session=not windows,
                     stdin=subprocess.DEVNULL)
    print(f"relaunch queued: {shlex.join(argv)}", flush=True)
    tree = [psutil.Process(row["root_pid"]), *psutil.Process(row["root_pid"]).children(recursive=True)]
    for proc in reversed(tree):  # leaves first, so no child is orphaned mid-teardown
        try:
            proc.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(tree, timeout=10)
    print(f"ended pid {row['root_pid']} tree; session {row['session_id']} resumes in {row['cwd']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", help="Hermes root (default: derived from HERMES_HOME)")
    ap.add_argument("--busy-minutes", type=float, default=5.0,
                    help="treat a session with activity this recent as busy (default 5)")
    ap.add_argument("--json", action="store_true", help="print rows as JSON")
    ap.add_argument("--restart", metavar="SESSION_ID", help="restart this idle session")
    ap.add_argument("--yes", action="store_true", help="actually restart (without it, --restart is a dry run)")
    args = ap.parse_args(argv)
    rows = collect(hermes_root(args.root), args.busy_minutes)
    if not args.restart:
        print(json.dumps(rows, indent=2) if args.json else "", end="")
        if not args.json:
            print_table(rows)
        return 0
    match = [r for r in rows if r["session_id"] == args.restart]
    if not match:
        print(f"no live CLI/TUI session {args.restart}", file=sys.stderr)
        return 1
    if not args.yes:
        print_table(match)
        print("dry run: add --yes to restart")
        return 0
    shared = [r["session_id"] for r in rows if r["root_pid"] == match[0]["root_pid"] and r is not match[0]]
    if shared:  # ending the tree would also end these
        print(f"refused: pid {match[0]['root_pid']} also hosts {', '.join(shared)}", file=sys.stderr)
        return 2
    return restart(match[0])


if __name__ == "__main__":
    sys.exit(main())
