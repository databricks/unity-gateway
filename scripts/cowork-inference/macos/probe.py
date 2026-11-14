import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from controls import first_label_with_prefix, matches_labels, model_option_label
from diagnostics import redact
from terminal import PROBE_TIMEOUT_SECONDS

from ucode.os_compatibility import subprocess_cross_os as process

MODEL = os.environ.get("UG_COWORK_MODEL") or "system.ai.claude-haiku-4-5"
PROVIDER = os.environ.get("UG_COWORK_PROVIDER", "")
RELAY = os.environ.get("UG_COWORK_RELAY") == "true"
MODEL_LABEL_SUFFIXES = ("", " Extended", " Low", " Medium", " High", " Max")
PROMPT = (
    "Reply with only the digits for forty-eight thousand two hundred thirteen. Do not use tools."
)
TEMP = Path(os.environ["RUNNER_TEMP"])
EVIDENCE = TEMP / "macos-cowork-evidence"


def save(name, text):
    (EVIDENCE / name).write_text(redact(text))


def ui(*args, wait_until_enabled=True, timeout=45):
    if wait_until_enabled and args[0] in ("click", "type"):
        wait_for(args[1], enabled=True)
    result = process.run(
        [str(TEMP / "desktop-ui"), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def wait_for(label, timeout=60, require_all=False, enabled=False):
    deadline = time.monotonic() + timeout
    last = ""
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            last = ui("snapshot", timeout=min(45, remaining))
            if matches_labels(last, label, require_all=require_all, enabled=enabled):
                return last
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    save("last-ui.json", last)
    raise RuntimeError(f"Timed out waiting for {label}")


def open_cowork():
    navigation_labels = ("Cowork", "Scheduled")
    navigation = wait_for((*navigation_labels, "Show sidebar"), enabled=True)
    if not matches_labels(navigation, navigation_labels, enabled=True):
        try:
            ui("click", "Show sidebar", wait_until_enabled=False)
        except RuntimeError as exc:
            if str(exc) != "Control unavailable: Show sidebar":
                raise
        navigation = wait_for(navigation_labels, enabled=True)
    save("navigation-ui.json", navigation)
    if matches_labels(navigation, "Cowork", enabled=True):
        ui("click", "Cowork")
    return wait_for(("Scheduled", "Write your prompt to Claude", "Start task"), require_all=True)


def select_model(snapshot, label):
    """Select the target model in Cowork, returning the picker label Desktop opened with."""
    selected_labels = {f"Model: {label}{suffix}" for suffix in MODEL_LABEL_SUFFIXES}
    initial = first_label_with_prefix(snapshot, "Model: ")
    if initial is None:
        raise RuntimeError("Cowork did not show a model picker")
    # Desktop starts on its own default model, which is not the target for every provider service.
    if initial not in selected_labels:
        ui("click", initial)
        ui("click", wait_for_model_option(label))
        wait_for(selected_labels)
    return initial


def wait_for_model_option(label, timeout=60):
    deadline = time.monotonic() + timeout
    snapshot = ""
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            snapshot = ui("snapshot", timeout=min(45, remaining))
            if option := model_option_label(snapshot, label):
                return option
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            snapshot = str(exc)
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    save("last-ui.json", snapshot)
    raise RuntimeError(f"Timed out waiting for model option {label}")


def check_model_source(profile):
    if profile["inferenceProvider"] != "gateway":
        raise RuntimeError("Expected gateway inference")
    headers = profile.get("inferenceCustomHeaders", {})
    configured_provider = headers.get("Databricks-Model-Provider-Service") or ""
    if configured_provider != PROVIDER:
        raise RuntimeError(
            f"Desktop routes to model provider service {configured_provider!r}, expected {PROVIDER!r}"
        )
    if not PROVIDER and headers.get("Databricks-Model-Service-Parent-Schema") not in (
        None,
        "",
        "system.ai",
    ):
        raise RuntimeError("Expected system.ai schema")
    helper_args = profile.get("inferenceCredentialHelperArgs", [])
    if ("--relay" in helper_args) != RELAY:
        raise RuntimeError(f"Desktop credential helper relay mode should be {RELAY}")
    if ("--claude-binary" in helper_args) != RELAY:
        raise RuntimeError(f"Desktop credential helper Claude CLI binary should be {RELAY}")


def configured_model_label(profile):
    models = profile["inferenceModels"]
    targets = [model for model in models if model["name"] == MODEL]
    if len(targets) != 1:
        raise RuntimeError(f"Expected model missing or ambiguous: {MODEL}")
    label = targets[0].get("labelOverride") or MODEL
    matching_labels = [
        model for model in models if (model.get("labelOverride") or model["name"]) == label
    ]
    if len(matching_labels) != 1:
        raise RuntimeError("Configured target model label is ambiguous")
    return label


def configure():
    workspace = os.environ.get("UCODE_TEST_WORKSPACE", "")
    if not workspace or not os.environ.get("DATABRICKS_BEARER"):
        raise RuntimeError("Existing CI workspace and bearer are required")
    if RELAY and not (PROVIDER and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()):
        raise RuntimeError("A relay test requires a provider service and CLAUDE_CODE_OAUTH_TOKEN")
    config = Path.home() / ".databrickscfg"
    config.write_text(f"[desktop-ci]\nhost = {workspace}\nauth_type = databricks-cli\n")
    config.chmod(0o600)
    result = process.run(
        [
            str(Path(sys.executable).parent / "ug"),
            "configure",
            "--profile",
            "desktop-ci",
            "--agents",
            "claude",
            "--user",
            "--disable-databricks-ai-tools",
            "--skip-upgrade",
            *(["--provider", PROVIDER] if PROVIDER else []),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )
    save("configure.txt", result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("UG configure failed; see configure.txt")
    profile = json.loads(
        (
            Path.home() / "Library/Application Support/Claude-3p/configLibrary/"
            "5b36a0b9-b56d-47cf-b6a4-0904e0bc14f6.json"
        ).read_text()
    )
    check_model_source(profile)
    label = configured_model_label(profile)
    save(
        "configuration.json",
        json.dumps(
            {
                "provider": "gateway",
                "modelProvider": PROVIDER,
                "relay": RELAY,
                "model": MODEL,
                "label": label,
            }
        ),
    )
    return label


def stop_probe(signum, _frame):
    raise TimeoutError(f"Cowork probe stopped by {signal.Signals(signum).name}")


def main(private, capability="inference"):
    global EVIDENCE
    if sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Use a disposable macOS GitHub Actions runner")
    baseline_evidence = EVIDENCE
    if capability != "inference":
        EVIDENCE = baseline_evidence / capability
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    report = {
        "model": MODEL,
        "modelProvider": PROVIDER,
        "relay": RELAY,
        "responseAsserted": False,
    }
    app = None
    task = None
    raw_log = private / "native.log"
    handlers = {sig: signal.signal(sig, stop_probe) for sig in (signal.SIGTERM, signal.SIGALRM)}
    signal.alarm(PROBE_TIMEOUT_SECONDS)
    try:
        if capability == "inference":
            model_label = configure()
            print("UG gateway profile configured", flush=True)
        else:
            import tasks

            model_label = json.loads((baseline_evidence / "configuration.json").read_text())[
                "label"
            ]
            task = tasks.prepare_task(capability, sys.modules[__name__])
            save("task.json", json.dumps(task, indent=2))
            report.update(capability=capability, passed=False, submitted=False)
        with os.fdopen(os.open(raw_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as log:
            app = process.popen(
                [
                    "/Applications/Claude.app/Contents/MacOS/Claude",
                    "--force-renderer-accessibility",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
        if capability == "inference":
            snapshot = wait_for(("Get started", "Continue"), timeout=60)
            save("initial-ui.json", snapshot)
            if matches_labels(snapshot, "Get started"):
                ui("click", "Get started")
            wait_for("Continue")
            ui("click", "Continue")
            print("Gateway onboarding accepted", flush=True)
            snapshot = open_cowork()
        else:
            snapshot = tasks.new_task(sys.modules[__name__])
        report["coworkActivated"] = True
        print("Cowork task screen ready", flush=True)
        save("cowork-ui.json", snapshot)
        report["initialModel"] = select_model(snapshot, model_label)
        print("Cowork model selected", flush=True)
        if task:
            tasks.run_task(sys.modules[__name__], task, report)
        else:
            ui("type", "Write your prompt to Claude", PROMPT)
            ui("click", "Start task")
            print("Start task requested", flush=True)
            snapshot = wait_for(
                ("Claude responded: 48213", "Claude finished the response"),
                timeout=240,
                require_all=True,
            )
            save("response-ui.json", snapshot)
            report.update(responseAsserted=True, responseFinished=True, expectedResponse="48213")
    except Exception as exc:
        report["error"] = str(exc)
        report["responseAsserted"] = False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if app:
                try:
                    save("final-ui.json", ui("snapshot"))
                except Exception as exc:
                    report["captureError"] = str(exc)
                try:
                    process.run(
                        ["/usr/sbin/screencapture", "-x", str(EVIDENCE / "desktop.png")], timeout=15
                    )
                except Exception as exc:
                    report["screenshotError"] = str(exc)
                try:
                    app.terminate()
                    try:
                        app.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        app.kill()
                        app.wait(timeout=10)
                    report["exitCode"] = app.poll()
                except (OSError, subprocess.TimeoutExpired) as exc:
                    report["cleanupError"] = str(exc)
                    report["responseAsserted"] = False
            try:
                if raw_log.exists():
                    save("native.log", raw_log.read_text(errors="replace"))
                    raw_log.unlink()
            except OSError as exc:
                report["logError"] = str(exc)
                report["responseAsserted"] = False
            if task:
                report["passed"] = report["responseAsserted"]
                try:
                    tasks.capture_sessions(sys.modules[__name__], task)
                except OSError as exc:
                    report["sessionCaptureError"] = str(exc)
            save("result.json", json.dumps(report, indent=2))
            print(
                "Cowork inference passed"
                if report["responseAsserted"]
                else "Cowork probe failed; see evidence"
            )
        finally:
            EVIDENCE = baseline_evidence
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
    return 0 if report["responseAsserted"] else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "inference"))
