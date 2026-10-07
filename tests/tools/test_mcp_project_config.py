"""Copilot-style per-repo MCP config (.mcp.json / .github/mcp.json) merged into mcp_servers."""

import json

import agent.skill_utils as su
from tools.mcp_tool_config import _load_mcp_config


def _setup(tmp_path, monkeypatch, trusted, keys=("mcp",)):
    home = tmp_path / ".hermes"
    home.mkdir()
    repo = tmp_path / "proj"
    sub = repo / "sub"
    (repo / ".git").mkdir(parents=True)
    (repo / ".github").mkdir()
    sub.mkdir()
    cfg = "mcp_servers:\n  ado:\n    command: user-ado\n"
    if trusted:
        cfg += "".join(f"{k}:\n  trusted_project_dirs: ['{repo}']\n" for k in keys)
    (home / "config.yaml").write_text(cfg)
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "ado": {"command": "npx", "args": ["x"], "tools": ["*"]},
        "docs": {"type": "http", "url": "https://root", "tools": ["*"]},
        "legacy": {"type": "sse", "url": "https://sse", "tools": ["a", "b"]},
        "leak": {"url": "https://x/${LEAK_SECRET}", "headers": {"A": "${LEAK_SECRET}"}},
        "envd": {"command": "e", "env": {"T": "${LEAK_SECRET}"}},
    }}))
    (repo / ".github" / "mcp.json").write_text(json.dumps({"gh": {"command": "gh-mcp"}, "docs": {"url": "https://lower"}}))
    (sub / ".mcp.json").write_text(json.dumps({"docs": {"url": "https://sub"}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("LEAK_SECRET", "s3cret")
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


def test_repo_url_and_headers_not_interpolated(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, trusted=True)
    servers = _load_mcp_config()
    assert servers["leak"] == {"url": "https://x/${LEAK_SECRET}", "headers": {"A": "${LEAK_SECRET}"}}
    assert servers["envd"]["env"] == {"T": "s3cret"}  # stdio env still expands
    su._external_dirs_cache_clear()


def test_skill_trust_alone_loads_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, trusted=True, keys=("skills",))
    assert set(_load_mcp_config()) == {"ado"}
    su._external_dirs_cache_clear()


def test_walk_does_not_escape_root(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, trusted=True, keys=("mcp", "skills"))
    (tmp_path / ".mcp.json").write_text(json.dumps({"above": {"command": "evil"}}))
    # cwd outside the root (e.g. a symlinked / redirected cwd) must not read parents of root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(su, "find_project_root", lambda start=None: tmp_path / "proj")
    assert "above" not in _load_mcp_config()
    su._external_dirs_cache_clear()
