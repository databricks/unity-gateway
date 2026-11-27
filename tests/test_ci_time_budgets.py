"""Contracts for CI job time budgets."""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
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
            if "steps: *managed-steps" in job:
                job = jobs["managed"]
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


def test_live_shard_setup_summary_shows_download_stalls_without_weakening_budget(tmp_path):
    source = (WORKFLOWS / "integration.yml").read_text(encoding="utf-8")
    job = source.split("\n  live-shard-times:\n", 1)[1].split("\n  cujs:\n", 1)[0]
    steps = (
        "*budget-start",
        "actions/checkout@",
        "Mark live-shard checkout complete",
        "pattern: integration-full-claude-windows-latest-shard*",
        "Mark full-shard download complete",
        "pattern: integration-anthropic-mps-claude-windows-latest-shard*",
        "Mark MPS-shard download complete",
        "pattern: e2e-cujs-*shard*",
        "Mark CUJ-shard download complete",
        "*budget-setup-done",
        "Summarize live-shard setup",
        "*budget-check",
    )
    positions = [job.index(step) for step in steps]
    assert positions == sorted(positions)
    block = job.split("      - name: Summarize live-shard setup\n", 1)[1].split("\n      - ", 1)[0]
    script = textwrap.dedent(block.split("        run: |\n", 1)[1])
    summary = tmp_path / "summary.md"
    env = {
        **os.environ,
        "GITHUB_STEP_SUMMARY": str(summary),
        "BUDGET_JOB_STARTED": "100",
        "BUDGET_COVERAGE_CHECKOUT_DONE": "104",
        "BUDGET_COVERAGE_FULL_DONE": "234",
        "BUDGET_COVERAGE_MPS_DONE": "235",
        "BUDGET_COVERAGE_CUJ_DONE": "236",
        "BUDGET_RUN_STARTED": "237",
    }
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "-eu", "-c", script], env=env, check=True)
    text = summary.read_text()
    for row in (
        "| Checkout and clock start | 4 |",
        "| Full-shard artifacts | 130 |",
        "| MPS-shard artifacts | 1 |",
        "| CUJ-shard artifacts | 1 |",
        "| Other setup | 1 |",
    ):
        assert row in text
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    assert budgets["integration"]["live-shard-times"]["Live shard coverage"] == {
        "setup": 20,
        "run": 40,
        "total": 45,
    }
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--budgets",
            str(BUDGETS),
            "--workflow",
            "integration",
            "--job",
            "live-shard-times",
            "--name",
            "Live shard coverage",
            "--started",
            "100",
            "--run-started",
            "237",
            "--ended",
            "238",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "setup 137s > 20s" in result.stdout


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
    (repo / "pyproject.toml").write_text("version = '2'\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "change dependencies"], check=True)
    changed = subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--name-only",
            "HEAD^1",
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
        check = source.split("name: Check this job's time budget\n", 1)[1]
        check = re.split(r"(?m)^(?:      - |  [a-z0-9-]+:)", check, maxsplit=1)[0]
        assert "if: ${{ github.event_name == 'pull_request' && !cancelled() }}" in check
        assert "continue-on-error:" not in check
        if workflow != "user-journey-test-required":
            assert "git diff --name-only HEAD^1 HEAD -- uv.lock pyproject.toml" in check
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
            elif "steps: *managed-steps" in job:
                job = jobs["managed"]
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
        assert _merge_queue_shape(current), workflow


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
        match = re.search(rf"(?m)^        {field}: (.+)$", jobs[job])
        assert match, (job, field)
        expression = match.group(1)
        if expression.startswith("["):
            return [value.strip().strip("'\"") for value in expression[1:-1].split(",")]
        assert "fromJSON(" in expression, (job, field, expression)
        choices = re.findall(r"'(\[[^']+\])'", expression)
        assert choices, (job, field, expression)
        return sorted({value for choice in choices for value in json.loads(choice)})

    def os_name(value):
        return "Windows" if value == "windows-latest" else "Linux"

    expected = {
        "smoke": {
            f"Smoke journeys · {agent.capitalize()} · {os_name(os)}"
            for agent in values("smoke", "agent")
            for os in values("smoke", "os")
        }
    }
    full_matrix = jobs["full"].split("      matrix:\n", 1)[1].split("\n    env:", 1)[0]
    full_entries = []
    for block in re.split(r"(?m)^          - ", full_matrix)[1:]:
        fields = dict(line.strip().split(":", 1) for line in block.splitlines() if ":" in line)
        full_entries.append({key: value.strip().strip('"') for key, value in fields.items()})
    expected["full"] = {
        f"Full journeys · {entry['agent'].capitalize()} · {os_name(entry['os'])}"
        + (" · Smart routing" if entry.get("smart_routing") == "true" else "")
        for entry in full_entries
    }
    matrix_line = re.search(r"(?m)^        include: (.+)$", jobs["anthropic-mps"]).group(1)
    choices = re.findall(r"'(\[[^']+\])'", matrix_line)
    assert choices, matrix_line
    anthropic_entries = [entry for choice in choices for entry in json.loads(choice)]
    expected["anthropic-mps"] = {
        f"Anthropic MPS - Staging workspace · {entry['agent'].capitalize()} · {os_name(entry['os'])}"
        + (" (non-blocking)" if entry["agent"] == "copilot" else "")
        for entry in anthropic_entries
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
    expected["managed-smart"] = {"Managed config · Codex · Smart routing"}
    assert "BUDGET_WORKFLOW: integration" in jobs["managed-smart"]
    assert "BUDGET_JOB_NAME: Managed config · Codex · Smart routing" in jobs["managed-smart"]
    assert "steps: *managed-steps" in jobs["managed-smart"]
    for job, platform in (
        ("cowork-inference-windows", "Windows"),
        ("cowork-inference-macos", "macOS"),
    ):
        sources = re.findall(r"(?m)^            name: (.+)$", jobs[job])
        expected[job] = {f"Cowork desktop tasks · {platform} · {name}" for name in sources}
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
        if workflow == "integration":
            configure = (ROOT / "scripts/cowork-inference/configure.ps1").read_text()
            exported = set(re.findall(r'"([A-Z_]*PAT)=\$pat"', configure))
            assert exported == {"UG_COWORK_PAT"}
            assert exported <= cleared_at_check
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


def test_budget_diff_uses_merge_parent_when_recorded_base_moved(tmp_path):
    import shlex

    repo = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "-b", "ci-base", str(repo)], check=True)
    for key, value in (("user.name", "CI Test"), ("user.email", "ci-test@example.invalid")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / "pyproject.toml").write_text("version = '1'\n")
    (repo / "note.txt").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    recorded_base = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "feature"], check=True)
    (repo / "pyproject.toml").write_text("version = '2'\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "dependency change"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-qb", "moved-main", recorded_base], check=True
    )
    (repo / "note.txt").write_text("after\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "advance main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "merge", "-q", "--no-ff", "feature", "-m", "merge"], check=True
    )
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=2", "--branch=moved-main", repo.as_uri(), str(shallow)],
        check=True,
    )
    missing = subprocess.run(
        ["git", "-C", str(shallow), "cat-file", "-e", recorded_base], capture_output=True
    )
    assert missing.returncode != 0
    for workflow in ("ci", "integration", "publish-skills-test", "zizmor"):
        source = (WORKFLOWS / f"{workflow}.yml").read_text(encoding="utf-8")
        command = re.search(r"changed=\$\((git diff [^)]+)\)", source).group(1)
        args = [recorded_base if item == "$BUDGET_BASE" else item for item in shlex.split(command)]
        result = subprocess.run(
            [args[0], "-C", str(shallow), *args[1:]], text=True, capture_output=True
        )
        assert result.returncode == 0, (workflow, result.stderr)
        assert result.stdout.splitlines() == ["pyproject.toml"]


