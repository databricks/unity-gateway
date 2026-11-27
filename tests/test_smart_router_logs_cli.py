"""Tests for the `ug smart-router-logs` JSON reader."""

import hashlib
import json
import uuid

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.smart_routing import routing, session_log

runner = CliRunner()


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    monkeypatch.setattr(session_log, "LOG_DIR", directory)
    return directory


def decision(model="selected"):
    return routing.RoutingDecision(model, "reason", ((model, "claude"), ("other", None)), "stable")


def write(payload, task="inspect parser"):
    session_log.append_decision("claude", payload, task, decision(), "requested", routing_ms=840)


def payload(session_id="s1", **extra):
    base = {"session_id": session_id, "tool_use_id": "tool1", "tool_input": {}}
    base.update(extra)
    return base


def read_overviews(session="s1"):
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", session])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["decisions"]


def test_overview_shape_and_order(log_dir):
    write(payload(), task="first task")
    write(payload(), task="second task")
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1"])
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["v"] == 1
    overviews = document["decisions"]
    assert [o["preview"] for o in overviews] == ["first task", "second task"]
    assert set(overviews[0]) == {
        "decision_id",
        "at",
        "kind",
        "preview",
        "selected_model",
        "routing_ms",
    }
    assert overviews[0]["kind"] == "subagent"
    assert overviews[0]["selected_model"] == "selected"
    assert overviews[0]["routing_ms"] == 840
    assert uuid.UUID(hex=overviews[0]["decision_id"]).version == 4
    assert overviews[0]["at"] <= overviews[1]["at"]


def test_empty_task_has_null_preview(log_dir):
    write(payload(), task="")
    (overview,) = read_overviews()
    assert overview["preview"] is None


def test_malformed_foreign_and_unknown_type_lines_skipped(log_dir):
    path = log_dir / "claude-s1.jsonl"
    log_dir.mkdir()
    write(payload(), task="kept")
    good = path.read_text()
    path.write_text(
        "not json\n"
        + "[" * 10_000
        + "]" * 10_000
        + "\n"
        + json.dumps({"v": 1})
        + "\n"
        + json.dumps({"v": 2, "type": "decision", "decision_id": "newer"})
        + "\n"
        + json.dumps({"v": 1, "type": "other", "decision_id": "future"})
        + "\n"
        + json.dumps([1, 2, 3])
        + "\n"
        + "\n"
        + good
    )
    overviews = read_overviews()
    assert len(overviews) == 1
    assert overviews[0]["preview"] == "kept"


def test_missing_session_returns_empty_list(log_dir):
    assert read_overviews(session="absent") == []


def test_unreadable_log_returns_error_envelope(log_dir):
    """A log that exists but cannot be read must not be reported as empty history."""
    (log_dir / "claude-s1.jsonl").mkdir(parents=True)
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1"])
    assert result.exit_code == 1
    document = json.loads(result.output)
    assert document["v"] == 1
    assert "cannot read session log at" in document["error"]
    assert "claude-s1.jsonl" in document["error"]


def test_decision_hit_returns_full_record(log_dir):
    write(payload())
    record = json.loads((log_dir / "claude-s1.jsonl").read_text())
    result = runner.invoke(
        cli.app, ["smart-router-logs", "--session", "s1", "--decision", record["decision_id"]]
    )
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["v"] == 1
    assert document["decision"] == record
    assert record["routing_ms"] == 840
    assert record["requested_model"] == "requested"
    assert record["prompt"] == {
        "preview": "inspect parser",
        "sha256": hashlib.sha256(b"inspect parser").hexdigest(),
        "length": len("inspect parser"),
    }


def test_decision_miss_exits_1(log_dir):
    write(payload())
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1", "--decision", "nope"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"v": 1, "error": "decision not found"}


def lines_of(output):
    """Output lines with the box borders removed; every box line must be 77 characters."""
    lines = []
    for line in output.splitlines():
        if line.startswith(("┌", "└")):
            assert len(line) == 77
            continue
        if line.startswith("│ "):
            assert len(line) == 77 and line.endswith(" │")
            line = line[2:-2].rstrip()
        lines.append(line)
    return lines


