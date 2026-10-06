from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.copilot_acp_launcher import (
    CopilotACPLaunchSpec,
    _ACP_PROBE_CACHE,
    probe_acp_support,
    resolve_copilot_acp_launch_spec,
)


@pytest.fixture
def profile():
    return SimpleNamespace(
        process_command="copilot",
        process_args=("--acp", "--stdio"),
        process_command_env_vars=("HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH"),
        process_args_env_var="HERMES_COPILOT_ACP_ARGS",
    )


@pytest.fixture(autouse=True)
def clear_probe_cache():
    _ACP_PROBE_CACHE.clear()
    yield
    _ACP_PROBE_CACHE.clear()


def test_launcher_defaults_to_native_and_resolves_agency_argv(profile):
    native = resolve_copilot_acp_launch_spec(profile, config={}, env={})
    agency = resolve_copilot_acp_launch_spec(
        profile,
        config={"copilot_acp": {"launcher": "agency"}},
        env={},
    )

    assert native == CopilotACPLaunchSpec(
        kind="native",
        command="copilot",
        args=("--acp", "--stdio"),
    )
    assert native.spawn_argv == ("copilot", "--acp", "--stdio")
    assert native.probe_argv == ("copilot", "--help")
    assert agency.spawn_argv == ("agency", "copilot", "--acp", "--stdio")
    assert agency.probe_argv == ("agency", "copilot", "--help")


@pytest.mark.parametrize(
    ("env", "expected_command", "expected_args"),
    [
        (
            {"HERMES_COPILOT_ACP_COMMAND": "/opt/custom-copilot"},
            "/opt/custom-copilot",
            ("--acp", "--stdio"),
        ),
        (
            {"COPILOT_CLI_PATH": "/opt/copilot-from-path"},
            "/opt/copilot-from-path",
            ("--acp", "--stdio"),
        ),
        (
            {"HERMES_COPILOT_ACP_ARGS": "bridge --acp --stdio"},
            "copilot",
            ("bridge", "--acp", "--stdio"),
        ),
    ],
)
def test_any_environment_override_uses_native_profile_fields(
    profile, env, expected_command, expected_args
):
    spec = resolve_copilot_acp_launch_spec(
        profile,
        config={"copilot_acp": {"launcher": "agency"}},
        env=env,
    )

    assert spec.kind == "override"
    assert spec.command == expected_command
    assert spec.args == expected_args


def test_default_environment_resolves_each_bound_profile_scope(
    profile, monkeypatch
):
    from agent.secret_scope import (
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )

    monkeypatch.setenv("HERMES_COPILOT_ACP_COMMAND", "process-copilot")
    monkeypatch.setenv("HERMES_COPILOT_ACP_ARGS", "--process")
    observed = []
    set_multiplex_active(True)
    try:
        for secrets in (
            {
                "HERMES_COPILOT_ACP_COMMAND": "profile-a-copilot",
                "HERMES_COPILOT_ACP_ARGS": "--acp --profile-a",
            },
            {
                "COPILOT_CLI_PATH": "profile-b-copilot",
                "HERMES_COPILOT_ACP_ARGS": "--acp --profile-b",
            },
        ):
            token = set_secret_scope(secrets)
            try:
                observed.append(
                    resolve_copilot_acp_launch_spec(profile).spawn_argv
                )
            finally:
                reset_secret_scope(token)
    finally:
        set_multiplex_active(False)

    assert observed == [
        ("profile-a-copilot", "--acp", "--profile-a"),
        ("profile-b-copilot", "--acp", "--profile-b"),
    ]


def test_invalid_stored_launcher_fails_visibly(profile):
    with pytest.raises(
        ValueError,
        match="Invalid copilot_acp.launcher 'automatic'. Expected 'native' or 'agency'.",
    ):
        resolve_copilot_acp_launch_spec(
            profile,
            config={"copilot_acp": {"launcher": "automatic"}},
            env={},
        )


@pytest.mark.parametrize("launcher", [["agency"], {"kind": "agency"}])
def test_invalid_non_scalar_launcher_fails_visibly(profile, launcher):
    with pytest.raises(
        ValueError,
        match=(
            "Invalid copilot_acp.launcher .* "
            "Expected 'native' or 'agency'."
        ),
    ):
        resolve_copilot_acp_launch_spec(
            profile,
            config={"copilot_acp": {"launcher": launcher}},
            env={},
        )


def test_real_loader_reads_profile_a_then_b_then_a(tmp_path, monkeypatch, profile):
    profile_a = tmp_path / "profiles" / "a"
    profile_b = tmp_path / "profiles" / "b"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    (profile_a / "config.yaml").write_text(
        "copilot_acp:\n  launcher: agency\n",
        encoding="utf-8",
    )
    (profile_b / "config.yaml").write_text(
        "copilot_acp:\n  launcher: native\n",
        encoding="utf-8",
    )

    observed = []
    for home in (profile_a, profile_b, profile_a):
        monkeypatch.setenv("HERMES_HOME", str(home))
        observed.append(
            resolve_copilot_acp_launch_spec(profile, env={}).spawn_argv
        )

    assert observed == [
        ("agency", "copilot", "--acp", "--stdio"),
        ("copilot", "--acp", "--stdio"),
        ("agency", "copilot", "--acp", "--stdio"),
    ]


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def test_agency_probe_uses_prefix_and_cache_keys_include_full_argv():
    copilot = CopilotACPLaunchSpec(
        kind="agency",
        command="agency",
        args=("copilot", "--acp", "--stdio"),
    )
    other = CopilotACPLaunchSpec(
        kind="override",
        command="agency",
        args=("other", "--acp", "--stdio"),
    )

    with patch(
        "agent.copilot_acp_launcher.subprocess.run",
        side_effect=[
            _completed("Usage: copilot [--acp]"),
            _completed("Usage: other"),
        ],
    ) as run:
        assert probe_acp_support(copilot) is True
        assert probe_acp_support(copilot) is True
        assert probe_acp_support(other) is False

    assert [call.args[0] for call in run.call_args_list] == [
        ["agency", "copilot", "--help"],
        ["agency", "other", "--help"],
    ]


def test_nonzero_probe_exit_is_inconclusive_and_uncached():
    spec = CopilotACPLaunchSpec(
        kind="native",
        command="copilot",
        args=("--acp", "--stdio"),
    )

    with patch(
        "agent.copilot_acp_launcher.subprocess.run",
        return_value=_completed("", returncode=1),
    ) as run:
        assert probe_acp_support(spec) is None
        assert probe_acp_support(spec) is None

    assert run.call_count == 2
    assert _ACP_PROBE_CACHE == {}


def test_custom_transport_without_exact_acp_token_is_not_probed():
    spec = CopilotACPLaunchSpec(
        kind="override",
        command="custom",
        args=("--acp=true", "--stdio"),
    )

    with patch("agent.copilot_acp_launcher.subprocess.run") as run:
        assert probe_acp_support(spec) is True

    run.assert_not_called()
