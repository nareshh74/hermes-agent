"""Real CLI/QMD integration against a recording loopback transport fixture.

The provider is fake: these tests prove host wiring, not hosted-model reasoning.
"""
from pathlib import Path
import hashlib
import importlib.util
import json
import shutil
import sys

import pytest

from tests.e2e.core.windows._helpers import hermes, last_user, make_home, persisted_messages, system_prompt
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.plugins.kb_test_support import require_qmd_fixture

SOURCE = Path(__file__).parents[2] / 'standalone-kb'
pytestmark = [pytest.mark.platforms('windows'), pytest.mark.live_system_guard_bypass]


def load_plugin():
    spec = importlib.util.spec_from_file_location('kb_surface_plugin', SOURCE / '__init__.py', submodule_search_locations=[str(SOURCE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def setup(home, tmp_path):
    fixture = require_qmd_fixture()
    vault = tmp_path / 'Synthetic 中文 Vault'
    vault.mkdir()
    note = vault / 'Relay α.md'
    note.write_text('# Deployment\nHX-731 uses the amber relay.\n', encoding='utf-8')
    trace = tmp_path / 'qmd-calls.jsonl'
    wrapper = tmp_path / 'qmd-wrapper.py'
    wrapper.write_text('import sys,subprocess,json\nfrom pathlib import Path\n'
                       + f'p=Path({str(trace)!r})\n'
                       + 'with p.open("a",encoding="utf-8") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                       + f'raise SystemExit(subprocess.call([{fixture.bun!r},{str(fixture.qmd)!r}]+sys.argv[1:]))\n', encoding='utf-8')
    settings = {'enabled': True, 'vault_root': str(vault), 'collections': {'facts': '.'},
                'exclusions': [], 'state_dir': str(home.hermes_home / 'cache' / 'naresh-kb'),
                'qmd_command': [sys.executable, str(wrapper)], 'mode': 'lexical',
                'timeout_seconds': 5, 'refresh_interval_seconds': 3600}
    shutil.copytree(SOURCE, home.hermes_home / 'plugins' / 'naresh-kb')
    home.update_config(lambda cfg: cfg.update(plugins={'enabled': ['naresh-kb'], 'entries': {'naresh-kb': {'settings': {'kb': settings}}}}))
    memories = home.hermes_home / 'memories'
    memories.mkdir(exist_ok=True)
    (memories / 'MEMORY.md').write_text('Existing synthetic memory marker KEEP-MEMORY-42.', encoding='utf-8')
    (memories / 'USER.md').write_text('Existing synthetic user marker KEEP-USER-42.', encoding='utf-8')
    plugin = load_plugin()
    plugin.refresh(plugin.Settings(home.hermes_home, settings))
    return note, trace, settings


def test_fresh_cli_automatic_recall_restart_prefix_and_memory_unchanged(tmp_path):
    with FakeLLMServer([Text('Transport fixture ACK one'), Text('Transport fixture ACK two')]) as server:
        home = make_home(tmp_path, server.base_url)
        note, trace, settings = setup(home, tmp_path)
        watched = [home.hermes_home / 'config.yaml', home.hermes_home / 'memories' / 'MEMORY.md', home.hermes_home / 'memories' / 'USER.md']
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
        first = hermes(home, 'chat', '-q', 'What relay does HX-731 use?', '-Q', '--toolsets', '')
        assert first.returncode == 0, first.tail()
        requests = server.main_requests()
        assert requests and 'amber relay' in last_user(requests[0])
        assert 'Relay α.md' in last_user(requests[0])
        assert 'KEEP-MEMORY-42' in system_prompt(requests[0])
        assert 'KEEP-USER-42' in system_prompt(requests[0])
        sid = persisted_messages(home)[0]['session_id']
        second = hermes(home, 'chat', '-q', 'Is HX-731 still documented?', '-Q', '--resume', sid, '--toolsets', '')
        assert second.returncode == 0, second.tail()
        requests = server.main_requests()
        assert len(requests) == 2
        assert system_prompt(requests[0]) == system_prompt(requests[1])
        assert requests[0]['messages'][-1]['content'] == [m for m in requests[1]['messages'] if m['role'] == 'user'][0]['content']
        assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
        searches = [json.loads(line) for line in trace.read_text().splitlines() if 'search' in json.loads(line)]
        assert len(searches) == 2


def test_backend_failure_allows_fresh_cli_normal_chat(tmp_path):
    with FakeLLMServer([Text('Transport fixture ACK after backend failure')]) as server:
        home = make_home(tmp_path, server.base_url)
        note, trace, settings = setup(home, tmp_path)
        settings['qmd_command'] = [sys.executable, '-c', 'raise SystemExit(7)']
        home.update_config(lambda cfg: cfg['plugins']['entries']['naresh-kb']['settings'].update(kb=settings))
        result = hermes(home, 'chat', '-q', 'What relay does HX-731 use?', '-Q', '--toolsets', '')
        assert result.returncode == 0, result.tail()
        assert 'ACK after backend failure' in result.stdout
        assert 'unavailable' in last_user(server.main_requests()[0])
        state = json.loads((Path(settings['state_dir']) / 'status.json').read_text())
        assert state['state'] == 'error'


def test_once_per_user_turn_even_with_tool_round(tmp_path):
    with FakeLLMServer([ToolCall('read_file', {'path': str(tmp_path / 'Synthetic 中文 Vault' / 'Relay α.md')}), Text('Transport fixture tool ACK')]) as server:
        home = make_home(tmp_path, server.base_url)
        note, trace, settings = setup(home, tmp_path)
        result = hermes(home, 'chat', '-q', 'What relay does HX-731 use?', '-Q', '--toolsets', 'file')
        assert result.returncode == 0, result.tail()
        requests = server.main_requests()
        assert len(requests) == 2
        assert 'amber relay' in last_user(requests[0])
        searches = [json.loads(line) for line in trace.read_text().splitlines() if 'search' in json.loads(line)]
        assert len(searches) == 1


def test_separate_cli_watcher_updates_add_edit_rename_delete_within_interval(tmp_path):
    import subprocess
    import time
    from tests.e2e.core.windows._helpers import hermes_argv, kill_tree, process_tree, wait_until
    with FakeLLMServer([]) as server:
        home = make_home(tmp_path, server.base_url)
        note, trace, settings = setup(home, tmp_path)
        settings['refresh_interval_seconds'] = 4
        home.update_config(lambda cfg: cfg['plugins']['entries']['naresh-kb']['settings'].update(kb=settings))
        api = load_plugin()
        cfg = api.Settings(home.hermes_home, settings)
        old_time = json.loads((cfg.state / 'manifest.json').read_text())['updated_at']
        with (tmp_path / 'watcher.log').open('wb') as log:
            process = subprocess.Popen(hermes_argv('kb', 'watch'), cwd=home.project, env=home.env(), stdout=log, stderr=log)
            try:
                def manifest():
                    return json.loads((cfg.state / 'manifest.json').read_text())
                wait_until(lambda: manifest()['updated_at'] > old_time, 60, 'watcher first cycle', interval=0.05)
                def mutate(action, predicate):
                    action()
                    started = time.monotonic()
                    wait_until(lambda: predicate(manifest()), 8, 'changed index manifest', interval=0.05)
                    assert time.monotonic() - started <= 4
                mutate(lambda: note.write_text('# Deployment\nHX-731 now uses the violet relay.\n', encoding='utf-8'),
                       lambda m: m['files'].get('facts/Relay α.md') == hashlib.sha256(note.read_bytes()).hexdigest())
                assert 'violet relay' in api.recall(home.hermes_home, settings, 'HX-731')['context']
                added = note.with_name('Added.md')
                mutate(lambda: added.write_text('# Addition\nAD-555 uses green relay.\n', encoding='utf-8'), lambda m: 'facts/Added.md' in m['files'])
                renamed = added.with_name('Renamed.md')
                mutate(lambda: added.rename(renamed), lambda m: 'facts/Renamed.md' in m['files'] and 'facts/Added.md' not in m['files'])
                assert 'Renamed.md' in api.recall(home.hermes_home, settings, 'AD-555')['context']
                mutate(lambda: renamed.unlink(), lambda m: 'facts/Renamed.md' not in m['files'])
                assert api.recall(home.hermes_home, settings, 'AD-555') is None
            finally:
                kill_tree(process_tree(process.pid))
                process.wait(timeout=10)
        assert api.refresh(cfg)['changed'] is False
