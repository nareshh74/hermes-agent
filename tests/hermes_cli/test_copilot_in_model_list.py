"""Tests for GitHub Copilot entries shown in the /model picker."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.model_switch import list_authenticated_providers
from hermes_cli import model_switch_providers
from hermes_cli import models
from hermes_cli.models import provider_model_ids


@patch.dict(os.environ, {"GH_TOKEN": "test-key"}, clear=False)
def test_copilot_picker_uses_live_catalog_when_available():
    live_models = ["gpt-5.4", "claude-sonnet-4.6", "gemini-3.1-pro-preview"]

    with patch("agent.models_dev.fetch_models_dev", return_value={}), \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value="gh-token"), \
         patch("hermes_cli.models._fetch_github_models", return_value=live_models):
        providers = list_authenticated_providers(current_provider="openrouter", max_models=50)

    copilot = next((p for p in providers if p["slug"] == "copilot"), None)

    assert copilot is not None
    assert copilot["models"] == live_models
    assert copilot["total_models"] == len(live_models)


# --- copilot-acp: external_process availability (#63662) -------------------
#
# copilot-acp holds no API key, OAuth token, or credential-pool entry by
# design — the spawned `copilot --acp --stdio` subprocess brings its own auth.
# The picker loop used to filter it out unconditionally (has_creds never had
# an external_process branch), so the provider was invisible in every picker
# even with a perfectly resolvable executable.


@pytest.fixture()
def _no_other_copilot_creds(monkeypatch):
    """Make sure copilot-acp visibility comes ONLY from executable resolution:
    no env tokens, no configured ACP endpoint, no auth-store entry, no seeded
    credential pool."""
    # COPILOT_ACP_BASE_URL is not a credential, but an `acp+tcp://` value marks
    # the provider configured with no executable at all (hermes_cli/auth.py), so
    # a host that sets it would decide the outcome instead of the test.
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "HERMES_COPILOT_ACP_COMMAND",
                "COPILOT_CLI_PATH", "COPILOT_ACP_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    import hermes_cli.auth as auth
    import hermes_cli.model_switch as model_switch

    monkeypatch.setattr(auth, "_load_auth_store", lambda: {})
    monkeypatch.setattr(model_switch_providers, "_credential_pool_is_usable", lambda *a, **k: False)


def test_copilot_acp_listed_when_executable_resolves(tmp_path, monkeypatch, _no_other_copilot_creds):
    fake = tmp_path / ("copilot.exe" if os.name == "nt" else "copilot")
    fake.write_text("", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("HERMES_COPILOT_ACP_COMMAND", str(fake))

    with patch("agent.models_dev.fetch_models_dev", return_value={}), \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=None), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]):
        providers = list_authenticated_providers(current_provider="openrouter", max_models=50)

    acp = next((p for p in providers if p["slug"] == "copilot-acp"), None)

    assert acp is not None, "copilot-acp must be listed when its executable resolves"
    assert acp["models"], "copilot-acp row must offer at least the curated fallback models"


def test_copilot_acp_hidden_when_executable_missing(monkeypatch, _no_other_copilot_creds):
    # `copilot` may genuinely be installed on a dev machine — force the
    # resolution miss so the test pins behaviour, not the host's PATH.
    import hermes_cli.auth as auth

    monkeypatch.setattr(auth.shutil, "which", lambda *_a, **_k: None)

    with patch("agent.models_dev.fetch_models_dev", return_value={}), \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=None), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]):
        providers = list_authenticated_providers(current_provider="openrouter", max_models=50)

    assert all(p["slug"] != "copilot-acp" for p in providers), \
        "copilot-acp must stay hidden when no executable resolves"


_ACP_CREDS = {"api_key": "copilot-acp", "base_url": "acp://copilot", "command": "copilot", "args": ["--acp", "--stdio"]}


@pytest.fixture()
def _fresh_acp_memo(monkeypatch):
    monkeypatch.setattr(models, "_copilot_acp_session_memo", {})
    yield
    monkeypatch.setattr(models, "_copilot_acp_session_memo", {})


@pytest.mark.parametrize(
    ("session_probe", "github_token", "github_models", "expected"),
    [
        # Signed-in session, no GitHub token anywhere: the session list wins, the API is never asked.
        ({"return_value": ["auto", "gpt-5.6-sol", "claude-sonnet-5"]}, "", [], ["auto", "gpt-5.6-sol", "claude-sonnet-5"]),
        # Session probe fails: token-based GitHub discovery is still the next source.
        ({"side_effect": TimeoutError("probe timeout")}, "catalog-token", ["api-fallback-model"], ["api-fallback-model"]),
    ],
    ids=["session-wins-without-token", "github-fallback-when-probe-fails"],
)
def test_copilot_acp_catalog_prefers_authenticated_session(
        _fresh_acp_memo, session_probe, github_token, github_models, expected):
    with patch("hermes_cli.auth.resolve_external_process_provider_credentials", return_value=_ACP_CREDS), \
         patch("agent.copilot_acp_client.CopilotACPClient.list_models", **session_probe) as list_models, \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=github_token), \
         patch("hermes_cli.models._fetch_github_models", return_value=github_models) as github:
        assert provider_model_ids("copilot-acp", force_refresh=True) == expected

    list_models.assert_called_once()
    assert github.called is bool(github_token)


@pytest.mark.parametrize(
    ("github_token", "github_models", "expected"),
    [
        ("catalog-token", ["api-model"], ["api-model"]),
        ("", [], models._PROVIDER_MODELS["copilot"]),
    ],
    ids=["github-catalog", "curated-catalog"],
)
def test_copilot_acp_missing_profile_uses_catalog_fallback(
    _fresh_acp_memo, monkeypatch, github_token, github_models, expected
):
    monkeypatch.setattr("providers.get_provider_profile", lambda _provider: None)
    with patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=github_token), \
         patch("hermes_cli.models._fetch_github_models", return_value=github_models):
        assert provider_model_ids("copilot-acp", force_refresh=True) == expected


def test_copilot_acp_session_probe_is_memoized_across_model_switch_validation(_fresh_acp_memo):
    """``/model`` validation reads the catalog uncached on every switch; each miss spawns the CLI.
    A run of switches must pay one probe, and a failed probe must not be retried per switch."""
    from hermes_cli.models_validate import validate_requested_model

    with patch("hermes_cli.auth.resolve_external_process_provider_credentials", return_value=_ACP_CREDS), \
         patch("agent.copilot_acp_client.CopilotACPClient.list_models", return_value=["gpt-5.6-terra"]) as list_models, \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=""), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]):
        for _ in range(3):
            verdict = validate_requested_model("gpt-5.6-terra", "copilot-acp", api_key="copilot-acp", base_url="acp://copilot")
            assert verdict["accepted"] and verdict["recognized"]
    assert list_models.call_count == 1

    models._copilot_acp_session_memo.clear()
    with patch("hermes_cli.auth.resolve_external_process_provider_credentials", return_value=_ACP_CREDS), \
         patch("agent.copilot_acp_client.CopilotACPClient.list_models", side_effect=RuntimeError("not signed in")) as list_models, \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=""), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]):
        for _ in range(3):
            provider_model_ids("copilot-acp")
    assert list_models.call_count == 1


def test_copilot_acp_session_memo_isolated_by_profile_and_spawn_argv(
    tmp_path, monkeypatch, _fresh_acp_memo
):
    from agent.copilot_acp_launcher import resolve_copilot_acp_launch_spec
    from agent.secret_scope import (
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    profile = SimpleNamespace(
        process_command="copilot",
        process_args=("--acp", "--stdio"),
        process_command_env_vars=(
            "HERMES_COPILOT_ACP_COMMAND",
            "COPILOT_CLI_PATH",
        ),
        process_args_env_var="HERMES_COPILOT_ACP_ARGS",
    )

    def fetch_models(*, launch_spec=None):
        launch = launch_spec or resolve_copilot_acp_launch_spec(profile)
        return [f"{get_hermes_home().name}:{launch.display}"]

    profile.fetch_models = fetch_models
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda provider: profile if provider == "copilot-acp" else None,
    )

    homes = {}
    for name in ("a", "b"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(
            "copilot_acp:\n  launcher: native\n",
            encoding="utf-8",
        )
        homes[name] = home

    def discover(home: Path):
        home_token = set_hermes_home_override(home)
        secret_token = set_secret_scope({}, profile_home=str(home))
        try:
            return models._copilot_acp_session_models(False)
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)

    set_multiplex_active(True)
    try:
        assert discover(homes["a"]) == ["a:copilot --acp --stdio"]
        assert discover(homes["b"]) == ["b:copilot --acp --stdio"]
        (homes["a"] / "config.yaml").write_text(
            "copilot_acp:\n  launcher: agency\n",
            encoding="utf-8",
        )
        assert discover(homes["a"]) == [
            "a:agency copilot --acp --stdio"
        ]
        (homes["a"] / "config.yaml").write_text(
            "copilot_acp:\n  launcher: native\n",
            encoding="utf-8",
        )
        assert discover(homes["a"]) == ["a:copilot --acp --stdio"]
    finally:
        set_multiplex_active(False)


def test_copilot_acp_cache_fingerprint_tracks_effective_launcher(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_COPILOT_ACP_COMMAND",
        "COPILOT_CLI_PATH",
        "HERMES_COPILOT_ACP_ARGS",
    ):
        monkeypatch.delenv(name, raising=False)

    config_path = home / "config.yaml"
    config_path.write_text(
        "copilot_acp:\n  launcher: native\n",
        encoding="utf-8",
    )
    native = models._credential_fingerprint("copilot-acp")
    config_path.write_text(
        "copilot_acp:\n  launcher: agency\n",
        encoding="utf-8",
    )

    assert models._credential_fingerprint("copilot-acp") != native
