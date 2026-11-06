"""Unit tests for the `/ug-latency` benchmark script and its comment-triggered workflow."""

import json
import os
import re
import stat
from pathlib import Path

import pytest

from scripts import ug_latency as lat

posix_only = pytest.mark.skipif(os.name == "nt", reason="runs POSIX shell scripts")
ROOT = Path(__file__).parent.parent
WORKFLOW = (ROOT / ".github/workflows/ug-latency.yml").read_text()
MEMBER_ONLY = 'contains(fromJSON(\'["MEMBER", "OWNER"]\'), github.event.comment.author_association)'


def test_percentile_interpolates_and_handles_single_values():
    assert lat.percentile([5.0], 90) == 5.0
    assert lat.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert lat.percentile([0.0, 10.0], 90) == pytest.approx(9.0)
    assert lat.percentile([3.0, 1.0, 2.0], 100) == 3.0
    with pytest.raises(ValueError):
        lat.percentile([], 50)


def test_summarize_reports_median_min_p90_max():
    summary = lat.summarize([4.0, 1.0, 2.0, 3.0, 10.0])

    assert summary is not None
    assert (summary["median"], summary["min"], summary["max"]) == (3.0, 1.0, 10.0)
    assert summary["p90"] == pytest.approx(7.6)
    assert lat.summarize([]) is None


@pytest.mark.parametrize(
    ("before", "after", "verdict"),
    [
        (1.0, 1.02, "within noise"),  # under 50 ms
        (10.0, 10.3, "within noise"),  # over 50 ms but under 5%
        (1.0, 1.5, "slower"),
        (1.0, 0.5, "faster"),
    ],
)
def test_compare_flags_only_changes_beyond_noise(before, after, verdict):
    result = lat.compare({"median": before}, {"median": after})

    assert result is not None
    assert result["verdict"] == verdict
    assert result["delta"] == pytest.approx(after - before)
    assert result["percent"] == pytest.approx((after - before) / before * 100)


def test_compare_needs_both_sides():
    assert lat.compare(None, {"median": 1.0}) is None
    assert lat.compare({"median": 1.0}, None) is None


def test_iteration_and_command_parsing():
    assert lat.parse_iterations("5") == 5
    assert lat.parse_iterations(lat.MAX_ITERATIONS) == lat.MAX_ITERATIONS
    for bad in ("0", "16", "-1"):
        with pytest.raises(ValueError):
            lat.parse_iterations(bad)
    with pytest.raises(ValueError):
        lat.parse_iterations("many")
    assert lat.parse_command_list("codex, configure-user,claude") == [
        "configure-user",
        "claude",
        "codex",
    ]
    for bad in ("", "configure,bogus"):
        with pytest.raises(ValueError):
            lat.parse_command_list(bad)


def test_every_requested_command_has_a_distinct_invocation():
    argvs = {
        name: lat.build_argv("/x/ug", lat.SPECS[name], "https://w.example")
        for name in lat.DISPLAY_ORDER
    }

    assert argvs["configure"][-2:] == ["--workspace", "https://w.example"]
    assert argvs["configure-user"][-1] == "--user"
    assert argvs["configure-global"][-1] == "--global"
    assert "--user" not in argvs["configure"] and "--global" not in argvs["configure"]
    assert argvs["claude"] == ["/x/ug", "claude"]
    assert argvs["codex"] == ["/x/ug", "codex"]
    assert lat.setup_argv("/x/ug", "https://w.example")[-1] == "--user"
    assert set(lat.EXECUTION_ORDER) == set(lat.DISPLAY_ORDER)
    assert lat.EXECUTION_ORDER[-2:] == ("configure", "configure-global")


