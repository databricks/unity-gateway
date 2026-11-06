import json
import runpy
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/cowork-inference/macos"
redact = runpy.run_path(str(SCRIPTS / "diagnostics.py"))["redact"]
matches_labels = runpy.run_path(str(SCRIPTS / "controls.py"))["matches_labels"]


def test_desktop_evidence_redacts_credentials_and_preserves_diagnostics(monkeypatch):
    monkeypatch.setenv("DATABRICKS_BEARER", "ci-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    text = "failed ci-secret oauth-secret Authorization: Bearer unknown-secret eyJhbGci.eyJzdWI.signature"
    assert (
        redact(text) == "failed [REDACTED] [REDACTED] Authorization: Bearer [REDACTED] [REDACTED]"
    )
    assert redact("Claude responded: 48213") == "Claude responded: 48213"


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (["AXButton", "Start task", "", "", "", "1", "0"], True),
        (["AXButton", "Start task", "", "", "", "0", "0"], False),
        (["AXButton", "Start task later", "", "", "", "1", "0"], False),
        (["Start task"], False),
        ([], False),
    ],
)
def test_desktop_control_wait_requires_exact_enabled_label(snapshot, expected):
    assert matches_labels(json.dumps([snapshot]), "Start task", enabled=True) is expected


@pytest.mark.parametrize("answer", ["48213", "48214", "Reply with 48213", ""])
@pytest.mark.parametrize("finished", [True, False])
def test_desktop_response_requires_exact_answer_and_completion(answer, finished):
    rows = [["AXStaticText", "", f"Claude responded: {answer}", "", "", "1", "0"]]
    if finished:
        rows.append(["AXStaticText", "", "Claude finished the response", "", "", "1", "0"])
    assert matches_labels(
        json.dumps(rows),
        ("Claude responded: 48213", "Claude finished the response"),
        require_all=True,
    ) is (answer == "48213" and finished)


@pytest.mark.parametrize("value", ["\n", "\n\n", "line one\nline two | value"])
def test_desktop_prompt_with_multiline_value_remains_accessible(value):
    snapshot = json.dumps([["AXTextArea", "", "Write your prompt to Claude", value, "", "1", "0"]])
    assert matches_labels(snapshot, "Write your prompt to Claude", enabled=True)
