import json
import os
import sys
import time
from pathlib import Path

from ucode.os_compatibility import subprocess_cross_os as process

MODEL = "system.ai.claude-haiku-4-5"
PROMPT = (
    "Reply with only the digits for forty-eight thousand two hundred thirteen. Do not use tools."
)
TEMP = Path(os.environ["RUNNER_TEMP"])
EVIDENCE = TEMP / "macos-cowork-evidence"


def save(name, text):
    bearer = os.environ.get("DATABRICKS_BEARER", "")
    if bearer:
        text = text.replace(bearer, "[REDACTED]")
    (EVIDENCE / name).write_text(text)


def ui(*args):
    result = process.run(
        ["/usr/bin/osascript", str(TEMP / "desktop-ui.scpt"), *args],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def wait_for(label, timeout=40):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            last = ui("snapshot")
            if any(value in last for value in ([label] if isinstance(label, str) else label)):
                return last
        except RuntimeError as exc:
            last = str(exc)
        time.sleep(2)
    save("last-ui.txt", last)
    raise RuntimeError(f"Timed out waiting for {label}")


def configure():
    workspace = os.environ.get("UCODE_TEST_WORKSPACE", "")
    if not workspace or not os.environ.get("DATABRICKS_BEARER"):
        raise RuntimeError("Existing CI workspace and bearer are required")
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
            "--skip-validate",
            "--skip-upgrade",
        ],
        capture_output=True,
        text=True,
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
    if profile["inferenceProvider"] != "gateway":
        raise RuntimeError("Expected gateway inference")
    headers = profile.get("inferenceCustomHeaders", {})
    if headers.get("Databricks-Model-Provider-Service"):
        raise RuntimeError("Expected system.ai model services")
    if headers.get("Databricks-Model-Service-Parent-Schema") not in (None, "", "system.ai"):
        raise RuntimeError("Expected system.ai schema")
    models = [m for m in profile["inferenceModels"] if m["name"] == MODEL]
    if len(models) != 1:
        raise RuntimeError("Expected system.ai model missing or ambiguous")
    label = models[0].get("labelOverride") or MODEL
    save("configuration.json", json.dumps({"provider": "gateway", "model": MODEL, "label": label}))
    return label


def main():
    if sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Use a disposable macOS GitHub Actions runner")
    EVIDENCE.mkdir(exist_ok=True)
    report = {"model": MODEL, "responseAsserted": False}
    app = None
    raw_log = TEMP / "claude-native.log"
    try:
        model_label = configure()
        with raw_log.open("w") as log:
            app = process.popen(
                [
                    "/Applications/Claude.app/Contents/MacOS/Claude",
                    "--force-renderer-accessibility",
                ],
                stdout=log,
                stderr=log,
            )
        snapshot = wait_for(("Get started", "Continue"), timeout=60)
        save("initial-ui.txt", snapshot)
        if "Get started" in snapshot:
            ui("click", "Get started")
        wait_for("Continue")
        ui("click", "Continue")
        wait_for("Cowork", timeout=60)
        ui("click", "Cowork")
        report["coworkActivated"] = True
        snapshot = wait_for("Write your prompt to Claude", timeout=60)
        save("cowork-ui.txt", snapshot)
        if f"Model: {model_label}" not in snapshot:
            raise RuntimeError("Cowork did not select the expected system.ai model")
        ui("type", "Write your prompt to Claude", PROMPT)
        time.sleep(1)
        ui("click", "Start task")
        report["promptSubmitted"] = True
        snapshot = wait_for("Claude finished the response", timeout=240)
        if "Claude responded: 48213" not in snapshot:
            raise RuntimeError("Completed response did not match expected answer")
        report.update(responseAsserted=True, responseFinished=True, expectedResponse="48213")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if app:
            try:
                save("final-ui.txt", ui("snapshot"))
            except Exception as exc:
                report["captureError"] = str(exc)
            process.run(
                ["/usr/sbin/screencapture", "-x", str(EVIDENCE / "desktop.png")], timeout=15
            )
            app.terminate()
            try:
                app.wait(timeout=10)
            except Exception:
                app.kill()
            report["exitCode"] = app.poll()
        if raw_log.exists():
            save("native.log", raw_log.read_text(errors="replace"))
        save("result.json", json.dumps(report, indent=2))
        print(
            "Cowork inference passed"
            if report["responseAsserted"]
            else "Cowork probe failed; see evidence"
        )
    return 0 if report["responseAsserted"] else 1


if __name__ == "__main__":
    sys.exit(main())
