"""Use the existing real TUI driver with stricter CUJ completion evidence."""

from tests.integration.utils.evidence import assert_no_terminal_api_error
from tests.integration.utils.terminal import AgentTerminal

from .constants import CLAUDE, CODEX


class Terminal(AgentTerminal):
    def __init__(self, session, name, args, *, agent=None):
        """Run `ug <args>`; bare `ug` (empty args) names the agent it is expected to launch."""
        agent = agent or (args[0] if args else None)
        if agent not in (CLAUDE, CODEX) or (args and args[0] != agent):
            raise ValueError("CUJ terminal requires a Claude or Codex command or bare ug agent.")
        super().__init__(session, agent, [str(session.binary), *args], name)

    def wait_until(
        self,
        done,
        description,
        *,
        timeout=240,
        rejected=("Do you want to proceed?",),
        on_screen=None,
    ):
        """Wait for `done()`, failing on API errors or any `rejected` permission prompt.

        `on_screen(screen)` may answer an expected dialog; it returns True when it sent keys.
        """

        def completed(screen):
            assert_no_terminal_api_error(screen)
            # Do not use wait_for_task's optional tool-permission approval.
            assert not any(prompt in screen for prompt in rejected), (
                "Unexpected permission request; inspect the actual command:\n" + screen
            )
            if on_screen is not None and on_screen(screen):
                return False
            return done()

        self.wait_for(completed, description, timeout=timeout)

    def task(self, evidence, task):
        self.wait_until(lambda: evidence.completed(task) is not None, "completed native file task")
