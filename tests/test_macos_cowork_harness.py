import importlib.util
import json
import os
import runpy
import select
import signal
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/cowork-inference/macos"
redact = runpy.run_path(str(SCRIPTS / "diagnostics.py"))["redact"]
matches_labels = runpy.run_path(str(SCRIPTS / "controls.py"))["matches_labels"]


def test_desktop_evidence_redacts_credentials_and_preserves_diagnostics(monkeypatch):
    monkeypatch.setenv("DATABRICKS_BEARER", "ci-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    text = (
        "failed ci-secret oauth-secret Authorization: Bearer unknown-secret "
        "eyJtest-header.test-payload.test-signature"
    )
    assert (
        redact(text) == "failed [REDACTED] [REDACTED] Authorization: Bearer [REDACTED] [REDACTED]"
    )
    assert redact("Claude responded: 48213") == "Claude responded: 48213"


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (["AXButton", "Start task", "", "", "", "1", "0"], True),
        (["AXButton", "Start task", "", "", "", "0", "0"], False),
        (["AXButton", "Start task later", "", "", "", "1", "0"], False),
        (["Start task"], False),
        ([], False),
    ],
)
def test_desktop_control_wait_requires_exact_enabled_label(snapshot, expected):
    assert matches_labels(json.dumps([snapshot]), "Start task", enabled=True) is expected


@pytest.mark.parametrize("answer", ["48213", "48214", "Reply with 48213", ""])
@pytest.mark.parametrize("finished", [True, False])
def test_desktop_response_requires_exact_answer_and_completion(answer, finished):
    rows = [["AXStaticText", "", f"Claude responded: {answer}", "", "", "1", "0"]]
    if finished:
        rows.append(["AXStaticText", "", "Claude finished the response", "", "", "1", "0"])
    assert matches_labels(
        json.dumps(rows),
        ("Claude responded: 48213", "Claude finished the response"),
        require_all=True,
    ) is (answer == "48213" and finished)


