"""User prompts always carry an HH:MM label, regardless of display.timestamps."""
from datetime import datetime
from types import SimpleNamespace

from hermes_cli.cli_agent_setup_mixin import _collect_resume_entries
from hermes_cli.cli_stream_mixin import CLIStreamMixin


def test_resume_recap_shows_original_user_time():
    ts = datetime(2026, 1, 2, 9, 7).timestamp()
    history = [{"role": "user", "content": "hi", "timestamp": ts}]
    entries, _, _ = _collect_resume_entries(history, {"timestamps": True}, lambda t: t)
    assert entries == [("user", "hi 09:07")]
    entries, _, _ = _collect_resume_entries(history, {"timestamps": False}, lambda t: t)
    assert entries == [("user", "hi 09:07")]


def test_single_line_live_prompt_gets_timestamp(monkeypatch):
    printed = []
    monkeypatch.setattr("cli.ChatConsole", lambda: SimpleNamespace(print=printed.append))
    cli = SimpleNamespace(show_timestamps=False, timestamp_format="%H:%M")
    cli._format_submitted_user_message_preview = (
        lambda text: CLIStreamMixin._format_submitted_user_message_preview(cli, text))
    CLIStreamMixin._print_user_message_preview(cli, "hello")
    assert printed[-1].endswith(f" [dim]{datetime.now().strftime('%H:%M')}[/]")
