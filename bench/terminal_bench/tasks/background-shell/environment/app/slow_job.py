"""A slow job. It waits for its token in ack.txt, then prints its result once it finishes."""

import hashlib
import secrets
import sys
import time
from pathlib import Path

app = Path(__file__).resolve().parent
token = secrets.token_hex(4)
result = secrets.token_hex(8)
state = app / ".job_state"
state.mkdir(exist_ok=True)
(state / "started").write_text(str(time.time()))
print(f"TOKEN={token}", flush=True)
ack = app / "ack.txt"
deadline = time.monotonic() + 180
while not (ack.exists() and ack.read_text().strip() == token):
    if time.monotonic() > deadline:
        sys.exit("no ack.txt with the token after 3 minutes")
    time.sleep(1)
(state / "acked").write_text(str(time.time()))
time.sleep(90)
(state / "result.sha256").write_text(hashlib.sha256(result.encode()).hexdigest())
print(f"RESULT={result}", flush=True)