def test_isolated_environment_scopes_home_and_puts_stubs_first(tmp_path):
    base = {"PATH": "/usr/bin", "DATABRICKS_BEARER": "leak", "ANTHROPIC_API_KEY": "leak"}
    env = lat.isolated_environment(
        tmp_path / "h", tmp_path / "stubs", base, "tok", bin_dir=tmp_path / "venv" / "bin"
    )

    # Stubs first, then the build's own bin, so `ug` on PATH is the build under test.
    assert env["PATH"].split(os.pathsep)[:3] == [
        str(tmp_path / "stubs"),
        str(tmp_path / "venv" / "bin"),
        "/usr/bin",
    ]
    assert env["HOME"] == str(tmp_path / "h")
    assert env["CODEX_HOME"] == str(tmp_path / "h" / ".codex")
    assert env["DATABRICKS_BEARER"] == "tok"
    assert "ANTHROPIC_API_KEY" not in env
    assert "DATABRICKS_BEARER" not in lat.isolated_environment(tmp_path, tmp_path, base, None)


@posix_only
def test_stubs_exit_zero_and_report_a_version_ug_accepts(tmp_path):
    import subprocess

    lat.write_stubs(tmp_path)
    for name in ("claude", "codex"):
        path = tmp_path / name
        assert path.stat().st_mode & stat.S_IXUSR
        version = subprocess.run([str(path), "--version"], capture_output=True, text=True)
        other = subprocess.run([str(path), "--anything"], capture_output=True, text=True)
        assert version.returncode == other.returncode == 0
        assert lat.AGENT_STUB_VERSION in version.stdout
        assert other.stdout == ""


def _side(samples, failures=0):
    return {
        "samples": samples,
        "ok": len(samples),
        "failures": failures,
        "summary": lat.summarize(samples),
    }


def _results():
    return {
        "meta": {
            "iterations": 5,
            "warmup": 1,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        },
        "results": [
            {
                "id": "claude",
                "status": "ok",
                "before": _side([1.0, 1.0, 1.0]),
                "after": _side([1.5, 1.5, 1.5], failures=2),
            },
            {
                "id": "configure",
                "status": "ok",
                "before": _side([2.0, 2.0]),
                "after": _side([2.0, 2.01]),
            },
            {"id": "configure-global", "status": "skipped"},
        ],
    }


def test_markdown_has_marker_table_and_every_stat():
    text = lat.render_markdown(_results(), "https://github.com/o/r/actions/runs/1")

    assert text.startswith(lat.MARKER + "\n")
    assert "`" + "a" * 12 + "`" in text and "`" + "b" * 12 + "`" in text
    claude = next(line for line in text.splitlines() if line.startswith("| `ug claude`"))
    assert "1.00 s" in claude and "1.50 s" in claude
    assert "+500 ms" in claude and "+50.0%" in claude and "slower" in claude
    assert "| 3 / 3 |" in claude
    configure = next(line for line in text.splitlines() if line.startswith("| `ug configure`"))
    assert "within noise" not in configure and "**" not in configure
    assert "ug configure --global` | skipped" in text
    assert "0 failed run(s) before, 2 after" in text
    assert "[Workflow run](https://github.com/o/r/actions/runs/1)" in text
    # Rows follow the requested order, absent commands are omitted.
    assert text.index("`ug configure`") < text.index("`ug claude`")
    assert "ug codex" not in text.split("---|", 1)[1]


def test_markdown_treats_the_result_file_as_untrusted():
    evil = {
        "meta": {"base_sha": "x\n<script>", "head_sha": "c" * 40, "iterations": "<b>"},
        "results": [
            {
                "id": "claude",
                "status": "ok",
                "label": "[evil](http://x)",
                "before": {
                    "summary": {"median": "<img src=x>", "min": float("nan")},
                    "last_error": "@everyone",
                },
                "after": {"summary": {"median": -3, "p90": float("inf")}},
            },
            "not a dict",
            {"id": "../../etc", "status": "ok"},
        ],
    }
    text = lat.render_markdown(evil, "javascript:alert(1)")

    for forbidden in ("<script>", "<img", "evil", "@everyone", "javascript:", "inf", "nan"):
        assert forbidden not in text
    assert "unknown" in text
    assert "n/a" in text
    assert lat.render_markdown({}, None).startswith(lat.MARKER)


