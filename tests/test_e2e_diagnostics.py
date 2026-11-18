import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests import test_e2e as e2e
from ucode.agents import opencode


@pytest.mark.parametrize("times_out", [True, False])
def test_opencode_failure_preserves_recent_logs_without_token(
    monkeypatch, tmp_path, capsys, times_out
):
    token = "private-workspace-token"
    output = "startup-only-marker" + "x" * 12000 + token + " service=llm status=waiting"

    def run_agent(cmd, *, env, timeout):
        assert cmd[-3:] == ["--print-logs", "--log-level", "INFO"]
        assert timeout == 180
        if times_out:
            raise subprocess.TimeoutExpired(cmd, timeout, output.encode(), output.encode())
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
            "https://workspace.example",
            token,
        )

    reported = str(failure.value) + capsys.readouterr().err
    assert "service=llm status=waiting" in reported
    assert "<redacted>" in reported
    assert token not in reported
    assert "startup-only-marker" not in reported
    assert ("TIMEOUT after" if times_out else "rc=1") in reported


def test_e2e_artifact_redaction_preserves_xml(tmp_path):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text()
    block = workflow.split("      - name: Redact e2e test results\n", 1)[1].split(
        "      - name: Upload e2e test results\n", 1
    )[0]
    script = textwrap.dedent(
        block.split("          python - <<'PY'\n", 1)[1].rsplit("          PY\n", 1)[0]
    )
    (tmp_path / "e2e-results.xml").write_text(
        '<testsuite name="workspace-sentinel"><failure>bearer-sentinel</failure></testsuite>'
    )
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "DATABRICKS_BEARER": "bearer-sentinel",
            "UCODE_TEST_WORKSPACE": "workspace-sentinel",
        },
        check=True,
        timeout=30,
    )
    report = tmp_path / "e2e-results-redacted.xml"
    assert "sentinel" not in report.read_text()
    suite = ET.parse(report).getroot()
    assert suite.attrib["name"] == "<redacted>"
    assert suite.findtext("failure") == "<redacted>"
    assert "path: e2e-results-redacted.xml" in workflow
