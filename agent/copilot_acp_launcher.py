from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

LauncherKind = Literal["native", "agency", "override"]


@dataclass(frozen=True)
class CopilotACPLaunchSpec:
    kind: LauncherKind
    command: str
    args: tuple[str, ...]

    @property
    def spawn_argv(self) -> tuple[str, ...]:
        return (self.command, *self.args)

    @property
    def probe_argv(self) -> tuple[str, ...]:
        try:
            acp_index = self.args.index("--acp")
        except ValueError:
            return ()
        return (self.command, *self.args[:acp_index], "--help")

    @property
    def display(self) -> str:
        return shlex.join(self.spawn_argv)


_ACP_PROBE_CACHE: dict[tuple[str, ...], bool] = {}


def _provider_profile(profile: Any | None) -> Any:
    if profile is not None:
        return profile
    from providers import get_provider_profile

    resolved = get_provider_profile("copilot-acp")
    if resolved is None:
        raise RuntimeError("The copilot-acp provider profile is unavailable.")
    return resolved


def _native_fields(profile: Any) -> tuple[str, tuple[str, ...]]:
    command = str(getattr(profile, "process_command", "") or "")
    args = tuple(str(arg) for arg in (getattr(profile, "process_args", ()) or ()))
    return command, args


def _nonempty_env(env: Mapping[str, str] | None, name: str) -> str:
    if env is None:
        from agent.secret_scope import get_secret_str

        return get_secret_str(name).strip()
    return str(env.get(name, "") or "").strip()


def resolve_copilot_acp_launch_spec(
    profile: Any | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    command: str | None = None,
    args: Sequence[str] | None = None,
) -> CopilotACPLaunchSpec:
    profile = _provider_profile(profile)
    native_command, native_args = _native_fields(profile)

    explicit_command = str(command or "").strip()
    explicit_args = tuple(str(arg) for arg in (args or ()))
    if explicit_command or explicit_args:
        return CopilotACPLaunchSpec(
            kind="override",
            command=explicit_command or native_command,
            args=explicit_args or native_args,
        )

    command_env_vars = tuple(
        str(name)
        for name in (getattr(profile, "process_command_env_vars", ()) or ())
    )
    args_env_var = str(getattr(profile, "process_args_env_var", "") or "")
    command_values = [
        _nonempty_env(env, name) for name in command_env_vars
    ]
    raw_args = _nonempty_env(env, args_env_var) if args_env_var else ""
    if any(command_values) or raw_args:
        return CopilotACPLaunchSpec(
            kind="override",
            command=next((value for value in command_values if value), native_command),
            args=tuple(shlex.split(raw_args)) if raw_args else native_args,
        )

    if config is None:
        from hermes_cli.config import load_config

        config = load_config()
    section = config.get("copilot_acp", {})
    if not isinstance(section, Mapping):
        raise ValueError(
            "Invalid copilot_acp configuration. Expected a mapping with launcher "
            "'native' or 'agency'."
        )
    launcher = section.get("launcher", "native")
    if not isinstance(launcher, str) or launcher not in {"native", "agency"}:
        raise ValueError(
            f"Invalid copilot_acp.launcher {launcher!r}. Expected 'native' or 'agency'."
        )
    if launcher == "agency":
        return CopilotACPLaunchSpec(
            kind="agency",
            command="agency",
            args=("copilot", *native_args),
        )
    return CopilotACPLaunchSpec(
        kind="native",
        command=native_command,
        args=native_args,
    )


def resolve_launch_command(spec: CopilotACPLaunchSpec) -> str | None:
    return shutil.which(spec.command)


def probe_acp_support(
    spec: CopilotACPLaunchSpec, *, timeout: float = 5.0
) -> bool | None:
    probe_argv = spec.probe_argv
    if not probe_argv:
        return True
    if (cached := _ACP_PROBE_CACHE.get(probe_argv)) is not None:
        return cached
    try:
        probe = subprocess.run(
            list(probe_argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if probe.returncode != 0:
        return None
    verdict = bool(
        re.search(
            r"(?:^|[\s\[])--acp(?:[\s=\],]|$)",
            probe.stdout,
            re.MULTILINE,
        )
    )
    _ACP_PROBE_CACHE[probe_argv] = verdict
    return verdict
