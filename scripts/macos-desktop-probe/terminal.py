import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

from ucode.os_compatibility import subprocess_cross_os as process


def main():
    if sys.platform != "darwin":
        raise SystemExit("Use a disposable macOS GitHub Actions runner")
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        environment = Path(sys.argv[2])
        os.environ.update(json.loads(environment.read_text()))
        environment.unlink()
        os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name("probe.py"))])
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Use a disposable macOS GitHub Actions runner")
    private = Path(tempfile.mkdtemp(prefix="macos-probe-", dir=os.environ["RUNNER_TEMP"]))
    environment = private / "environment.json"
    output = private / "output.txt"
    status = private / "status.txt"
    command = private / "probe.command"
    try:
        environment.write_text(
            json.dumps(
                {
                    key: os.environ[key]
                    for key in (
                        "UCODE_TEST_WORKSPACE",
                        "DATABRICKS_BEARER",
                        "RUNNER_TEMP",
                        "GITHUB_ACTIONS",
                        "PATH",
                    )
                }
            )
        )
        environment.chmod(0o600)
        invocation = shlex.join(
            [sys.executable, str(Path(__file__).absolute()), "--child", str(environment)]
        )
        command.write_text(
            "#!/bin/bash\n"
            f"{invocation} > {shlex.quote(str(output))} 2>&1\n"
            f"echo $? > {shlex.quote(str(status))}\n"
        )
        command.chmod(0o700)
        process.run(["/usr/bin/open", "-a", "Terminal", str(command)], check=True)
        deadline = time.monotonic() + 600
        reported = set()
        stages = {
            "UG gateway profile configured",
            "Gateway onboarding accepted",
            "Cowork selected",
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
                    return int(result)
            time.sleep(2)
        raise RuntimeError("Terminal probe did not finish within 600 seconds")
    finally:
        if output.exists():
            text = output.read_text(errors="replace").replace(
                os.environ["DATABRICKS_BEARER"], "[REDACTED]"
            )
            evidence = Path(os.environ["RUNNER_TEMP"]) / "macos-cowork-evidence"
            (evidence / "terminal.txt").write_text(text)
        shutil.rmtree(private)


if __name__ == "__main__":
    sys.exit(main())
