"""Copilot CLI user-level ~/.copilot/mcp-config.json merged into mcp_servers."""

import json
import os

from tools.mcp_tool_config import _load_mcp_config


def test_copilot_user_servers_merged(tmp_path, monkeypatch):
    home = tmp_path / "hermes_test"
    (home / "config.yaml").write_text("mcp_servers:\n  ado:\n    command: user-ado\n")
    copilot = tmp_path / "copilot_test"
    copilot.mkdir(exist_ok=True)
    (copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {
        "ado": {"type": "stdio", "command": "npx", "tools": ["*"]},
        "eng": {"type": "http", "url": "https://mcp.eng.ms", "tools": ["*"]},
        "gb": {"type": "http", "url": "http://127.0.0.1:3131/mcp", "headers": {"A": "${CP_TOKEN}"}, "tools": ["a"]},
    }}))
    monkeypatch.setenv("CP_TOKEN", "tok")
    servers = _load_mcp_config()
    assert os.environ["COPILOT_HOME"] == str(copilot)
    assert servers["ado"] == {"command": "user-ado"}  # config.yaml wins
    assert servers["eng"] == {"url": "https://mcp.eng.ms"}
    assert servers["gb"] == {"url": "http://127.0.0.1:3131/mcp", "headers": {"A": "tok"}, "tools": {"include": ["a"]}}


def test_missing_or_bad_copilot_config(tmp_path):
    assert "eng" not in _load_mcp_config()
    copilot = tmp_path / "copilot_test"
    copilot.mkdir(exist_ok=True)
    (copilot / "mcp-config.json").write_text("{bad")
    assert "eng" not in _load_mcp_config()


def test_copilot_servers_enabled_for_toolsets(tmp_path):
    from hermes_cli.tools_config import enabled_mcp_server_names
    copilot = tmp_path / "copilot_test"
    copilot.mkdir(exist_ok=True)
    (copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {
        "eng": {"url": "https://x"}, "off": {"url": "https://y", "enabled": False}}}))
    names = enabled_mcp_server_names({})
    assert "eng" in names and "off" not in names


def test_mcp_list_shows_copilot_servers(tmp_path, capsys):
    from hermes_cli.mcp_config import cmd_mcp_list
    copilot = tmp_path / "copilot_test"
    copilot.mkdir(exist_ok=True)
    (copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {"eng": {"url": "https://x"}}}))
    cmd_mcp_list()
    assert "eng (copilot)" in capsys.readouterr().out