def write_codex(session_id="c1", task="inspect parser", model="system.ai.databricks-gpt-5-6-sol"):
    session_log.append_decision(
        "codex", payload(session_id), task, decision(model), "requested", routing_ms=1234
    )


def table(*args):
    return runner.invoke(
        cli.app,
        ["smart-router-logs", "--session", "c1", "--harness", "codex", "--format", "table", *args],
    )


def test_harness_defaults_to_claude(log_dir):
    write_codex()
    assert read_overviews(session="c1") == []


def test_codex_harness_reads_codex_log_as_json(log_dir):
    write_codex()
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "c1", "--harness", "codex"])
    assert result.exit_code == 0
    assert [o["preview"] for o in json.loads(result.output)["decisions"]] == ["inspect parser"]


def test_table_shows_header_and_row_with_stripped_model_prefix(log_dir):
    write_codex()
    result = table()
    assert result.exit_code == 0
    lines = lines_of(result.output)
    assert lines[0] == "1 routed subagents this session (showing last 1)"
    assert lines[1] == ""
    assert lines[2].split() == ["ID", "WHEN", "TASK", "MODEL", "ROUTING"]
    assert lines[-1] == "Show one: $smart-router logs --id <ID>"
    assert lines[3].split(maxsplit=3)[1:3] == ["just", "now"]
    assert "inspect parser" in lines[3]
    assert "gpt-5-6-sol" in lines[3]
    assert "system.ai" not in lines[3]
    assert "1234 ms" in lines[3]


def test_table_caps_rows_and_truncates_task(log_dir):
    for index in range(25):
        write_codex(task=f"task {index} " + "x" * 200)
    result = table()
    lines = lines_of(result.output)
    assert lines[0] == "25 routed subagents this session (showing last 20)"
    assert len(lines) == 3 + 20 + 1
    assert "task 5 " in lines[3]
    assert "task 4 " not in result.output
    assert all("…" in line for line in lines[3:23])


def test_table_empty_message(log_dir):
    result = table()
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "┌" + "─" * 75 + "┐",
        "│ No routed subagents recorded this session yet." + " " * 27 + " │",
        "└" + "─" * 75 + "┘",
    ]


def test_table_error_is_one_plain_line(log_dir):
    (log_dir / "codex-c1.jsonl").mkdir(parents=True)
    result = table()
    assert result.exit_code == 1
    assert result.output.startswith("error: cannot read session log at ")
    assert len(result.output.splitlines()) == 1


def test_unknown_harness_rejected(log_dir):
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1", "--harness", "gemini"])
    assert result.exit_code == 1
    assert "unknown harness" in json.loads(result.output)["error"]


def test_table_has_id_column_and_hint(log_dir):
    write_codex()
    decision_id = json.loads((log_dir / "codex-c1.jsonl").read_text())["decision_id"]
    lines = lines_of(table().output)
    assert lines[3].split()[0] == decision_id[:8]
    assert lines[-1] == "Show one: $smart-router logs --id <ID>"


def detail(query):
    return table("--decision", query)


def test_detail_renders_all_fields_and_marks_selected(log_dir):
    session_log.append_decision(
        "codex",
        payload("c1", tool_input={"subagent_type": "explorer"}),
        "inspect parser",
        routing.RoutingDecision(
            "system.ai.databricks-gpt-5-6-sol",
            "Needs depth. " * 20,
            (("system.ai.databricks-gpt-5-6-sol", "codex"), ("system.ai.gpt-5-6-luna", "codex")),
            "stable",
        ),
        "requested",
        routing_ms=1234,
    )
    record = json.loads((log_dir / "codex-c1.jsonl").read_text())
    result = detail(record["decision_id"])
    assert result.exit_code == 0
    lines = lines_of(result.output)
    assert lines[0] == f"Routing decision {record['decision_id'][:8]} · just now"
    assert lines[1] == ""
    assert lines[2] == "Prompt preview:  inspect parser"
    assert lines[3] == "Agent type:      explorer"
    assert lines[4] == "Selected:        gpt-5-6-sol"
    assert lines[5] == "Routing time:    1.2s"
    assert lines[6].startswith("Rationale:       Needs depth. Needs depth.")
    assert all(line.startswith(" " * 17) for line in lines[7:9])
    assert lines[-5:] == [
        "",
        "Candidates",
        "  ● gpt-5-6-sol  (selected)",
        "  ○ gpt-5-6-luna",
        "Back to the list: $smart-router logs",
    ]


