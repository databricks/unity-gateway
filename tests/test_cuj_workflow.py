"""Local contracts for the independent, manually dispatched live CUJ workflow."""

import re
from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/e2e-integration.yml"


def test_cuj_workflow_is_manual_and_separate_from_existing_ci():
    workflow = WORKFLOW.read_text()
    triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
    assert "  workflow_dispatch:" in triggers
    assert "pull_request" not in triggers and "push:" not in triggers
    assert "workflow_call:" not in triggers
    assert "name: Full E2E CUJs · Smart routing" in workflow
    assert "runs-on: ubuntu-22.04" in workflow
    assert "continue-on-error" not in workflow


def test_cuj_workflow_serializes_its_workspace_without_canceling_cleanup():
    workflow = WORKFLOW.read_text()
    concurrency = workflow.split("\nconcurrency:\n", 1)[1].split("\njobs:\n", 1)[0]
    assert "group: full-e2e-cuj-smart-routing-dbc-1a9622fc-2e91" in concurrency
    assert "cancel-in-progress: false" in concurrency
    assert "${{" not in concurrency, "Do not partition the same workspace by branch or PR"
    assert "CUJ_WORKSPACE: https://dbc-1a9622fc-2e91.cloud.databricks.com" in workflow


def test_cuj_workflow_scopes_credentials_to_the_live_step():
    workflow = WORKFLOW.read_text()
    setup, live = workflow.split("      - name: Run test_cuj_smart_routing with both agents\n", 1)
    live, artifacts = live.split("      - name: Upload full E2E CUJ evidence\n", 1)
    for name in ("UG_CUJ_SP_CLIENT_ID", "UG_CUJ_SP_CLIENT_SECRET"):
        assert name not in setup and name not in artifacts
        assert f"{name}: ${{{{ secrets.{name} }}}}" in live
    assert "--profile" not in live and "DATABRICKS_BEARER" not in live
    assert "persist-credentials: false" in setup
    assert "--collect-only -q -k test_cuj_smart_routing" in setup
    assert "--confcutdir=tests/e2e_integration" in setup
    assert "|| true" not in workflow


def test_cuj_workflow_runs_both_agents_and_archives_failure_evidence():
    workflow = WORKFLOW.read_text()
    assert "--suite e2e-integration" in workflow
    assert '--claude-version "$CLAUDE_VERSION" --codex-version "$CODEX_VERSION"' in workflow
    assert '--workspace "$CUJ_WORKSPACE"' in workflow
    assert "bwrap --ro-bind / / --unshare-user --proc /proc --dev /dev /usr/bin/true" in workflow
    artifacts = workflow.split("      - name: Upload full E2E CUJ evidence\n", 1)[1]
    assert "if: ${{ always() }}" in artifacts
    assert "*.xml" in artifacts and "/artifacts/" in artifacts
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in artifacts
    assert "build-home" not in artifacts and "installer.npmrc" not in artifacts
    actions = re.findall(r"uses: ([^\s]+)", workflow)
    assert actions and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in actions)
