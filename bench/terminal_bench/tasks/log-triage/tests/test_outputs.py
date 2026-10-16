"""Verify report.json against counts recomputed from the logs."""

import json
import os
import re
from collections import Counter
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))
LINE = re.compile(r"^\S+ (INFO|WARN|ERROR)\s+\[(\w+)\] .*?(?: code=(E\d+))?$")


def expected() -> dict:
    report = {}
    for service_dir in sorted((APP / "logs").iterdir()):
        levels: Counter[str] = Counter()
        codes: Counter[str] = Counter()
        for log in service_dir.glob("*.log"):
            for line in log.read_text().splitlines():
                match = LINE.match(line)
                assert match, line
                levels[match[1]] += 1
                if match[3]:
                    codes[match[3]] += 1
        top = max(sorted(codes), key=lambda code: codes[code])
        report[service_dir.name] = {
            "errors": levels["ERROR"],
            "warnings": levels["WARN"],
            "top_error_code": top,
        }
    return report


def main() -> None:
    actual = json.loads((APP / "report.json").read_text())
    want = expected()
    assert actual == want, {"expected": want, "actual": actual}
    print("ok")


if __name__ == "__main__":
    main()