def test_duration_formatting():
    assert lat._fmt(0.1234) == "123 ms"
    assert lat._fmt(2.0) == "2.00 s"
    assert lat._fmt(None) == "n/a"
    assert lat._fmt_delta(-0.25) == "-250 ms"
    assert lat._fmt_delta(0.0) == "0 ms"


def _fake_ug(path: Path, record: Path, *, fail_on: str | None = None) -> str:
    path.write_text(
        "#!/bin/sh\n"
        f'echo "$(basename "$0") $HOME $*" >> "{record}"\n'
        f'case "$*" in {fail_on or "NEVER-MATCHES"}) '
        'echo "boom $DATABRICKS_BEARER https://w.example" >&2; exit 3;; esac\n'
        "sleep 0.01\nexit 0\n"
    )
    path.chmod(0o755)
    return str(path)


@posix_only
def test_measure_alternates_builds_isolates_homes_and_redacts(tmp_path):
    record = tmp_path / "calls.log"
    before = _fake_ug(tmp_path / "before-ug", record)
    after = _fake_ug(tmp_path / "after-ug", record, fail_on="codex")
    logs: list[str] = []
    checkpoints: list[list[str]] = []

    data = lat.measure(
        binaries={"before": before, "after": after},
        workspace="https://w.example",
        commands=["configure-user", "claude", "codex"],
        iterations=2,
        warmup=1,
        work_dir=tmp_path / "work",
        bearer="s3cret",
        reset_global=False,
        checkpoint=lambda partial: checkpoints.append([r["id"] for r in partial["results"]]),
        log=logs.append,
    )

    by_id = {r["id"]: r for r in data["results"]}
    assert [r["id"] for r in data["results"]] == ["configure-user", "claude", "codex"]
    assert by_id["configure-user"]["before"]["ok"] == by_id["configure-user"]["after"]["ok"] == 2
    assert by_id["codex"]["after"]["ok"] == 0
    assert by_id["codex"]["after"]["failures"] == 2
    assert by_id["codex"]["before"]["ok"] == 2
    # The failing build printed the bearer and workspace; neither reaches the logs or the JSON.
    assert "<redacted>" in "\n".join(logs)
    for secret in ("s3cret", "https://w.example"):
        assert secret not in json.dumps(data) + "\n".join(logs)
    # Results are saved after every command, so a later hang keeps the earlier timings.
    assert checkpoints[0] == [] and ["configure-user"] in checkpoints
    assert checkpoints[-1] == ["configure-user", "claude", "codex"]
    lines = [line.split(" ", 2) for line in record.read_text().splitlines()]
    configure = [
        (who, home) for who, home, rest in lines if "--workspace" in rest and "--user" in rest
    ]
    timed = configure[:6]  # configure-user runs first: 1 warmup + 2 timed per build
    # The build that goes first alternates (before, after, then after, before ...).
    assert [who for who, _ in timed] == [
        "before-ug",
        "after-ug",
        "after-ug",
        "before-ug",
        "before-ug",
        "after-ug",
    ]
    # Every configure run gets its own fresh HOME.
    assert len({home for _, home in timed}) == 6
    assert not (tmp_path / "work" / "configure-user" / "before" / "run-0").exists()


@posix_only
def test_failed_launch_setup_is_counted_and_explained(tmp_path):
    record = tmp_path / "calls.log"
    good = _fake_ug(tmp_path / "good", record)
    bad = _fake_ug(tmp_path / "bad", record, fail_on="configure*")

    data = lat.measure(
        binaries={"before": good, "after": bad},
        workspace="https://w.example",
        commands=["claude"],
        iterations=2,
        warmup=1,
        work_dir=tmp_path / "work",
        bearer=None,
        reset_global=False,
        log=lambda message: None,
    )

    after = data["results"][0]["after"]
    assert (after["ok"], after["failures"], after["setup_failed"]) == (0, 2, True)
    assert data["results"][0]["before"]["setup_failed"] is False
    assert "bad" not in {line.split(" ")[0] for line in record.read_text().splitlines()[2:]}
    text = lat.render_markdown(data)
    assert "setup `ug configure --user` failed on after" in text


