"""Verify the agent waited for the long job and captured its output."""

import hashlib
import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def main() -> None:
    state = APP / ".job_state"
    assert (state / "result.sha256").exists(), "slow_job.py never finished"
    value = (APP / "result.txt").read_text().strip().removeprefix("RESULT=")
    assert hashlib.sha256(value.encode()).hexdigest() == (state / "result.sha256").read_text()
    print("ok")


if __name__ == "__main__":
    main()
