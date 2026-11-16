import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from diagnostics import redact

from ucode.os_compatibility import subprocess_cross_os as process

PROBE_TIMEOUT_SECONDS = 900
PROBE_CLEANUP_SECONDS = 90
REQUIRED_PROBE_ENVIRONMENT = (
    "UCODE_TEST_WORKSPACE",
    "DATABRICKS_BEARER",
    "RUNNER_TEMP",
    "GITHUB_ACTIONS",
    "PATH",
)
OPTIONAL_PROBE_ENVIRONMENT = (
    "UG_COWORK_PROVIDER",
    "UG_COWORK_MODEL",
    "UG_COWORK_RELAY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
TERMINAL_TIMEOUT_SECONDS = PROBE_TIMEOUT_SECONDS + PROBE_CLEANUP_SECONDS + 30


def stop_process_group(private):
    pid_file = private / "pid.txt"
    if not pid_file.exists():
        return
    group = int(pid_file.read_text())
    if group <= 1 or group == os.getpgrp():
        raise RuntimeError("Refusing to terminate the runner process group")
    status = private / "status.txt"
    if not status.exists():
        try:
            os.kill(group, signal.SIGTERM)
            deadline = time.monotonic() + PROBE_CLEANUP_SECONDS
            while time.monotonic() < deadline and not status.exists():
                os.kill(group, 0)
                time.sleep(0.1)
        except ProcessLookupError:
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        if sig == signal.SIGTERM:
            time.sleep(2)


def cleanup(private, failure, capability="inference"):
    errors = []
    try:
        stop_process_group(private)
    except (OSError, ValueError, RuntimeError) as exc:
        errors.append(str(exc))
    evidence = Path(os.environ["RUNNER_TEMP"]) / "macos-cowork-evidence"
    if capability != "inference":
        evidence /= capability
    try:
        evidence.mkdir(parents=True, exist_ok=True)
        for source, destination in (("output.txt", "terminal.txt"), ("native.log", "native.log")):
            try:
                path = private / source
                if path.exists():
                    (evidence / destination).write_text(redact(path.read_text(errors="replace")))
            except OSError as exc:
                errors.append(str(exc))
        if failure or errors:
            with (evidence / "terminal.txt").open("a") as log:
                log.write(redact("\n".join(filter(None, [failure, *errors]))))
    except OSError as exc:
        errors.append(str(exc))
    finally:
        try:
            shutil.rmtree(private)
        except OSError as exc:
            errors.append(str(exc))
    for error in errors:
        print(redact(f"Probe cleanup failed: {error}"), file=sys.stderr)
    return errors


def main():
    if sys.platform != "darwin":
        raise SystemExit("Use a disposable macOS GitHub Actions runner")
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    parser.add_argument("--capability", choices=("inference", "file", "skill"), default="inference")
    args = parser.parse_args()
    if args.child:
        environment = args.child
        os.environ.update(json.loads(environment.read_text()))
        environment.unlink()
        if os.getpgrp() != os.getpid():
            os.setsid()
        (environment.parent / "pid.txt").write_text(str(os.getpid()))
        os.execv(
            sys.executable,
            [
                sys.executable,
                str(Path(__file__).with_name("probe.py")),
                str(environment.parent),
                args.capability,
            ],
        )
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Use a disposable macOS GitHub Actions runner")
    return run_terminal(args.capability)


def run_terminal(capability="inference"):
    private = Path(tempfile.mkdtemp(prefix="macos-probe-", dir=os.environ["RUNNER_TEMP"]))
    environment = private / "environment.json"
    output = private / "output.txt"
    status = private / "status.txt"
    command = private / "probe.command"
    failure = ""
    try:
        environment.write_text(
            json.dumps(
                {key: os.environ[key] for key in REQUIRED_PROBE_ENVIRONMENT}
                | {key: os.environ[key] for key in OPTIONAL_PROBE_ENVIRONMENT if key in os.environ}
            )
        )
        environment.chmod(0o600)
        invocation = shlex.join(
            [
                sys.executable,
                str(Path(__file__).absolute()),
                "--child",
                str(environment),
                "--capability",
                capability,
            ]
        )
        command.write_text(
            "#!/bin/bash\n"
            "umask 077\n"
            f"{invocation} > {shlex.quote(str(output))} 2>&1\n"
            f"echo $? > {shlex.quote(str(status))}\n"
        )
        command.chmod(0o700)
        process.run(
            ["/usr/bin/open", "-a", "Terminal", str(command)],
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        deadline = time.monotonic() + TERMINAL_TIMEOUT_SECONDS
        reported = set()
        stages = {
            "UG gateway profile configured",
            "Gateway onboarding accepted",
            "Cowork task screen ready",
            "Cowork model selected",
            "Start task requested",
            "Cowork inference passed",
            "Cowork probe failed; see evidence",
        }
        while time.monotonic() < deadline:
            if output.exists():
                for line in output.read_text(errors="replace").splitlines():
                    if line in stages and line not in reported:
                        print(line, flush=True)
                        reported.add(line)
            if status.exists():
                result = status.read_text().strip()
                if result:
                    result = int(result)
                    break
            time.sleep(2)
        else:
            raise RuntimeError(
                f"Terminal probe did not finish within {TERMINAL_TIMEOUT_SECONDS} seconds"
            )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        failure = str(exc)
        raise
    finally:
        errors = cleanup(private, failure, capability)
    if errors:
        raise RuntimeError("Terminal probe cleanup failed; see diagnostics")
    return result


if __name__ == "__main__":
    sys.exit(main())