@posix_only
def test_main_exit_status_and_json_output(tmp_path, monkeypatch):
    record = tmp_path / "calls.log"
    good = _fake_ug(tmp_path / "good", record)
    bad = _fake_ug(tmp_path / "bad", record, fail_on="claude")
    out = tmp_path / "out.json"
    common = [
        "measure",
        "--workspace",
        "https://w.example/",
        "--commands",
        "claude",
        "--iterations",
        "1",
        "--warmup",
        "0",
        "--base-sha",
        "a" * 40,
        "--head-sha",
        "b" * 40,
        "--output",
        str(out),
    ]
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)

    assert lat.main([*common, "--before", good, "--after", good]) == 0
    saved = json.loads(out.read_text())
    assert saved["meta"]["head_sha"] == "b" * 40 and saved["results"][0]["before"]["ok"] == 1
    assert lat.main([*common, "--before", good, "--after", bad]) == 1
    assert (
        lat.main(
            [*common[:3], "--commands", "nope", *common[5:], "--before", good, "--after", good]
        )
        == 2
    )
    with pytest.raises(SystemExit):
        lat.main([*common, "--before", str(tmp_path / "missing"), "--after", good])
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert lat.main([*common, "--before", good, "--after", good, "--reset-global"]) == 2


def test_render_subcommand_writes_markdown_and_failure_comment(tmp_path):
    results = tmp_path / "r.json"
    results.write_text(json.dumps(_results()))

    assert lat.main(["render", str(results), "--output", str(tmp_path / "c.md")]) == 0
    assert (tmp_path / "c.md").read_text().startswith(lat.MARKER)
    assert (
        lat.main(
            ["render", str(tmp_path / "none"), "--failure", "--output", str(tmp_path / "f.md")]
        )
        == 0
    )
    failure = (tmp_path / "f.md").read_text()
    # The marker is what makes a later run replace an earlier result with this failure.
    assert failure.startswith(lat.MARKER + "\n") and "failed" in failure


# --- workflow contract -------------------------------------------------------------------------


def _job(name: str) -> str:
    rest = WORKFLOW.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z]+:\n", rest, maxsplit=1)[0]


def test_workflow_only_runs_for_authorized_comments_from_the_default_branch():
    authorize, reject = _job("authorize"), _job("reject")

    assert re.search(r"(?m)^on:\n  issue_comment:\n    types: \[created\]\n", WORKFLOW)
    assert "pull_request_target" not in WORKFLOW
    command = "startsWith(github.event.comment.body, '/ug-latency ')"
    assert command in authorize and command in reject
    assert (
        "github.event.issue.pull_request" in authorize
        and "github.event.issue.pull_request" in reject
    )
    assert MEMBER_ONLY in authorize
    assert f"!({MEMBER_ONLY} ||" in reject
    for job in (authorize, reject):
        assert "github.event.comment.author_association == 'CONTRIBUTOR'" in job
        assert "github.event.comment.user.id == github.event.issue.user.id" in job
    assert "set -euo pipefail" in authorize
    assert '[[ "$head_sha" =~ ^[0-9a-f]{40}$ ]]' in authorize
    assert '[[ "$base_sha" =~ ^[0-9a-f]{40}$ ]]' in authorize
    assert "merge_base_commit.sha" in authorize
    assert "runs-on: ubuntu-latest" not in WORKFLOW
    assert not re.search(r"(?m)^\s*[^#\n]*\bjq\s+-", WORKFLOW)


