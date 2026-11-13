"""Mint or revoke the short-lived PAT behind the Desktop jobs' `desktop-ci` profile.

Desktop starts MCP connectors with a near-empty environment, so the job's DATABRICKS_BEARER never
reaches UG's connector proxy. A PAT profile in ~/.databrickscfg is read from disk instead, as a
user's CLI login would be. `create` mints the PAT with the job's bearer and writes the profile;
`revoke` deletes it. Only the token id is kept in STATE.
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

LIFETIME_SECONDS = 2 * 60 * 60
COMMENT = "ug-ci-cowork-desktop"


def call(path, payload):
    workspace = os.environ["UCODE_TEST_WORKSPACE"].rstrip("/")
    request = urllib.request.Request(
        f"{workspace}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['DATABRICKS_BEARER']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def create(config, profile, state):
    created = call(
        "/api/2.0/token/create", {"lifetime_seconds": LIFETIME_SECONDS, "comment": COMMENT}
    )
    state.write_text(created["token_info"]["token_id"])
    workspace = os.environ["UCODE_TEST_WORKSPACE"].rstrip("/")
    config.write_text(
        f"[{profile}]\nhost = {workspace}\nauth_type = pat\ntoken = {created['token_value']}\n"
    )
    if os.name != "nt":
        config.chmod(0o600)
    # Let the job redact the new credential from its evidence.
    print(created["token_value"])


def revoke(state):
    if not state.exists():
        return
    call("/api/2.0/token/delete", {"token_id": state.read_text().strip()})
    state.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "revoke"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile", default="desktop-ci")
    args = parser.parse_args()
    if args.action == "create":
        create(args.config, args.profile, args.state)
    else:
        revoke(args.state)


if __name__ == "__main__":
    sys.exit(main())
