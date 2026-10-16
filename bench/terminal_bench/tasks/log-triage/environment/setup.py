"""Generate deterministic service logs under <app>/logs."""

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

SERVICES = ["auth", "billing", "gateway", "search", "storage"]
CODES = ["E101", "E202", "E303", "E404", "E505", "E606"]
MESSAGES = {
    "INFO": ["request served", "cache warmed", "heartbeat ok", "config reloaded"],
    "WARN": ["slow upstream", "retrying request", "queue depth high"],
    "ERROR": ["upstream failed", "timeout", "bad payload", "disk full"],
}


def main(app: Path) -> None:
    rng = random.Random(4242)
    start = datetime(2026, 9, 1)
    for service in SERVICES:
        # Each service favors a different error code so the answer isn't uniform.
        weights = [rng.randint(1, 4) for _ in CODES]
        weights[rng.randrange(len(CODES))] += 8
        for day in range(4):
            date = start + timedelta(days=day)
            lines = []
            for i in range(rng.randint(250, 400)):
                ts = date + timedelta(seconds=i * 37 + rng.randint(0, 30))
                level = rng.choices(["INFO", "WARN", "ERROR"], [80, 12, 8])[0]
                message = rng.choice(MESSAGES[level])
                code = f" code={rng.choices(CODES, weights)[0]}" if level == "ERROR" else ""
                lines.append(f"{ts.isoformat()} {level:<5} [{service}] {message}{code}")
            out = app / "logs" / service / f"{date:%Y-%m-%d}.log"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(lines) + "\n", newline="\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
