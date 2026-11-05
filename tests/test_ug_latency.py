"""Unit tests for the `/ug-latency` benchmark script and its comment-triggered workflow."""

import json
import os
import re
import stat
from pathlib import Path

import pytest

from scripts import ug_latency as lat

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
    assert (summary["n"], summary["median"], summary["min"], summary["max"]) == (5, 3.0, 1.0, 10.0)
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


def test_builds_alternate_who_runs_first():
    assert [lat.run_order(i)[0] for i in range(4)] == ["before", "after", "before", "after"]
    assert all(set(lat.run_order(i)) == {"before", "after"} for i in range(4))


def test_isolated_environment_scopes_home_and_puts_stubs_first(tmp_path):
    base = {"PATH": "/usr/bin", "DATABRICKS_BEARER": "leak", "ANTHROPIC_API_KEY": "leak"}
    env = lat.isolated_environment(tmp_path / "h", tmp_path / "stubs", base, "tok")

    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "stubs")
    assert env["HOME"] == str(tmp_path / "h")
    assert env["CODEX_HOME"] == str(tmp_path / "h" / ".codex")
    assert env["DATABRICKS_BEARER"] == "tok"
    assert "ANTHROPIC_API_KEY" not in env
    assert "DATABRICKS_BEARER" not in lat.isolated_environment(tmp_path, tmp_path, base, None)


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


def test_failure_comment_keeps_the_marker_so_it_replaces_earlier_results():
    text = lat.render_failure("https://github.com/o/r/actions/runs/2")

    assert text.startswith(lat.MARKER + "\n")
    assert "failed" in text and "actions/runs/2" in text


def test_duration_formatting():
    assert lat._fmt(0.1234) == "123 ms"
    assert lat._fmt(2.0) == "2.00 s"
    assert lat._fmt(None) == "n/a"
    assert lat._fmt_delta(-0.25) == "-250 ms"
    assert lat._fmt_delta(0.0) == "0 ms"


def _fake_ug(path: Path, record: Path, *, fail_on: str | None = None) -> str:
    path.write_text(
        "#!/bin/sh\n"
        f'echo "$HOME $*" >> "{record}"\n'
        f'case "$*" in {fail_on or "NEVER-MATCHES"}) echo boom >&2; exit 3;; esac\n'
        "sleep 0.01\nexit 0\n"
    )
    path.chmod(0o755)
    return str(path)


def test_measure_alternates_builds_isolates_homes_and_redacts(tmp_path):
    record = tmp_path / "calls.log"
    before = _fake_ug(tmp_path / "before-ug", record)
    after = _fake_ug(tmp_path / "after-ug", record, fail_on="codex")
    logs: list[str] = []

    data = lat.measure(
        binaries={"before": before, "after": after},
        workspace="https://w.example",
        commands=["configure-user", "claude", "codex"],
        iterations=2,
        warmup=1,
        work_dir=tmp_path / "work",
        bearer="s3cret",
        reset_global=False,
        log=logs.append,
    )

    by_id = {r["id"]: r for r in data["results"]}
    assert [r["id"] for r in data["results"]] == ["configure-user", "claude", "codex"]
    assert by_id["configure-user"]["before"]["ok"] == by_id["configure-user"]["after"]["ok"] == 2
    assert by_id["claude"]["comparison"] is not None
    assert by_id["codex"]["after"]["ok"] == 0
    assert by_id["codex"]["after"]["failures"] == 2
    assert by_id["codex"]["before"]["ok"] == 2
    assert "s3cret" not in json.dumps(data) + "\n".join(logs)
    homes = [line.split(" ", 1)[0] for line in record.read_text().splitlines()]
    configure_homes = [
        h
        for h, line in zip(homes, record.read_text().splitlines(), strict=True)
        if "--workspace" in line and "--user" in line
    ]
    # Every timed configure run uses its own fresh HOME (launch setup runs reuse theirs).
    timed = [h for h in configure_homes if "configure-user" in h]
    assert len(timed) == len(set(timed)) == 6
    assert not (tmp_path / "work" / "configure-user" / "before" / "run-0").exists()


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
    assert "failed" in (tmp_path / "f.md").read_text()


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
