"""Use the existing real TUI driver with stricter CUJ completion evidence."""

from tests.integration.utils.evidence import assert_no_terminal_api_error
from tests.integration.utils.terminal import AgentTerminal

from .constants import CLAUDE, CODEX


class Terminal(AgentTerminal):
    def __init__(self, session, name, args):
        if not args or args[0] not in (CLAUDE, CODEX):
            raise ValueError("CUJ terminal requires a Claude or Codex command.")
        super().__init__(session, args[0], [str(session.binary), *args], name)

    def task(self, evidence, task):
        def completed(screen):
            assert_no_terminal_api_error(screen)
            # Do not use wait_for_task's optional tool-permission approval.
            assert "Do you want to proceed?" not in screen, (
                "Unexpected permission request; inspect the actual command:\n" + screen
            )
            return evidence.completed(task) is not None

        self.wait_for(completed, "completed native file task", timeout=240)
