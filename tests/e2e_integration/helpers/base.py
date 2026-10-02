"""Explicit workspace configuration shared by full E2E CUJs."""

from typing import ClassVar


class BaseCujTest:
    """Declare WORKSPACE_URL on each concrete CUJ class; use self.workspace_url.

    This declares configuration only. The function-scoped cuj fixture separately
    reserves the dedicated workspace and verifies cleanup across runs.
    """

    WORKSPACE_URL: ClassVar[str] = ""

    @property
    def workspace_url(self) -> str:
        return self.WORKSPACE_URL