@pytest.mark.parametrize("value", ["\n", "\n\n", "line one\nline two | value"])
def test_desktop_prompt_with_multiline_value_remains_accessible(value):
    snapshot = json.dumps([["AXTextArea", "", "Write your prompt to Claude", value, "", "1", "0"]])
    assert matches_labels(snapshot, "Write your prompt to Claude", enabled=True)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("UCODE_TEST_WORKSPACE", "https://workspace.example")
    monkeypatch.setenv("DATABRICKS_BEARER", "test-bearer")
    modules = {}
    for name in ("terminal", "probe"):
        spec = importlib.util.spec_from_file_location(
            f"macos_cowork_{name}", SCRIPTS / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[name] = module
    (tmp_path / "macos-cowork-evidence").mkdir()
    return SimpleNamespace(**modules, root=tmp_path)


@pytest.mark.parametrize(
    ("layout", "missing_control"),
    [
        ("cowork-tab", None),
        ("claude-tab", None),
        ("sidebar-cowork", None),
        ("sidebar-claude", None),
        ("sidebar-auto-opens", None),
        ("claude-tab", "Scheduled"),
        ("claude-tab", "Write your prompt to Claude"),
        ("claude-tab", "Start task"),
    ],
)
def test_cowork_navigation_requires_complete_task_screen(
    harness, monkeypatch, layout, missing_control
):
    task_rows = [
        ["AXRadioButton", "", "Claude", "1", "", "1", "0"],
        ["AXButton", "Scheduled", "", "", "", "1", "0"],
        ["AXTextArea", "", "Write your prompt to Claude", "\n", "", "1", "0"],
        ["AXButton", "", "Start task", "", "", "0", "0"],
    ]
    task_snapshot = json.dumps([row for row in task_rows if missing_control not in row])
    cowork_tab = json.dumps([["AXButton", "", "Cowork", "", "", "1", "0"]])
    snapshot = task_snapshot if layout == "claude-tab" else cowork_tab
    if layout.startswith("sidebar-"):
        snapshot = json.dumps([["AXButton", "", "Show sidebar", "", "", "1", "0"]])
    now = 0

    def advance(seconds):
        nonlocal now
        now += seconds

    def native_ui(args, **kwargs):
        nonlocal snapshot
        operation, *labels = args[1:]
        if operation == "snapshot":
            return subprocess.CompletedProcess(args, 0, stdout=snapshot)
        if operation == "click" and labels == ["Show sidebar"]:
            snapshot = cowork_tab if layout == "sidebar-cowork" else task_snapshot
            if layout == "sidebar-auto-opens":
                return subprocess.CompletedProcess(
                    args, 1, stderr="Control unavailable: Show sidebar"
                )
        elif operation == "click" and labels == ["Cowork"]:
            snapshot = task_snapshot
        else:
            pytest.fail(f"Unexpected native UI action: {args[1:]}")
        return subprocess.CompletedProcess(args, 0, stdout="clicked")

    # Native Accessibility is unavailable here; navigation and readiness checks stay real.
    monkeypatch.setattr(harness.probe, "process", SimpleNamespace(run=native_ui))
    monkeypatch.setattr(
        harness.probe, "time", SimpleNamespace(monotonic=lambda: now, sleep=advance)
    )
    if missing_control:
        with pytest.raises(RuntimeError, match="Timed out waiting for"):
            harness.probe.open_cowork()
        assert now == 60
        assert (harness.root / "macos-cowork-evidence/last-ui.json").read_text() == task_snapshot
    else:
        assert harness.probe.open_cowork() == task_snapshot
        assert now == 0
        navigation = (harness.root / "macos-cowork-evidence/navigation-ui.json").read_text()
        assert matches_labels(navigation, ("Cowork", "Scheduled"), enabled=True)


@pytest.mark.parametrize("recovers", [True, False])
def test_accessibility_timeout_uses_remaining_wait_budget(harness, monkeypatch, recovers):
    now = 0
    calls = []
    snapshot = json.dumps([["AXButton", "Ready", "", "", "", "1", "0"]])

    def advance(seconds):
        nonlocal now
        now += seconds

    def snapshot_process(args, **kwargs):
        calls.append(kwargs["timeout"])
        if recovers and len(calls) == 2:
            return subprocess.CompletedProcess(args, 0, stdout=snapshot)
        advance(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    # Accessibility calls require macOS; the polling and deadline logic stay real.
    monkeypatch.setattr(harness.probe, "process", SimpleNamespace(run=snapshot_process))
    monkeypatch.setattr(
        harness.probe, "time", SimpleNamespace(monotonic=lambda: now, sleep=advance)
    )
    if recovers:
        assert harness.probe.wait_for("Ready", timeout=60) == snapshot
        assert calls == [45, 13]
    else:
        with pytest.raises(RuntimeError, match="Timed out waiting for Ready"):
            harness.probe.wait_for("Ready", timeout=60)
        assert now == 60
        assert calls == [45, 13]
        assert "timed out" in (harness.root / "macos-cowork-evidence/last-ui.json").read_text()


@pytest.mark.parametrize("label", [None, "Haiku"])
def test_target_model_label_is_unambiguous(harness, label):
    target = {"name": harness.probe.MODEL, "labelOverride": label}
    other = {"name": "system.ai.other-model", "labelOverride": "Other"}
    assert harness.probe.configured_model_label({"inferenceModels": [target, other]}) == (
        label or harness.probe.MODEL
    )


@pytest.mark.parametrize("other", [{"name": "Haiku"}, {"name": "other", "labelOverride": "Haiku"}])
def test_target_model_rejects_duplicate_display_labels(harness, other):
    models = [{"name": harness.probe.MODEL, "labelOverride": "Haiku"}, other]
    with pytest.raises(RuntimeError, match="label is ambiguous"):
        harness.probe.configured_model_label({"inferenceModels": models})


@pytest.mark.parametrize("target_count", [0, 2])
def test_target_model_must_appear_exactly_once(harness, target_count):
    models = [{"name": harness.probe.MODEL}] * target_count
    with pytest.raises(RuntimeError, match="model missing or ambiguous"):
        harness.probe.configured_model_label({"inferenceModels": models})


@pytest.mark.skipif(os.name != "posix", reason="macOS probe uses POSIX signals and permissions")
@pytest.mark.parametrize("failure", ["launch", "cleanup", "native_log", "result"])
def test_probe_failures_preserve_evidence_and_restore_signals(harness, monkeypatch, failure):
    private = harness.root / "private"
    private.mkdir(mode=0o700)
    evidence = harness.root / "macos-cowork-evidence"
    profile = harness.root / (
        "Library/Application Support/Claude-3p/configLibrary/"
        "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json"
    )
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "inferenceProvider": "gateway",
                "inferenceModels": [{"name": harness.probe.MODEL}],
            }
        )
    )
    if failure == "native_log":
        (evidence / "native.log").mkdir()
    if failure == "result":
        (evidence / "result.json").mkdir()

    def run_native(args, **kwargs):
        if Path(args[0]).name == "desktop-ui":
            raise OSError("Accessibility unavailable")
        return subprocess.CompletedProcess(args, 0, stdout="configured", stderr="")

    def terminate():
        raise PermissionError("Cannot terminate Claude test-bearer")

    def launch_native(args, **kwargs):
        assert stat.S_IMODE(os.fstat(kwargs["stdout"].fileno()).st_mode) == 0o600
        kwargs["stdout"].write("native test-bearer")
        if failure != "cleanup":
            raise OSError("Claude launch failed test-bearer")
        return SimpleNamespace(terminate=terminate)

    # Native UG/Claude/Accessibility boundaries are unavailable in this component test.
    monkeypatch.setattr(
        harness.probe, "process", SimpleNamespace(run=run_native, popen=launch_native)
    )
    monkeypatch.setattr(
        harness.probe, "sys", SimpleNamespace(platform="darwin", executable=sys.executable)
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: harness.root))
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGALRM)}
    if failure == "result":
        with pytest.raises(OSError):
            harness.probe.main(private)
    else:
        assert harness.probe.main(private) == 1
        report = json.loads((evidence / "result.json").read_text())
        assert report["responseAsserted"] is False
        if failure == "cleanup":
            assert report["error"] == "Accessibility unavailable"
            assert report["cleanupError"] == "Cannot terminate Claude [REDACTED]"
        else:
            assert report["error"] == "Claude launch failed [REDACTED]"
        if failure == "native_log":
            assert report["logError"]
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    assert signal.alarm(0) == 0
    if failure == "native_log":
        assert (private / "native.log").read_text() == "native test-bearer"
    else:
        assert (evidence / "native.log").read_text() == "native [REDACTED]"
        assert not (private / "native.log").exists()


