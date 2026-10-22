"""Terminal Bench helpers and the GPT-6 lanes of the Terminal Bench workflow."""

import json
import re
import sys
from argparse import Namespace
from pathlib import Path

import pytest

BENCH = Path(__file__).parent.parent / "bench/terminal_bench"
WORKFLOW = Path(__file__).parent.parent / ".github/workflows/terminal-bench.yml"
GPT6 = "system.ai.gpt-6-luna"

sys.path.insert(0, str(BENCH))
import run_native  # noqa: E402
import summarize_harbor  # noqa: E402
from stream_models import models_in_log  # noqa: E402


def _stream_json(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_models_in_log_reads_claude_stream_json_and_skips_other_lines(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(
        "ug banner\n"
        + _stream_json(
            {"type": "system", "subtype": "init", "model": GPT6},
            {"type": "assistant", "message": {"model": GPT6, "content": []}},
            {"type": "assistant", "message": {"model": "<synthetic>"}},
            {"type": "assistant", "message": {"model": "other-model"}},
            {"type": "user", "model": "ignored", "message": {"model": "ignored"}},
        )
        + "{not json\n[1, 2]\n"
    )

    assert models_in_log(log) == [GPT6, "other-model"]
    assert models_in_log(tmp_path / "missing.log") == []


def test_models_in_log_is_empty_for_codex_json(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(_stream_json({"type": "thread.started", "thread_id": "t"}))

    assert models_in_log(log) == []


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_launch_pins_the_model_before_the_separator(agent, tmp_path):
    argv, _ = run_native.agent_command(agent, "ug", GPT6, tmp_path, "do it", resume=False)

    assert argv[:5] == ["ug", agent, "--model", GPT6, "--"]
    unpinned, _ = run_native.agent_command(agent, "ug", None, tmp_path, "do it", resume=False)
    assert "--model" not in unpinned


def test_native_summary_names_the_pinned_model_and_the_models_that_answered(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    result = {
        "task": "log-triage",
        "attempt": 1,
        "passed": True,
        "agent_exit": 0,
        "timed_out": False,
        "duration_sec": 1.5,
        "models": [GPT6],
    }
    args = Namespace(agent="claude", model=GPT6, min_passes=1, attempts=1)

    run_native.write_summary(args, [result], tmp_path)

    summary = capsys.readouterr().out
    assert f"### Native tasks · claude · {GPT6} · " in summary
    assert f"| log-triage | 1/1 | pass | 0 | 1.5 | {GPT6} |" in summary


def test_harbor_summary_lists_the_models_in_each_trials_agent_log(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    trial = tmp_path / "job" / "fix-git__abc"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"task_name": "fix-git", "verifier_result": {"rewards": {"reward": 1}}})
    )
    (trial / "agent" / "ug-claude.txt").write_text(
        _stream_json({"type": "system", "subtype": "init", "model": GPT6})
    )

    status = summarize_harbor.main(tmp_path, "TB2 · claude-gpt-6", 1, False, 3)

    assert status == 0
    assert f"| fix-git | 1/1 | 1 | - | {GPT6} |" in capsys.readouterr().out


def _job(workflow: str, name: str) -> str:
    return re.split(rf"(?m)^  {name}:\n", workflow, maxsplit=1)[1].split("\n\n", 1)[0]


@pytest.mark.parametrize("job", ["harbor", "native"])
def test_workflow_runs_each_agent_on_the_default_model_and_on_gpt6(job):
    body = _job(WORKFLOW.read_text(), job)

    assert "agent: [claude, codex]" in body
    assert f"model: ['', {GPT6}]" in body
    # GPT-6 lanes ignore the dispatch input; the default lanes keep honoring it.
    assert "MODEL: ${{ matrix.model || inputs.model }}" in body
    assert "LANE: ${{ matrix.agent }}${{ matrix.model && '-gpt-6' || '' }}" in body
    assert "${{ matrix.model && ' · GPT-6' || '' }}" in body.split("runs-on:", 1)[0]
    assert "inputs.model" not in WORKFLOW.read_text().split("jobs:", 1)[0].split("env:", 1)[1]


def test_workflow_artifact_names_differ_per_model_and_default_names_are_unchanged():
    workflow = WORKFLOW.read_text()

    assert "name: terminal-bench-harbor-${{ env.LANE }}\n" in workflow
    assert "name: terminal-bench-native-${{ env.LANE }}-${{ matrix.os }}\n" in workflow
    # The summary titles carry the lane too, so the GPT-6 tables are told apart.
    assert '"TB2 · $LANE"' in workflow
    assert '"ug tasks in Harbor · $LANE"' in workflow