@posix_only
@pytest.mark.parametrize(
    ("comment", "iterations"),
    [
        ("/ug-latency", "5"),
        ("/ug-latency 1", "1"),
        ("/ug-latency 15", "15"),
        ("/ug-latency   7  ", "7"),
        ("/ug-latency 0", None),
        ("/ug-latency 16", None),
        ("/ug-latency 100", None),
        ("/ug-latency many", None),
        ("/ug-latency 3 extra", None),
    ],
)
def test_workflow_command_parser_runs_with_a_fake_gh(tmp_path, comment, iterations):
    import subprocess
    import textwrap

    step = _job("authorize").split("Pin the base and head commits", 1)[1]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh.log"
    (bin_dir / "gh").write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in\n'
        f"  *compare/*) echo {'b' * 40};;\n"
        f'  api\\ repos/*/pulls/*) printf "{"a" * 40}\\tmain\\towner/repo\\n";;\n'
        "esac\n"
    )
    (bin_dir / "gh").chmod(0o755)
    output = tmp_path / "output"
    output.touch()
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_OUTPUT": str(output),
            "PR_NUMBER": "7",
            "COMMENT_ID": "9",
            "COMMENT_BODY": comment,
            "GH_TOKEN": "x",
        },
        capture_output=True,
        text=True,
    )

    if iterations is None:
        assert result.returncode == 1 and "Latency run not started" in calls.read_text()
        assert output.read_text() == ""
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text().splitlines() == [
            f"head_sha={'a' * 40}",
            f"base_sha={'b' * 40}",
            f"iterations={iterations}",
        ]


def test_workflow_measure_job_runs_trusted_script_on_pinned_commits_without_write_access():
    measure = _job("measure")

    assert "needs: authorize" in measure
    assert "group: ug-latency-${{ github.event.issue.number }}" in measure
    assert "ref: ${{ github.event.repository.default_branch }}" in measure
    assert "ref: ${{ needs.authorize.outputs.base_sha }}" in measure
    assert "ref: ${{ needs.authorize.outputs.head_sha }}" in measure
    checkouts = re.findall(r"uses: actions/checkout@.*\n((?:        .*\n)*)", measure)
    assert len(checkouts) == 3
    assert all("persist-credentials: false" in block for block in checkouts)
    assert "pull-requests: write" not in measure and "contents: read" in measure
    assert "trusted/scripts/ug_latency.py" in measure
    assert "head/scripts" not in measure and "base/scripts" not in measure
    # The workspace credential reaches only the measurement step, never the build step.
    install, timed = measure.split("Time the commands on both builds", 1)
    assert "secrets." not in install
    assert "secrets.UCODE_TEST_WORKSPACE" in timed and "secrets.DATABRICKS_BEARER" in timed
    assert "--reset-global" in timed
    assert "enable-cache: false" in measure


def test_workflow_report_updates_one_marked_comment_with_the_trusted_renderer():
    report = _job("report")

    assert "needs.authorize.result == 'success'" in report
    assert "ref: ${{ github.event.repository.default_branch }}" in report
    assert "pull-requests: write" in report
    assert lat.MARKER in report
    assert "-X PATCH" in report and "-X POST" in report
    assert 'select(.user.login == \\"github-actions[bot]\\"' in report
    assert "scripts/ug_latency.py render" in report
    # Reports are serialized, skip cancelled measurements, and skip when the PR head moved on.
    assert "group: ug-latency-report-${{ github.event.issue.number }}" in report
    assert "cancel-in-progress: false" in report
    assert "needs.measure.result != 'cancelled'" in report
    assert '"$current" != "$HEAD_SHA"' in report
    assert "ug_latency.py measure" not in report


def test_workflow_actions_are_pinned_to_shas_and_use_existing_secret_names():
    assert not re.findall(r"uses: [^\n]+@(?![0-9a-f]{40}\b)", WORKFLOW)
    assert set(re.findall(r"secrets\.(\w+)", WORKFLOW)) == {
        "UCODE_TEST_WORKSPACE",
        "DATABRICKS_BEARER",
    }
    integration = (ROOT / ".github/workflows/integration.yml").read_text()
    for name in ("UCODE_TEST_WORKSPACE", "DATABRICKS_BEARER"):
        assert f"secrets.{name}" in integration
