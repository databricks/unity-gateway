"""Use the existing real TUI driver with stricter CUJ completion evidence."""

from tests.integration.utils.evidence import assert_no_terminal_api_error
from tests.integration.utils.terminal import AgentTerminal


class Terminal(AgentTerminal):
    def __init__(self, session, name, args, *, evidence):
        self.evidence = evidence
        super().__init__(session, args[0], [str(session.binary), *args], name)

    def __exit__(self, exc_type, exc, traceback):
        try:
            super().__exit__(exc_type, exc, traceback)
        finally:
            self.session.record(f"{self.name}-native", self.evidence.snapshot())
        if isinstance(exc, AssertionError):
            raise AssertionError(self.session.redact(str(exc))) from None

    def task(self, evidence, task):
        def completed(screen):
            assert_no_terminal_api_error(screen)
            # Do not use wait_for_task's optional tool-permission approval.
            assert "Do you want to proceed?" not in screen, (
                "Unexpected permission request; inspect the actual command:\n" + screen
            )
            return evidence.completed(task) is not None

        self.wait_for(completed, "completed native file task", timeout=240)
