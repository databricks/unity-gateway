"""Component checks for gateway authentication and the official Claude review workflow."""

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
    assert "/pull/${{ github.event.pull_request.number || github.event.issue.number }}" in workflow


def test_workflow_uses_stock_official_action_and_review_plugin_without_source_patches():
    workflow = WORKFLOW_PATH.read_text()
    assert (
        "uses: anthropics/claude-code-action@2dca132ff0e0c4094ce6048b422c6915a071210b" in workflow
    )
    assert "plugin_marketplaces: https://github.com/anthropics/claude-code.git" in workflow
    assert "plugins: code-review@claude-code-plugins" in workflow
    assert "git apply" not in workflow
    assert "gateway-classifier.patch" not in workflow
    assert ".ug-review-action" not in workflow
    assert "--plugin-dir" not in workflow
    assert all(
        re.fullmatch(r"[0-9a-f]{40}", action)
        for action in re.findall(r"uses: actions/checkout@(\S+)", workflow)
    )


def test_workflow_routes_root_and_family_aliases_through_gateway_without_direct_anthropic_calls():
    workflow = WORKFLOW_PATH.read_text()
    assert "/ai-gateway/anthropic\n" in workflow
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL: system.ai.claude-haiku-5-5" in workflow
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL: system.ai.sonnet-5-5" in workflow
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL: system.ai.claude-opus-5-5" in workflow
    assert "--model sonnet" in workflow
    assert "anthropic_api_key: ${{ steps.gateway-auth.outputs.token }}" in workflow
    assert 'classify_inline_comments: "false"' in workflow
    assert 'ANTHROPIC_CUSTOM_HEADERS: "x-databricks-use-coding-agent-mode: true"' in workflow
    assert "UG_CI_REVIEW_MODEL" not in workflow
    assert "CLAUDE_CONFIG_DIR: ${{ runner.temp }}" in workflow


def test_workflow_uses_existing_authenticated_npm_proxy_without_patching_stock_installer():
    workflow = WORKFLOW_PATH.read_text()
    assert "id-token: write" in workflow
    assert "jfrog/setup-jfrog-cli@279b1f629f43dd5bc658d8361ac4802a7ef8d2d5" in workflow
    assert "oidc-provider-name: github-actions" in workflow
    assert (
        "NPM_CONFIG_REGISTRY: https://databricks.jfrog.io/artifactory/api/npm/db-npm/" in workflow
    )
    assert "NPM_CONFIG_USERCONFIG: ${{ runner.temp }}/ug-review-package-config/.npmrc" in workflow
    assert "XDG_CONFIG_HOME: ${{ runner.temp }}/ug-review-package-config" in workflow
    assert "NPM_TOKEN: ${{ steps.jfrog.outputs.oidc-token }}" in workflow
    assert "umask 077" in workflow
    assert "//%s:_authToken=%s" in workflow
    assert (
        'if: always()\n        run: rm -f "$RUNNER_TEMP/ug-review-package-config/.npmrc"'
        in workflow
    )
    job_environment = workflow.split("    env:\n", 1)[1].split("    steps:\n", 1)[0]
    assert "${{ runner." not in job_environment
    assert "path_to_bun_executable" not in workflow
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in workflow
    assert "strict-ssl=false" not in workflow


def test_workflow_applies_trusted_rubric_to_parent_and_subagents_and_enables_inline_tool():
    workflow = WORKFLOW_PATH.read_text()
    assert (
        'cat AGENTS.md .github/ug-review-instructions.md > "$RUNNER_TEMP/ug-review-policy.md"'
        in workflow
    )
    assert "--append-system-prompt-file ${{ runner.temp }}/ug-review-policy.md" in workflow
    assert "--append-subagent-system-prompt-file ${{ runner.temp }}/ug-review-policy.md" in workflow
    assert "--setting-sources user,project" in workflow
    assert "--permission-mode dontAsk" in workflow
    assert "--disallowedTools" in workflow
    assert "mcp__github_inline_comment__create_inline_comment" in workflow
    assert "/code-review:code-review --comment" in workflow
    assert not (AUTH_PATH.parent / "review.py").exists()
