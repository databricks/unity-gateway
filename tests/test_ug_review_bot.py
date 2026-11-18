"""Component checks for gateway authentication and the Open Code Review workflow."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
AUTH_PATH = ROOT / ".github" / "scripts" / "ug-review" / "auth.py"
SPEC = importlib.util.spec_from_file_location("ug_review_auth", AUTH_PATH)
assert SPEC is not None and SPEC.loader is not None
gateway_auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway_auth)
RECOMMENDATION_PATH = ROOT / ".github" / "scripts" / "ug-review" / "recommendation.py"
RECOMMENDATION_SPEC = importlib.util.spec_from_file_location(
    "ug_review_recommendation", RECOMMENDATION_PATH
)
assert RECOMMENDATION_SPEC is not None and RECOMMENDATION_SPEC.loader is not None
recommendation = importlib.util.module_from_spec(RECOMMENDATION_SPEC)
RECOMMENDATION_SPEC.loader.exec_module(recommendation)
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


def test_workflow_keeps_authorized_comment_trusted_checkout_and_existing_credentials():
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
    assert "pull_request:\n    types: [labeled, ready_for_review]" in workflow
    assert "github.event_name == 'pull_request' &&" in workflow
    assert "github.event.pull_request.head.repo.full_name == github.repository &&" in workflow
    assert (
        "(github.event.action == 'labeled' &&\n"
        '              contains(fromJSON(\'["ug-review", "code-review"]\'), github.event.label.name)'
        in workflow
    )
    assert "github.event_name == 'issue_comment' &&" in workflow
    assert (
        "ug-review-${{ github.event.pull_request.number || github.event.issue.number }}" in workflow
    )
    assert "pr_number: ${{ steps.pr.outputs.number }}" in workflow
    assert "base_ref: ${{ steps.pr.outputs.base }}" in workflow
    assert "head_sha: ${{ steps.pr.outputs.head }}" in workflow


def _review_runs_on_pull_request_event(workflow: str, action: str, label, in_repo: bool) -> bool:
    text = (ROOT / ".github" / "workflows" / workflow).read_text().split("\n  review:\n", 1)[1]
    guard = re.search(r"if: >-\s*\$\{\{(.*?)\}\}", text, re.S)
    assert guard is not None
    expression = " ".join(guard[1].replace("&&", " and ").replace("||", " or ").split())
    event = SimpleNamespace(
        action=action,
        label=SimpleNamespace(name=label) if label else None,
        pull_request=SimpleNamespace(
            head=SimpleNamespace(
                repo=SimpleNamespace(full_name="databricks/unity-gateway" if in_repo else "x/fork")
            )
        ),
    )
    github = SimpleNamespace(
        event_name="pull_request", event=event, repository="databricks/unity-gateway"
    )
    # The guard uses only boolean operators, equality, contains, and fromJSON.
    return bool(
        eval(
            expression,
            {"__builtins__": {}},
            {
                "github": github,
                "contains": lambda items, value: value in items,
                "fromJSON": json.loads,
            },
        )
    )


@pytest.mark.parametrize("workflow", ["ug-review.yml", "polly-review.yml"])
@pytest.mark.parametrize(
    ("action", "label", "in_repo", "runs"),
    [
        ("ready_for_review", None, True, True),
        ("ready_for_review", None, False, False),
        ("labeled", "ug-review", True, True),
        ("labeled", "code-review", True, True),
        ("labeled", "code-review", False, False),
        ("labeled", "bug", True, False),
    ],
)
def test_pull_request_guard_runs_ready_for_review_and_review_labels_only_in_repo(
    workflow, action, label, in_repo, runs
):
    assert _review_runs_on_pull_request_event(workflow, action, label, in_repo) == runs


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


def test_workflow_posts_findings_inline_and_drops_summary_only_when_nothing_was_routed():
    workflow = WORKFLOW_PATH.read_text()
    assert "route_severity_below" not in workflow
    step = workflow.split("      - name: Remove the summary comment\n", 1)[1].split(
        "\n      - name:", 1
    )[0]
    assert "steps.ocr.outputs.comments_failed == '0'" in step
    assert "steps.ocr.outputs.comments_routed == '0'" in step
    assert "<!-- ocr-summary-run:${process.env.REVIEW_RUN} -->" in step
    assert "github.rest.issues.deleteComment(args)" in step


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("RECOMMENDATION: APPROVE", "approve"),
        ("RECOMMENDATION: REJECT", "reject"),
        ("  recommendation: reject  ", "reject"),
        ("Some preamble.\nRECOMMENDATION: APPROVE\nREASONS:\n- ok", "approve"),
        ("the verdict is RECOMMENDATION: APPROVE on this line", None),
        ("RECOMMENDATION: MAYBE", None),
        ("RECOMMENDATION:APPROVE", None),
        ("", None),
        (None, None),
    ],
)
def test_recommendation_marker_parses_only_on_its_own_line(text, expected):
    assert recommendation.parse_recommendation(text) == expected


def test_extract_reasons_reads_bullets_after_the_reasons_header():
    text = "RECOMMENDATION: REJECT\nREASONS:\n- leaks credentials\n- 2. breaks revert\n"
    assert recommendation.extract_reasons(text, "reject") == [
        "leaks credentials",
        "breaks revert",
    ]


def test_extract_reasons_falls_back_to_plain_lines_without_a_reasons_header():
    text = "RECOMMENDATION: APPROVE\nThe remaining findings are minor.\n"
    assert recommendation.extract_reasons(text, "approve") == ["The remaining findings are minor."]


def test_extract_reasons_keeps_a_fallback_when_the_marker_has_no_reasons():
    text = "RECOMMENDATION: REJECT\n"
    reasons = recommendation.extract_reasons(text, "reject")
    assert reasons == ["The reviewer returned the marker without reasons; see the inline findings."]


def _recommendation_environment(monkeypatch, tmp_path, result, model_text=None, model_error=None):
    result_path = tmp_path / "ocr-result.json"
    result_path.write_text(json.dumps(result))
    output_path = tmp_path / "github-output"
    output_path.write_text("")
    body_path = tmp_path / "recommendation-body.md"
    monkeypatch.setenv("OCR_RESULT_PATH", str(result_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setenv("BODY_PATH", str(body_path))
    monkeypatch.setenv("RECOMMENDATION_RUN", "run-1")
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("GATEWAY_TOKEN", "access-token")

    calls = []

    def request(host, token, model, briefs):
        calls.append((host, token, model, briefs))
        if model_error is not None:
            raise model_error
        return model_text

    monkeypatch.setattr(recommendation, "request_recommendation", request)
    return output_path, body_path, calls


def test_zero_finding_complete_review_approves_without_a_model_call(monkeypatch, tmp_path, capsys):
    result = {"status": "complete", "summary": {"files_reviewed": 4}, "comments": []}
    output_path, body_path, calls = _recommendation_environment(
        monkeypatch,
        tmp_path,
        result,
        model_error=AssertionError("A clean review must not request a recommendation."),
    )

    assert recommendation.main() == 0
    assert "recommendation=approve\n" in output_path.read_text()
    body = body_path.read_text()
    assert "**RECOMMENDATION: APPROVE**" in body
    assert "no findings on the changed lines" in body
    assert "<!-- ug-review-recommendation-run:run-1 -->" in body
    assert calls == []


def test_findings_reach_the_model_and_a_reject_marker_removes_the_label(monkeypatch, tmp_path):
    result = {
        "status": "complete",
        "summary": {"files_reviewed": 2},
        "comments": [
            {
                "path": "a.py",
                "start_line": 1,
                "end_line": 2,
                "severity": "high",
                "category": "bug",
                "content": "drops user state",
            },
        ],
    }
    output_path, body_path, calls = _recommendation_environment(
        monkeypatch,
        tmp_path,
        result,
        model_text="RECOMMENDATION: REJECT\nREASONS:\n- drops user state\n",
    )

    assert recommendation.main() == 0
    assert "recommendation=reject\n" in output_path.read_text()
    host, token, model, briefs = calls[0]
    assert (host, token) == ("https://example.databricks.com", "access-token")
    assert model
    assert briefs[0]["path"] == "a.py" and briefs[0]["severity"] == "high"
    body = body_path.read_text()
    assert "**RECOMMENDATION: REJECT**" in body
    assert "- drops user state" in body
    assert "ready-to-approve" in body


def test_unparseable_model_output_yields_no_recommendation_and_no_label_change(
    monkeypatch, tmp_path, capsys
):
    result = {
        "status": "complete",
        "comments": [{"path": "a.py", "start_line": 1, "end_line": 1, "content": "nit"}],
    }
    output_path, body_path, _ = _recommendation_environment(
        monkeypatch, tmp_path, result, model_text="Looks fine to me overall."
    )

    assert recommendation.main() == 0
    assert "recommendation=none\n" in output_path.read_text()
    body = body_path.read_text()
    assert "**No recommendation**" in body
    assert "parseable `RECOMMENDATION: APPROVE|REJECT`" in body
    assert "No label was changed." in body
    assert "::warning::" in capsys.readouterr().out


def test_failed_recommendation_request_still_posts_the_note_without_failing(
    monkeypatch, tmp_path, capsys
):
    result = {
        "status": "complete",
        "comments": [{"path": "a.py", "start_line": 1, "end_line": 1, "content": "bug"}],
    }
    output_path, body_path, _ = _recommendation_environment(
        monkeypatch,
        tmp_path,
        result,
        model_error=recommendation.RecommendationError("gateway down"),
    )

    assert recommendation.main() == 0
    assert "recommendation=none\n" in output_path.read_text()
    assert "gateway down" in body_path.read_text()
    assert "::warning::" in capsys.readouterr().out


@pytest.mark.parametrize(
    "result",
    [
        {"status": "skipped", "comments": []},
        {
            "status": "complete",
            "comments": [],
            "warnings": [{"Type": "review_limit", "Message": "x"}],
        },
    ],
)
def test_incomplete_or_warned_reviews_do_not_recommend(monkeypatch, tmp_path, result):
    output_path, body_path, calls = _recommendation_environment(
        monkeypatch,
        tmp_path,
        result,
        model_error=AssertionError("An unsafe verdict must not reach the model."),
    )

    assert recommendation.main() == 0
    assert "recommendation=none\n" in output_path.read_text()
    assert "No recommendation" in body_path.read_text()
    assert calls == []


def test_workflow_posts_recommendation_and_keeps_the_ready_to_approve_label_current():
    workflow = WORKFLOW_PATH.read_text()
    derive_index = workflow.index("- name: Derive the review recommendation")
    publish_index = workflow.index("- name: Publish the review recommendation")
    completeness_index = workflow.index("- name: Report review completeness")
    assert completeness_index < derive_index < publish_index
    assert "run: python3 .ug-review-trusted/.github/scripts/ug-review/recommendation.py" in workflow
    assert "GATEWAY_TOKEN: ${{ steps.gateway-auth.outputs.token }}" in workflow
    assert "GATEWAY_MODEL: system.ai.claude-opus-5-5" in workflow
    assert "steps.ocr.outcome == 'success'" in workflow

    publish = workflow[publish_index:].split("\n      - name:", 1)[0]
    assert "RECOMMENDATION_LABEL: ready-to-approve" in publish
    assert "createComment" in publish
    assert "addLabels({ ...args, labels: [label] })" in publish
    assert "listLabelsOnIssue" in publish
    assert "removeLabel({ ...args, name: label })" in publish
    # The job already holds the narrow write scopes the label sync needs.
    assert "issues: write" in workflow
    assert "pull-requests: write" in workflow
    assert "contents: write" not in workflow
