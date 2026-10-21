"""Bearer command for the Copilot sign-in CUJs, run as ``pat_bearer.py MODE HOST STATE_DIR``.

``mint`` prints a fresh short-lived workspace PAT (ug's ``DATABRICKS_BEARER_COMMAND`` contract)
issued with ``UG_INTEGRATION_ISSUER_BEARER``, unless ``sign-out`` has run. ``revoke`` and
``sign-out`` delete every PAT minted so far, as a user revoking or signing out mid-session would,
then wait out the workspace's auth cache; ``cleanup`` deletes them at teardown. Token ids, never
token values, are kept in STATE_DIR.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

LIFETIME_SECONDS = 30
# The workspace keeps accepting a deleted token from its auth cache for up to a minute.
AUTH_CACHE_SECONDS = 60


def databricks(host: str, *args: str, check: bool = True) -> str:
    env = {**os.environ, "DATABRICKS_HOST": host}
    env["DATABRICKS_TOKEN"] = env.pop("UG_INTEGRATION_ISSUER_BEARER")
    env.pop("DATABRICKS_BEARER", None)
    env.pop("DATABRICKS_BEARER_COMMAND", None)
    result = subprocess.run(
        ["databricks", *args, "-o", "json"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    if check and result.returncode:
        sys.exit(f"databricks {args[0]} {args[1]} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def main() -> None:
    mode, host, state = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    minted = state / "minted"
    if mode == "mint":
        if (state / "signed-out").exists():
            sys.exit("signed out")
        info = json.loads(
            databricks(
                host,
                "tokens",
                "create",
                "--lifetime-seconds",
                str(LIFETIME_SECONDS),
                "--comment",
                "ug-integration-copilot",
            )
        )
        with minted.open("a", encoding="utf-8") as ids:
            ids.write(info["token_info"]["token_id"] + "\n")
        print(info["token_value"])
        return
    if mode == "sign-out":
        (state / "signed-out").touch()
    token_ids = minted.read_text(encoding="utf-8").split() if minted.exists() else []
    for token_id in token_ids:
        databricks(host, "tokens", "delete", token_id, check=mode != "cleanup")
    if mode != "cleanup":
        time.sleep(AUTH_CACHE_SECONDS + 3)
        (state / "revoked").write_text(str(len(token_ids)), encoding="utf-8")


if __name__ == "__main__":
    main()
