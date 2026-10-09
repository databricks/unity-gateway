"""A slow job. It prints its result to stdout only once it finishes."""

import hashlib
import secrets
import time
from pathlib import Path

result = secrets.token_hex(8)
state = Path(__file__).resolve().parent / ".job_state"
state.mkdir(exist_ok=True)
(state / "started").write_text(str(time.time()))
time.sleep(150)
(state / "result.sha256").write_text(hashlib.sha256(result.encode()).hexdigest())
print(f"RESULT={result}", flush=True)
