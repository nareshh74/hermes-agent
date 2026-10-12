"""Additive profile-scoped recall and separate read-only-vault index maintenance."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata as _metadata
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time


_CORE_RUNTIME_REQUIREMENTS = {
    'psutil': ((6,), (8,)),
    'ruamel.yaml': ((0, 18), (0, 20)),
}
_STABLE_PART = r'(?:0|[1-9][0-9]*)'
_STABLE_VERSION = re.compile(rf'{_STABLE_PART}(?:\.{_STABLE_PART})*')


def _check_core_runtime_dependencies() -> None:
    """Fail plugin load outside the first-party Python 3.14 non-Android runtime contract."""
    if sys.version_info[:2] != (3, 14) or sys.platform == 'android':
        raise ImportError('naresh-kb requires the installed Hermes Python 3.14 runtime')
    for package, (floor, ceiling) in _CORE_RUNTIME_REQUIREMENTS.items():
        try:
            raw = _metadata.version(package)
        except _metadata.PackageNotFoundError as exc:
            raise ImportError('naresh-kb requires the installed Hermes Python 3.14 runtime') from exc
        if not _STABLE_VERSION.fullmatch(raw):
            raise ImportError('naresh-kb requires the installed Hermes Python 3.14 runtime')
        version = tuple(int(part) for part in raw.split('.'))
        if not (floor <= version < ceiling):
            raise ImportError('naresh-kb requires the installed Hermes Python 3.14 runtime')


_check_core_runtime_dependencies()

from hermes_constants import get_hermes_home
from ruamel.yaml import YAML

from .kb_backend import run, search
from .kb_core import Settings, digest, ensure_state, render
from .kb_scope import excluded, safe_chain
from . import kb_multiscope

_SCOPE_STATUS_LOCK = threading.Lock()


class StaleIndexError(RuntimeError):
    """The bounded lookup cannot trust the current index manifest."""


def failure_details(exc: BaseException) -> dict:
    """Project an exception to a fixed, non-secret lookup failure cause."""
    from .kb_backend import QmdExitError
    if isinstance(exc, StaleIndexError):
        return {'code': 'stale_index'}
    if isinstance(exc, QmdExitError):
        return {'code': 'qmd_exit', 'exit_code': exc.exit_code}
    if isinstance(exc, TimeoutError):
        return {'code': 'qmd_timeout'}
    if isinstance(exc, ValueError):
        return {'code': 'invalid_backend_result'}
    if isinstance(exc, OSError):
        return {'code': 'backend_os_error'}
    return {'code': 'internal_error'}


def _last_error(details: dict) -> dict:
    return {**details, 'at': time.time()}


def failure_label(details: dict) -> str:
    suffix = ':' + str(details['exit_code']) if 'exit_code' in details else ''
    return str(details['code']) + suffix


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.kb-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def signature(cfg: Settings) -> str:
    embed_identity = cfg.model_provenance.get('embed')
    if embed_identity is None and cfg.models.get('embed'):
        # Legacy single-level configs remain path-sensitive. Two-level configs require
        # pinned provenance, so their shared signature never depends on a caller's path.
        embed_identity = {'path': cfg.models['embed']}
    values = [cfg.scope, str(cfg.vault), cfg.collections, cfg.exclusions,
              cfg.max_note_bytes, embed_identity]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def verify_model_provenance(cfg: Settings) -> None:
    """Verify pinned index-model bytes during refresh, never on per-turn lookup."""
    if 'embed' not in cfg.models:
        return
    expected = cfg.model_provenance.get('embed')
    if expected is None:
        if cfg.require_model_provenance:
            raise ValueError('embedding model provenance is required')
        return
    path = Path(cfg.models['embed'])
    if (not path.is_absolute() or not path.is_file() or
            path.stat().st_size != expected['bytes'] or digest(path) != expected['sha256']):
        raise ValueError('embedding model provenance mismatch')


def status(cfg: Settings, state: str, **fields) -> None:
    with contextlib.suppress(OSError, ValueError):
        with _SCOPE_STATUS_LOCK:
            ensure_state(cfg)
            path = cfg.state / 'status.json'
            previous = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
            if 'last_error' not in fields and isinstance(previous.get('last_error'), dict):
                fields['last_error'] = previous['last_error']
            write_json(path, {'state': state, 'checked_at': time.time(), **fields})


def scope_status(cfg: Settings, scope: str, state: str, **fields) -> None:
    """Record reader health only in the active profile's owned state."""
    with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
        with _SCOPE_STATUS_LOCK:
            ensure_state(cfg)
            path = cfg.state / 'scope-status.json'
            data = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
            previous = data.get(scope, {}) if isinstance(data.get(scope), dict) else {}
            if 'last_error' not in fields and isinstance(previous.get('last_error'), dict):
                fields['last_error'] = previous['last_error']
            data[scope] = {'state': state, 'checked_at': time.time(), **fields}
            write_json(path, data)


