"""Isaac's saved smart routing feedback, which Omnigent sends in place of an off reason.

Omnigent's Smart Routing switch has Isaac save the user's reason before it types
``/smart-router off --feedback-id <id>``. Isaac enforces the reason rules when it
saves, so the off check only confirms that the report is a smart routing report
about this Claude session.
"""

import json
import os
import uuid
from pathlib import Path

_ISAAC_LAUNCH_MODE_ENV_VAR = "ISAAC_LAUNCH_MODE"
_CLAUDE_SESSION_ID_ENV_VAR = "CLAUDE_CODE_SESSION_ID"
# Isaac refuses larger receipts too.
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024


def require_saved_session_feedback(feedback_id: str) -> None:
    """Raise ``ValueError`` unless ``feedback_id`` is this session's saved smart routing feedback."""
    if os.environ.get(_ISAAC_LAUNCH_MODE_ENV_VAR) != "omni":
        raise ValueError("A feedback ID can turn Smart Router off only in an Omnigent session.")
    try:
        feedback_id = str(uuid.UUID(feedback_id))
    except ValueError:
        raise ValueError(f"Feedback ID {feedback_id} is not a UUID.") from None
    receipt = _read_receipt(Path.home() / ".isaac" / "feedback" / feedback_id / "receipt.json")
    request = receipt.get("request")
    session_id = os.environ.get(_CLAUDE_SESSION_ID_ENV_VAR, "")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("feedback_id") != feedback_id
        or not isinstance(request, dict)
        or request.get("category") != "smart-routing"
        or not session_id
        or request.get("session_id") != session_id
    ):
        raise ValueError(
            f"Feedback {feedback_id} is not smart routing feedback for this Claude session."
        )


def _read_receipt(path: Path) -> dict:
    receipt = None
    try:
        # A FIFO or directory in place of the file must fail instead of blocking the toggle.
        if path.is_file():
            with path.open("rb") as stream:
                data = stream.read(_MAX_RECEIPT_BYTES + 1)
            if len(data) <= _MAX_RECEIPT_BYTES:
                receipt = json.loads(data)
    except (OSError, ValueError):
        pass
    if not isinstance(receipt, dict):
        raise ValueError(f"Isaac has no saved feedback {path.parent.name}.")
    return receipt