def test_terminal_launch_failure_removes_credentials_and_retains_diagnostics(harness, monkeypatch):
    def fail_to_open_terminal(args, **kwargs):
        private = Path(args[-1]).parent
        if os.name == "posix":
            assert stat.S_IMODE(private.stat().st_mode) == 0o700
            assert stat.S_IMODE((private / "environment.json").stat().st_mode) == 0o600
        (private / "output.txt").write_text("launch failed test-bearer")
        raise OSError("Terminal launch failed")

    # Terminal.app is a native launch boundary unavailable on non-macOS runners.
    monkeypatch.setattr(harness.terminal, "process", SimpleNamespace(run=fail_to_open_terminal))
    with pytest.raises(OSError, match="Terminal launch failed"):
        harness.terminal.run_terminal()
    assert not list(harness.root.glob("macos-probe-*"))
    text = (harness.root / "macos-cowork-evidence/terminal.txt").read_text()
    assert "launch failed [REDACTED]" in text
    assert "Terminal launch failed" in text
    assert "test-bearer" not in text


@pytest.mark.skipif(os.name != "posix", reason="macOS cleanup uses POSIX process groups")
@pytest.mark.parametrize("invalid_group", [True, False])
@pytest.mark.parametrize("launch_error", [True, False])
def test_cleanup_errors_still_remove_credentials_and_keep_logs(
    harness, monkeypatch, invalid_group, launch_error
):
    def open_terminal(args, **kwargs):
        private = Path(args[-1]).parent
        (private / "pid.txt").write_text("1" if invalid_group else "12345")
        (private / "status.txt").write_text("0")
        (private / "output.txt").write_text("terminal output test-bearer\n")
        (private / "native.log").write_text("Authorization: Bearer test-bearer")
        if launch_error:
            raise OSError("Original probe failure")
        return subprocess.CompletedProcess(args, 0)

    def deny_kill(*args):
        assert not invalid_group, "An invalid process group must never be signalled"
        raise PermissionError("Cannot signal process group")

    # Terminal launch and OS permission errors are controlled; cleanup stays real.
    monkeypatch.setattr(harness.terminal, "process", SimpleNamespace(run=open_terminal))
    monkeypatch.setattr(harness.terminal.os, "killpg", deny_kill)
    error_type = OSError if launch_error else RuntimeError
    error_message = "Original probe failure" if launch_error else "Terminal probe cleanup failed"
    with pytest.raises(error_type, match=error_message):
        harness.terminal.run_terminal()
    assert not list(harness.root.glob("macos-probe-*"))
    evidence = harness.root / "macos-cowork-evidence"
    text = (evidence / "terminal.txt").read_text()
    assert "terminal output [REDACTED]" in text
    assert ("Refusing" if invalid_group else "Cannot signal") in text
    if launch_error:
        assert "Original probe failure" in text
    assert (evidence / "native.log").read_text() == "Authorization: Bearer [REDACTED]"


