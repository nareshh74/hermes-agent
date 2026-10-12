"""Final source-review regressions for two-level KB isolation and concurrency."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time

import psutil
import pytest

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

SOURCE = Path(__file__).parents[2] / 'standalone-kb'
PROFILE_NAMES = (
    'default', 'alpha', 'beta', 'gamma', 'delta',
    'epsilon', 'zeta', 'eta', 'theta',
)


def plugin():
    for name in list(sys.modules):
        if name == 'final_review_plugin' or name.startswith('final_review_plugin.'):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        'final_review_plugin', SOURCE / '__init__.py', submodule_search_locations=[str(SOURCE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def settings(vault: Path, profile: str, **overrides):
    command = [sys.executable, '-c', 'raise SystemExit(1)']
    values = {
        'enabled': True, 'layout': 'two_level', 'profile_id': profile,
        'vault_root': str(vault), 'shared_writer_profile': 'default',
        'qmd_command': command, 'shared_index': {'qmd_command': command},
        'mode': 'lexical', 'top_k': 3, 'max_context_chars': 3000,
        'timeout_seconds': 1, 'refresh_interval_seconds': 3600,
    }
    values.update(overrides)
    return values


def provision(api, configs):
    for cfg in configs.values():
        api.ensure_state(cfg)
        (cfg.state / 'index.sqlite').write_bytes(b'fixture')
        (cfg.state / 'config').mkdir(exist_ok=True)
        (cfg.state / 'config' / 'index.yml').write_text('collections: {}', encoding='utf-8')
        manifest = {'signature': api.signature(cfg), 'updated_at': time.time(), 'files': api.snapshot(cfg)}
        if cfg.scope == 'shared':
            manifest['shared_settings_sha256'] = api.kb_multiscope._persist_shared_settings(cfg)
        api.write_json(cfg.state / 'manifest.json', manifest)


def rows(cfg, _query):
    manifest = json.loads((cfg.state / 'manifest.json').read_text(encoding='utf-8'))
    return [{'file': 'qmd://' + key, 'score': 1.0, 'line': 2} for key in manifest['files']]


def make_nine_profile_fixture(tmp_path):
    system = tmp_path / 'hermes-root'
    vault = tmp_path / 'vault'
    (vault / 'shared').mkdir(parents=True)
    (vault / 'shared' / 'Shared.md').write_text('# Shared\ncommon canary marker SHARED-NINE.\n', encoding='utf-8')
    homes = {}
    for name in PROFILE_NAMES:
        home = system if name == 'default' else system / 'profiles' / name
        private = vault / 'profiles' / name
        private.mkdir(parents=True)
        (private / 'Private.md').write_text(
            f'# {name}\nprivate canary marker ONLY-{name}.\n', encoding='utf-8')
        homes[name] = home
    return system, vault, homes


def test_nine_profiles_include_shared_and_only_the_bound_private_canary(tmp_path, monkeypatch):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    monkeypatch.setattr(api.kb_multiscope, 'search', rows)
    shared_done = False
    for name, home in homes.items():
        configs = api.kb_multiscope.build_scopes(home, settings(vault, name))
        provision(api, {'private': configs['private']})
        if not shared_done:
            provision(api, {'shared': configs['shared']})
            shared_done = True
    for name, home in homes.items():
        context = api.kb_multiscope.recall_two_level(
            home, settings(vault, name), 'canary marker', platform='tui')['context']
        assert 'SHARED-NINE' in context and f'ONLY-{name}' in context
        assert all(f'ONLY-{other}' not in context for other in PROFILE_NAMES if other != name)


def test_context_local_home_selects_matching_config_and_mismatch_fails_closed(tmp_path, monkeypatch):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    monkeypatch.setattr(api.kb_multiscope, 'search', rows)
    alpha = homes['alpha']
    good = settings(vault, 'alpha')
    configs = api.kb_multiscope.build_scopes(alpha, good)
    provision(api, configs)
    configs_by_home = {str(alpha.resolve()): good}

    class Context:
        def get_config(self, key, default):
            return configs_by_home.get(str(get_hermes_home().resolve()), default) if key == 'kb' else default
        def register_hook(self, _name, callback):
            self.callback = callback
        def register_cli_command(self, *_args):
            pass

    ctx = Context()
    api.register(ctx)
    token = set_hermes_home_override(alpha)
    try:
        assert 'ONLY-alpha' in ctx.callback(user_message='canary marker', platform='tui')['context']
        configs_by_home[str(alpha.resolve())] = settings(vault, 'beta')
        denied = ctx.callback(user_message='canary marker', platform='tui')['context']
        assert 'unavailable' in denied and 'ONLY-' not in denied
    finally:
        reset_hermes_home_override(token)


def test_only_writer_persists_canonical_shared_index_settings(tmp_path, monkeypatch):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    monkeypatch.setattr(api, 'refresh', lambda cfg, **_kwargs: {'changed': False, 'file_count': 1})
    monkeypatch.setattr(api, 'refresh_locked', lambda cfg, **_kwargs: {'changed': False, 'file_count': 1})
    writer = settings(vault, 'default', qmd_command=['private-qmd'],
                      shared_index={'qmd_command': ['canonical-qmd'], 'max_note_bytes': 2048})
    api.kb_multiscope.refresh_two_level(homes['default'], writer)
    owned = system / 'knowledge-base' / 'shared' / 'shared-settings.json'
    before = owned.read_bytes()
    reader = settings(vault, 'alpha', qmd_command=['untrusted-private-qmd'],
                      shared_index={'qmd_command': ['canonical-qmd'], 'max_note_bytes': 2048},
                      max_note_bytes=4096,
                      exclusions=['reader-only/**'])
    shared = api.kb_multiscope.build_scopes(homes['alpha'], reader)['shared']
    assert shared.command == ['canonical-qmd'] and shared.max_note_bytes == 2048
    api.kb_multiscope.refresh_two_level(homes['alpha'], reader)
    assert owned.read_bytes() == before


def test_synchronized_shared_refresh_and_read_asserts_content_not_result_types(tmp_path, monkeypatch):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    home = homes['alpha']
    values = settings(vault, 'alpha')
    configs = api.kb_multiscope.build_scopes(home, values)
    provision(api, configs)
    monkeypatch.setattr(api.kb_multiscope, 'search', rows)
    shared_note = vault / 'shared' / 'Shared.md'
    shared_note.write_text('# Shared\ncommon canary marker SHARED-UPDATED.\n', encoding='utf-8')
    entered = threading.Event()
    release = threading.Event()

    def blocked_run(_cfg, _args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return ''

    monkeypatch.setattr(api, 'run', blocked_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshing = pool.submit(api.refresh, configs['shared'])
        assert entered.wait(5)
        during = pool.submit(api.kb_multiscope.recall_two_level, home, values,
                             'canary marker', platform='tui').result(timeout=5)
        release.set()
        refreshing.result(timeout=5)
    assert during and 'ONLY-alpha' in during['context']
    assert 'SHARED-NINE' not in during['context']
    after = api.kb_multiscope.recall_two_level(home, values, 'canary marker', platform='tui')
    assert after and 'SHARED-UPDATED' in after['context'] and 'ONLY-alpha' in after['context']


@pytest.mark.platforms('windows')
def test_two_scope_thread_timeouts_kill_both_descendant_trees(tmp_path):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    pid_dir = tmp_path / 'pids'
    pid_dir.mkdir()
    script = tmp_path / 'spawn.py'
    script.write_text(
        'import json,os,subprocess,sys,time\nfrom pathlib import Path\n'
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])\n'
        'name=Path(os.environ["INDEX_PATH"]).parent.name\n'
        'Path(sys.argv[1],name+".json").write_text(json.dumps([os.getpid(),child.pid]))\n'
        'time.sleep(30)\n', encoding='utf-8')
    values = settings(vault, 'alpha', qmd_command=[sys.executable, str(script), str(pid_dir)],
                      shared_index={'qmd_command': [sys.executable, str(script), str(pid_dir)]},
                      timeout_seconds=2.0)
    configs = api.kb_multiscope.build_scopes(homes['alpha'], values)
    provision(api, configs)
    result = api.kb_multiscope.recall_two_level(
        homes['alpha'], values, 'canary marker', platform='tui')
    assert result and result['context'].count('qmd_timeout') == 2
    pid_files = list(pid_dir.glob('*.json'))
    assert len(pid_files) == 2
    recorded = [pid for path in pid_files for pid in json.loads(path.read_text())]
    deadline = time.monotonic() + 3
    while any(psutil.pid_exists(pid) for pid in recorded) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(psutil.pid_exists(pid) for pid in recorded)
    assert all(not list(cfg.state.glob('kb-process-*')) for cfg in configs.values())


def test_tampered_shared_data_command_is_never_executed(tmp_path):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    safe = [sys.executable, '-c', 'print("[]")']
    values = settings(vault, 'alpha', qmd_command=safe,
                      shared_index={'qmd_command': safe})
    configs = api.kb_multiscope.build_scopes(homes['alpha'], values)
    provision(api, configs)
    canary = tmp_path / 'COMMAND-EXECUTED'
    poison = [sys.executable, '-c',
              f'from pathlib import Path; Path({str(canary)!r}).write_text("pwned"); print("[]")']
    record = json.loads((configs['shared'].state / 'shared-settings.json').read_text())
    record['index']['qmd_command'] = poison
    api.write_json(configs['shared'].state / 'shared-settings.json', record)

    api.kb_multiscope.recall_two_level(
        homes['alpha'], values, 'canary marker', platform='tui')
    assert not canary.exists()


def test_explicit_shared_command_mismatch_fails_only_shared_scope(tmp_path, monkeypatch):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    command_a = [sys.executable, '-c', 'print("[]")']
    command_b = [sys.executable, '-c', 'print("different")']
    original = settings(vault, 'alpha', shared_index={'qmd_command': command_a})
    configs = api.kb_multiscope.build_scopes(homes['alpha'], original)
    provision(api, configs)
    monkeypatch.setattr(api.kb_multiscope, 'search', rows)

    mismatched = settings(vault, 'alpha', shared_index={'qmd_command': command_b})
    response = api.kb_multiscope.recall_two_level(
        homes['alpha'], mismatched, 'canary marker', platform='tui')['context']
    assert 'ONLY-alpha' in response
    assert 'SHARED-NINE' not in response


def test_competing_writer_cannot_publish_settings_without_refresh_lock(tmp_path, monkeypatch):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    command = [sys.executable, '-c', 'print("[]")']
    first = settings(vault, 'default', shared_index={'qmd_command': command, 'max_note_bytes': 2048})
    second = settings(vault, 'default', shared_index={'qmd_command': command, 'max_note_bytes': 4096})
    entered = threading.Event()
    release = threading.Event()

    def blocked_run(cfg, _args, **_kwargs):
        if cfg.scope == 'shared':
            entered.set()
            assert release.wait(5)
        return ''

    monkeypatch.setattr(api, 'run', blocked_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(api.kb_multiscope.refresh_two_level, homes['default'], first)
        assert entered.wait(5)
        owned = system / 'knowledge-base' / 'shared' / 'shared-settings.json'
        locked_bytes = owned.read_bytes()
        rejected = pool.submit(api.kb_multiscope.refresh_two_level, homes['default'], second).result(timeout=5)
        assert rejected['shared'] == {'error_type': 'RuntimeError'}
        assert owned.read_bytes() == locked_bytes
        release.set()
        active.result(timeout=5)


def test_reader_during_settings_refresh_gets_private_or_consistent_shared_snapshot(tmp_path, monkeypatch):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    command = [sys.executable, '-c', 'print("[]")']
    initial = settings(vault, 'default', shared_index={'qmd_command': command, 'max_note_bytes': 2048})
    monkeypatch.setattr(api, 'run', lambda *_args, **_kwargs: '')
    first = api.kb_multiscope.refresh_two_level(homes['default'], initial)
    assert set(first) == {'private', 'shared'}
    reader_home = homes['alpha']
    reader_initial = settings(vault, 'alpha', shared_index={'qmd_command': command, 'max_note_bytes': 2048})
    provision(api, {'private': api.kb_multiscope.build_scopes(reader_home, reader_initial)['private']})
    (vault / 'shared' / 'Shared.md').write_text(
        '# Shared\ncommon canary marker SHARED-TRANSACTIONAL.\n', encoding='utf-8')
    entered = threading.Event()
    release = threading.Event()

    def blocked_run(cfg, _args, **_kwargs):
        if cfg.scope == 'shared':
            entered.set()
            assert release.wait(5)
        return ''

    monkeypatch.setattr(api, 'run', blocked_run)
    monkeypatch.setattr(api.kb_multiscope, 'search', rows)
    updated = settings(vault, 'default', shared_index={'qmd_command': command, 'max_note_bytes': 4096})
    reader_updated = settings(vault, 'alpha', shared_index={'qmd_command': command, 'max_note_bytes': 4096})
    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshing = pool.submit(api.kb_multiscope.refresh_two_level, homes['default'], updated)
        assert entered.wait(5)
        during = api.kb_multiscope.recall_two_level(
            reader_home, reader_updated, 'canary marker', platform='tui')['context']
        assert 'ONLY-alpha' in during and 'SHARED-' not in during
        release.set()
        refreshing.result(timeout=5)
    after = api.kb_multiscope.recall_two_level(
        reader_home, reader_updated, 'canary marker', platform='tui')['context']
    assert 'ONLY-alpha' in after and 'SHARED-TRANSACTIONAL' in after
    record = json.loads((system / 'knowledge-base/shared/shared-settings.json').read_text())
    manifest = json.loads((system / 'knowledge-base/shared/manifest.json').read_text())
    assert manifest['shared_settings_sha256'] == record['settings_sha256']


def test_status_reports_failed_shared_construction_and_healthy_private(tmp_path):
    system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    values = settings(vault, 'alpha')
    configs = api.kb_multiscope.build_scopes(homes['alpha'], values)
    provision(api, configs)
    record = json.loads((configs['shared'].state / 'shared-settings.json').read_text())
    record['index']['models'] = {'embed': 5}
    record['settings_sha256'] = api.kb_multiscope._json_hash(record['index'])
    api.write_json(configs['shared'].state / 'shared-settings.json', record)

    states = api.kb_multiscope.status_two_level(homes['alpha'], values)
    assert states['shared'] == {'state': 'error', 'code': 'configuration_error'}
    assert states['private']['state'] in {'unprovisioned', 'fresh'}
    del record['index']['models']
    record['settings_sha256'] = api.kb_multiscope._json_hash(record['index'])
    api.write_json(configs['shared'].state / 'shared-settings.json', record)
    missing = api.kb_multiscope.status_two_level(homes['alpha'], values)
    assert missing['shared'] == {'state': 'error', 'code': 'configuration_error'}
    assert missing['private']['state'] in {'unprovisioned', 'fresh'}