def test_detail_handles_missing_rationale_candidates_and_route_agent(log_dir):
    log_dir.mkdir()
    record = {
        "v": 1,
        "type": "decision",
        "decision_id": "abcd1234",
        "prompt": None,
        "original": {"subagent_type": "ug-smart-router:ucode-route-x"},
        "selected_model": "m",
        "routing_ms": 50,
        "rationale": "  ",
        "candidates": [],
    }
    (log_dir / "codex-c1.jsonl").write_text(json.dumps(record) + "\n")
    output = "\n".join(lines_of(detail("abcd1234").output))
    assert "Prompt preview:  (prompt not recorded)" in output
    assert "Agent type" not in output
    assert "Routing time:    0.1s" in output
    assert "Rationale:       No rationale returned." in output
    assert "Candidates" not in output
    assert output.endswith("\nBack to the list: $smart-router logs")


def valid_record(decision_id, **overrides):
    return {
        "v": 1,
        "type": "decision",
        "decision_id": decision_id,
        "at": 1.0,
        "prompt": {"preview": "p"},
        "original": {"subagent_type": None},
        "selected_model": "m",
        "routing_ms": 5,
        "rationale": None,
        "candidates": [],
        **overrides,
    }


def test_detail_rejects_record_the_claude_view_rejects(log_dir):
    log_dir.mkdir()
    bad = valid_record("abcd1234", prompt={"preview": 7})
    (log_dir / "codex-c1.jsonl").write_text(json.dumps(bad) + "\n")
    result = detail("abcd1234")
    assert result.exit_code == 1
    assert result.output == "error: Could not load this routing decision.\n"


def test_detail_prefix_matching(log_dir):
    log_dir.mkdir()
    ids = ["abcd1111" + "0" * 24, "abcd2222" + "0" * 24, "ffff3333" + "0" * 24]
    (log_dir / "codex-c1.jsonl").write_text(
        "".join(json.dumps(valid_record(i)) + "\n" for i in ids)
    )
    assert detail(ids[0]).exit_code == 0
    assert detail("ffff").exit_code == 0
    ambiguous = detail("abcd")
    assert ambiguous.exit_code == 1 and "ambiguous" in ambiguous.output
    short = detail("ab")
    assert short.exit_code == 1 and "at least 4" in short.output
    missing = detail("eeee")
    assert missing.exit_code == 1 and missing.output == "error: decision not found\n"
    assert len(missing.output.splitlines()) == 1