@pytest.mark.skipif(os.name != "posix", reason="macOS cleanup uses POSIX process groups")
def test_terminal_timeout_stops_process_and_preserves_final_evidence(harness, monkeypatch):
    children = []
    child_code = """
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from probe import stop_probe

private = Path(sys.argv[1])
evidence = Path(os.environ['RUNNER_TEMP']) / 'macos-cowork-evidence'
(private / 'pid.txt').write_text(str(os.getpid()))
(private / 'native.log').write_text('native test-bearer')
signal.signal(signal.SIGTERM, stop_probe)
try:
    print('ready', flush=True)
    signal.pause()
except TimeoutError as exc:
    (private / 'output.txt').write_text(str(exc) + ' test-bearer')
finally:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (evidence / 'result.json').write_text('{"responseAsserted": false}')
    (private / 'status.txt').write_text('1')
"""

    def open_terminal_child(args, **kwargs):
        private = Path(args[-1]).parent
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(private), str(SCRIPTS)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        children.append(child)
        assert select.select([child.stdout], [], [], 10)[0], "Child did not start"
        assert child.stdout.readline().strip() == "ready"
        return subprocess.CompletedProcess(args, 0)

    # Replace only Terminal.app; use a real isolated process and the probe's signal handler.
    monkeypatch.setattr(harness.terminal, "process", SimpleNamespace(run=open_terminal_child))
    monkeypatch.setattr(harness.terminal, "TERMINAL_TIMEOUT_SECONDS", 0)
    try:
        with pytest.raises(RuntimeError, match="Terminal probe did not finish"):
            harness.terminal.run_terminal()
        assert children[0].wait(timeout=5) == 0
        assert not list(harness.root.glob("macos-probe-*"))
        evidence = harness.root / "macos-cowork-evidence"
        assert json.loads((evidence / "result.json").read_text()) == {"responseAsserted": False}
        assert (evidence / "native.log").read_text() == "native [REDACTED]"
        text = (evidence / "terminal.txt").read_text()
        assert "SIGTERM" in text
        assert "[REDACTED]" in text
        assert "test-bearer" not in text
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)


@pytest.fixture
def tasks(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("macos_cowork_tasks", SCRIPTS / "tasks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    return module


@pytest.mark.skipif(os.name != "posix", reason="macOS probe uses POSIX signals and permissions")
@pytest.mark.parametrize("capture_failure", [False, True])
def test_task_diagnostics_preserve_report_and_independent_errors(
    harness, tasks, monkeypatch, capture_failure
):
    private = harness.root / "private"
    private.mkdir(mode=0o700)
    evidence = harness.probe.EVIDENCE
    (evidence / "configuration.json").write_text(json.dumps({"label": harness.probe.MODEL}))
    task = tasks.prepare_task("file", harness.probe)
    transcript = tasks.session_root() / "org/account/projects/session/current.jsonl"
    transcript.parent.mkdir(parents=True)
    if capture_failure:
        (evidence / "file/native-session-0.jsonl").mkdir(parents=True)

    def native_ui(args, **kwargs):
        if Path(args[0]).name == "desktop-ui":
            raise OSError("Accessibility unavailable test-bearer")
        return subprocess.CompletedProcess(args, 0)

    def launch_native(*args, **kwargs):
        transcript.write_bytes(task["prompt"].encode("utf-8") + b" test-bearer \xff")
        modified = transcript.stat().st_mtime + 60
        os.utime(transcript, (modified, modified))
        return SimpleNamespace(terminate=lambda: None, wait=lambda **kwargs: 0, poll=lambda: 0)

    # Native app and Accessibility require macOS; task setup and diagnostic capture stay real.
    monkeypatch.setattr(
        harness.probe, "process", SimpleNamespace(run=native_ui, popen=launch_native)
    )
    monkeypatch.setitem(sys.modules, "tasks", tasks)
    monkeypatch.setitem(sys.modules, harness.probe.__name__, harness.probe)
    monkeypatch.setattr(
        harness.probe, "sys", SimpleNamespace(platform="darwin", modules=sys.modules)
    )
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGALRM)}
    assert harness.probe.main(private, "file") == 1
    report = json.loads((evidence / "file/result.json").read_text())
    assert report["passed"] is False
    assert report["responseAsserted"] is False
    assert report["error"] == "Accessibility unavailable [REDACTED]"
    assert report["captureError"] == "Accessibility unavailable [REDACTED]"
    if capture_failure:
        assert "native-session-0.jsonl" in report["sessionCaptureError"]
    else:
        assert "sessionCaptureError" not in report
        assert (evidence / "file/native-session-0.jsonl").read_text() == (
            task["prompt"] + " [REDACTED] \ufffd"
        )
    assert harness.probe.EVIDENCE == evidence
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    assert signal.alarm(0) == 0


