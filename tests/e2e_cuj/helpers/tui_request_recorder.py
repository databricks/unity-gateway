"""CUJ configuration glue for the shared real-workspace request recorder."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from tests.integration.utils.request_recorder import (
    RecordedRequest as RecordedRequest,
)
from tests.integration.utils.request_recorder import (
    RecordedResponse as RecordedResponse,
)
from tests.integration.utils.request_recorder import (
    TuiRequestRecorder as RequestRecorder,
)

from .session import UserSession


class TuiRequestRecorder(RequestRecorder):
    def __init__(self, upstream: str) -> None:
        super().__init__(upstream)
        self._session_home: Path | None = None

    def configure_session(self, session: UserSession, args: list[str]) -> None:
        """Configure against the upstream, then retarget this isolated session to the recorder."""
        if "--workspace" in args:
            raise ValueError("configure_session owns --workspace")
        session.configure([*args, "--workspace", self.upstream])
        self.retarget_session(session.home)

    def prepare_launch(self) -> None:
        """Keep the recorder-scoped managed-config cache fresh for a no-preflight launch."""
        if self._session_home is None:
            raise RuntimeError("configure_session must run before prepare_launch")
        path = self._session_home / ".ucode" / "managed-config.json"
        payload = json.loads(path.read_text())
        if payload.get("workspace") != self.url:
            raise AssertionError("Managed-config cache does not target the recorder")
        payload["retrieved_at"] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(payload, indent=2) + "\n")

    def retarget_session(self, home: Path) -> None:
        """Retarget a session configured outside configure_session, e.g. in a terminal."""
        state_path = home / ".ucode" / "state.json"
        state = json.loads(state_path.read_text())
        upstream = state.get("current_workspace")
        workspaces = state.get("workspaces")
        if upstream != self.upstream or not isinstance(workspaces, dict):
            raise AssertionError("ug configure did not persist the recorder upstream")
        upstream_state = workspaces.get(upstream)
        if not isinstance(upstream_state, dict):
            raise AssertionError("ug configure wrote no upstream workspace state")
        workspaces[self.url] = deepcopy(upstream_state)
        state["current_workspace"] = self.url
        state_path.write_text(json.dumps(state, indent=2) + "\n")

        managed_path = home / ".ucode" / "managed-config.json"
        managed = json.loads(managed_path.read_text())
        if managed.get("workspace") != upstream:
            raise AssertionError("ug configure wrote no upstream managed-config cache")
        managed["workspace"] = self.url
        managed_path.write_text(json.dumps(managed, indent=2) + "\n")
        self._session_home = home
        self.prepare_launch()
