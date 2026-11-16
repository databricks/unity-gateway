"""Component coverage of remote transport; does not claim live inference."""

import json
import shlex
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cloud


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cloud, "load_state", lambda: {})
    monkeypatch.setattr(cloud.shutil, "which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr(cloud, "databricks_cli_path", lambda: "databricks")
    monkeypatch.setattr(
        cloud,
        "list_profile_entries",
        lambda: [{"name": "test", "host": "https://test.cloud.databricks.com"}],
    )
    monkeypatch.setattr(cloud, "apply_environment", lambda _: None)
    monkeypatch.setattr(cloud, "ensure_remote_auth", lambda *a: None)

    def response(args, **kwargs):
        if args[0] == "ssh":
            execute(args)
        stdout = (
            "hostname gateway.example.com\nport 2222\nidentityfile /tmp/key\n"
            if "-G" in args
            else json.dumps({"sandboxId": "test-sbx"})
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    run = Mock(side_effect=response)
    execute = Mock()
    monkeypatch.setattr(cloud, "run", run)
    monkeypatch.setattr(cloud, "exec_or_spawn", execute)
    monkeypatch.setattr(cloud, "create_sandbox", lambda t, agent: cloud.checked(t, t.cli("create")))
    return run, execute


def test_claude_preserves_prompt_and_headless_transport(setup):
    """Scenario: headless prompt has shell syntax. Expected: literal argv and no PTY."""
    run, execute = setup
    prompt = 'Say "hello"; $(touch /tmp/should-not-run)\nnext'
    r = CliRunner().invoke(
        cloud.app, ["--profile", "test", "claude", "-p", prompt, "--output-format", "json"]
    )
    assert r.exit_code == 0, r.output
    argv = execute.call_args.args[0]
    assert "-T" in argv and "-tt" not in argv
    remote = argv[-1].split(" && exec ", 1)[1]
    assert shlex.split(remote) == [
        "env",
        "_UCODE_DISPATCH_ACTIVE=1",
        "python3",
        "-c",
        cloud.Path(cloud.__file__).with_name("cloud_remote.py").read_text(encoding="utf-8"),
        "claude",
        "--workspace",
        "https://test.cloud.databricks.com",
        "-p",
        prompt,
        "--output-format",
        "json",
    ]
    assert run.call_count == 6
    assert "Sandbox: test-sbx" in r.output


def test_codex_exec_reuses_sandbox(setup):
    """Scenario: reuse with native Codex exec flags. Expected: no create, preserved flags."""
    run, execute = setup
    r = CliRunner().invoke(
        cloud.app,
        ["--profile", "test", "--sandbox", "existing", "codex", "exec", "--json", "hello"],
    )
    assert r.exit_code == 0, r.output
    assert run.call_count == 4
    assert "existing@gateway.example.com" in execute.call_args.args[0]
    assert shlex.split(execute.call_args.args[0][-1].split(" && exec ")[1])[-3:] == [
        "exec",
        "--json",
        "hello",
    ]


def test_create_failure_does_not_launch(setup):
    run, execute = setup
    run.side_effect = None
    run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="quota exceeded")
    r = CliRunner().invoke(cloud.app, ["--profile", "test", "claude", "-p", "hello"])
    assert r.exit_code == 1
    assert "quota exceeded" in r.output
    execute.assert_not_called()


def test_interactive_requires_terminal_before_creation(setup):
    run, _ = setup
    r = CliRunner().invoke(cloud.app, ["--profile", "test", "claude"])
    assert r.exit_code != 0
    assert "needs a terminal" in r.output
    run.assert_not_called()


def test_profile_workspace_mismatch_rejected(setup):
    run, _ = setup
    r = CliRunner().invoke(
        cloud.app,
        [
            "--workspace",
            "https://wrong.cloud.databricks.com",
            "--profile",
            "test",
            "claude",
            "-p",
            "hi",
        ],
    )
    assert r.exit_code == 1
    assert "does not match" in r.output
    run.assert_not_called()


def test_remote_directory_quoting():
    t = cloud.Target("https://test.cloud.databricks.com", None, None, "/tmp/a; touch nope")
    command = cloud.remote_command("codex", ["exec", "hello"], t)
    assert "mkdir -p -- '/tmp/a; touch nope'" in command


def test_forwarded_workspace_cannot_select_another_gateway(setup):
    run, _ = setup
    r = CliRunner().invoke(
        cloud.app, ["--profile", "test", "claude", "--workspace", "https://other", "-p", "hi"]
    )
    assert r.exit_code != 0
    assert "before the agent" in r.output
    run.assert_not_called()


def test_profile_does_not_inherit_unrelated_token(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "not-used")
    t = cloud.Target("https://test.cloud.databricks.com", "test", None, "/tmp/project")
    assert "DATABRICKS_TOKEN" not in t.env()
    assert t.env()["DATABRICKS_CONFIG_PROFILE"] == "test"


def test_help_has_no_remote_side_effects(setup):
    run, execute = setup
    assert CliRunner().invoke(cloud.app, ["--help"]).exit_code == 0
    run.assert_not_called()
    execute.assert_not_called()


def test_delete_requires_explicit_yes_to_skip_confirmation(setup):
    _, execute = setup
    r = CliRunner().invoke(cloud.app, ["--profile", "test", "delete", "test-sbx"])
    assert r.exit_code == 0
    assert "--auto-approve" not in execute.call_args.args[0]
    r = CliRunner().invoke(cloud.app, ["--profile", "test", "delete", "test-sbx", "--yes"])
    assert r.exit_code == 0
    assert "--auto-approve" in execute.call_args.args[0]


def test_interactive_allocates_pty(setup, monkeypatch):
    """Scenario: interactive terminal. Expected: SSH requests a PTY for the remote TUI."""
    _, execute = setup
    monkeypatch.setattr(cloud.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    ctx = SimpleNamespace(obj=(None, "test", None, "/tmp/project"), args=[])
    cloud.launch(ctx, "claude")
    assert "-tt" in execute.call_args.args[0]
    assert "script -q -e -c" in execute.call_args.args[0][-1]
    assert "stty rows" in execute.call_args.args[0][-1]
    saved = cloud.read_json_safe(
        cloud.session_path(
            cloud.Target("https://test.cloud.databricks.com", "test", None, "/tmp/project"),
            "claude",
        )
    )
    assert saved["session"] in execute.call_args.args[0][-1]
    assert "--session-id" in execute.call_args.args[0][-1]
    assert "--resume" in execute.call_args.args[0][-1]


def test_setup_failure_deletes_created_headless_sandbox(setup):
    run, execute = setup
    run.side_effect = [
        SimpleNamespace(returncode=0, stdout="", stderr=""),
        SimpleNamespace(returncode=0, stdout='{"sandboxId":"test-sbx"}', stderr=""),
        SimpleNamespace(returncode=127, stdout="", stderr="missing claude"),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]
    r = CliRunner().invoke(cloud.app, ["--profile", "test", "claude", "-p", "hi"])
    assert r.exit_code == 1
    assert "Deleted Sandbox test-sbx" in r.output
    execute.assert_not_called()
    assert any("delete" in call.args[0] for call in run.call_args_list)


def test_terminal_defaults_are_forwarded_without_credentials(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("COLORFGBG", "15;0")
    monkeypatch.setenv("TERM_PROGRAM", "test; $(touch /tmp/nope)")
    monkeypatch.setenv("SECRET_TOKEN", "must-not-transfer")
    monkeypatch.setattr(cloud.shutil, "get_terminal_size", lambda _: (100, 30))
    t = cloud.Target("https://test.cloud.databricks.com", None, None, "/tmp/project")
    command = cloud.remote_command("claude", [], t, tty=True)
    outer = shlex.split(command.split(" && exec ", 1)[1])
    assert "COLORTERM=truecolor" in outer
    assert "COLORFGBG=15;0" in outer
    assert "TERM_PROGRAM=test; $(touch /tmp/nope)" in outer
    assert "must-not-transfer" not in command
    inner = outer[outer.index("-c") + 1]
    assert inner.startswith("stty rows 30 cols 100; exec env _UCODE_DISPATCH_ACTIVE=1 python3 -c")


def test_terminal_type_fallback(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    t = cloud.Target("https://test.cloud.databricks.com", None, None, "/tmp/project")
    assert "TERM=xterm-256color" in cloud.remote_command("claude", [], t, tty=True)


def test_create_quota_stops_retrying_and_hides_debug_secrets(monkeypatch):
    """Component process fixture: a retrying CLI reports quota then sleeps."""
    import subprocess
    import sys
    import time

    processes = []

    def start(args, **kwargs):
        assert "--debug" in args
        code = (
            "import sys,time; "
            "print('Authorization: secret-test-value', file=sys.stderr, flush=True); "
            "print('RESOURCE_EXHAUSTED: Cannot create sandboxes because it would exceed "
            "the configured limit of 200.', file=sys.stderr, flush=True); time.sleep(60)"
        )
        process = subprocess.Popen([sys.executable, "-c", code], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(cloud, "popen", start)
    monkeypatch.setattr(cloud, "databricks_cli_path", lambda: "databricks")
    t = cloud.Target("https://test.cloud.databricks.com", None, None, "/tmp/project")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="configured limit: 200") as error:
        cloud.create_sandbox(t, "claude")
    assert "secret-test-value" not in str(error.value)
    assert "RESOURCE_EXHAUSTED" in str(error.value)
    assert time.monotonic() - started < 10
    assert processes[0].poll() is not None


def test_create_success_preserves_json(monkeypatch):
    import subprocess
    import sys

    monkeypatch.setattr(cloud, "databricks_cli_path", lambda: "databricks")
    monkeypatch.setattr(
        cloud,
        "popen",
        lambda args, **kwargs: subprocess.Popen(
            [sys.executable, "-c", "import json; print(json.dumps(dict(sandboxId='created')))"],
            **kwargs,
        ),
    )
    t = cloud.Target("https://test.cloud.databricks.com", None, None, "/tmp/project")
    assert json.loads(cloud.create_sandbox(t, "claude")) == {"sandboxId": "created"}


def test_headless_failure_preserves_status_and_cleans_up(setup):
    run, _ = setup
    original = run.side_effect

    def response(args, **kwargs):
        if args[0] == "ssh":
            return SimpleNamespace(returncode=7)
        return original(args, **kwargs)

    run.side_effect = response
    result = CliRunner().invoke(cloud.app, ["--profile", "test", "claude", "-p", "hi"])
    assert result.exit_code == 7
    assert "Deleted Sandbox test-sbx" in result.output


def test_reused_headless_sandbox_is_not_deleted(setup):
    run, _ = setup
    result = CliRunner().invoke(
        cloud.app, ["--profile", "test", "--sandbox", "existing", "claude", "-p", "hi"]
    )
    assert result.exit_code == 0
    assert not any("delete" in call.args[0] for call in run.call_args_list)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_default_interactive_launch_ignores_previous_session(setup, monkeypatch, agent):
    """A plain launch creates a new Sandbox even when a previous session is saved."""
    run, execute = setup
    target = cloud.Target("https://test.cloud.databricks.com", "test", None, "/tmp/project")
    previous = "12345678-1234-1234-1234-123456789abc"
    cloud.atomic_write_json(
        cloud.session_path(target, agent), {"sandbox": "existing", "session": previous}
    )
    read_saved = Mock(side_effect=AssertionError("Default launch must not read saved sessions"))
    monkeypatch.setattr(cloud, "read_json_safe", read_saved)
    monkeypatch.setattr(cloud.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    cloud.launch(SimpleNamespace(obj=(None, "test", None, "/tmp/project"), args=[]), agent)
    read_saved.assert_not_called()
    assert any("create" in call.args[0] for call in run.call_args_list)
    assert not any("list" in call.args[0] for call in run.call_args_list)
    assert "test-sbx@gateway.example.com" in execute.call_args.args[0]
    saved = json.loads(cloud.session_path(target, agent).read_text())
    assert saved["sandbox"] == "test-sbx"
    if agent == "claude":
        assert saved["session"] != previous


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_explicit_cloud_resume_reuses_previous_session_without_creation(setup, monkeypatch, agent):
    """--resume directly reopens the scoped session instead of offering a picker."""
    run, execute = setup
    original = run.side_effect
    session = "12345678-1234-1234-1234-123456789abc"
    target = cloud.Target("https://test.cloud.databricks.com", "test", None, "/tmp/project")
    cloud.atomic_write_json(
        cloud.session_path(target, agent), {"sandbox": "existing", "session": session}
    )

    def response(args, **kwargs):
        if "list" in args:
            return SimpleNamespace(returncode=0, stdout='[{"sandboxId":"existing"}]', stderr="")
        if "find " in args[-1]:
            return SimpleNamespace(returncode=0, stdout="/a/conversation.jsonl", stderr="")
        return original(args, **kwargs)

    run.side_effect = response
    monkeypatch.setattr(cloud.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    remote = Mock(wraps=cloud.remote_command)
    monkeypatch.setattr(cloud, "remote_command", remote)
    cloud.launch(SimpleNamespace(obj=(None, "test", None, "/tmp/project", True), args=[]), agent)
    assert remote.call_args.args[1] == (
        ["--resume", session] if agent == "claude" else ["resume", "--last"]
    )
    assert not any("create" in call.args[0] for call in run.call_args_list)
    assert "existing@gateway.example.com" in execute.call_args.args[0]


@pytest.mark.parametrize("flags,expected", [([], False), (["--resume"], True)])
def test_resume_option_is_parsed_before_agent(setup, monkeypatch, flags, expected):
    launch = Mock()
    monkeypatch.setattr(cloud, "launch", launch)
    result = CliRunner().invoke(
        cloud.app, ["--profile", "test", *flags, "claude", "--model", "opus"]
    )
    assert result.exit_code == 0, result.output
    assert launch.call_args.args[0].obj[-1] is expected
    assert launch.call_args.args[0].args == ["--model", "opus"]


@pytest.mark.parametrize("saved", [{}, {"sandbox": "bad/id"}])
def test_explicit_resume_without_saved_sandbox_does_not_create(setup, monkeypatch, saved):
    run, execute = setup
    target = cloud.Target("https://test.cloud.databricks.com", "test", None, "/tmp/project", True)
    cloud.atomic_write_json(cloud.session_path(target, "claude"), saved)
    monkeypatch.setattr(cloud.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    with pytest.raises(cloud.typer.Exit):
        cloud.launch(
            SimpleNamespace(obj=(None, "test", None, "/tmp/project", True), args=[]), "claude"
        )
    run.assert_not_called()
    execute.assert_not_called()


def test_explicit_resume_of_deleted_sandbox_fails_without_creation(setup, monkeypatch):
    target = cloud.Target("https://test.cloud.databricks.com", "test", None, "/tmp/project", True)
    cloud.atomic_write_json(cloud.session_path(target, "claude"), {"sandbox": "deleted"})
    checked = Mock(return_value="[]")
    monkeypatch.setattr(cloud, "checked", checked)
    with pytest.raises(RuntimeError, match="no longer exists"):
        cloud.choose_previous(target, "claude", [])
    assert checked.call_count == 1
    assert not cloud.session_path(target, "claude").exists()


@pytest.mark.parametrize("args", [["claude", "-p", "hi"], ["codex", "exec", "hi"]])
def test_cloud_resume_rejects_headless_jobs(setup, args):
    run, execute = setup
    result = CliRunner().invoke(cloud.app, ["--profile", "test", "--resume", *args])
    assert result.exit_code != 0
    assert "interactive conversations" in result.output
    run.assert_not_called()
    execute.assert_not_called()


def test_cloud_resume_rejects_conflicting_sandbox_selection(setup):
    run, _ = setup
    result = CliRunner().invoke(
        cloud.app, ["--profile", "test", "--resume", "--sandbox", "existing", "claude"]
    )
    assert result.exit_code != 0
    assert "not both" in result.output
    run.assert_not_called()


def test_cloud_resume_is_not_silently_ignored_by_list(setup):
    run, _ = setup
    result = CliRunner().invoke(cloud.app, ["--profile", "test", "--resume", "list"])
    assert result.exit_code != 0
    assert "only supported with claude or codex" in result.output
    run.assert_not_called()


def test_resume_scope_and_explicit_selection(setup):
    a = cloud.Target("https://test.cloud.databricks.com", "test", None, "/tmp/project")
    b = cloud.Target(a.host, "other", None, a.directory)
    c = cloud.Target(a.host, a.profile, None, "/another")
    d = cloud.Target("https://other.cloud.databricks.com", a.profile, None, a.directory)
    assert len({cloud.session_path(t, "claude") for t in (a, b, c, d)}) == 4
    assert cloud.session_path(a, "codex") != cloud.session_path(a, "claude")
    a.sandbox = "explicit"
    assert cloud.choose_previous(a, "claude", []) == ("explicit", [], None)


def test_exit_instructions_preserve_exit_status_and_quote(tmp_path):
    import subprocess

    marker = tmp_path / "should-not-exist"
    instructions = f"Resume: $(touch {marker})"
    result = subprocess.run(
        ["sh", "-c", cloud.with_exit_instructions("exit 7", instructions)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7
    assert instructions in result.stderr
    assert not marker.exists()


def test_resume_after_incomplete_onboarding_fails_without_creating(setup, monkeypatch):
    target = cloud.Target("https://test.cloud.databricks.com", "test", None, "/tmp/project", True)
    cloud.atomic_write_json(
        cloud.session_path(target, "claude"),
        {"sandbox": "existing", "session": "12345678-1234-1234-1234-123456789abc"},
    )
    checked = Mock(side_effect=['[{"sandboxId":"existing"}]', ""])
    monkeypatch.setattr(cloud, "checked", checked)
    with pytest.raises(RuntimeError, match="ug cloud --sandbox existing claude"):
        cloud.choose_previous(target, "claude", [])
    assert checked.call_count == 2


@pytest.mark.parametrize("configured", [True, False])
def test_remote_auth_uses_service_credential_without_browser(monkeypatch, configured):
    replies = [json.dumps({"credential_available": True, "configured": configured})]
    if not configured:
        replies += ["configured", json.dumps({"credential_available": True, "configured": True})]
    checked = Mock(side_effect=replies)
    monkeypatch.setattr(cloud, "checked", checked)
    monkeypatch.setattr(cloud, "databricks_cli_path", lambda: "databricks")
    target = cloud.Target("https://test.cloud.databricks.com", "local-user", "existing", "/tmp")
    cloud.ensure_remote_auth(target, "existing", "claude")
    assert checked.call_count == (1 if configured else 3)
    if not configured:
        command = checked.call_args_list[1].args[1][-1]
        assert "--profile DEFAULT --agents claude --use-pat" in command
        assert "local-user" not in command
        assert "auth login" not in command


def test_missing_remote_credential_blocks_agent_launch(monkeypatch):
    checked = Mock(return_value=json.dumps({"credential_available": False, "configured": False}))
    monkeypatch.setattr(cloud, "checked", checked)
    monkeypatch.setattr(cloud, "databricks_cli_path", lambda: "databricks")
    target = cloud.Target("https://test.cloud.databricks.com", None, "existing", "/tmp")
    with pytest.raises(RuntimeError, match="Remote browser login will not be started"):
        cloud.ensure_remote_auth(target, "existing", "claude")
    assert checked.call_count == 1


def test_failed_remote_auth_repair_does_not_proceed(monkeypatch):
    state = json.dumps({"credential_available": True, "configured": False})
    monkeypatch.setattr(cloud, "checked", Mock(side_effect=[state, "", state]))
    monkeypatch.setattr(cloud, "databricks_cli_path", lambda: "databricks")
    target = cloud.Target("https://test.cloud.databricks.com", None, "existing", "/tmp")
    with pytest.raises(RuntimeError, match="Stopped before launching"):
        cloud.ensure_remote_auth(target, "existing", "claude")


@pytest.mark.parametrize("args", [["claude", "--resume", "abc"], ["codex", "resume", "--last"]])
def test_explicit_resume_requires_sandbox(setup, monkeypatch, args):
    run, _ = setup
    monkeypatch.setattr(cloud.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    ctx = SimpleNamespace(obj=(None, "test", None, "/tmp/project"), args=args[1:])
    with pytest.raises(cloud.typer.Exit):
        cloud.launch(ctx, args[0])
    run.assert_not_called()


@pytest.mark.parametrize("flag", ["--bg", "--background", "--cloud", "--remote"])
def test_detached_launch_not_misclassified(setup, flag):
    run, _ = setup
    result = CliRunner().invoke(cloud.app, ["--profile", "test", "claude", flag])
    assert result.exit_code != 0
    assert "not supported" in result.output
    run.assert_not_called()


def test_explicit_resume_preserves_exact_uuid(setup):
    target = cloud.Target("https://test.cloud.databricks.com", "test", "existing", "/tmp")
    session = "12345678-1234-1234-1234-123456789abc"
    assert cloud.choose_previous(target, "claude", ["--resume", session])[2] == session


def test_cleanup_failure_returns_error_and_recovery_command(setup):
    run, _ = setup
    original = run.side_effect

    def response(args, **kwargs):
        if "delete" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="service unavailable")
        return original(args, **kwargs)

    run.side_effect = response
    result = CliRunner().invoke(cloud.app, ["--profile", "test", "claude", "-p", "hi"])
    assert result.exit_code == 1
    assert "Cleanup failed" in result.output
    assert "delete test-sbx" in result.output


def test_interactive_status_is_short_and_resume_prints_only_on_exit(setup, monkeypatch, capsys):
    _, execute = setup
    monkeypatch.setattr(cloud.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        cloud,
        "load_state",
        lambda: {"workspace": "https://test.cloud.databricks.com", "profile": "test"},
    )
    cloud.launch(
        SimpleNamespace(obj=(None, "test", None, "/home/sandbox-agent/project"), args=[]),
        "claude",
    )
    assert capsys.readouterr().err.splitlines() == [
        "Starting Claude in a new Sandbox...",
        "Sandbox: test-sbx",
    ]
    remote = execute.call_args.args[0][-1]
    assert "Resume: ug cloud --resume claude" in remote
    assert "Delete: ug cloud delete test-sbx" in remote
    assert remote.count("Sandbox retained: test-sbx") == 1


def test_resume_hint_keeps_nondefault_scope_and_save_failure_fallback():
    target = cloud.Target("https://other", "other-profile", "existing", "/custom project")
    hint = cloud.resume_instructions(target, "existing", "claude", "session")
    assert "--profile other-profile --directory '/custom project' --resume claude" in hint
    fallback = cloud.resume_instructions(target, "existing", "claude", "session", shortcut=False)
    assert "--sandbox existing --directory '/custom project' claude --resume session" in fallback


def test_remote_status_filter_preserves_warnings_errors_and_prompts(monkeypatch):
    import io

    from rich.console import Console
    from rich.panel import Panel

    from ucode.cloud_remote import StartupConsole

    output = io.StringIO()
    original = Console(file=output, color_system=None)
    compact = StartupConsole(original)
    compact.print("[bold green]✔[/bold green] Unity Gateway connected")
    compact.print("[dim]•[/dim] Using your settings")
    compact.print(Panel("Configured", title="Configuration Complete"))
    compact.print(Panel("Launching Claude Code with Unity Gateway"))
    compact.print("[bold yellow]![/bold yellow] Review your MCP configuration.")
    compact.print("[bold yellow]![/bold yellow] Review your MCP configuration.")
    compact.print("[bold red]ERROR[/bold red] Authentication failed")
    compact.print("Unrecognized diagnostic: keep this visible")
    monkeypatch.setattr("builtins.input", lambda: "yes")
    assert compact.input("Continue? ") == "yes"
    text = output.getvalue()
    assert "connected" not in text and "Configured" not in text and "Launching" not in text
    assert "Using your settings" not in text
    assert text.count("Review your MCP configuration.") == 1
    assert "Authentication failed" in text
    assert "Unrecognized diagnostic" in text
    assert "Continue?" in text


def test_keyboard_interrupt_cleans_created_headless_sandbox(setup):
    run, _ = setup
    original = run.side_effect

    def response(args, **kwargs):
        if args[0] == "ssh":
            raise KeyboardInterrupt
        return original(args, **kwargs)

    run.side_effect = response
    result = CliRunner().invoke(cloud.app, ["--profile", "test", "claude", "-p", "hi"])
    assert result.exit_code == 130
    assert "Deleted Sandbox test-sbx" in result.output
