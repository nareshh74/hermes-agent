from __future__ import annotations

import pytest

from hermes_cli import model_setup_flows as flows
from hermes_cli.config import load_config, read_raw_config


def _clear_launch_overrides(monkeypatch):
    for name in (
        "HERMES_COPILOT_ACP_COMMAND",
        "COPILOT_CLI_PATH",
        "HERMES_COPILOT_ACP_ARGS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_setup_defaults_to_native_and_cancel_writes_nothing(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "display:\n  skin: mono\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_launch_overrides(monkeypatch)
    seen = {}

    def cancel_launcher(title, rows, default_idx):
        seen.update(title=title, rows=rows, default_idx=default_idx)
        return -1

    monkeypatch.setattr(flows, "_curses_choice", cancel_launcher)

    flows._model_flow_copilot_acp(load_config())

    assert seen == {
        "title": "Select the Copilot ACP launcher:",
        "rows": [
            "Native Copilot CLI  (copilot --acp --stdio)",
            "Agency  (agency copilot --acp --stdio)",
        ],
        "default_idx": 0,
    }
    assert read_raw_config() == {"display": {"skin": "mono"}}


@pytest.mark.parametrize(
    ("choice", "launcher", "command"),
    [
        (0, "native", "copilot --acp --stdio"),
        (1, "agency", "agency copilot --acp --stdio"),
    ],
)
def test_setup_persists_launcher_only_after_model_selection(
    tmp_path, monkeypatch, capsys, choice, launcher, command
):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_launch_overrides(monkeypatch)

    monkeypatch.setattr(flows, "_curses_choice", lambda *_: choice)
    monkeypatch.setattr(
        "agent.copilot_acp_launcher.shutil.which",
        lambda candidate: f"C:/Tools/{candidate}.exe",
    )
    monkeypatch.setattr(
        "plugins.model-providers.copilot-acp.CopilotACPProfile.fetch_models",
        lambda *_args, **_kwargs: ["gpt-5.4"],
    )
    monkeypatch.setattr(
        flows,
        "_copilot_catalog",
        lambda _key: ([], [], lambda model: model),
    )
    monkeypatch.setattr(
        flows,
        "_pick_model_or_prompt",
        lambda models, *_args, **_kwargs: models[0],
    )

    flows._model_flow_copilot_acp(load_config())

    assert read_raw_config()["copilot_acp"] == {"launcher": launcher}
    assert f"Command: {command}" in capsys.readouterr().out


def test_setup_model_cancel_does_not_persist_selected_launcher(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "display:\n  skin: mono\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_launch_overrides(monkeypatch)

    monkeypatch.setattr(flows, "_curses_choice", lambda *_: 1)
    monkeypatch.setattr(
        "agent.copilot_acp_launcher.shutil.which",
        lambda command: "C:/Tools/agency.exe" if command == "agency" else None,
    )
    monkeypatch.setattr(
        "plugins.model-providers.copilot-acp.CopilotACPProfile.fetch_models",
        lambda *_args, **_kwargs: ["gpt-5.4"],
    )
    monkeypatch.setattr(
        flows,
        "_copilot_catalog",
        lambda _key: ([], [], lambda model: model),
    )
    monkeypatch.setattr(flows, "_pick_model_or_prompt", lambda *_args, **_kwargs: None)

    flows._model_flow_copilot_acp(load_config())

    assert read_raw_config() == {"display": {"skin": "mono"}}


def test_agency_config_does_not_change_direct_http_copilot(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "copilot_acp:\n  launcher: agency\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_direct_http_token")

    from hermes_cli.auth import resolve_api_key_provider_credentials

    resolved = resolve_api_key_provider_credentials("copilot")

    assert resolved["provider"] == "copilot"
    assert resolved["api_key"] == "gho_direct_http_token"
    assert resolved["source"] == "COPILOT_GITHUB_TOKEN"
