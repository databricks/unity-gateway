"""Contract for PR live-suite scheduling and the required e2e rollup."""

import ast
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

CI = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"


def _job(name):
    return re.split(
        r"\n  [a-z][a-z0-9-]*:\n", CI.read_text().split(f"\n  {name}:\n", 1)[1], maxsplit=1
    )[0]


def _condition(name, event, draft, gate="success", *, cancelled=False):
    expression = re.search(r"(?m)^    if: \$\{\{ (.+) }}$", _job(name)).group(1)
    for key, value in {
        "github.event_name": event,
        "github.event.pull_request.draft": draft,
        "needs.security-gate.result": gate,
        "cancelled()": cancelled,
        "always()": True,
    }.items():
        expression = expression.replace(key, repr(value))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    tree = ast.parse(expression.strip(), mode="eval")
    assert all(
        isinstance(
            node,
            (
                ast.Expression,
                ast.BoolOp,
                ast.UnaryOp,
                ast.Compare,
                ast.Constant,
                ast.And,
                ast.Or,
                ast.Not,
                ast.Eq,
                ast.NotEq,
            ),
        )
        for node in ast.walk(tree)
    )
    return eval(compile(tree, "<workflow condition>", "eval"), {"__builtins__": {}})


def test_pull_request_subscriptions_include_ready_and_draft_transitions():
    triggers = CI.read_text().split("\npermissions:", 1)[0]
    assert (
        "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft]" in triggers
    )


@pytest.mark.parametrize(
    ("event", "draft", "live"),
    [
        ("pull_request", True, False),
        ("pull_request", False, True),
        ("merge_group", False, False),
        ("push", False, True),
    ],
)
@pytest.mark.parametrize("job", ["e2e-shards", "integration"])
def test_live_jobs_follow_readiness_and_leave_other_events_unchanged(job, event, draft, live):
    assert _condition(job, event, draft) is live


def test_fork_readiness_does_not_change_the_existing_job_conditions():
    for job in ("e2e-shards", "integration"):
        assert "head.repo" not in _job(job).split("\n    if:", 1)[1].split("\n", 1)[0]
        assert _condition(job, "pull_request", False)
        assert not _condition(job, "pull_request", True)


def test_converting_to_draft_cancels_the_prior_pull_request_run():
    concurrency = CI.read_text().split("\nconcurrency:\n", 1)[1].split("\njobs:\n", 1)[0]
    assert (
        "group: ci-${{ github.event.pull_request.number || github.ref }}-${{ github.event_name }}"
        in concurrency
    )
    assert "cancel-in-progress: ${{ github.event_name != 'schedule' }}" in concurrency
    assert not _condition("e2e-shards", "pull_request", True)
    assert not _condition("integration", "pull_request", True)


def test_live_jobs_remain_behind_security_gate():
    shards = _job("e2e-shards")
    assert "needs: security-gate" in shards
    condition = re.search(r"(?m)^    if: (.*)$", shards).group(1)
    # A status function removes GitHub's implicit success() guard on dependencies.
    assert not re.search(r"\b(?:always|cancelled|failure|success)\(", condition)
    assert "needs: security-gate" in _job("integration")
    assert not _condition("integration", "pull_request", False, "failure")


@pytest.mark.parametrize("cancelled", [False, True])
def test_merge_group_rollup_keeps_mains_cancellation_condition(cancelled):
    assert _condition("required-e2e", "merge_group", False, cancelled=cancelled) == (not cancelled)


def test_pr_rollup_evaluates_even_when_upstream_work_is_cancelled():
    assert _condition("required-e2e", "pull_request", False, cancelled=True)


@pytest.mark.skipif(os.name == "nt", reason="required-e2e runs on Linux")
@pytest.mark.parametrize("shards", ["success", "failure", "skipped", "cancelled"])
def test_agent_rollup_requires_every_shard(shards):
    agent = _job("e2e")
    assert "needs: [security-gate, e2e-shards]" in agent
    assert "needs.security-gate.result == 'success'" in agent
    result = subprocess.run(
        ["/bin/bash", "-c", agent.split("        run: ", 1)[1].split("\n      - ", 1)[0].strip()],
        env={**os.environ, "RESULT": shards},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is (shards == "success")


@pytest.mark.skipif(os.name == "nt", reason="required-e2e runs on Linux")
@pytest.mark.parametrize("gate", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize("agent", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize("integration", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_required_rollup_fails_closed(event, gate, agent, integration):
    required = _job("required-e2e")
    assert "needs: [security-gate, e2e, integration]" in required
    assert (
        "if: ${{ always() && (github.event_name == 'pull_request' || !cancelled()) }}" in required
    )
    script = required.split("        run: ", 1)[1].split("\n      - ", 1)[0].strip()
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            **os.environ,
            "EVENT_NAME": event,
            "GATE_RESULT": gate,
            "RESULT": agent,
            "INTEGRATION_RESULT": integration,
        },
        capture_output=True,
        timeout=10,
        check=False,
    )
    # The gate must succeed on every event; on merge_group the e2e and integration
    # results are skipped by design and only the gate still fails the rollup.
    assert (result.returncode == 0) is (
        gate == "success" and (event == "merge_group" or agent == integration == "success")
    )


@pytest.mark.skipif(os.name == "nt", reason="required-tests runs on Linux")
@pytest.mark.parametrize("gate", ["success", "failure", "cancelled", "skipped"])
def test_required_tests_rollup_requires_the_security_gate(gate):
    required = _job("required-tests")
    assert (
        "needs: [security-gate, checks, unit-linux, unit-windows, unit-test-times, "
        "windows-provisioning, vscode-extension]" in required
    )
    script = textwrap.dedent(required.split("        run: |", 1)[1].split("\n      - ", 1)[0])
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            **os.environ,
            "GATE_RESULT": gate,
            "LINT_RESULT": "success",
            "RESULT": "success",
            "WINDOWS_RESULT": "success",
            "PROVISIONING_RESULT": "success",
            "VSCODE_EXTENSION_RESULT": "success",
            "COVERAGE_RESULT": "success",
        },
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is (gate == "success")