def snapshot(cfg: Settings) -> dict:
    files = {}
    for collection, folder in cfg.collections.items():
        root = cfg.vault / folder
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith('.') and
                       safe_chain(Path(directory) / name, cfg.vault) and
                       not excluded((Path(directory) / name).relative_to(root).as_posix() + '/', cfg.exclusions) and
                       not excluded((Path(directory) / name).relative_to(cfg.vault).as_posix() + '/', cfg.exclusions)]
            for name in names:
                relative = (Path(directory) / name).relative_to(root).as_posix()
                source = cfg.source(collection, relative)
                if source:
                    files[collection + '/' + relative] = digest(source)
    return files


def qmd_config(cfg: Settings) -> dict:
    collections = {}
    for collection, folder in cfg.collections.items():
        prefix = folder.rstrip('/') + '/'
        patterns = [p[len(prefix):] if p.casefold().startswith(prefix.casefold()) else p for p in cfg.exclusions]
        ignore = [''.join('[' + c.lower() + c.upper() + ']' if c.isascii() and c.isalpha() else c
                          for c in pattern) for pattern in patterns]
        collections[collection] = {'path': str(cfg.vault / folder), 'pattern': '**/*.md',
                                   'ignore': ignore, 'includeByDefault': False}
    return {'collections': collections, **({'models': cfg.models} if cfg.models else {})}


def acquire_refresh_lock(path: Path):
    stream = path.open('a+b')
    try:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except OSError:
        stream.close()
        raise RuntimeError('refresh already running') from None


def refresh_locked(cfg: Settings, *, timeout: float = 120,
                   manifest_fields: dict | None = None) -> dict:
    """Refresh while the caller owns cfg.state/refresh.lock."""
    manifest_fields = dict(manifest_fields or {})
    if cfg.scope == 'shared' and hasattr(cfg, 'shared_settings_sha256'):
        manifest_fields.setdefault('shared_settings_sha256', cfg.shared_settings_sha256)
    try:
        verify_model_provenance(cfg)
        files = snapshot(cfg)
        old_path = cfg.state / 'manifest.json'
        old = json.loads(old_path.read_text(encoding='utf-8')) if old_path.exists() else {}
        changed = (old.get('files') != files or old.get('signature') != signature(cfg) or
                   any(old.get(key) != value for key, value in manifest_fields.items()))
        if changed:
            config = cfg.state / 'config' / 'index.yml'
            config.parent.mkdir(parents=True, exist_ok=True)
            with config.open('w', encoding='utf-8') as stream:
                YAML().dump(qmd_config(cfg), stream)
            run(cfg, ['update'], timeout=timeout)
            content_changed = bool(set(files.values()) - set(old.get('files', {}).values()))
            if cfg.mode != 'lexical' and (content_changed or old.get('signature') != signature(cfg)):
                from .kb_backend import search_args
                search_args(cfg, 'local model validation')
                run(cfg, ['embed'], timeout=timeout)
            after = snapshot(cfg)
            files = {k: v for k, v in files.items() if after.get(k) == v}
        manifest = {'signature': signature(cfg), 'files': files, 'updated_at': time.time(),
                    **manifest_fields}
        write_json(old_path, manifest)
        status(cfg, 'fresh', changed=changed, file_count=len(files))
        return {'changed': changed, 'file_count': len(files)}
    except Exception as exc:
        status(cfg, 'error', error_type=type(exc).__name__)
        raise