def test_missing_budget_reports_an_actionable_error(tmp_path):
    budgets = tmp_path / "budgets.json"
    budgets.write_text("{}")
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
            "new variant",
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
    assert "::error::" in result.stdout
    assert "ci/sample/new variant" in result.stdout
    assert "Traceback" not in result.stderr


def test_trusted_user_journey_reports_missing_budget(tmp_path):
    import os
    import textwrap

    source = (WORKFLOWS / "user-journey-test-required.yml").read_text(encoding="utf-8")
    check = source.split("      - name: Check this job's time budget\n", 1)[1]
    python = check.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    budget_dir = tmp_path / "budget-source" / ".github"
    budget_dir.mkdir(parents=True)
    (budget_dir / "ci-time-budgets.json").write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(python)],
        cwd=tmp_path,
        env={
            **os.environ,
            "BUDGET_JOB_STARTED": "100",
            "BUDGET_RUN_STARTED": "110",
            "BUDGET_ENDED": "130",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "::error::No time budget for user-journey-test-required" in result.stderr
    assert "Traceback" not in result.stderr


def test_trusted_gate_accounts_for_budget_checkout_in_setup():
    workflow = (WORKFLOWS / "user-journey-test-required.yml").read_text(encoding="utf-8")
    assert workflow.index("name: Read the PR's time budget") < workflow.index(
        "name: Mark setup done"
    )
    assert workflow.index("name: Mark setup done") < workflow.index(
        "name: Require meaningful user-journey coverage"
    )


def test_shards_share_one_budget_per_operating_system():
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))["ci"]
    for job in ("unit-linux", "unit-windows"):
        values = list(budgets[job].values())
        assert values
        assert all(value == values[0] for value in values), job


