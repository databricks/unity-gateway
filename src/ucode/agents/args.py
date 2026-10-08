"""Parsing helpers shared by coding-agent launchers."""

from __future__ import annotations

from dataclasses import dataclass

_CODEX_VALUE_OPTIONS = frozenset(
    {
        "-c",
        "--config",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-a",
        "--ask-for-approval",
        "-C",
        "--cd",
        "--add-dir",
    }
)
_CODEX_BOOLEAN_OPTIONS = frozenset(
    {
        "--strict-config",
        "--worktree",
        "--search",
        "--no-alt-screen",
    }
)


@dataclass(frozen=True)
class LaunchOptions:
    """Invocation-scoped options shared by agent launchers."""

    launch_smart_routing: bool = False
    user_pinned_model: str | None = None


def codex_option_only_launch(tool_args: list[str]) -> bool:
    """Recognize known scalar/boolean TUI options, excluding models and positionals."""
    index = 0
    while index < len(tool_args):
        argument = tool_args[index]
        if argument in _CODEX_BOOLEAN_OPTIONS:
            index += 1
            continue
        if argument.startswith("--"):
            option, separator, value = argument.partition("=")
        else:
            option = argument[:2]
            value = argument[2:].removeprefix("=")
            separator = argument[2:]
        if option not in _CODEX_VALUE_OPTIONS:
            return False
        if not separator:
            index += 1
            if index == len(tool_args):
                return False
            value = tool_args[index]
        if not value or value.startswith("-"):
            return False
        if option in {"-c", "--config"}:
            key, equals, config_value = value.partition("=")
            if not key.strip() or not equals or not config_value.strip():
                return False
            if key.strip() == "model":
                return False
        index += 1
    return True


def explicit_model_arg_value(tool_args: list[str]) -> str | None:
    """Return the last model selected before the harness's ``--`` separator."""
    model: str | None = None
    index = 0
    while index < len(tool_args):
        arg = tool_args[index]
        if arg == "--":
            break
        if arg in {"--model", "-m"}:
            if index + 1 < len(tool_args) and not tool_args[index + 1].startswith("-"):
                model = tool_args[index + 1]
                index += 1
        elif arg.startswith("--model="):
            value = arg.partition("=")[2]
            if value:
                model = value
        index += 1
    return model


def has_explicit_model_arg(tool_args: list[str]) -> bool:
    """Return whether the harness receives a ``--model`` option before ``--``."""
    for arg in tool_args:
        if arg == "--":
            return False
        if arg in {"--model", "-m"} or arg.startswith("--model="):
            return True
    return False
