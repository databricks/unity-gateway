"""Component checks for the Polly review workflow and its summary helper."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "polly-review.yml"
SUMMARY_PATH = ROOT / ".github" / "scripts" / "polly-review" / "summary.py"
SPEC = importlib.util.spec_from_file_location("polly_review_summary", SUMMARY_PATH)
assert SPEC is not None and SPEC.loader is not None
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


def _workflow() -> str:
    return WORKFLOW_PATH.read_text()


def test_workflow_triggers_match_ug_review():
    workflow = _workflow()
    ug_review = (ROOT / ".github" / "workflows" / "ug-review.yml").read_text()

    def trigger_and_condition(text: str) -> tuple[str, str]:
        on_block = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        condition = text.split("    if: >-\n", 1)[1].split("    #", 1)[0]
        return on_block, condition

    assert trigger_and_condition(workflow) == trigger_and_condition(ug_review)
    assert "pull_request_target" not in workflow
    assert "workflow_dispatch" not in workflow
    assert "polly-review-${{ github.event.pull_request.number || github.event.issue.number }}" in (
        workflow
    )
    assert "cancel-in-progress: true" in workflow


def test_workflow_uses_protected_runner_trusted_checkout_and_gateway_token():
    workflow = _workflow()
    assert "group: databricks-protected-runner-group" in workflow
    assert "labels: linux-ubuntu-latest" in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "path: .ug-review-trusted" in workflow
    assert "run: python3 .ug-review-trusted/.github/scripts/ug-review/auth.py" in workflow
    assert "DATABRICKS_CLIENT_ID: 6476a5e7-7f32-4073-abc5-ee30bb7d0060" in workflow
    assert "${{ secrets.UG_CLI_REVIEW_BOT_SP_CLIENT_SECRET }}" in workflow
    assert "LLM_API_KEY: ${{ steps.gateway-auth.outputs.token }}" in workflow
    assert "secrets.LLM_API_KEY" not in workflow
    assert "GATEWAY_BASE_URL" not in workflow
    assert "/ai-gateway/anthropic" in workflow
    assert "/ai-gateway/codex/v1" in workflow
    assert "timeout-minutes: 45" in workflow
    for permission in (
        "contents: read",
        "id-token: write",
        "issues: read",
        "pull-requests: write",
    ):
        assert f"      {permission}\n" in workflow
    assert "contents: write" not in workflow


def test_workflow_pins_actions_omnigent_and_models():
    workflow = _workflow()
    refs = re.findall(r"uses: [\w.-]+/[\w.-]+@(\S+)", workflow)
    assert refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
    assert re.search(r"^      OMNIGENT_SHA: [0-9a-f]{40}$", workflow, re.MULTILINE)
    assert "system.ai.claude-opus-5-5" in workflow
    assert workflow.count("system.ai.gpt-6-1-sol") == 2
    assert "system.ai.gpt-5-6" not in workflow
    assert "vars.UG_POLLY_HARNESS || 'codex'" in workflow
    assert "uv run --no-sync omnigent run examples/polly/" in workflow
    assert (
        "id: polly\n        # Bounded below the job timeout so a hung sub-agent still leaves diagnostics.\n        timeout-minutes: 25\n"
        in workflow
    )
    assert "uv export --frozen --package omnigent --extra all --no-dev" in workflow
    assert "uv pip install --no-config --python .venv -r" in workflow
    assert "uv sync" not in workflow
    assert "omnigent-ai/omnigent/.github/actions" not in workflow
    assert "security-gate" not in workflow
    assert "create-github-app-token" not in workflow
    assert "REVIEW_ALLOWLIST" not in workflow


def test_workflow_installs_packages_only_through_jfrog_proxies():
    workflow = _workflow()
    assert "jfrog/setup-jfrog-cli@279b1f629f43dd5bc658d8361ac4802a7ef8d2d5" in workflow
    assert "oidc-provider-name: github-actions" in workflow
    assert (
        "NPM_CONFIG_REGISTRY: https://databricks.jfrog.io/artifactory/api/npm/db-npm/" in workflow
    )
    assert "https://databricks.jfrog.io/artifactory/api/pypi/db-pypi/simple" in workflow
    assert "UV_DEFAULT_INDEX: databricks-pypi=${{ env.PYPI_INDEX }}" in workflow
    assert "UV_INDEX_DATABRICKS_PYPI_USERNAME: ${{ steps.jfrog.outputs.oidc-user }}" in workflow
    assert "UV_INDEX_DATABRICKS_PYPI_PASSWORD: ${{ steps.jfrog.outputs.oidc-token }}" in workflow
    assert "umask 077" in workflow
    assert "pypi.org" not in workflow
    assert "UV_INDEX_URL" not in workflow
    assert "PIP_INDEX_URL" not in workflow
    assert "@anthropic-ai/claude-code@$CLAUDE_VERSION" in workflow
    assert "@openai/codex@$CODEX_VERSION" in workflow
    assert (
        'if: always()\n        run: rm -f "$RUNNER_TEMP/ug-review-package-config/.npmrc"'
        " ~/.databrickscfg ~/.omnigent/config.yaml" in workflow
    )
    job_environment = workflow.split("    env:\n", 1)[1].split("    steps:\n", 1)[0]
    assert "${{ runner." not in job_environment


def test_workflow_versions_match_integration_workflow():
    workflow = _workflow()
    integration = (ROOT / ".github" / "workflows" / "integration.yml").read_text()
    for name in ("CLAUDE_VERSION", "CODEX_VERSION"):
        pinned = re.search(rf"^      {name}: (\S+)$", workflow, re.MULTILINE)
        assert pinned is not None
        assert f"'{pinned[1]}'" in integration


def test_workflow_prompt_uses_trusted_policy_and_marks_pr_content_untrusted():
    workflow = _workflow()
    assert "ug-review-instructions.md" in workflow
    assert 'policy_dir / "AGENTS.md"' in workflow
    assert "untrusted data" in workflow
    assert "<!-- POLLY_REVIEW_START -->" in workflow
    for omnigent_only in (
        "DATABASE_BEST_PRACTICES",
        "feature-map",
        "omnigent/harnesses",
        "Package extras",
    ):
        assert omnigent_only not in workflow


def test_workflow_skips_reviewed_heads_and_posts_with_markers_and_token_scan():
    workflow = _workflow()
    assert 'select(.user.type == "Bot")' in workflow
    assert "<!-- polly-reviewed-sha: ${HEAD_SHA} -->" in workflow
    assert "<!-- polly-skipped-sha: ${HEAD_SHA} -->" in workflow
    assert "/review force" not in workflow
    assert "content=eyes" in workflow
    assert workflow.count("GH_TOKEN: ${{ github.token }}") >= 4
    scan = workflow.index("Scan review output for secrets")
    post = workflow.index("name: Post review comment")
    assert workflow.index("compose /tmp/pr_diff_summary.json") < scan < post
    assert 'grep -qF "$GATEWAY_TOKEN" /tmp/polly_review.txt' in workflow
    assert "model_at_capacity=true" in workflow
    assert 'text.replace(token, "[REDACTED]")' in workflow


def test_embedded_python_blocks_are_syntactically_valid():
    workflow = _workflow()
    blocks = re.findall(
        r"python3 (?:-u )?<<'(PY|PYEOF)'\n(.*?)\n\s*\1\n", workflow, flags=re.DOTALL
    )
    assert len(blocks) == 4
    for _, body in blocks:
        lines = body.splitlines()
        indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
        ast.parse("\n".join(line[indent:] for line in lines))


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("tests/test_cli.py", summary.TESTS),
        ("src/ucode/foo_test.py", summary.TESTS),
        ("README.md", "Documentation"),
        ("uv.lock", "Dependencies and lockfiles"),
        ("src/ucode/cli.py", "Implementation / other"),
    ],
)
def test_category(filename, expected):
    assert summary.category(filename) == expected


def test_diff_stats_counts_lines_by_category(tmp_path):
    diff = tmp_path / "pr.diff"
    diff.write_text(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1 +1,2 @@\n-old\n+new\n+newer\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n"
        "+++ b/tests/test_a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    stats = summary.diff_stats(diff)
    assert stats["test_files"] == ["tests/test_a.py"]
    assert "2 files; +3 / -2 lines" in stats["markdown"]


REVIEW = """<!-- POLLY_REVIEW_START -->
## Blocking issues
None.

