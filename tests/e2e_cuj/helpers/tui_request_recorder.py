"""CUJ configuration glue for the shared real-workspace request recorder."""

import json
import subprocess
import tempfile
from copy import deepcopy
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
from ucode.managed_files import managed_file_fingerprint

from .constants import WORKSPACE_POLICY_PATH
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
        """Keep the recorded policy fingerprint current for a no-preflight launch.

        A launch replays the saved policy without refetching. A configure without a terminal
        installs no machine policy, so there is nothing to update.
        """
        if self._session_home is None:
            raise RuntimeError("configure_session must run before prepare_launch")
        if not WORKSPACE_POLICY_PATH.exists():
            return
        if json.loads(WORKSPACE_POLICY_PATH.read_text()).get("workspace") != self.url:
            raise AssertionError("The installed workspace policy does not target the recorder")
        refresh_path = self._session_home / ".ucode" / "refresh_ts.json"
        refresh = json.loads(refresh_path.read_text())
        refresh[self.url] = {
            **refresh[self.url],
            "policy_fingerprint": managed_file_fingerprint(WORKSPACE_POLICY_PATH),
        }
        refresh_path.write_text(json.dumps(refresh, indent=2) + "\n")

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

        refresh_path = home / ".ucode" / "refresh_ts.json"
        # A read that installs nothing leaves no stamp; launches then read through the recorder.
        if refresh_path.is_file():
            refresh = json.loads(refresh_path.read_text())
            if upstream in refresh:
                refresh[self.url] = refresh[upstream]
                refresh_path.write_text(json.dumps(refresh, indent=2) + "\n")
        if WORKSPACE_POLICY_PATH.exists():
            # A terminal configure installed the root-owned policy; point it at the recorder too.
            policy = json.loads(WORKSPACE_POLICY_PATH.read_text())
            if policy.get("workspace") != upstream:
                raise AssertionError("ug configure installed no upstream workspace policy")
            policy["workspace"] = self.url
            with tempfile.NamedTemporaryFile("w", suffix=".json", dir=home) as staged:
                staged.write(json.dumps(policy, indent=2) + "\n")
                staged.flush()
                # Copying over the existing file keeps its root ownership and 0644 mode.
                subprocess.run(["sudo", "cp", staged.name, str(WORKSPACE_POLICY_PATH)], check=True)
        self._session_home = home
        self.prepare_launch()
