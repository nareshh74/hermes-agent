"""Native prompt timestamps after retiring the always-on fork policy."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from hermes_cli.cli_agent_setup_mixin import _collect_resume_entries
from hermes_cli.cli_stream_mixin import CLIStreamMixin


@pytest.mark.parametrize("enabled", [False, True])
def test_resume_recap_keeps_native_user_text(enabled):
    ts = datetime(2026, 1, 2, 9, 7).timestamp()
    history = [{"role": "user", "content": "hi", "timestamp": ts}]
    entries, _, _ = _collect_resume_entries(history, {"timestamps": enabled}, lambda t: t)
    assert entries == [("user", "hi")]
    assert history[0]["timestamp"] == ts


@pytest.mark.parametrize("enabled", [False, True])
def test_preview_respects_native_timestamp_setting(monkeypatch, enabled):
    monkeypatch.setattr("cli.datetime", SimpleNamespace(now=lambda: datetime(2026, 1, 2, 9, 7)))
    cli = SimpleNamespace(show_timestamps=enabled, timestamp_format="%H:%M")
    rendered = CLIStreamMixin._format_submitted_user_message_preview(cli, "hello\nworld")
    assert ("[dim]09:07[/]" in rendered) is enabled


@pytest.mark.parametrize("enabled", [False, True])
def test_single_line_echo_keeps_native_rendering(monkeypatch, enabled):
    printed = []
    monkeypatch.setattr("cli.ChatConsole", lambda: SimpleNamespace(print=printed.append))
    cli = SimpleNamespace(show_timestamps=enabled, timestamp_format="%H:%M")
    cli._format_submitted_user_message_preview = (
        lambda text: CLIStreamMixin._format_submitted_user_message_preview(cli, text))
    CLIStreamMixin._print_user_message_preview(cli, "hello")
    assert "hello" in printed[-1]
    assert "[dim]" not in printed[-1]
