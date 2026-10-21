"""Native ACP setup remains usable after retiring the fork's Agency launcher."""

import pytest

from hermes_cli import auth
from hermes_cli import model_setup_flows as flows
from hermes_cli.config import read_raw_config


@pytest.fixture
def native_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(auth, "get_external_process_provider_status", lambda _provider: {
        "command": "copilot", "base_url": "acp://copilot",
    })
    monkeypatch.setattr(auth, "resolve_external_process_provider_credentials", lambda _provider: {
        "command": "copilot", "args": ["--acp", "--stdio"], "base_url": "acp://copilot",
    })
    monkeypatch.setattr(auth, "resolve_api_key_provider_credentials", lambda _provider: {"api_key": ""})
    monkeypatch.setattr(flows, "_copilot_catalog", lambda _key: ([], ["native-model"], str))
    monkeypatch.setattr(flows, "_pick_model_or_prompt", lambda *_args, **_kwargs: "native-model")
    monkeypatch.setattr(flows, "_curses_choice", lambda *_args: pytest.fail("Unexpected launcher picker"))
    return tmp_path


def test_native_setup_ignores_retired_launcher_setting(native_setup):
    flows._model_flow_copilot_acp({"copilot_acp": {"launcher": "agency"}})

    saved = read_raw_config()
    assert saved["model"]["provider"] == "copilot-acp"
    assert saved["model"]["default"] == "native-model"
    assert saved["model"]["base_url"] == "acp://copilot"
    assert "copilot_acp" not in saved


def test_missing_native_cli_does_not_save_model(native_setup, monkeypatch, capsys):
    def missing(_provider):
        raise auth.AuthError("Native CLI missing", provider="copilot-acp", code="missing_external_process_cli")

    monkeypatch.setattr(auth, "resolve_external_process_provider_credentials", missing)
    flows._model_flow_copilot_acp({})

    assert "Native CLI missing" in capsys.readouterr().out
    assert not (native_setup / "config.yaml").exists()


def test_unexpected_setup_error_is_not_reported_as_missing_cli(native_setup, monkeypatch):
    def broken(_provider):
        raise RuntimeError("Unexpected resolver failure")

    monkeypatch.setattr(auth, "resolve_external_process_provider_credentials", broken)
    with pytest.raises(RuntimeError, match="Unexpected resolver failure"):
        flows._model_flow_copilot_acp({})