def test_enforced_shard_limits_have_a_sufficient_pooled_sample():
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    calibration = budgets["_unit_calibration"]
    assert len(set(calibration["run_ids"])) >= 20
    for job, os_name in (("unit-linux", "linux"), ("unit-windows", "windows")):
        data = calibration[os_name]
        assert data["samples"] >= 20
        for budget in budgets["ci"][job].values():
            for phase in ("setup", "run", "total"):
                p95 = data["p95"][phase]
                assert budget[phase] >= max(2 * p95, 1.15 * data["observed_max"][phase])


def test_windows_shard_budget_covers_recorded_runner_outlier():
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    data = budgets["_unit_calibration"]["windows"]
    recent = data["recent_outlier_calibration"]
    assert data["samples"] >= 4096
    assert data["p50"]["run"] == 17
    assert data["p95"]["run"] <= data["p99"]["run"]
    assert recent["phase_margin"]["run"] >= 8
    assert len(set(recent["run_ids"])) >= 3
    assert recent["samples"] == 256
    assert recent["outlier_run"] in recent["run_ids"]
    assert recent["outlier_job"] == 139031483052
    assert recent["run_p99"] == 32
    assert recent["run_max"] == data["observed_max"]["run"] == 57
    assert recent["run_margin"] >= 8
    assert recent["reason"]
    for budget in budgets["ci"]["unit-windows"].values():
        assert budget["setup"] >= 60 and budget["run"] >= 65 and budget["total"] >= 85
        assert budget["run"] - recent["run_max"] == recent["run_margin"]
        assert all(budget[phase] >= data["observed_max"][phase] for phase in budget)


def test_every_enforced_nonshard_budget_cites_samples_and_headroom():
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    calibration = budgets["_job_calibration"]
    for workflow, jobs in budgets.items():
        if workflow.startswith("_"):
            continue
        for job, variants in jobs.items():
            if workflow == "ci" and job in ("unit-linux", "unit-windows"):
                continue
            for name, budget in variants.items():
                entry = calibration["jobs"][f"{workflow}/{job}/{name}"]
                assert re.fullmatch(r"C\d+", entry["source_set"])
                assert 1 <= entry["distinct_runs"] <= entry["samples"]
                assert entry["limited_sample"] == (entry["samples"] < 20)
                for phase in ("setup", "run", "total"):
                    p95 = entry["p95"][phase]
                    assert budget[phase] >= max(2 * p95, 1.15 * entry["observed_max"][phase])

    gate = calibration["gate"]
    assert gate["samples"] >= 20
    assert re.fullmatch(r"C\d+", gate["source_set"])
    assert 20 <= gate["distinct_runs"] <= gate["samples"]
    source = (WORKFLOWS / "security-gate.yml").read_text(encoding="utf-8")
    limit = int(re.search(r'\[ "\$elapsed" -gt (\d+) \]', source).group(1))
    assert limit >= max(2 * gate["p95"], 1.15 * gate["observed_max"])


def test_all_budget_limits_cover_audited_phase_distributions():
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8"))
    for job, system in (("unit-linux", "linux"), ("unit-windows", "windows")):
        entry = budgets["_unit_calibration"][system]
        for budget in budgets["ci"][job].values():
            for phase in budget:
                assert (
                    0
                    <= entry["p50"][phase]
                    <= entry["p95"][phase]
                    <= entry["p99"][phase]
                    <= entry["observed_max"][phase]
                )
                assert budget[phase] >= 1.15 * entry["observed_max"][phase]
    for workflow, jobs in budgets.items():
        if workflow.startswith("_"):
            continue
        for job, variants in jobs.items():
            if workflow == "ci" and job in ("unit-linux", "unit-windows"):
                continue
            for name, budget in variants.items():
                entry = budgets["_job_calibration"]["jobs"][f"{workflow}/{job}/{name}"]
                assert entry["distinct_runs"] >= 3
                for phase in budget:
                    assert (
                        0
                        <= entry["p50"][phase]
                        <= entry["p95"][phase]
                        <= entry["p99"][phase]
                        <= entry["observed_max"][phase]
                    )
                    assert budget[phase] >= 1.15 * entry["observed_max"][phase]
    gate = budgets["_job_calibration"]["gate"]
    assert gate["scope"] == "successful PR gates"
    assert gate["excluded_failed"] == 20
    assert gate["p50"] <= gate["p95"] <= gate["p99"] <= gate["observed_max"]


def test_pi_windows_budget_covers_observed_successful_test_run():
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--budgets",
            str(BUDGETS),
            "--workflow",
            "ci",
            "--job",
            "e2e-shards",
            "--name",
            "Agent launch tests · Pi · Windows",
            "--started",
            "100",
            "--run-started",
            "321",
            "--ended",
            "607",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "setup: 221s /" in result.stdout
    assert "run: 286s /" in result.stdout
    assert "total: 507s /" in result.stdout


def test_artifact_audit_budget_allows_observed_download_latency():
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--budgets",
            str(BUDGETS),
            "--workflow",
            "ci",
            "--job",
            "unit-test-times",
            "--name",
            "Unit test times",
            "--started",
            "100",
            "--run-started",
            "232",
            "--ended",
            "234",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
