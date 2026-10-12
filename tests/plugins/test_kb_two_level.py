"""Two-level shared plus profile-private KB behavior contracts."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import time

import pytest

SOURCE = Path(__file__).parents[2] / 'standalone-kb'


def plugin():
    for name in list(sys.modules):
        if name == 'two_level_plugin' or name.startswith('two_level_plugin.'):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location('two_level_plugin', SOURCE / '__init__.py', submodule_search_locations=[str(SOURCE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def two_level(tmp_path):
    system = tmp_path / 'hermes-root'
    profiles = system / 'profiles'
    vault = tmp_path / 'Hermes Knowledge'
    for relative in ['shared', 'profiles/default', 'profiles/alpha', 'profiles/beta']:
        (vault / relative).mkdir(parents=True)
    (vault / 'shared' / 'Shared.md').write_text('# Shared\nSH-100 uses the silver protocol.\n')
    (vault / 'profiles' / 'alpha' / 'Private.md').write_text('# Alpha\nPA-200 uses the amber protocol.\n')
    (vault / 'profiles' / 'beta' / 'Private.md').write_text('# Beta\nPB-300 uses the blue protocol.\n')
    alpha = profiles / 'alpha'
    beta = profiles / 'beta'
    default = system
    for home in [alpha, beta]:
        (home / 'knowledge-base' / 'state').mkdir(parents=True)
    (system / 'knowledge-base' / 'shared').mkdir(parents=True)
    (system / 'knowledge-base' / 'profile').mkdir(parents=True)

    def values(profile):
        command = [sys.executable, '-c', 'raise SystemExit(1)']
        return {'enabled': True, 'layout': 'two_level', 'profile_id': profile,
                'vault_root': str(vault), 'shared_writer_profile': 'default',
                'qmd_command': command, 'shared_index': {'qmd_command': command}, 'mode': 'lexical',
                'top_k': 3, 'max_context_chars': 1000, 'timeout_seconds': 2,
                'refresh_interval_seconds': 60, 'allowed_sender_hashes': [hashlib.sha256(b'owner').hexdigest()]}
    return system, default, alpha, beta, vault, values


def provision(api, configs):
    for cfg in configs.values():
        api.ensure_state(cfg)
        (cfg.state / 'index.sqlite').write_bytes(b'fixture')
        (cfg.state / 'config').mkdir(exist_ok=True)
        (cfg.state / 'config' / 'index.yml').write_text('collections: {}')
        manifest = {'signature': api.signature(cfg), 'updated_at': time.time(), 'files': api.snapshot(cfg)}
        if cfg.scope == 'shared':
            manifest['shared_settings_sha256'] = api.kb_multiscope._persist_shared_settings(cfg)
        api.write_json(cfg.state / 'manifest.json', manifest)


def fake_rows(cfg, query):
    rows = []
    for key in json.loads((cfg.state / 'manifest.json').read_text())['files']:
        collection, relative = key.split('/', 1)
        rows.append({'file': f'qmd://{collection}/{relative}', 'score': 1, 'line': 2})
    return rows


def test_two_profiles_see_shared_and_only_their_private_canary(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    monkeypatch.setattr(api.kb_multiscope, 'search', fake_rows)
    alpha_cfg = api.kb_multiscope.build_scopes(alpha, values('alpha'))
    beta_cfg = api.kb_multiscope.build_scopes(beta, values('beta'))
    provision(api, alpha_cfg)
    provision(api, beta_cfg)
    shared_manifest = json.loads((alpha_cfg['shared'].state / 'manifest.json').read_text())
    assert shared_manifest['files']
    rendered = api.render(alpha_cfg['shared'], fake_rows(alpha_cfg['shared'], 'uses protocol'), shared_manifest['files'], query='uses protocol')
    assert rendered, {'rows': fake_rows(alpha_cfg['shared'], 'uses protocol'), 'manifest': shared_manifest,
                      'source': alpha_cfg['shared'].source('shared', 'Shared.md')}
    assert api.kb_multiscope._records(alpha_cfg['shared'], 'uses protocol')
    assert api.kb_multiscope._records(alpha_cfg['private'], 'uses protocol')
    a = api.kb_multiscope.recall_two_level(alpha, values('alpha'), 'uses protocol', platform='cli')['context']
    b = api.kb_multiscope.recall_two_level(beta, values('beta'), 'uses protocol', platform='cli')['context']
    assert 'silver protocol' in a and 'amber protocol' in a and 'blue protocol' not in a
    assert 'silver protocol' in b and 'blue protocol' in b and 'amber protocol' not in b
    assert '"scope": "shared"' in a and '"scope": "profile:alpha"' in a


def test_binding_rejects_wrong_home_profile_other_platform_and_sender(two_level):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    with pytest.raises(ValueError):
        api.kb_multiscope.build_scopes(alpha, values('beta'))
    assert api.kb_multiscope.authorized(values('alpha'), 'cli', '')
    assert api.kb_multiscope.authorized(values('alpha'), 'whatsapp', 'owner')
    assert not api.kb_multiscope.authorized(values('alpha'), 'whatsapp', 'stranger')
    assert not api.kb_multiscope.authorized(values('alpha'), 'telegram', 'owner')
    data = values('alpha')
    data['allowed_sender_hashes'] = data['allowed_sender_hashes'][0]
    data['qmd_command'] = json.dumps(data['qmd_command'])
    assert api.kb_multiscope.authorized(data, 'whatsapp', 'owner')
    assert api.kb_multiscope.build_scopes(alpha, data)['private'].command[0] == sys.executable


def test_combined_top_k_and_context_budget_are_global(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    data = values('alpha')
    data.update(top_k=1, max_context_chars=600)
    configs = api.kb_multiscope.build_scopes(alpha, data)
    provision(api, configs)
    monkeypatch.setattr(api.kb_multiscope, 'search', fake_rows)
    text = api.kb_multiscope.recall_two_level(alpha, data, 'uses protocol', platform='cli')['context']
    assert len(text) <= 600
    assert len(json.loads(text[text.index('['):])) == 1


def test_shared_refresh_has_one_writer_and_nonwriter_updates_private_only(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    calls = []
    monkeypatch.setattr(api, 'run', lambda cfg, args, **kwargs: calls.append((cfg.scope, tuple(args))) or '')
    beta_result = api.kb_multiscope.refresh_two_level(beta, values('beta'))
    assert set(beta_result) == {'private'}
    default_result = api.kb_multiscope.refresh_two_level(default, values('default'))
    assert set(default_result) == {'shared', 'private'}
    assert sum(scope == 'shared' for scope, args in calls) == 1


def test_shared_refresh_lock_serializes_competing_writers(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    cfg = api.kb_multiscope.build_scopes(default, values('default'))['shared']
    entered = []
    def slow_run(*args, **kwargs):
        entered.append(1)
        time.sleep(0.2)
        return ''
    monkeypatch.setattr(api, 'run', slow_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(api.refresh, cfg) for _ in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except RuntimeError:
            outcomes.append('locked')
    assert outcomes.count('locked') == 1 and len(entered) == 1


def test_shared_index_signature_is_canonical_while_private_overrides_remain_local(two_level):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    shared_model = vault.parent / 'shared-embed.gguf'
    alpha_model = vault.parent / 'alpha-embed.gguf'
    beta_model = vault.parent / 'beta-embed.gguf'
    for path, body in [(shared_model, b'shared'), (alpha_model, b'alpha'), (beta_model, b'beta')]:
        path.write_bytes(body)
    shared_index = {
        'qmd_command': values('alpha')['shared_index']['qmd_command'],
        'models': {'embed': str(shared_model)},
        'model_provenance': {'embed': {'sha256': hashlib.sha256(b'shared').hexdigest(), 'bytes': 6}},
    }
    alpha_values = values('alpha') | {
        'mode': 'lexical', 'exclusions': ['alpha-only/**'],
        'models': {'embed': str(alpha_model)},
        'model_provenance': {'embed': {'sha256': hashlib.sha256(b'alpha').hexdigest(), 'bytes': 5}},
        'shared_index': shared_index,
    }
    beta_values = values('beta') | {
        'mode': 'hybrid', 'exclusions': ['beta-only/**'],
        'models': {'embed': str(beta_model)},
        'model_provenance': {'embed': {'sha256': hashlib.sha256(b'beta').hexdigest(), 'bytes': 4}},
        'shared_index': shared_index,
    }
    alpha_cfg = api.kb_multiscope.build_scopes(alpha, alpha_values)
    beta_cfg = api.kb_multiscope.build_scopes(beta, beta_values)

    assert api.signature(alpha_cfg['shared']) == api.signature(beta_cfg['shared'])
    assert alpha_cfg['shared'].exclusions == beta_cfg['shared'].exclusions
    assert alpha_cfg['shared'].models == beta_cfg['shared'].models == {'embed': str(shared_model)}
    assert alpha_cfg['shared'].mode == 'lexical' and beta_cfg['shared'].mode == 'hybrid'
    assert api.signature(alpha_cfg['private']) != api.signature(beta_cfg['private'])
    assert 'alpha-only/**' in alpha_cfg['private'].exclusions
    assert 'beta-only/**' in beta_cfg['private'].exclusions


def test_balanced_scope_selection_prevents_private_starvation_and_topk1_uses_best(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    for number in range(2, 5):
        (vault / 'shared' / f'Shared-{number}.md').write_text(
            f'# Shared {number}\nshared distractor uses protocol {number}.\n', encoding='utf-8')
    data = values('alpha') | {'top_k': 3, 'max_context_chars': 3000}
    configs = api.kb_multiscope.build_scopes(alpha, data)
    provision(api, configs)

    def scored_rows(cfg, query):
        rows = fake_rows(cfg, query)
        for row in rows:
            row['score'] = 0.99 if cfg.scope == 'profile:alpha' else 0.60
        return rows

    monkeypatch.setattr(api.kb_multiscope, 'search', scored_rows)
    context = api.kb_multiscope.recall_two_level(
        alpha, data, 'uses protocol', platform='cli')['context']
    records = json.loads(context[context.index('['):])
    assert len(records) == 3
    assert {record['scope'] for record in records} == {'shared', 'profile:alpha'}

    one = api.kb_multiscope.recall_two_level(
        alpha, data | {'top_k': 1}, 'uses protocol', platform='cli')['context']
    assert json.loads(one[one.index('['):])[0]['scope'] == 'profile:alpha'


def test_internal_settings_are_keyword_only_and_plain_config_injection_is_rejected(two_level):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    injected = values('alpha') | {'_state_namespace': str(alpha.parent / 'beta'), '_scope': 'shared'}
    with pytest.raises(ValueError, match='internal'):
        api.kb_multiscope.build_scopes(alpha, injected)
    with pytest.raises(ValueError, match='internal'):
        api.Settings(alpha, injected | {
            'vault_root': str(vault / 'profiles' / 'alpha'),
            'collections': {'profile-alpha': '.'},
            'state_dir': str(alpha / 'knowledge-base' / 'state'),
        })


def test_windows_alias_segments_are_rejected_but_normal_unicode_and_spaces_are_allowed(two_level):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    cfg = api.kb_multiscope.build_scopes(alpha, values('alpha'))['private']
    (vault / 'profiles' / 'alpha' / 'Folder ü').mkdir()
    (vault / 'profiles' / 'alpha' / 'Folder ü' / 'note name.md').write_text('normal unicode path')
    (vault / 'profiles' / 'alpha' / 'private.').mkdir(exist_ok=True)
    (vault / 'profiles' / 'alpha' / 'private.' / 'alias.md').write_text('must not be reachable')
    assert cfg.source('profile-alpha', 'Folder ü/note name.md') is not None
    for relative in ['private./alias.md', 'private /alias.md', 'Folder ü/note name.md.', 'Folder ü/note name.md ']:
        assert cfg.source('profile-alpha', relative) is None


def test_missing_private_scope_and_multimodal_text_still_recall_shared(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    configs = api.kb_multiscope.build_scopes(alpha, values('alpha'))
    provision(api, {'shared': configs['shared']})
    shutil.rmtree(vault / 'profiles' / 'alpha')
    monkeypatch.setattr(api.kb_multiscope, 'search', fake_rows)
    result = api.kb_multiscope.recall_two_level(
        alpha, values('alpha'), [{'type': 'image', 'url': 'fixture'},
                                 {'type': 'text', 'text': 'uses protocol'}], platform='tui')
    assert result and 'silver protocol' in result['context']
    assert 'profile:alpha' not in result['context']


def test_private_refresh_failure_does_not_skip_shared_owner_cycle(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    calls = []

    def refresh(cfg, **kwargs):
        calls.append(cfg.scope)
        if cfg.scope.startswith('profile:'):
            raise RuntimeError('private fixture failure')
        return {'changed': False, 'file_count': 1}

    monkeypatch.setattr(api, 'refresh', refresh)
    monkeypatch.setattr(api, 'refresh_locked', refresh)
    result = api.kb_multiscope.refresh_two_level(default, values('default'))
    assert calls == ['profile:default', 'shared']
    assert result['private'] == {'error_type': 'RuntimeError'}
    assert result['shared']['file_count'] == 1


def test_shared_reader_writes_only_profile_local_scope_status(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    configs = api.kb_multiscope.build_scopes(alpha, values('alpha'))
    provision(api, configs)
    monkeypatch.setattr(api.kb_multiscope, 'search', fake_rows)
    shared_status = configs['shared'].state / 'status.json'
    shared_status.unlink(missing_ok=True)
    api.kb_multiscope.recall_two_level(alpha, values('alpha'), 'uses protocol', platform='cli')
    assert not shared_status.exists()
    scope_status = json.loads((configs['private'].state / 'scope-status.json').read_text())
    assert scope_status['shared']['state'] == 'recalled'


def test_authorization_matches_documented_runtime_surfaces_and_exact_sender_identity(two_level):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    data = values('alpha')
    for platform in ['cli', 'tui', 'desktop', 'api_server']:
        assert api.kb_multiscope.authorized(data, platform, '')
    for platform in ['cron', 'subagent', 'telegram', 'webhook']:
        assert not api.kb_multiscope.authorized(data, platform, 'owner')
    assert api.kb_multiscope.authorized(data, 'whatsapp', 'owner')
    assert not api.kb_multiscope.authorized(data, 'whatsapp', ' owner ')


def test_model_provenance_is_verified_only_at_refresh(two_level, monkeypatch):
    system, default, alpha, beta, vault, values = two_level
    api = plugin()
    model = vault.parent / 'embed.gguf'
    model.write_bytes(b'pinned model')
    metadata = {'embed': {'sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
                          'bytes': model.stat().st_size}}
    data = values('alpha') | {'mode': 'semantic', 'models': {'embed': str(model)},
                              'model_provenance': metadata}
    cfg = api.kb_multiscope.build_scopes(alpha, data)['private']
    monkeypatch.setattr(api, 'run', lambda *args, **kwargs: '')
    api.refresh(cfg)
    model.write_bytes(b'replaced same path')
    with pytest.raises(ValueError, match='provenance'):
        api.refresh(cfg)
