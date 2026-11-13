"""Component guards for command construction and optional startup imports."""

import json
import subprocess
import sys

import typer
from typer.core import TyperGroup
from typer.main import get_command
from typer.utils import get_params_from_function

from ucode import cli


def test_cli_callbacks_reuse_declared_options(monkeypatch) -> None:
    """Typer must not reconstruct CLI-owned options when inspecting them again."""
    callbacks = (cli.default, cli.configure, cli.claude_cmd, cli.codex_cmd)
    # Python 3.14 evaluates annotations on first access, then caches them.
    for callback in callbacks:
        get_params_from_function(callback)

    def unexpected_option(*args, **kwargs):
        raise AssertionError("command construction re-evaluated an Option declaration")

    # Scope this guard to this module's callbacks: separately defined helper commands
    # can choose their own annotation strategy without changing CLI-owned metadata.
    with monkeypatch.context() as scoped:
        scoped.setattr(typer, "Option", unexpected_option)
        for callback in callbacks:
            assert get_params_from_function(callback)

    command = get_command(cli.app)
    assert isinstance(command, TyperGroup)
    assert "--header" in [flag for param in command.params for flag in param.opts]
    with command.make_context("ug", [], resilient_parsing=True) as context:
        claude = command.get_command(context, "claude")
    assert claude is not None
    assert "--model" in [flag for param in claude.params for flag in param.opts]


def test_databricks_import_defers_codex_catalog() -> None:
    guarded = ["ucode.codex_config"]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ucode.databricks; import json, sys; "
            f"print(json.dumps([name for name in {guarded!r} if name in sys.modules]))",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    assert json.loads(result.stdout) == []