## Summary

### Changes
Adds a helper.

### Tests
`tests/test_a.py`

| Test / case | Behavior protected | Layer | Needed? | Action / rationale |
| --- | --- | --- | --- | --- |
| `test_one` | helper output | unit | keep | covers the happy path |

### Scope
No unrelated changes.
"""
STATS = {"markdown": "**Diff size (computed):** 2 files", "test_files": ["tests/test_a.py"]}


def test_compose_inserts_stats_and_collapses_tests():
    result = summary.compose_review(REVIEW, STATS)
    assert result.index("## Summary") < result.index("**Diff size (computed):**")
    assert "<summary>Test-by-test assessment</summary>" in result


@pytest.mark.parametrize(
    ("review", "message"),
    [
        (REVIEW.replace("## Summary", "## Overview"), "exactly one '## Summary'"),
        (REVIEW.replace("### Scope\nNo unrelated changes.\n", ""), "one '### Scope'"),
        (REVIEW.replace("| keep |", "| maybe |"), "unknown test verdict"),
        (REVIEW.replace("tests/test_a.py", "tests/other.py"), "unassessed test files"),
    ],
)
def test_compose_rejects_incomplete_reviews(review, message):
    with pytest.raises(ValueError, match=message):
        summary.compose_review(review, STATS)


def test_compose_rejects_oversized_reviews():
    with pytest.raises(ValueError, match="comment budget"):
        summary.compose_review(REVIEW + "x" * 61000, STATS)


def test_linked_issues_parses_graphql_and_truncates_bodies(monkeypatch):
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "totalCount": 2,
                        "nodes": [
                            {
                                "number": 7,
                                "title": "Bug",
                                "state": "OPEN",
                                "body": "b" * 5000,
                                "repository": {"nameWithOwner": "o/r"},
                            }
                        ],
                    }
                }
            }
        }
    }
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(summary.subprocess, "run", run)
    result = summary.linked_issues("o/r#9")
    assert result["omitted"] == 1
    assert result["issues"][0]["issue"] == "o/r#7"
    assert len(result["issues"][0]["body"]) == summary.LINKED_ISSUE_BODY_LIMIT
    assert result["issues"][0]["body_truncated"] is True
    assert "number=9" in calls[0]


def test_linked_issues_reports_unavailable_lookup(monkeypatch, capsys):
    def run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="boom")

    monkeypatch.setattr(summary.subprocess, "run", run)
    assert summary.linked_issues("o/r#9") is None
    assert "Could not read linked issues" in capsys.readouterr().out