def test_macos_file_fixture_keeps_answer_out_of_prompt(tasks, tmp_path):
    task = tasks.prepare_task("file", SimpleNamespace(TEMP=tmp_path))
    assert Path(task["expectedFile"]).read_text() == task["expected"]
    assert task["expected"] not in task["prompt"]
    assert Path(task["expectedFile"]).parent == Path(task["folder"])


@pytest.mark.parametrize("listing", ["id", "pluginId", "name"])
@pytest.mark.parametrize("failure", [None, "exit", "outcome", "disabled"])
def test_macos_skill_install_checks_native_result(tasks, tmp_path, monkeypatch, listing, failure):
    account = tasks.session_root() / "org/account"
    account.mkdir(parents=True)
    (account / "cowork_account_settings.json").write_text("{}")
    logs = {}
    plugin = {"enabled": failure != "disabled", "installPath": str(tmp_path / "installed")}
    if listing == "name":
        plugin.update(name="cowork-probe", marketplace="cowork-probe-marketplace")
    else:
        plugin[listing] = tasks.PLUGIN_ID

    def native_plugin(args, **kwargs):
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(account)
        result = {"outcome": "failed" if failure == "outcome" else "ok"}
        if args[1:3] == ["plugin", "list"]:
            result = {"data": {"plugins": [plugin]}}
        return subprocess.CompletedProcess(
            args,
            1 if failure == "exit" else 0,
            stdout=json.dumps(result) if kwargs.get("text") else json.dumps(result).encode(),
            stderr="diagnostic outside JSON" if kwargs.get("text") else b"diagnostic outside JSON",
        )

    # Native plugin installation is unavailable on Linux; fixture creation and validation stay real.
    monkeypatch.setattr(tasks.shutil, "which", lambda _: "/installed/claude")
    monkeypatch.setattr(tasks, "process", SimpleNamespace(run=native_plugin))
    desktop = SimpleNamespace(TEMP=tmp_path, save=logs.__setitem__)
    if failure:
        with pytest.raises(RuntimeError):
            tasks.prepare_task("skill", desktop)
        return
    task = tasks.prepare_task("skill", desktop)
    source = tmp_path / "cowork-probe-marketplace/plugins/cowork-probe/skills/probe-summary"
    assert (source / "references/facts.txt").read_text() == task["expected"]
    assert task["expected"] not in task["prompt"]
    assert task["expected"] not in (source / "SKILL.md").read_text()
    assert task["expectedFile"] == str(
        tmp_path / "installed/skills/probe-summary/references/facts.txt"
    )
    assert "diagnostic outside JSON" in logs["skill-install-3.txt"]


@pytest.mark.parametrize("copies", [0, 1, 2])
@pytest.mark.parametrize("matches_source", [True, False])
def test_macos_skill_runtime_copy_must_match_installed_fixture(
    tasks, tmp_path, copies, matches_source
):
    source = tmp_path / "installed/skills/probe-summary/references/facts.txt"
    source.parent.mkdir(parents=True)
    source.write_text("unique-fixture-value")
    (source.parents[1] / "SKILL.md").write_text("Read references/facts.txt")
    runtime = tmp_path / "claude-hostloop-plugins"
    for index in range(copies):
        path = runtime / f"copy-{index}/1.0.0/skills/probe-summary/references/facts.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(source.read_bytes())
        (path.parents[1] / "SKILL.md").write_text(
            "Read references/facts.txt" if matches_source else "Different skill"
        )
    task = {"capability": "skill", "expectedFile": str(source)}
    if copies == 2 and matches_source:
        with pytest.raises(RuntimeError, match="Ambiguous"):
            tasks.runtime_task(task, runtime)
        return
    result = tasks.runtime_task(task, runtime)
    expected = runtime / "copy-0/1.0.0/skills/probe-summary/references/facts.txt"
    assert result["expectedFile"] == str(expected if copies == 1 and matches_source else source)
