"""Reference solution for log-triage."""

import json
import os
import re
from collections import Counter
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
report = {}
for service_dir in sorted((app / "logs").iterdir()):
    text = "".join(log.read_text() for log in service_dir.glob("*.log"))
    codes = Counter(re.findall(r" ERROR .* code=(E\d+)$", text, re.MULTILINE))
    report[service_dir.name] = {
        "errors": len(re.findall(r"^\S+ ERROR ", text, re.MULTILINE)),
        "warnings": len(re.findall(r"^\S+ WARN ", text, re.MULTILINE)),
        "top_error_code": min(codes, key=lambda code: (-codes[code], code)),
    }
(app / "report.json").write_text(json.dumps(report, indent=2))
