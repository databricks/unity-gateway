"""Component checks for gateway authentication and the Open Code Review workflow."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
AUTH_PATH = ROOT / ".github" / "scripts" / "ug-review" / "auth.py"
SPEC = importlib.util.spec_from_file_location("ug_review_auth", AUTH_PATH)
assert SPEC is not None and SPEC.loader is not None
gateway_auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway_auth)
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ug-review.yml"


def test_oauth_access_token_reuses_service_principal_exchange(monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-id")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "client-secret")
    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return BytesIO(json.dumps({"access_token": "access-token"}).encode())

    monkeypatch.setattr(gateway_auth.urllib.request, "urlopen", urlopen)

    assert gateway_auth._oauth_access_token("https://example.databricks.com/") == "access-token"
    request, timeout = requests[0]
    assert request.full_url == "https://example.databricks.com/oidc/v1/token"
    assert request.method == "POST"
    assert request.get_header("Authorization") == (
        "Basic " + base64.b64encode(b"client-id:client-secret").decode()
    )
    assert request.data == b"grant_type=client_credentials&scope=all-apis"
    assert timeout == 60


@pytest.mark.parametrize(
    "payload",
    [{}, [], {"access_token": None}, {"access_token": ""}, {"access_token": "token\ninjected=1"}],
)
def test_oauth_rejects_missing_or_unsafe_tokens(monkeypatch, payload):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-id")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        gateway_auth.urllib.request,
        "urlopen",
        lambda request, timeout: BytesIO(json.dumps(payload).encode()),
    )
    with pytest.raises(gateway_auth.GatewayAuthError, match="no valid access token"):
        gateway_auth._oauth_access_token("https://example.databricks.com")


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (urllib.error.URLError("unreachable"), "request failed"),
        (TimeoutError("timed out"), "request failed"),
        (json.JSONDecodeError("invalid", "", 0), "invalid JSON"),
        (
            urllib.error.HTTPError(
                "https://example.databricks.com", 401, "Unauthorized", {}, BytesIO(b"secret")
            ),
            "HTTP 401",
        ),
    ],
)
def test_oauth_failures_are_actionable_without_response_secrets(monkeypatch, failure, message):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-id")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "client-secret")

    def urlopen(request, timeout):
        raise failure

    monkeypatch.setattr(gateway_auth.urllib.request, "urlopen", urlopen)
    with pytest.raises(gateway_auth.GatewayAuthError, match=message) as error:
        gateway_auth._oauth_access_token("https://example.databricks.com")
    assert "client-secret" not in str(error.value)
    assert "b'secret'" not in str(error.value)


def test_auth_masks_token_before_exporting_same_job_output(monkeypatch, tmp_path, capsys):
    output_path = tmp_path / "output"
    output_path.write_text("existing=value\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setattr(gateway_auth, "_oauth_access_token", lambda host: "access-token")

    assert gateway_auth.main() == 0
    assert capsys.readouterr().out == "::add-mask::access-token\n"
    assert output_path.read_text() == "existing=value\ntoken=access-token\n"


def test_auth_missing_configuration_fails_before_requesting_a_token(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    def unexpected_request(host):
        pytest.fail("No token should be minted without a destination for the output.")

    monkeypatch.setattr(gateway_auth, "_oauth_access_token", unexpected_request)
    assert gateway_auth.main() == 1
    assert "GITHUB_OUTPUT is missing" in capsys.readouterr().out


def test_workflow_keeps_member_only_comment_trusted_checkout_and_existing_credentials():
    workflow = WORKFLOW_PATH.read_text()
    assert "issue_comment:\n    types: [created]" in workflow
    assert "github.event.issue.pull_request &&" in workflow
    assert "github.event.comment.body == '/ug-review'" in workflow
    assert 'fromJSON(\'["MEMBER", "OWNER"]\')' in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "group: databricks-protected-runner-group" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "${{ secrets.UG_CLI_REVIEW_BOT_SP_CLIENT_SECRET }}" in workflow
    assert "DATABRICKS_CLIENT_ID: 6476a5e7-7f32-4073-abc5-ee30bb7d0060" in workflow
    assert "contents: write" not in workflow
    assert "pull_request_target" not in workflow
    assert "github.event.pull_request.head.sha" not in workflow


def test_workflow_accepts_either_review_label_only_on_in_repo_pull_requests():
    workflow = WORKFLOW_PATH.read_text()
    assert "pull_request:\n    types: [labeled]" in workflow
    assert "github.event_name == 'pull_request' &&" in workflow
    assert "github.event.pull_request.head.repo.full_name == github.repository &&" in workflow
    assert (
        'contains(fromJSON(\'["ug-review", "code-review"]\'), github.event.label.name)' in workflow
    )
    assert "github.event_name == 'issue_comment' &&" in workflow
    assert (
        "ug-review-${{ github.event.pull_request.number || github.event.issue.number }}" in workflow
    )
    assert "pr_number: ${{ steps.pr.outputs.number }}" in workflow
    assert "base_ref: ${{ steps.pr.outputs.base }}" in workflow
    assert "head_sha: ${{ steps.pr.outputs.head }}" in workflow


def test_workflow_uses_pinned_open_code_review_action_and_cli():
    workflow = WORKFLOW_PATH.read_text()
    assert "uses: alibaba/open-code-review@cf64e7080f600d507888a26785ad6a8b7a13ab6a\n" in workflow
    assert 'ocr_version: "1.12.0"' in workflow
    assert "anthropics/claude-code-action" not in workflow
    assert all(
        re.fullmatch(r"[0-9a-f]{40}", ref)
        for ref in re.findall(r"uses: [\w.-]+/[\w.-]+@(\S+)", workflow)
    )
    assert 'upload_artifacts: "false"' in workflow
    assert "rule: .github/open-code-review/rules.json" in workflow
    rules = json.loads((ROOT / ".github" / "open-code-review" / "rules.json").read_text())
    assert "tests/**/*.py" in rules["include"]


def test_workflow_routes_review_through_gateway_with_service_principal_token():
    workflow = WORKFLOW_PATH.read_text()
    assert "llm_url: ${{ env.DATABRICKS_HOST }}/ai-gateway/anthropic\n" in workflow
    assert "llm_auth_token: ${{ steps.gateway-auth.outputs.token }}" in workflow
    assert "llm_auth_header: Authorization" in workflow
    assert 'llm_use_anthropic: "true"' in workflow
    assert "llm_model: system.ai.claude-opus-5-5" in workflow
    assert '"thinking":{"type":"adaptive"}' in workflow
    assert "llm_extra_headers: x-databricks-use-coding-agent-mode=true" in workflow
    assert "UG_CI_REVIEW_MODEL" not in workflow
    assert "ANTHROPIC_" not in workflow


def test_workflow_uses_existing_authenticated_npm_proxy():
    workflow = WORKFLOW_PATH.read_text()
    assert "id-token: write" in workflow
    assert "jfrog/setup-jfrog-cli@279b1f629f43dd5bc658d8361ac4802a7ef8d2d5" in workflow
    assert "oidc-provider-name: github-actions" in workflow
    assert (
        "NPM_CONFIG_REGISTRY: https://databricks.jfrog.io/artifactory/api/npm/db-npm/" in workflow
    )
    assert "NPM_CONFIG_USERCONFIG: ${{ runner.temp }}/ug-review-package-config/.npmrc" in workflow
    assert "NPM_TOKEN: ${{ steps.jfrog.outputs.oidc-token }}" in workflow
    assert "umask 077" in workflow
    assert "//%s:_authToken=%s" in workflow
    assert (
        'if: always()\n        run: rm -f "$RUNNER_TEMP/ug-review-package-config/.npmrc"'
        in workflow
    )
    job_environment = workflow.split("    env:\n", 1)[1].split("    steps:\n", 1)[0]
    assert "${{ runner." not in job_environment
    assert "BUN_CONFIG_TOKEN" not in workflow
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in workflow
    assert "strict-ssl=false" not in workflow


def test_workflow_passes_trusted_rubric_as_background_and_checks_completeness():
    workflow = WORKFLOW_PATH.read_text()
    assert "cat AGENTS.md .github/ug-review-instructions.md" in workflow
    assert "path: .ug-review-trusted" in workflow
    assert "working-directory: .ug-review-trusted" in workflow
    assert "run: python3 .ug-review-trusted/.github/scripts/ug-review/auth.py" in workflow
    assert "background: ${{ steps.policy.outputs.background }}" in workflow
    assert "OCR review is incomplete" in workflow
    assert "OCR result does not match the requested commit." in workflow
    assert not (AUTH_PATH.parent / "review.py").exists()
