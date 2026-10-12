"""Two-level retrieval: one shared scope plus exactly the active profile's private scope."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import time

from .kb_backend import search
from .kb_core import PREFIX, Settings, render

_PROFILE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}')
_PRIVATE_KEYS = {
    'qmd_command', 'mode', 'models', 'model_provenance', 'rerank_enabled', 'top_k',
    'max_context_chars', 'max_note_bytes', 'timeout_seconds', 'refresh_interval_seconds',
    'min_score', 'lexical_min_terms', 'exclusions',
}
_SHARED_INDEX_KEYS = {'qmd_command', 'models', 'model_provenance', 'max_note_bytes'}
_RETRIEVAL_KEYS = {
    'mode', 'rerank_enabled', 'top_k', 'max_context_chars', 'timeout_seconds',
    'refresh_interval_seconds', 'min_score', 'lexical_min_terms',
}
_SHARED_SETTINGS = 'shared-settings.json'
_SHARED_RECORD_KEYS = frozenset({'qmd_command_sha256', 'models', 'model_provenance', 'max_note_bytes'})
_LOCAL_USER_PLATFORMS = frozenset({'cli', 'tui', 'desktop'})


def identity(home: Path) -> tuple[Path, str]:
    home = home.resolve()
    if home.parent.name == 'profiles':
        return home.parent.parent, home.name
    return home, 'default'


def _validate_public_values(values: dict) -> None:
    if not isinstance(values, dict) or any(isinstance(key, str) and key.startswith('_') for key in values):
        raise ValueError('internal settings are not accepted from configuration')
    shared = values.get('shared_index', {})
    if (not isinstance(shared, dict) or set(shared) - _SHARED_INDEX_KEYS or
            any(isinstance(key, str) and key.startswith('_') for key in shared) or
            'qmd_command' not in shared):
        raise ValueError('shared_index must contain only public canonical settings')


def _command_argv(value) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError('shared_index qmd_command requires explicit JSON argv')
    return value


def _json_hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _record_index(values: dict) -> dict:
    command = _command_argv(values['qmd_command'])
    return {'qmd_command_sha256': _json_hash(command), 'models': values.get('models', {}),
            'model_provenance': values.get('model_provenance', {}),
            'max_note_bytes': values.get('max_note_bytes', 1024 * 1024)}


def _shared_index_values(state: Path, values: dict, *, use_owned: bool) -> tuple[dict, str]:
    supplied = dict(values['shared_index'])
    supplied['qmd_command'] = _command_argv(supplied['qmd_command'])
    expected = _record_index(supplied)
    path = state / _SHARED_SETTINGS
    if use_owned and path.is_file():
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(data, dict) or set(data) != {'schema', 'index', 'settings_sha256'} or
                data.get('schema') != 2 or not isinstance(data.get('index'), dict) or
                set(data['index']) != _SHARED_RECORD_KEYS or
                data['settings_sha256'] != _json_hash(data['index'])):
            raise ValueError('invalid owned shared settings')
        if data['index']['qmd_command_sha256'] != expected['qmd_command_sha256']:
            raise ValueError('trusted shared qmd_command does not match writer record')
        loaded = {'qmd_command': supplied['qmd_command'], 'models': data['index']['models'],
                  'model_provenance': data['index']['model_provenance'],
                  'max_note_bytes': data['index']['max_note_bytes']}
        return loaded, data['settings_sha256']
    return supplied, _json_hash(expected)


def _build_scopes(home: Path, values: dict, *, for_refresh: bool = False) -> tuple[dict[str, Settings], list[tuple[str, str]]]:
    _validate_public_values(values)
    system, actual = identity(Path(home))
    profile = str(values.get('profile_id', ''))
    if not _PROFILE.fullmatch(profile) or profile != actual:
        raise ValueError('profile_id does not match the active profile home')
    vault = Path(values['vault_root']).absolute()
    shared_state = system / 'knowledge-base' / 'shared'
    private_state = (system / 'knowledge-base' / 'profile' if profile == 'default'
                     else Path(home) / 'knowledge-base' / 'state')
    writer = profile == values.get('shared_writer_profile', 'default')
    retrieval = {key: values[key] for key in _RETRIEVAL_KEYS if key in values}
    private_values = {key: values[key] for key in _PRIVATE_KEYS if key in values}
    private_values.update({'vault_root': str(vault / 'profiles' / profile),
                           'collections': {'profile-' + profile: '.'}, 'state_dir': str(private_state)})
    configs: dict[str, Settings] = {}
    failures: list[tuple[str, str]] = []
    try:
        shared_index, shared_settings_sha256 = _shared_index_values(
            shared_state, values, use_owned=not (writer and for_refresh))
        shared_values = {
            **shared_index,
            **retrieval, 'vault_root': str(vault / 'shared'), 'collections': {'shared': '.'},
            'state_dir': str(shared_state), 'exclusions': [],
        }
        shared = Settings(system, shared_values, state_namespace=shared_state, scope='shared',
                          require_model_provenance=True)
        shared.shared_settings_sha256 = shared_settings_sha256
        configs['shared'] = shared
    except Exception as exc:  # health: allow BLE001 -- isolate shared construction failure by type.
        failures.append(('shared', type(exc).__name__))
    try:
        configs['private'] = Settings(Path(home), private_values, state_namespace=private_state,
                                      scope='profile:' + profile, require_model_provenance=True)
    except Exception as exc:  # health: allow BLE001 -- isolate private construction failure by type.
        failures.append(('private', type(exc).__name__))
    return configs, failures


def build_scopes(home: Path, values: dict) -> dict[str, Settings]:
    return _build_scopes(home, values)[0]


def authorized(values: dict, platform: str, sender_id: str) -> bool:
    platform = (platform or '').lower()
    if platform == 'bot_room':
        return False
    if platform == 'api_server':
        try:
            from gateway.hosted_room_execution_policy import current_room_execution_policy
            return current_room_execution_policy() is None
        except Exception:  # health: allow BLE001 -- pinned room discriminator must fail API recall closed.
            return False
    if platform in _LOCAL_USER_PLATFORMS:
        return True
    if platform not in {'whatsapp', 'whatsapp_cloud'} or not isinstance(sender_id, str) or not sender_id:
        return False
    digest = hashlib.sha256(sender_id.encode()).hexdigest()
    allowed = values.get('allowed_sender_hashes', [])
    if isinstance(allowed, str):
        allowed = [item.strip() for item in allowed.split(',') if item.strip()]
    return isinstance(allowed, list) and digest in allowed


def _records(cfg: Settings, query: str, *, local_status: Settings | None = None) -> list[dict]:
    manifest = json.loads((cfg.state / 'manifest.json').read_text(encoding='utf-8-sig'))
    age = time.time() - manifest['updated_at']
    from . import StaleIndexError, scope_status, signature
    scope_name = 'shared' if cfg.scope == 'shared' else 'private'
    settings_mismatch = (cfg.scope == 'shared' and
                         manifest.get('shared_settings_sha256') != cfg.shared_settings_sha256)
    if manifest.get('signature') != signature(cfg) or settings_mismatch or age > cfg.interval * 1.5:
        raise StaleIndexError()
    started = time.monotonic()
    rows = search(cfg, query)
    text = render(cfg, rows, manifest['files'], query=query, include_score=True)
    if local_status is not None:
        scope_status(local_status, scope_name, 'recalled' if text else 'abstained',
                     latency_ms=round((time.monotonic() - started) * 1000, 2))
    return json.loads(text[text.index('['):]) if text else []


def _query_text(query) -> str:
    if isinstance(query, list):
        query = ' '.join(part.get('text', '') for part in query
                         if isinstance(part, dict) and part.get('type') == 'text')
    return query if isinstance(query, str) else ''


def _select_records(by_scope: dict[str, list[dict]], limit: int, budget: int) -> list[dict]:
    """Represent each nonempty scope, then fill globally by score; private wins exact ties."""
    ranked = {scope: sorted(rows, key=lambda row: (-float(row.get('_score', -1)),
                                                    str(row.get('source', ''))))
              for scope, rows in by_scope.items()}
    priority = {'private': 0, 'shared': 1}
    selected: list[dict] = []
    used: set[int] = set()

    def try_add(record: dict) -> bool:
        public = {key: value for key, value in record.items() if key != '_score'}
        candidate = PREFIX + json.dumps(selected + [public], ensure_ascii=False)
        if len(selected) >= limit or len(candidate) > budget:
            return False
        selected.append(public)
        used.add(id(record))
        return True

    if limit == 1:
        candidates = [(scope, row) for scope, rows in ranked.items() for row in rows]
        candidates.sort(key=lambda item: (-float(item[1].get('_score', -1)),
                                          priority.get(item[0], 2), str(item[1].get('source', ''))))
        if candidates:
            try_add(candidates[0][1])
        return selected

    representatives = [(scope, rows[0]) for scope, rows in ranked.items() if rows]
    representatives.sort(key=lambda item: (priority.get(item[0], 2),
                                            -float(item[1].get('_score', -1)),
                                            str(item[1].get('source', ''))))
    for _scope, record in representatives:
        try_add(record)
    remaining = [(scope, row) for scope, rows in ranked.items() for row in rows if id(row) not in used]
    remaining.sort(key=lambda item: (-float(item[1].get('_score', -1)),
                                     priority.get(item[0], 2), str(item[1].get('source', ''))))
    for _scope, record in remaining:
        try_add(record)
    return selected


def recall_two_level(home: Path, values: dict, query, *, platform: str, sender_id: str = '') -> dict | None:
    if not values.get('enabled', False) or not authorized(values, platform, sender_id):
        return None
    query = _query_text(query)
    if not query.strip():
        return None
    try:
        configs, build_failures = _build_scopes(Path(home), values)
    except Exception:  # health: allow BLE001 -- identity/config failure is fail-closed and redacted.
        return {'context': 'Knowledge base unavailable (configuration_error). Normal chat may continue; no recalled evidence.'}
    failures = [(scope, {'code': 'configuration_error'}) for scope, _kind in build_failures]
    by_scope: dict[str, list[dict]] = {}
    local_status = configs.get('private')
    if configs:
        with ThreadPoolExecutor(max_workers=len(configs), thread_name_prefix='kb-read') as pool:
            future_scopes = {pool.submit(_records, cfg, query, local_status=local_status): scope
                             for scope, cfg in configs.items()}
            for future in as_completed(future_scopes):
                scope = future_scopes[future]
                try:
                    by_scope[scope] = future.result()
                except Exception as exc:  # health: allow BLE001 -- scope failure is redacted and isolated.
                    from . import _last_error, failure_details, scope_status
                    details = failure_details(exc)
                    failures.append((scope, details))
                    if local_status is not None:
                        scope_status(local_status, scope, 'error', **details,
                                     last_error=_last_error(details))
    selected = _select_records(by_scope, int(values.get('top_k', 3)),
                               int(values.get('max_context_chars', 6000)))
    if selected:
        return {'context': PREFIX + json.dumps(selected, ensure_ascii=False)}
    if failures:
        from . import failure_label
        labels = ', '.join(scope + ':' + failure_label(details) for scope, details in failures)
        return {'context': 'Knowledge base unavailable in bounded scope(s) (' + labels +
                '). Normal chat may continue; no recalled evidence.'}
    return None


def _persist_shared_settings(cfg: Settings) -> str:
    from . import ensure_state, verify_model_provenance, write_json
    verify_model_provenance(cfg)
    ensure_state(cfg)
    index = {'qmd_command_sha256': _json_hash(cfg.command), 'models': cfg.models,
             'model_provenance': cfg.model_provenance, 'max_note_bytes': cfg.max_note_bytes}
    settings_sha256 = _json_hash(index)
    write_json(cfg.state / _SHARED_SETTINGS,
               {'schema': 2, 'index': index, 'settings_sha256': settings_sha256})
    cfg.shared_settings_sha256 = settings_sha256
    return settings_sha256


def status_two_level(home: Path, values: dict) -> dict:
    try:
        configs, failures = _build_scopes(Path(home), values)
    except Exception:  # health: allow BLE001 -- status exposes only a redacted type.
        error = {'state': 'error', 'code': 'configuration_error'}
        return {'shared': dict(error), 'private': dict(error)}
    states = {scope: {'state': 'error', 'code': 'configuration_error'} for scope, _kind in failures}
    lookup_path = configs.get('private').state / 'scope-status.json' if configs.get('private') else None
    try:
        lookups = (json.loads(lookup_path.read_text(encoding='utf-8-sig'))
                   if lookup_path is not None and lookup_path.is_file() else {})
    except (OSError, ValueError, json.JSONDecodeError):
        lookups = {}
    for name, cfg in configs.items():
        path = cfg.state / 'status.json'
        try:
            refresh_state = (json.loads(path.read_text(encoding='utf-8-sig'))
                             if path.is_file() else {'state': 'unprovisioned'})
            lookup_state = lookups.get(name, {'state': 'unobserved'})
            states[name] = {**refresh_state, 'refresh': refresh_state, 'lookup': lookup_state}
        except (OSError, ValueError, json.JSONDecodeError):
            states[name] = {'state': 'error', 'code': 'invalid_status_record'}
    return states


def refresh_two_level(home: Path, values: dict) -> dict:
    configs, failures = _build_scopes(Path(home), values, for_refresh=True)
    from . import acquire_refresh_lock, ensure_state, refresh, refresh_locked
    result: dict[str, dict] = {scope: {'error_type': kind} for scope, kind in failures}
    order = ['private']
    if values.get('profile_id') == values.get('shared_writer_profile', 'default'):
        order.append('shared')
    for scope in order:
        cfg = configs.get(scope)
        if cfg is None:
            continue
        try:
            if scope == 'shared':
                ensure_state(cfg)
                lock = acquire_refresh_lock(cfg.state / 'refresh.lock')
                try:
                    settings_sha256 = _persist_shared_settings(cfg)
                    result[scope] = refresh_locked(
                        cfg, timeout=cfg.interval / 2,
                        manifest_fields={'shared_settings_sha256': settings_sha256})
                finally:
                    lock.close()
            else:
                result[scope] = refresh(cfg, timeout=cfg.interval / 2)
        except Exception as exc:  # health: allow BLE001 -- one scope cannot skip the other owner cycle.
            result[scope] = {'error_type': type(exc).__name__}
    return result
