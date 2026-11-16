"""CI selection and safety contracts; these do not establish a Windows E2E pass."""

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github/workflows/integration.yml"


def jobs():
    body = WORKFLOW.read_text().split("\njobs:\n", 1)[1]
    return dict(re.findall(r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", body))


def test_windows_mdm_lane_uses_real_script_and_workspace_credentials():
    lane = jobs()["mdm-provision-windows"]
    assert "runs-on: windows-latest" in lane
    assert "shell: powershell" in lane
    assert "continue-on-error" not in lane
    assert "github.event.pull_request.head.repo.full_name == github.repository" in lane
    assert "UG_MDM_E2E: '1'" in lane
    for secret in ("E2E_ADMIN_WORKSPACE", "E2E_ADMIN_SP_CLIENT_ID", "E2E_ADMIN_SP_CLIENT_SECRET"):
        assert f"secrets.{secret}" in lane
    assert "git rev-parse HEAD" in lane
    assert "ref: ${{ inputs.ref || github.sha }}" in lane
    assert "./tests/e2e_mdm_windows/run.ps1 -UgRef $ref" in lane
    assert "setup-node" not in lane and "npm install" not in lane
    assert "actions/checkout" in lane and "persist-credentials: false" in lane


def test_windows_mdm_is_required_except_for_explicit_version_runs():
    entries = jobs()
    lane = entries["mdm-provision-windows"]
    gate = entries["cujs"]
    assert "mdm-provision-windows" in re.search(r"(?m)^    needs: \[(.*)\]$", gate).group(1)
    condition = re.search(r"(?m)^    if: \$\{\{ (.*) && \(github.event_name", lane).group(1)
    assert f"WINDOWS_MDM_PINNED: ${{{{ !({condition}) }}}}" in gate
    code = textwrap.dedent(gate.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
    for pinned, mdm, other, expected in (
        ("false", "success", "success", 0),
        ("true", "skipped", "success", 0),
        ("false", "skipped", "success", 1),
        ("true", "failure", "success", 1),
        ("false", "cancelled", "success", 1),
        ("true", "skipped", "skipped", 1),
    ):
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **os.environ,
                "WINDOWS_MDM_PINNED": pinned,
                "RESULTS": json.dumps(
                    {"mdm-provision-windows": {"result": mdm}, "smoke": {"result": other}}
                ),
            },
        )
        assert result.returncode == expected, result.stdout + result.stderr


def test_worker_receives_credentials_through_a_restricted_pipe_not_task_arguments():
    helpers = (ROOT / "tests/e2e_mdm_windows/helpers.ps1").read_text()
    action = helpers.split("$action = New-ScheduledTaskAction", 1)[1].split("$settings =", 1)[0]
    assert "-PipeName" in action
    assert "secret" not in action.lower() and "bearer" not in action.lower()
    assert "NamedPipeServerStream" in helpers and "PipeSecurity" in helpers
    assert "$Environment.UG_MDM_INPUT_CLIENT_SECRET, $Environment.DATABRICKS_BEARER" in helpers
    assert "if ($Identity -ne 'SYSTEM') { $allowed += $Machine.UserSid }" in helpers
    assert "[MdmWindows]::PipeClient($pipe.SafePipeHandle)" in helpers
    assert "-MethodName GetOwnerSid" in helpers
    controller = (ROOT / "tests/e2e_mdm_windows/run.ps1").read_text()
    assert "Remove-Item Env:UG_MDM_INPUT_WORKSPACE, Env:UG_MDM_INPUT_CLIENT_ID," in controller
    assert "-Workspace `$workspace -ClientId `$clientId -ClientSecret `$secret" in controller
    assert "DATABRICKS_CLIENT_SECRET =" not in controller
    assert "[MdmWindows]::BatchLogon($machine.UserSid, $false)" in controller
    assert "timeout = $Timeout; secrets = $script:Secrets" not in helpers
    worker = (ROOT / "tests/e2e_mdm_windows/task-worker.ps1").read_text()
    assert "EnvironmentVariables.Clear()" in worker
    assert "RedirectStandardInput = $true" in worker
    assert "StandardInput.Close()" in worker
    assert "ReadToEndAsync()" in worker
    assert "[MdmProcessJob]::Attach()" in worker
    assert "limits.Basic.Flags = 0x2000" in worker


@pytest.mark.parametrize("name", ["run.ps1", "task-worker.ps1", "helpers.ps1"])
def test_e2e_does_not_import_or_rewrite_application_code(name):
    source = (ROOT / "tests/e2e_mdm_windows" / name).read_text()
    assert "from ucode" not in source and "import ucode" not in source
    assert "UCODE_MANAGED_CONFIG_STUB" not in source
    assert "UG_SKIP_OS_SETTINGS = '1'" not in source
