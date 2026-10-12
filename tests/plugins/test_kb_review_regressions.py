"""Native Windows review regressions for the standalone KB boundary."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from tests.plugins.kb_test_support import require_qmd_fixture
from tests.plugins.test_kb_standalone import core, plugin


@pytest.fixture
def review_data(tmp_path):
    home = tmp_path / 'review profile'
    vault = tmp_path / 'review vault'
    (vault / 'approved' / 'private').mkdir(parents=True)
    note = vault / 'approved' / 'fact.md'
    note.write_text('# Deployment\nHX-731 uses the amber relay.\n')
    (home / 'cache').mkdir(parents=True)
    settings = {'enabled': True, 'vault_root': str(vault), 'collections': {'facts': 'approved'},
                'exclusions': ['private/**'], 'state_dir': str(home / 'cache' / 'naresh-kb'),
                'qmd_command': [sys.executable, '-c', 'raise SystemExit(1)'],
                'mode': 'lexical', 'max_context_chars': 1200}
    (home / 'cache').mkdir(exist_ok=True)
    return home, vault, note, settings


def test_refuses_protected_or_unowned_state(review_data):
    home, vault, note, settings = review_data
    api = core()
    with pytest.raises(ValueError):
        api.Settings(home, {**settings, 'state_dir': str(home / 'memories')})
    state = Path(settings['state_dir'])
    state.mkdir()
    (state / 'unrelated.txt').write_text('must not be overwritten')
    with pytest.raises(ValueError):
        api.Settings(home, settings)
    assert (state / 'unrelated.txt').read_text() == 'must not be overwritten'


def test_top_level_recursive_exclusions_and_index_manifest(review_data):
    home, vault, note, settings = review_data
    private = vault / 'private'
    private.mkdir()
    (private / 'hidden.md').write_text('SYNTHETIC EXCLUDED')
    settings.update(collections={'facts': '.'}, exclusions=['**/PRIVATE/**', '**/raw/**'])
    api = plugin()
    cfg = api.Settings(home, settings)
    assert cfg.source('facts', 'private/hidden.md') is None
    assert all('private' not in p.casefold().split('/') for p in api.snapshot(cfg))


def test_malformed_note_does_not_hide_valid_evidence_and_editor_line_refs(review_data):
    home, vault, note, settings = review_data
    api = core()
    cfg = api.Settings(home, settings)
    bad = note.with_name('invalid.md')
    bad.write_bytes(b'# invalid\n\xff\xfe')
    note.write_text('# Deployment\nfirst\u2028same editor line\nHX-731 uses amber.\n', encoding='utf-8')
    files = {'facts/invalid.md': api.digest(bad), 'facts/' + note.name: api.digest(note)}
    rows = [{'file': 'qmd://facts/invalid.md', 'score': 1}, {'file': 'qmd://facts/' + note.name, 'score': 1, 'line': 3}]
    text = api.render(cfg, rows, files, query='HX-731')
    records = json.loads(text[text.index('['):])
    assert len(records) == 1 and records[0]['end_line'] == 3
    assert 'first\u2028same editor line' in records[0]['quote']


def test_native_semantic_is_build_never_cpu_and_requires_enabled_reranker(review_data):
    home, vault, note, settings = review_data
    api = plugin()
    model = home / 'routing-fixture-only.gguf'
    model.write_bytes(b'GGUF not used for inference')
    cfg = api.Settings(home, {**settings, 'mode': 'semantic', 'rerank_enabled': True, 'models': {'embed': str(model)}})
    assert api.kb_backend.child_env(cfg)['QMD_FORCE_CPU'] == '1'
    with pytest.raises(ValueError, match='local'):
        api.kb_backend.search_args(cfg, 'Describe deployment approach')


def test_rank_position_is_not_semantic_confidence(review_data):
    home, vault, note, settings = review_data
    api = plugin()
    model = home / 'fake-model-for-routing-only.gguf'
    model.write_bytes(b'GGUF fixture')
    cfg = api.Settings(home, {**settings, 'mode': 'semantic', 'models': {role: str(model) for role in ['embed', 'generate', 'rerank']}})
    api.ensure_state(cfg)
    (cfg.state / 'index.sqlite').write_bytes(b'fake backend index')
    (cfg.state / 'config').mkdir()
    (cfg.state / 'config' / 'index.yml').write_text('collections: {}')
    row = {'file': 'qmd://facts/' + note.name, 'score': 0.9, 'explain': {'vectorScores': [0.05]}}
    cfg.command = [sys.executable, '-c', 'print(' + repr(json.dumps([row])) + ')']
    rows = api.search(cfg, 'Describe deployment approach')
    assert rows[0]['score'] == 0.05
    assert api.render(cfg, rows, {'facts/' + note.name: api.digest(note)}, query='Describe deployment approach') == ''


@pytest.mark.platforms('windows')
@pytest.mark.live_system_guard_bypass
@pytest.mark.allow_real_home_io
def test_real_models_paraphrase_and_unanswerable_abstention():
    fixture = require_qmd_fixture(require_semantic_settings=True)
    settings = json.loads(fixture.semantic_settings.read_text(encoding='utf-8'))
    settings.pop('min_score', None)
    home = Path(settings['state_dir']).parent.parent
    api = plugin()
    for mode in ['semantic', 'hybrid']:
        settings['mode'] = mode
        api.refresh(api.Settings(home, settings))
        response = api.recall(home, settings, 'How can I ship an updated API without outages?')
        assert response and 'canary traffic shifting' in response['context']
        assert 'My Note ä.md' in response['context']
        assert api.recall(home, settings, 'What is the lunar spacecraft fuel mixture?') is None


@pytest.mark.platforms('windows')
@pytest.mark.live_system_guard_bypass
def test_real_junction_is_denied_at_collection_source_and_scan(review_data):
    home, vault, note, settings = review_data
    target = vault / 'approved' / 'private'
    alias = vault / 'approved' / 'alias'
    command = Path(os.environ['SYSTEMROOT']) / 'System32' / 'cmd.exe'
    result = subprocess.run([str(command), '/c', 'mklink', '/J', str(alias), str(target)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert result.returncode == 0, result.stderr
    try:
        api = plugin()
        cfg = api.Settings(home, settings)
        assert cfg.source('facts', 'alias/secret.md') is None
        assert not any('alias' in p for p in api.snapshot(cfg))
        with pytest.raises(ValueError):
            api.Settings(home, {**settings, 'collections': {'facts': 'approved/alias'}})
    finally:
        alias.rmdir()


@pytest.mark.platforms('windows')
@pytest.mark.parametrize('ending', ['success', 'failure', 'timeout', 'output'])
def test_windows_descendant_grandchild_cleanup_on_every_exit(review_data, ending):
    home, vault, note, settings = review_data
    api = plugin()
    cfg = api.Settings(home, {**settings, 'timeout_seconds': 3})
    pids = home / 'pids.json'
    child_script = home / 'child.py'
    child_script.write_text('import subprocess,sys,os,json,time\nfrom pathlib import Path\n'
                            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])\n'
                            + f'Path({str(pids)!r}).write_text(json.dumps([os.getpid(),p.pid]))\n'
                            + 'time.sleep(30)\n', encoding='utf-8')
    parent = home / 'parent.py'
    end = {'success': 'raise SystemExit(0)', 'failure': 'raise SystemExit(7)',
           'timeout': 'time.sleep(30)', 'output': 'print("x"*600000,flush=True)'}[ending]
    parent.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
                      + f'subprocess.Popen([sys.executable,{str(child_script)!r}])\n'
                      + f'while not Path({str(pids)!r}).exists(): time.sleep(0.01)\n'
                      + end + '\n', encoding='utf-8')
    cfg.command = [sys.executable, str(parent)]
    try:
        if ending == 'success':
            api.kb_backend.run(cfg, ['status'])
        else:
            kind = {'failure': RuntimeError, 'timeout': TimeoutError, 'output': ValueError}[ending]
            with pytest.raises(kind):
                api.kb_backend.run(cfg, ['status'])
        recorded = json.loads(pids.read_text())
        deadline = time.monotonic() + 2
        while any(psutil.pid_exists(pid) for pid in recorded) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any(psutil.pid_exists(pid) for pid in recorded)
        assert not list(cfg.state.glob('kb-process-*'))
    finally:
        if pids.exists():
            for pid in json.loads(pids.read_text()):
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass
