"""Copilot-style per-repo MCP config (.mcp.json / .github/mcp.json) merged into mcp_servers."""

import json

import agent.skill_utils as su
from tools.mcp_tool_config import _load_mcp_config


def _setup(tmp_path, monkeypatch, trusted):
    home = tmp_path / ".hermes"
    home.mkdir()
    repo = tmp_path / "proj"
    sub = repo / "sub"
    (repo / ".git").mkdir(parents=True)
    (repo / ".github").mkdir()
    sub.mkdir()
    cfg = "mcp_servers:\n  ado:\n    command: user-ado\n"
    if trusted:
        cfg += f"skills:\n  trusted_project_dirs: ['{repo}']\n"
    (home / "config.yaml").write_text(cfg)
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "ado": {"command": "npx", "args": ["x"], "tools": ["*"]},
        "docs": {"type": "http", "url": "https://root", "tools": ["*"]},
        "legacy": {"type": "sse", "url": "https://sse", "tools": ["a", "b"]},
    }}))
    (repo / ".github" / "mcp.json").write_text(json.dumps({"gh": {"command": "gh-mcp"}, "docs": {"url": "https://lower"}}))
    (sub / ".mcp.json").write_text(json.dumps({"docs": {"url": "https://sub"}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.chdir(sub)
    su._external_dirs_cache_clear()


def test_trusted_repo_servers_merged(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, trusted=True)
    servers = _load_mcp_config()
    assert servers["ado"] == {"command": "user-ado"}  # user config wins
    assert servers["docs"] == {"url": "https://sub"}  # closer file wins
    assert servers["legacy"] == {"url": "https://sse", "transport": "sse", "tools": {"include": ["a", "b"]}}
    assert servers["gh"] == {"command": "gh-mcp"}  # bare top-level format
    su._external_dirs_cache_clear()


def test_untrusted_repo_ignored(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, trusted=False)
    assert set(_load_mcp_config()) == {"ado"}
    su._external_dirs_cache_clear()
