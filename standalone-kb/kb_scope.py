"""Canonical path, exclusion and owned-state boundaries shared by indexing and lookup."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

DEFAULT_EXCLUSIONS = ('**/private/**', '**/raw/**')
OWNER_FILE = '.kb-owner.json'


def is_reparse(path: Path) -> bool:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(stat, 'st_file_attributes', 0) & 0x400)


def safe_chain(path: Path, boundary: Path) -> bool:
    return all(not is_reparse(part) for part in (path, *path.parents)
               if part == boundary or part.is_relative_to(boundary))


def excluded(relative: str, patterns) -> bool:
    relative = relative.casefold()
    for pattern in patterns:
        pattern = pattern.casefold()
        if fnmatch.fnmatchcase(relative, pattern):
            return True
        if pattern.startswith('**/') and fnmatch.fnmatchcase(relative, pattern[3:]):
            return True
    return False


def owner_record(home: Path) -> dict:
    return {'schema': 1, 'plugin': 'naresh-kb', 'home': os.path.normcase(str(home.resolve()))}


def validate_state(home: Path, state: Path, root: Path | None = None) -> None:
    root = (root or home / 'cache' / 'naresh-kb').resolve()
    if not state.is_absolute() or not state.resolve().is_relative_to(root.resolve()):
        raise ValueError('state_dir must be inside the dedicated profile cache/naresh-kb namespace')
    if not safe_chain(state, home):
        raise ValueError('state path contains a reparse point')
    if state.exists():
        marker = state / OWNER_FILE
        if marker.exists():
            if is_reparse(marker) or json.loads(marker.read_text(encoding='utf-8-sig')) != owner_record(home):
                raise ValueError('state ownership mismatch')
        elif any(state.iterdir()):
            raise ValueError('preexisting state directory is not owned by this plugin')
        for name in ('index.sqlite', 'config', 'cache', 'manifest.json', 'status.json',
                     'scope-status.json', 'shared-settings.json', 'refresh.lock'):
            if not safe_chain(state / name, state):
                raise ValueError('state artifact contains a reparse point')


def claim_state(home: Path, state: Path, root: Path | None = None) -> None:
    validate_state(home, state, root)
    state.mkdir(parents=True, exist_ok=True)
    marker = state / OWNER_FILE
    if not marker.exists():
        try:
            with marker.open('x', encoding='utf-8') as stream:
                json.dump(owner_record(home), stream)
        except FileExistsError:
            validate_state(home, state, root)
