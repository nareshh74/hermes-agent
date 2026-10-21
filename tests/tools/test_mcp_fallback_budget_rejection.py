"""Lower-priority fork MCP sources must not revive rejected native budgets."""
import pytest

from tools import mcp_tool_config


@pytest.mark.parametrize("source", ["_copilot_user_mcp_servers", "_project_mcp_servers"])
def test_fallback_preserves_native_budget_rejection(monkeypatch, source):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "mcp_servers": {"blocked": {"command": "python", "spend_limit": -1}}})
    monkeypatch.setattr(mcp_tool_config, "_copilot_user_mcp_servers", dict)
    monkeypatch.setattr(mcp_tool_config, "_project_mcp_servers", dict)
    monkeypatch.setattr(mcp_tool_config, "_portable_mcp_servers", lambda servers, rejected: None)
    monkeypatch.setattr(mcp_tool_config, source, lambda: {
        "blocked": {"command": "python"}, "allowed": {"command": "python"}})

    result = mcp_tool_config._load_mcp_config()

    assert "blocked" not in result
    assert result["allowed"]["command"] == "python"
