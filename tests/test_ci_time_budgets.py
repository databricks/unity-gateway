"""Contracts for CI job time budgets."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / "scripts" / "ci_time_budget.py"
BUDGETS = ROOT / ".github" / "ci-time-budgets.json"


def test_every_ci_job_checks_a_budget():
    assert BUDGETS.is_file(), "CI jobs have no shared budgets yet"
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    for workflow in (
        "ci",
        "integration",
        "publish-skills-test",
        "user-journey-test-required",
        "zizmor",
    ):
        source = (WORKFLOWS / f"{workflow}.yml").read_text().split("\njobs:\n", 1)[1]
        jobs = dict(re.findall(r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", source))
        for job_id, job in jobs.items():
            if re.search(r"(?m)^    uses: ", job):
                continue
            assert job_id in budgets[workflow], (workflow, job_id)
            variants = budgets[workflow][job_id]
            assert variants and all(
                set(budget) == {"setup", "run", "total"} for budget in variants.values()
            )
            name = re.search(r"(?m)^    name: (.+)$", job).group(1)
            if "${{" not in name:
                assert name in variants, (workflow, job_id, name)
            if "steps: *live-steps" in job:
                continue
            assert "budget-start" in job or "Start the budget clock" in job, (workflow, job_id)
            assert "budget-setup-done" in job or "Mark setup done" in job, (workflow, job_id)
            assert "budget-check" in job or "Check this job's time budget" in job, (
                workflow,
                job_id,
            )

    gate = (WORKFLOWS / "security-gate.yml").read_text()
    assert "Check the gate time budget" in gate
    assert "actions/checkout" not in gate


def test_job_over_budget_fails(tmp_path):
    budgets = tmp_path / "budgets.json"
    budgets.write_text(
        json.dumps({"ci": {"sample": {"sample": {"setup": 10, "run": 20, "total": 30}}}})
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--budgets",
            str(budgets),
            "--workflow",
            "ci",
            "--job",
            "sample",
            "--name",
            "sample",
            "--started",
            "100",
            "--run-started",
            "110",
            "--ended",
            "131",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "over budget" in result.stdout.lower()


def test_job_within_budget_passes(tmp_path):
    budgets = tmp_path / "budgets.json"
    budgets.write_text(
        json.dumps({"ci": {"sample": {"sample": {"setup": 10, "run": 20, "total": 30}}}})
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--budgets",
            str(budgets),
            "--workflow",
            "ci",
            "--job",
            "sample",
            "--name",
            "sample",
            "--started",
            "100",
            "--run-started",
            "110",
            "--ended",
            "130",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dependency_changes_make_overruns_a_notice(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.name", "CI Test"), ("user.email", "ci-test@example.invalid")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / "pyproject.toml").write_text("version = '1'\n")
    subprocess.run(["git", "-C", str(repo), "add", "pyproject.toml"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "pyproject.toml").write_text("version = '2'\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "change dependencies"], check=True)
    changed = subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--name-only",
            base,
            "HEAD",
            "--",
            "uv.lock",
            "pyproject.toml",
        ],
        text=True,
    )
    budgets = tmp_path / "budgets.json"
    budgets.write_text(
        json.dumps({"ci": {"sample": {"sample": {"setup": 10, "run": 20, "total": 30}}}})
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--budgets",
            str(budgets),
            "--workflow",
            "ci",
            "--job",
            "sample",
            "--name",
            "sample",
            "--started",
            "100",
            "--run-started",
            "110",
            "--ended",
            "131",
            "--changed-files",
            changed,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "::notice::" in result.stdout


def test_blocking_jobs_do_not_suppress_budget_failures():
    for workflow in (
        "ci",
        "integration",
        "publish-skills-test",
        "user-journey-test-required",
        "zizmor",
    ):
        source = (WORKFLOWS / f"{workflow}.yml").read_text()
        check = source.split("name: Check this job's time budget\n", 1)[1].split("\n      - ", 1)[0]
        assert "if: ${{ github.event_name == 'pull_request' && !cancelled() }}" in check
        assert "continue-on-error:" not in check
        if workflow != "user-journey-test-required":
            assert 'git diff --name-only "$BUDGET_BASE" HEAD -- uv.lock pyproject.toml' in check
            assert '--changed-files "$changed"' in check
        jobs = dict(
            re.findall(
                r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)",
                source.split("\njobs:\n", 1)[1],
            )
        )
        for job_id, job in jobs.items():
            if re.search(r"(?m)^    (?:uses: |continue-on-error: true$)", job):
                continue
            if "steps: *live-steps" in job:
                job = jobs["smoke"]
            check = re.search(
                r"(?m)^      - (?:[&*]budget-check\b|name: Check this job's time budget$)", job
            )
            assert check, (workflow, job_id)
            assert not re.search(r"(?m)^      - ", job[check.end() :]), (workflow, job_id)

    gate = (WORKFLOWS / "security-gate.yml").read_text()
    check = gate.split("      - name: Check the gate time budget\n", 1)[1]
    assert "continue-on-error:" not in check
    assert "exit 1" in check


@pytest.mark.parametrize(
    ("budget", "phase"),
    [
        ({"setup": 9, "run": 20, "total": 100}, "setup"),
        ({"setup": 10, "run": 19, "total": 100}, "run"),
        ({"setup": 10, "run": 20, "total": 29}, "total"),
    ],
)
def test_each_phase_independently_fails_when_over_budget(tmp_path, budget, phase):
    budgets = tmp_path / "budgets.json"
    budgets.write_text(json.dumps({"ci": {"sample": {"sample": budget}}}))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--budgets",
            str(budgets),
            "--workflow",
            "ci",
            "--job",
            "sample",
            "--name",
            "sample",
            "--started",
            "100",
            "--run-started",
            "110",
            "--ended",
            "130",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert f"{phase} " in result.stdout.split("::error::", 1)[1]


def test_every_dynamic_ci_shard_has_a_budget():
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    source = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    jobs = dict(re.findall(r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", source))
    for os_name, job_id in (("Linux", "unit-linux"), ("Windows", "unit-windows")):
        shard_list = re.search(r"shard: \[([^\]]+)\]", jobs[job_id], re.DOTALL).group(1)
        shards = [int(value.strip()) for value in shard_list.split(",")]
        expected = {f"Unit tests · {os_name} · {shard}/{len(shards)}" for shard in shards}
        assert set(budgets["ci"][job_id]) == expected

    shard_job = jobs["e2e-shards"]
    groups = re.search(r"group: \[([^\]]+)\]", shard_job).group(1).split(", ")
    names = re.findall(r"(?m)^            name: (.+)$", shard_job)
    assert len(groups) == len(names)
    expected = {f"{name} · {os_name}" for name in names for os_name in ("Linux", "Windows")}
    assert set(budgets["ci"]["e2e-shards"]) == expected

    expected = set()
    for path in (ROOT / "tests/e2e_cuj").glob("test_*.py"):
        name = re.search(r'^CUJ_NAME = "(.+)"$', path.read_text(encoding="utf-8"), re.MULTILINE)
        assert name, path
        expected.add(f"E2E CUJs · {name.group(1)}")
    assert set(budgets["integration"]["dedicated-cuj"]) == expected


def test_budget_steps_do_not_run_in_merge_queue():
    for workflow in ("ci", "security-gate", "publish-skills-test", "zizmor"):
        current = (WORKFLOWS / f"{workflow}.yml").read_text(encoding="utf-8")
        _merge_queue_shape(current)


def _merge_queue_shape(source):
    jobs = dict(
        re.findall(
            r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)",
            source.split("\njobs:\n", 1)[1],
        )
    )
    result = {}
    for job_id, job in jobs.items():
        header, _, steps = job.partition("\n    steps:")
        condition = re.search(r"(?m)^    if: (.*)$", header)
        if condition and "github.event_name != 'merge_group'" in condition.group(1):
            if "||" not in condition.group(1):
                continue
        metadata = tuple(re.findall(r"(?m)^    (name|needs|if|uses): (.*)$", header))
        selected = []
        for block in re.split(r"(?m)^      - ", steps)[1:]:
            first = block.split("\n", 1)[0]
            budget_alias = re.match(r"\*budget-(?:start|setup-done|check)$", first)
            budget_step = re.match(r"&budget-(?:start|setup-done|check)$", first) or first in {
                "name: Start the budget clock",
                "name: Mark setup done",
                "name: Check this job's time budget",
                "name: Start the gate budget clock",
                "name: Check the gate time budget",
            }
            if budget_alias:
                continue
            if budget_step:
                assert "github.event_name == 'pull_request'" in block, first
                continue
            if first.startswith("uses: actions/checkout@") and (
                "if: ${{ github.event_name == 'pull_request' }}" in block
            ):
                continue
            if (
                "github.event_name == 'pull_request' || github.event_name == 'issue_comment'"
                in block
            ):
                continue
            lines = [
                line
                for line in block.splitlines()
                if not line.lstrip().startswith("#")
                and line.strip()
                != "fetch-depth: ${{ github.event_name == 'pull_request' && 2 || 1 }}"
            ]
            selected.append("\n".join(lines).strip())
        result[job_id] = (metadata, selected)
    return result


def test_every_integration_matrix_variant_has_a_budget():
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    source = (WORKFLOWS / "integration.yml").read_text(encoding="utf-8")
    jobs = dict(re.findall(r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", source))

    def values(job, field):
        match = re.search(rf"(?m)^        {field}: \[([^\]]+)\]", jobs[job])
        assert match, (job, field)
        return [value.strip().strip("'\"") for value in match.group(1).split(",")]

    def os_name(value):
        return "Windows" if value == "windows-latest" else "Linux"

    expected = {}
    for job in ("smoke", "full"):
        label = "Smoke" if job == "smoke" else "Full"
        expected[job] = {
            f"{label} journeys · {agent.capitalize()} · {os_name(os)}"
            for agent in values(job, "agent")
            for os in values(job, "os")
        }
        if job == "full":
            extra = re.findall(
                r"(?m)^          - os: (.+)\n            agent: (.+)\n            smart_routing: true$",
                jobs[job],
            )
            expected[job].update(
                f"Full journeys · {agent.capitalize()} · {os_name(os)} · Smart routing"
                for os, agent in extra
            )
    expected["anthropic-mps"] = {
        f"Anthropic MPS - Staging workspace · {agent.capitalize()} · {os_name(os)}"
        + (" (non-blocking)" if agent == "copilot" else "")
        for agent in values("anthropic-mps", "agent")
        for os in values("anthropic-mps", "os")
    }
    expected["soft-fail"] = {
        f"Soft-fail journeys · {agent} · {os_name(os)} (non-blocking)"
        for agent in values("soft-fail", "agent")
        for os in values("soft-fail", "os")
    }
    for job, label in (
        ("opencode", "OpenCode"),
        ("copilot", "Copilot"),
        ("vscode", "VS Code Copilot Chat"),
    ):
        expected[job] = {
            f"Full journeys · {label} · {os_name(os)} (non-blocking)" for os in values(job, "os")
        }
    expected["managed"] = {
        f"Managed config · {agent.capitalize()}" for agent in values("managed", "agent")
    }
    expected["managed"].update(
        f"Managed config · {agent.capitalize()} · Smart routing"
        for agent in re.findall(
            r"(?m)^          - agent: (.+)\n            smart_routing: true$", jobs["managed"]
        )
    )
    for job, os in (("cowork-inference-windows", "Windows"), ("cowork-inference-macos", "macOS")):
        sources = re.findall(r"(?m)^            name: (.+)$", jobs[job])
        expected[job] = {f"Cowork desktop tasks · {os} · {name}" for name in sources}
    for job, names in expected.items():
        assert names, job
        assert set(budgets["integration"][job]) == names

    publisher = (WORKFLOWS / "publish-skills-test.yml").read_text(encoding="utf-8")
    versions = re.search(r"(?m)^        python: \[([^\]]+)\]", publisher).group(1)
    expected_publishers = {
        f"Publisher tests (Python {version.strip().strip(chr(39))})"
        for version in versions.split(",")
    }
    assert set(budgets["publish-skills-test"]["tests"]) == expected_publishers


def test_budget_steps_do_not_inherit_job_level_secrets():
    for workflow in ("ci", "integration", "publish-skills-test", "zizmor"):
        source = (WORKFLOWS / f"{workflow}.yml").read_text(encoding="utf-8")
        secret_free = source.split("env: &budget-secret-free\n", 1)[1].split("        run:", 1)[0]
        check = source.split("name: Check this job's time budget\n", 1)[1].split("\n      - ", 1)[0]
        cleared_at_start = set(re.findall(r"(?m)^          ([A-Z_]+): ''$", secret_free))
        cleared_at_check = set(re.findall(r"(?m)^          ([A-Z_]+): ''$", check))
        jobs = dict(
            re.findall(
                r"(?ms)^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)",
                source.split("\njobs:\n", 1)[1],
            )
        )
        for job_id, job in jobs.items():
            header = job.split("\n    steps:", 1)[0]
            names = set(re.findall(r"(?m)^      ([A-Z_]+): .*secrets\.", header))
            assert names <= cleared_at_start, (workflow, job_id, names - cleared_at_start)
            assert names <= cleared_at_check, (workflow, job_id, names - cleared_at_check)


def test_trusted_user_journey_budget_fails_on_overrun(tmp_path):
    import os
    import textwrap

    source = (WORKFLOWS / "user-journey-test-required.yml").read_text(encoding="utf-8")
    check = source.split("      - name: Check this job's time budget\n", 1)[1]
    python = check.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    budget_dir = tmp_path / "budget-source" / ".github"
    budget_dir.mkdir(parents=True)
    (budget_dir / "ci-time-budgets.json").write_text(
        json.dumps(
            {
                "user-journey-test-required": {
                    "require-user-journey-test": {
                        "User Journey Test Required": {"setup": 10, "run": 20, "total": 30}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(python)],
        cwd=tmp_path,
        env={
            **os.environ,
            "BUDGET_JOB_STARTED": "100",
            "BUDGET_RUN_STARTED": "110",
            "BUDGET_ENDED": "131",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Over budget" in result.stderr


def test_over_budget_with_changed_dependency_files_is_a_notice(tmp_path):
    budgets = tmp_path / "budgets.json"
    budgets.write_text(
        json.dumps({"ci": {"sample": {"sample": {"setup": 10, "run": 20, "total": 30}}}})
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--budgets",
            str(budgets),
            "--workflow",
            "ci",
            "--job",
            "sample",
            "--name",
            "sample",
            "--started",
            "100",
            "--run-started",
            "110",
            "--ended",
            "131",
            "--changed-files",
            "pyproject.toml",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "::notice::" in result.stdout