def refresh(cfg: Settings, *, timeout: float = 120) -> dict:
    """Only this explicit indexing path scans the vault or invokes update/embed."""
    ensure_state(cfg)
    lock = acquire_refresh_lock(cfg.state / 'refresh.lock')
    try:
        return refresh_locked(cfg, timeout=timeout)
    finally:
        lock.close()


def recall(home: Path, values: dict, user_message) -> dict | None:
    if not values.get('enabled', False):
        return None
    cfg = None
    try:
        cfg = Settings(home, values)
        if isinstance(user_message, list):
            user_message = ' '.join(p.get('text', '') for p in user_message
                                    if isinstance(p, dict) and p.get('type') == 'text')
        if not isinstance(user_message, str) or not user_message.strip():
            return None
        manifest = json.loads((cfg.state / 'manifest.json').read_text(encoding='utf-8'))
        age = time.time() - manifest['updated_at']
        if manifest.get('signature') != signature(cfg) or age > cfg.interval * 1.5:
            details = {'code': 'stale_index'}
            status(cfg, 'error', **details, last_error=_last_error(details),
                   age_seconds=max(0, round(age, 2)))
            return {'context': 'Knowledge base unavailable (stale_index). No recalled evidence; do not infer missing facts.'}
        started = time.monotonic()
        rows = search(cfg, user_message)
        text = render(cfg, rows, manifest['files'], query=user_message)
        status(cfg, 'recalled' if text else 'abstained', latency_ms=round((time.monotonic() - started) * 1000, 2))
        return {'context': text} if text else None
    except Exception as exc:  # health: allow BLE001 -- Fail-open recall boundary records only error type to avoid leaking note/query text.
        details = failure_details(exc)
        if cfg is not None:
            status(cfg, 'error', **details, last_error=_last_error(details))
        return {'context': 'Knowledge base unavailable (' + failure_label(details) +
                '). Normal chat may continue; no recalled evidence.'}


def register(ctx):
    def before_turn(user_message='', **kwargs):
        values = ctx.get_config('kb', {})
        if values.get('layout') == 'two_level':
            return kb_multiscope.recall_two_level(
                get_hermes_home(), values, user_message,
                platform=kwargs.get('platform', ''), sender_id=kwargs.get('sender_id', ''))
        return recall(get_hermes_home(), values, user_message)

    def setup(parser):
        parser.add_argument('action', choices=['refresh', 'watch', 'status'])

    def command(args):
        values = ctx.get_config('kb', {})
        if values.get('layout') == 'two_level':
            if args.action == 'status':
                print(json.dumps(kb_multiscope.status_two_level(get_hermes_home(), values)))
                return
            if args.action == 'refresh':
                print(json.dumps(kb_multiscope.refresh_two_level(get_hermes_home(), values)))
                return
            try:
                while True:
                    cycle = time.monotonic()
                    try:
                        kb_multiscope.refresh_two_level(get_hermes_home(), values)
                    except Exception as exc:  # health: allow BLE001 -- Keep the watcher alive and expose only a redacted error type.
                        private = kb_multiscope.build_scopes(get_hermes_home(), values)['private']
                        status(private, 'watch_error', error_type=type(exc).__name__)
                    interval = float(values.get('refresh_interval_seconds', 60))
                    time.sleep(max(0.05, interval / 2 - (time.monotonic() - cycle)))
            except KeyboardInterrupt:
                return
        cfg = Settings(get_hermes_home(), values)
        if args.action == 'status':
            print((cfg.state / 'status.json').read_text(encoding='utf-8'))
        elif args.action == 'refresh':
            print(json.dumps(refresh(cfg)))
        else:
            try:
                while True:
                    cycle = time.monotonic()
                    try:
                        refresh(cfg, timeout=cfg.interval / 2)
                    except Exception as exc:  # health: allow BLE001 -- Keep the separate watcher alive while exposing redacted error state.
                        status(cfg, 'error', error_type=type(exc).__name__)
                    time.sleep(max(0.05, cfg.interval / 2 - (time.monotonic() - cycle)))
            except KeyboardInterrupt:
                return

    ctx.register_hook('pre_llm_call', before_turn)
    ctx.register_cli_command('kb', 'Read-only vault index maintenance and status', setup, command)
