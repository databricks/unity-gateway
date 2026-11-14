import json
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.utils import macos_app
from tests.integration.utils.harness import UserSession


@pytest.mark.parametrize(
    "capture_failure,record_failure", [(False, False), (True, False), (False, True)]
)
def test_failed_click_preserves_error_and_redacted_accessibility_evidence(
    tmp_path, monkeypatch, capture_failure, record_failure
):
    monkeypatch.setenv("DATABRICKS_BEARER", "private-token")
    root, project = tmp_path / "session", tmp_path / "project"
    root.mkdir()
    project.mkdir()
    session = UserSession(root, project, Path(sys.executable), tmp_path / "artifacts")
    bundle = tmp_path / "ChatGPT.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "1", "CFBundleExecutable": "ChatGPT"})
    )
    app = macos_app.MacApp(session, bundle, "chatgpt")
    app.proc = SimpleNamespace(pid=42, poll=lambda: None)
    original = AssertionError("No AXRadioButton labelled Engineering")
    snapshots = iter(
        ["AXRadioButton\tEngineering\tprivate-token\t1\n", "AXButton\tContinue\tprivate-token\t\n"]
    )
    click_calls = []

    def native_tree(args, **kwargs):
        tree = next(snapshots)
        if capture_failure and "Continue" in tree:
            raise subprocess.CalledProcessError(1, args, stderr="private capture error")
        return subprocess.CompletedProcess(args, 0, stdout=tree)

    def native_click(*args, **kwargs):
        click_calls.append(args)
        raise original

    # Native Accessibility is unavailable here; only its process boundaries are controlled.
    monkeypatch.setattr(macos_app.subprocess, "run", native_tree)
    monkeypatch.setattr(session, "run", native_click)
    artifact = session.artifacts / "chatgpt-click-failure.json"
    if record_failure:
        artifact.mkdir()

    with pytest.raises(AssertionError) as caught:
        app.click("AXRadioButton", "Engineering")

    assert caught.value is original
    assert len(click_calls) == 1
    if record_failure:
        assert isinstance(caught.value.__cause__, OSError)
        return
    evidence = json.loads(artifact.read_text())
    assert evidence["target"] == ["AXRadioButton", "Engineering"]
    assert evidence["ready_tree"] == [["AXRadioButton", "Engineering", "<redacted>", "1"]]
    if capture_failure:
        assert evidence["capture_error"] == "CalledProcessError"
        assert "failure_tree" not in evidence
    else:
        assert evidence["failure_tree"] == [["AXButton", "Continue", "<redacted>", ""]]
    assert "private" not in artifact.read_text()
