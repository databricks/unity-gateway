"""Compact ug status output before handing the terminal to a remote agent.

Sent as source to the Sandbox so older image versions need no package upgrade.
Only Rich status messages are filtered; stdin, errors, prompts, subprocesses and
the agent's terminal streams remain untouched.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel


class StartupConsole(Console):
    def __init__(self, original: Console) -> None:
        super().__init__()
        self.original = original
        self.warnings: set[str] = set()

    def print(self, *objects: Any, **kwargs: Any) -> None:
        if not objects:
            return
        if len(objects) == 1:
            item = objects[0]
            if isinstance(item, Panel) and (
                str(item.title) == "Configuration Complete"
                or str(item.renderable).startswith("Launching ")
            ):
                return
            if isinstance(item, str):
                if item.startswith(("[bold green]", "[dim]•[/dim]")):
                    return
                if item.startswith("  [bold]Workspace:[/bold]"):
                    return
                if item.startswith("[bold yellow]![/bold yellow]"):
                    if item in self.warnings:
                        return
                    self.warnings.add(item)
        self.original.print(*objects, **kwargs)

    def input(self, *args: Any, **kwargs: Any) -> str:
        # Render prompts with the original console, including all prompt text.
        return self.original.input(*args, **kwargs)


def main() -> None:
    import sys

    from ucode import ui

    ui.console = StartupConsole(ui.console)
    # Import after selecting the console so CLI imports use the same instance.
    from ucode.entrypoint import main as ug_main

    sys.argv[0] = "ug"
    ug_main()


if __name__ == "__main__":
    main()
