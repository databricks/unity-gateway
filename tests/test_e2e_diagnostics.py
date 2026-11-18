import subprocess
import xml.etree.ElementTree as ET

import pytest

from scripts.run_integration import redact_junit_report
from tests import test_e2e as e2e
from ucode.agents import opencode


@pytest.mark.parametrize("outcome", ["timeout", "nonzero", "logs_only"])
def test_opencode_failure_preserves_recent_logs_without_token(
    monkeypatch, tmp_path, capsys, outcome
):
    token = "private-workspace-token"
    workspace = "https://workspace.example"
    output = "startup-only-marker" + "x" * 12000 + token + workspace + " service=llm status=waiting"

    def run_agent(cmd, *, env, timeout):
        assert cmd[-3:] == ["--print-logs", "--log-level", "INFO"]
        assert timeout == 180
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout, output.encode(), output.encode())
        if outcome == "logs_only":
            return subprocess.CompletedProcess(cmd, 0, "", output)
        return subprocess.CompletedProcess(cmd, 1, output, output)

    # Configuration and process execution are boundaries; exercise the real failure reporting.
    monkeypatch.setattr(opencode, "write_tool_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(e2e, "_require_binary", lambda binary: None)
    monkeypatch.setattr(e2e, "_run_agent", run_agent)

    with pytest.raises(pytest.fail.Exception) as failure:
        e2e.TestOpencodeLaunch().test_launch_opencode_per_model(
            ("gemini", "system.ai.gemini-4-carbon"),
            tmp_path,
            monkeypatch,
            {},
            workspace,
            token,
        )

    reported = str(failure.value) + capsys.readouterr().err
    assert "service=llm status=waiting" in reported
    assert "<redacted>" in reported
    assert token not in reported
    assert workspace not in reported
    assert "startup-only-marker" not in reported
    assert {"timeout": "TIMEOUT after", "nonzero": "rc=1", "logs_only": "rc=0"}[outcome] in reported


def test_e2e_artifact_redaction_preserves_xml(tmp_path):
    source, destination = tmp_path / "raw.xml", tmp_path / "redacted.xml"
    workspace, bearer = 'https://workspace.example/?w=1&x="private"', "bearer-sentinel"
    suite = ET.Element("testsuite", name=workspace, tests="1")
    failure = ET.SubElement(suite, "failure")
    failure.text = bearer
    failure.tail = bearer
    ET.ElementTree(suite).write(source, encoding="utf-8")

    redact_junit_report(source, destination, (bearer, workspace))

    report = destination.read_text()
    assert "sentinel" not in report
    assert "workspace.example" not in report
    suite = ET.parse(destination).getroot()
    assert suite.attrib["name"] == "<redacted>"
    assert suite.attrib["tests"] == "1"
    assert suite.findtext("failure") == "<redacted>"
    assert suite[0].tail == "<redacted>"


def test_e2e_artifact_is_absent_when_pytest_produced_no_report(tmp_path):
    destination = tmp_path / "redacted.xml"

    redact_junit_report(tmp_path / "missing.xml", destination, ("bearer",))

    assert not destination.exists()