def test_json_decision_still_requires_exact_id(log_dir):
    write_codex()
    decision_id = json.loads((log_dir / "codex-c1.jsonl").read_text())["decision_id"]
    result = runner.invoke(
        cli.app,
        [
            "smart-router-logs",
            "--session",
            "c1",
            "--harness",
            "codex",
            "--decision",
            decision_id[:8],
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.output) == {"v": 1, "error": "decision not found"}


def test_table_lists_the_same_rows_as_the_claude_pane(log_dir):
    log_dir.mkdir()
    records = [
        valid_record("aaaa0001", prompt={"preview": "kept"}),
        valid_record("aaaa0002", prompt={"preview": 7}),
        valid_record("aaaa0003", at="soon"),
        valid_record("aaaa0004", routing_ms=None),
        valid_record("aaaa0005", selected_model=None),
        valid_record("", prompt={"preview": "no id"}),
        valid_record("aaaa0007", prompt={"preview": None}),
        valid_record("aaaa0008", prompt={"preview": "  "}),
    ]
    (log_dir / "codex-c1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    lines = lines_of(table().output)
    assert lines[0] == "3 routed subagents this session (showing last 3)"
    assert [line.split()[0] for line in lines[3:6]] == ["aaaa0001", "aaaa0007", "aaaa0008"]
    assert lines[4].count("(prompt not recorded)") == 1
    assert "(prompt not recorded)" not in lines[5]  # whitespace is truthy, as in the Claude pane


def test_detail_shows_placeholder_for_non_string_or_missing_preview(log_dir):
    log_dir.mkdir()
    (log_dir / "codex-c1.jsonl").write_text(
        json.dumps(valid_record("abcd1234", prompt={"preview": None})) + "\n"
    )
    assert "Prompt preview:  (prompt not recorded)" in lines_of(detail("abcd1234").output)


def test_terminal_escapes_never_reach_the_output(log_dir):
    log_dir.mkdir()
    esc = "\x1b[31mred\x1b]0;title\x07\x9b2J"
    record = valid_record(
        "abcd1234",
        prompt={"preview": esc + " task…"},
        original={"subagent_type": esc},
        selected_model="system.ai." + esc,
        rationale=esc + " because",
        candidates=[{"model": esc, "harness": "codex"}],
    )
    (log_dir / "codex-c1.jsonl").write_text(json.dumps(record) + "\n")
    for output in (table().output, detail("abcd1234").output):
        assert not any(ord(c) < 32 and c != "\n" or 0x7F <= ord(c) <= 0x9F for c in output)
        assert all(
            len(line) == 77 for line in output.splitlines() if line.startswith(("┌", "│", "└"))
        )
    assert "task…" in "\n".join(lines_of(detail("abcd1234").output))


def test_oversized_number_and_escaped_id_do_not_break_the_table(log_dir):
    log_dir.mkdir()
    records = [
        valid_record("aaaa0001"),
        valid_record("aaaa0002", at=10**400),
        valid_record("\x1b[31mevil-id"),
    ]
    (log_dir / "codex-c1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    result = table()
    assert result.exit_code == 0
    lines = lines_of(result.output)
    assert lines[0] == "2 routed subagents this session (showing last 2)"
    assert "\x1b" not in result.output


@pytest.mark.parametrize("from_stdin", [False, True])
@pytest.mark.parametrize(
    "check",
    [
        pytest.param(
            {
                "routing_disabled": True,
                "router_name": "stable",
                "routing_unsupported_reason": "insufficient_eligible_options",
                "eligible_route_options": [],
                "ineligible_route_options": [
                    {"model": "m", "harness": "claude", "reason": "model_not_in_recipe"}
                ],
                "available_route_options": [{"model": "a", "harness": "claude_code"}],
                "validation_ms": 120,
            },
            id="unsupported",
        ),
        pytest.param(
            {
                "routing_disabled": True,
                "router_name": "stable",
                "validation_failed": True,
                "validation_failure_reason": "Smart Router returned an error (HTTP 500)",
                "eligible_route_options": [],
                "ineligible_route_options": [],
                "available_route_options": [],
                "validation_ms": 90,
            },
            id="failed",
        ),
    ],
)
def test_record_route_validation_lists_it_beside_decisions(log_dir, tmp_path, check, from_stdin):
    write(payload())
    result_file = tmp_path / "env.validation.json"
    result_file.write_text(json.dumps(check))
    source = "-" if from_stdin else str(result_file)
    args = ["smart-router-logs", "--session", "s1", "--record-route-validation", source]
    recorded = runner.invoke(cli.app, args, input=json.dumps(check) if from_stdin else None)
    assert recorded.exit_code == 0, recorded.output
    assert json.loads(recorded.output) == {"v": 1}
    listed = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1"])
    document = json.loads(listed.output)
    assert len(document["decisions"]) == 1
    [row] = document["route_validations"]
    assert row.pop("validation_id") and row.pop("at")
    assert row == {
        "v": session_log.SCHEMA_VERSION,
        "type": session_log.ROUTE_VALIDATION_TYPE,
        "harness": "claude",
        "session_id": "s1",
        "routing_unsupported_reason": None,
        "validation_failed": None,
        "validation_failure_reason": None,
        **check,
    }


def test_codex_route_validation_is_listed_under_the_codex_harness_only(log_dir):
    session_log.append_route_validation(
        "codex",
        "s1",
        {
            "routing_disabled": True,
            "router_name": "stable",
            "available_route_options": [{"model": "gpt-5", "harness": "codex"}],
        },
    )
    listed = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1", "--harness", "codex"])
    [row] = json.loads(listed.output)["route_validations"]
    assert (row["harness"], row["session_id"], row["router_name"]) == ("codex", "s1", "stable")
    claude = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1"])
    assert json.loads(claude.output)["route_validations"] == []


@pytest.mark.parametrize("content", [None, "not json", "[]"])
def test_record_route_validation_reports_an_unreadable_result(log_dir, tmp_path, content):
    result_file = tmp_path / "env.validation.json"
    if content is not None:
        result_file.write_text(content)
    args = ["smart-router-logs", "--session", "s1", "--record-route-validation", str(result_file)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert json.loads(result.output)["error"].startswith("cannot record route validation: ")
    assert not log_dir.exists()


STARTUP_UNSUPPORTED = {
    "routing_disabled": True,
    "router_name": "stable",
    "routing_unsupported_reason": "insufficient_eligible_options",
    "eligible_route_options": [],
    "ineligible_route_options": [],
    "available_route_options": [{"model": "gpt-5", "harness": "codex"}],
    "validation_ms": 120,
}
STARTUP_FAILED = {
    "routing_disabled": True,
    "router_name": "stable",
    "validation_failed": True,
    "validation_failure_reason": "Smart Router returned an error (HTTP 500)",
    "eligible_route_options": [],
    "ineligible_route_options": [],
    "available_route_options": [],
    "validation_ms": 90,
}
ROUTING_ON_UNSUPPORTED = {**STARTUP_UNSUPPORTED, "routing_disabled": False}


@pytest.mark.parametrize(
    ("check", "task"),
    [
        pytest.param(
            STARTUP_UNSUPPORTED, "Failed to enable smart routing.", id="startup-unsupported"
        ),
        pytest.param(STARTUP_FAILED, "Failed to validate smart routing.", id="startup-failed"),
        pytest.param(
            ROUTING_ON_UNSUPPORTED, "Warning: unsupported model list.", id="routing-on-unsupported"
        ),
    ],
)
def test_codex_validation_rows_list_in_the_table(log_dir, check, task):
    session_log.append_route_validation("codex", "s1", check)
    result = runner.invoke(
        cli.app, ["smart-router-logs", "--session", "s1", "--harness", "codex", "--format", "table"]
    )
    assert result.exit_code == 0, result.output
    [row] = [line for line in result.output.splitlines() if task in line]
    cells = [cell.strip() for cell in row.strip("│").split("  ") if cell.strip()]
    assert cells[-3:] == [task, "—", f"{check['validation_ms']} ms"]


@pytest.mark.parametrize(
    ("reason", "expected_reason"),
    [
        pytest.param(
            None, "Smart Routing does not support this agent's model list.", id="null-reason"
        ),
        pytest.param("harness_not_supported", "Harness not supported", id="reason-code"),
    ],
)
def test_codex_validation_detail_shows_models_and_reason(log_dir, reason, expected_reason):
    session_log.append_route_validation(
        "codex",
        "s1",
        {
            **STARTUP_UNSUPPORTED,
            "routing_unsupported_reason": reason,
            "eligible_route_options": [{"model": "system.ai.claude-sonnet", "harness": "claude"}],
            "ineligible_route_options": [
                {
                    "model": "system.ai.claude-haiku",
                    "harness": "claude",
                    "reason": "model_not_in_recipe",
                },
                {"model": "custom", "harness": "claude", "reason": None},
            ],
        },
    )
    [record] = session_log.read_records("codex", "s1", session_log.ROUTE_VALIDATION_TYPE)
    result = runner.invoke(
        cli.app,
        [
            "smart-router-logs",
            "--session",
            "s1",
            "--harness",
            "codex",
            "--format",
            "table",
            "--decision",
            record["validation_id"][:8],
        ],
    )
    assert result.exit_code == 0, result.output
    assert expected_reason in result.output
    for line in (
        "Validation Time: 0.1s",
        "✓ claude-sonnet",
        "✗ claude-haiku  Not available in Smart Router",
        "✗ custom  Not eligible for routing",
        "Supported models",
        "gpt-5",
    ):
        assert line in result.output
