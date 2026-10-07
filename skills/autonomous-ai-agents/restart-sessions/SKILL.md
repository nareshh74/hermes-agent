---
name: restart-sessions
description: Restart idle Hermes sessions onto a new install.
version: 1.0.0
author: Naresh Kumar (nareshh74), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, sessions, restart, update, resume, windows-terminal]
    related_skills: [hermes-agent]
---

# Restart Sessions Skill

After `hermes update` (or any new install), running CLI/TUI sessions keep the old code until
they restart. This skill lists live sessions across all profiles, flags busy ones, and restarts
idle ones with `hermes --resume <id> -p <profile>` in the same working directory. It never
restarts a busy session, and it does not restart the messaging gateway: use the built-in
`/restart` (gateway only) or `hermes gateway restart` for that.

## When to Use

- The user installed or updated Hermes and wants open sessions on the new version.
- The user asks which Hermes sessions are running, where, or which are busy.

## Prerequisites

- `psutil` (a Hermes core dependency). Run the script with the Python that runs Hermes.
- Windows: Windows Terminal (`wt`) puts each relaunch in a new tab; without it, a new console window opens.

## How to Run

Run through the `terminal` tool. `SKILL_DIR` is this skill's directory.

```bash
python "$SKILL_DIR/scripts/restart_sessions.py"                       # list (read-only)
python "$SKILL_DIR/scripts/restart_sessions.py" --restart <id>        # dry run for one session
python "$SKILL_DIR/scripts/restart_sessions.py" --restart <id> --yes  # restart it
```

## Quick Reference

| Flag | Effect |
|---|---|
| (none) | Table of live CLI/TUI sessions: profile, id, pid, cwd, last message, busy/idle, resume command |
| `--json` | Same rows as JSON |
| `--busy-minutes N` | Activity newer than N minutes counts as busy (default 5) |
| `--restart ID` | Dry run: show the one row and what would run |
| `--restart ID --yes` | Restart it; refused (exit 2) when busy or when the process also hosts another session |

Sources: each profile's `runtime/active_sessions.json` (live leases, verified by pid and process
start time) and `state.db` opened read-only. Busy means a turn label is set
(`last_activity_description`) or a message/activity is newer than `--busy-minutes`, checked across
compression continuations of the session.

## Procedure

1. Run the list. Show the user the table.
2. Tell the user what a restart loses and keeps:
   - Kept: the full conversation history (every turn is in `state.db`), model and profile, cwd.
   - Lost: in-flight background processes started from that session, browser sessions, open
     tool state (MCP connections reconnect), unsent input in the prompt box, and terminal scrollback.
3. Ask which idle sessions to restart. Never restart a `BUSY` row; ask the user to wait for it, or to
   finish it themselves, then list again.
4. Restart each chosen session with `--restart <id> --yes`, one at a time, the `[current]` session
   LAST (restarting it ends the process running this skill). Before the current one, say that this
   chat will close and reopen in a new tab.
5. The script opens the relaunch first; the relaunch waits for the old process tree to exit, then
   runs `hermes --resume <id> -p <profile>` (`--tui` for TUI sessions) in the old cwd. The old tree
   is then terminated (leaves first).

## Pitfalls

- The old process is terminated, not asked to exit: no external graceful-exit signal exists for a
  console app on Windows. This is safe only for idle sessions, because history is already persisted.
- The relaunch uses the launcher that started the session. After a blue/green or in-place update,
  confirm that the launcher now points at the new install.
- The new tab opens in the most recent Windows Terminal window (`wt -w 0`), not in the old tab's place.
- A freshly resumed TUI registers its lease on its first prompt, so it is not listed until then.
- Gateway, cron and desktop sessions are not listed. Only `cli` and `tui` leases are restartable here.

## Verification

```bash
python "$SKILL_DIR/scripts/restart_sessions.py" --json
```

After a restart, the same session id appears again with a new pid, and the resumed chat can
repeat an earlier message.
