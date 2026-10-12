"""Hosted RoomLink audience gate for additive KB recall."""
from concurrent.futures import ThreadPoolExecutor
import contextvars
import hashlib

from gateway.hosted_room_execution_policy import (
    RoomExecutionPolicy, bind_room_execution_policy, reset_room_execution_policy)
from tests.plugins.test_kb_final_review_regressions import (
    make_nine_profile_fixture, plugin, provision, rows, settings)


def room_policy():
    return RoomExecutionPolicy(
        version=1, target_profile='alpha', enabled_toolsets=('bot_room',),
        approval_mode='manual', max_iterations=1, policy_digest='synthetic-policy')


class Context:
    def __init__(self, values):
        self.values = values
        self.callback = None

    def get_config(self, key, default):
        return self.values if key == 'kb' else default

    def register_hook(self, _name, callback):
        self.callback = callback

    def register_cli_command(self, *_args):
        pass


def test_actual_room_policy_binder_denies_api_before_scope_or_search(tmp_path, monkeypatch):
    _system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    values = settings(vault, 'alpha')
    assert api.kb_multiscope.authorized(values, 'api_server', '')
    assert not api.kb_multiscope.authorized(values, 'bot_room', '')
    ctx = Context(values)
    api.register(ctx)
    monkeypatch.setattr(api.kb_multiscope, '_build_scopes',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('room touched scopes')))
    monkeypatch.setattr(api.kb_multiscope, 'search',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('room searched')))
    token = bind_room_execution_policy(room_policy())
    try:
        assert not api.kb_multiscope.authorized(values, 'api_server', '')
        assert ctx.callback(user_message='canary marker', platform='api_server') is None
    finally:
        reset_room_execution_policy(token)


def test_copied_hook_worker_context_preserves_room_denial(tmp_path):
    _system, vault, _homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    values = settings(vault, 'alpha')
    token = bind_room_execution_policy(room_policy())
    try:
        copied = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            admitted = pool.submit(copied.run, api.kb_multiscope.authorized,
                                   values, 'api_server', '').result(timeout=5)
        assert admitted is False
    finally:
        reset_room_execution_policy(token)


def test_api_policy_getter_error_or_unavailable_fails_closed(tmp_path, monkeypatch):
    _system, vault, _homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    values = settings(vault, 'alpha')
    import gateway.hosted_room_execution_policy as policy_module

    monkeypatch.setattr(policy_module, 'current_room_execution_policy',
                        lambda: (_ for _ in ()).throw(RuntimeError('synthetic getter failure')))
    assert not api.kb_multiscope.authorized(values, 'api_server', '')
    monkeypatch.delattr(policy_module, 'current_room_execution_policy')
    assert not api.kb_multiscope.authorized(values, 'api_server', '')


def test_owner_api_still_recalls_but_room_wire_has_no_kb_and_memory_unchanged(tmp_path, monkeypatch):
    _system, vault, homes = make_nine_profile_fixture(tmp_path)
    api = plugin()
    home = homes['alpha']
    values = settings(vault, 'alpha')
    provision(api, api.kb_multiscope.build_scopes(home, values))
    monkeypatch.setattr(api.kb_multiscope, 'search', rows)
    memories = home / 'memories'
    memories.mkdir(parents=True)
    memory = memories / 'MEMORY.md'
    user = memories / 'USER.md'
    memory.write_text('SYNTHETIC MEMORY UNCHANGED', encoding='utf-8')
    user.write_text('SYNTHETIC USER UNCHANGED', encoding='utf-8')
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (memory, user)}
    ctx = Context(values)
    api.register(ctx)
    monkeypatch.setattr(api, 'get_hermes_home', lambda: home)

    owner = ctx.callback(user_message='canary marker', platform='api_server')
    assert owner and 'SHARED-NINE' in owner['context'] and 'ONLY-alpha' in owner['context']
    token = bind_room_execution_policy(room_policy())
    try:
        peer_injection = ctx.callback(user_message='canary marker', platform='api_server')
    finally:
        reset_room_execution_policy(token)
    synthetic_wire = 'peer prompt' + ('\n\n' + peer_injection['context'] if peer_injection else '')
    assert 'Knowledge-base evidence' not in synthetic_wire
    assert 'SHARED-NINE' not in synthetic_wire and 'ONLY-alpha' not in synthetic_wire
    assert before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (memory, user)}
