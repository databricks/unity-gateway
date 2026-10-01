"""Plan or provision the persistent UC resources for the CUJ3 schema-pointer test.

This is an explicit, idempotent workspace operation. It never updates or deletes an
existing resource, and rejects existing services whose routing or selectors differ.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UC = "/api/2.1/unity-catalog"
SCHEMAS = ("models", "other_models", "tools", "other_tools", "skills", "other_skills")
MODEL_LEAVES = (
    "claude_sonnet",
    "claude_extra",
    "codex_primary",
    "codex_extra",
    "claude_decoy",
    "codex_decoy",
)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SOURCE = re.compile(r"system\.ai\.[A-Za-z0-9_-]+\Z")


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int):
        super().__init__(f"{method} {path.split('?', 1)[0]} returned HTTP {status}")
        self.status = status


class Client:
    def __init__(self, workspace: str, bearer: str):
        parsed = urllib.parse.urlsplit(workspace)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("--workspace must be an HTTPS workspace origin")
        self.origin = f"https://{parsed.netloc}"
        self.bearer = bearer

    def request(self, method: str, path: str, *, params=None, body=None, raw=None):
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Authorization": f"Bearer {self.bearer}"}
        if data is not None:
            headers["Content-Type"] = (
                "application/octet-stream" if raw is not None else "application/json"
            )
        request = urllib.request.Request(
            self.origin + path + query, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise ApiError(method, path, error.code) from None
        return json.loads(payload) if payload and raw is None else None

    def get_optional(self, path: str):
        try:
            return self.request("GET", path)
        except ApiError as error:
            if error.status == 404:
                return None
            raise


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


def ensure_catalog_and_schemas(client: Client, apply: bool):
    if client.get_optional(f"{UC}/catalogs/ug_e2e") is None:
        require(apply, "Missing catalog ug_e2e; rerun with --apply after review")
        client.request(
            "POST", f"{UC}/catalogs", body={"name": "ug_e2e", "comment": "UG CUJ fixtures"}
        )
    for schema in SCHEMAS:
        fqn = f"ug_e2e.{schema}"
        if client.get_optional(f"{UC}/schemas/{fqn}") is None:
            require(apply, f"Missing schema {fqn}; rerun with --apply after review")
            client.request(
                "POST",
                f"{UC}/schemas",
                body={"name": schema, "catalog_name": "ug_e2e", "comment": "UG CUJ fixture scope"},
            )
        print(f"schema {fqn}: ready")


def model_body(source: str):
    return {
        "config": {
            "routing": {
                "destinations": [
                    {
                        "name": source,
                        "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
                        "payPerTokenConfig": {"model": f"models/{source}"},
                    }
                ]
            },
            "usageTracking": {"enabled": True},
        }
    }


def ensure_model(client: Client, schema: str, leaf: str, source: str, apply: bool):
    fqn = f"ug_e2e.{schema}.{leaf}"
    existing = client.get_optional(f"{UC}/model-services/{fqn}")
    if existing is None:
        require(apply, f"Missing model service {fqn}; rerun with --apply after review")
        client.request(
            "POST",
            f"{UC}/model-services",
            params={"parent": f"schemas/ug_e2e.{schema}", "model_service_id": leaf},
            body=model_body(source),
        )
        existing = client.request("GET", f"{UC}/model-services/{fqn}")
    destinations = (((existing or {}).get("config") or {}).get("routing") or {}).get(
        "destinations"
    ) or []
    require(
        len(destinations) == 1 and destinations[0].get("name") == source,
        f"Model service {fqn} has unexpected routing",
    )
    print(f"model service {fqn}: {source}")


def ensure_mcp(client: Client, schema: str, leaf: str, connection: str, selector: str, apply: bool):
    fqn = f"ug_e2e.{schema}.{leaf}"
    existing = client.get_optional(f"{UC}/mcp-services/{fqn}")
    if existing is None:
        require(apply, f"Missing MCP service {fqn}; rerun with --apply after review")
        client.request(
            "POST",
            f"{UC}/mcp-services",
            params={"parent": f"schemas/ug_e2e.{schema}", "mcp_service_id": leaf},
            body={
                "mcp_service_type": "MCP_SERVICE_TYPE_EXTERNAL_MCP",
                "config": {
                    "source_connection": {"name": f"connections/{connection}"},
                    "include_tool_selectors": [selector],
                },
            },
        )
        existing = client.request("GET", f"{UC}/mcp-services/{fqn}")
    config = (existing or {}).get("config") or {}
    require(
        config.get("include_tool_selectors") == [selector],
        f"MCP service {fqn} has unexpected tool selectors",
    )
    require(
        (config.get("source_connection") or {}).get("name") == f"connections/{connection}",
        f"MCP service {fqn} uses another connection",
    )
    print(f"MCP service {fqn}: {selector}")


def ensure_skill(client: Client, schema: str, leaf: str, apply: bool):
    fqn = f"ug_e2e.{schema}.{leaf}"
    bundle = ROOT / "skills" / leaf / "SKILL.md"
    contents = bundle.read_bytes()
    existing = client.get_optional(f"{UC}/skills/{fqn}")
    if existing is None:
        require(apply, f"Missing skill {fqn}; rerun with --apply after review")
        client.request(
            "POST",
            f"{UC}/skills",
            params={"parent": f"schemas/ug_e2e.{schema}", "skill_id": leaf},
            body={},
        )
        path = f"/api/2.0/fs/files/Skills/ug_e2e/{schema}/{leaf}/SKILL.md"
        client.request("PUT", path, raw=contents)
        client.request("POST", f"{UC}/skills/{fqn}/finalize")
        existing = client.request("GET", f"{UC}/skills/{fqn}")
    require(
        existing.get("bundle_name") == leaf and existing.get("finalize_time"),
        f"Skill {fqn} is not finalized with the expected name",
    )
    print(f"skill {fqn}: ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Exact HTTPS workspace origin")
    parser.add_argument(
        "--bearer-env",
        default="DATABRICKS_BEARER",
        help="Environment variable containing a bearer token",
    )
    parser.add_argument(
        "--connection",
        required=True,
        help="Existing catalog.schema.connection to the fixture app /mcp",
    )
    for leaf in MODEL_LEAVES:
        parser.add_argument(
            f"--{leaf.replace('_', '-')}-source", required=True, help="Existing system.ai model FQN"
        )
    parser.add_argument(
        "--apply", action="store_true", help="Create missing resources; otherwise validate only"
    )
    args = parser.parse_args()
    bearer = os.environ.get(args.bearer_env, "")
    require(bool(bearer), f"{args.bearer_env} must contain an explicit workspace bearer")
    require(
        len(args.connection.split(".")) == 3
        and all(IDENT.fullmatch(p) for p in args.connection.split(".")),
        "--connection must be catalog.schema.connection",
    )
    sources = {leaf: getattr(args, f"{leaf}_source") for leaf in MODEL_LEAVES}
    require(
        all(SOURCE.fullmatch(source) for source in sources.values()),
        "Every model source must be an existing system.ai model FQN",
    )
    client = Client(args.workspace, bearer)
    ensure_catalog_and_schemas(client, args.apply)
    require(
        client.get_optional(f"{UC}/connections/{args.connection}") is not None,
        f"Connection {args.connection} must already exist",
    )
    for leaf in MODEL_LEAVES:
        schema = "other_models" if leaf.endswith("decoy") else "models"
        ensure_model(client, schema, leaf, sources[leaf], args.apply)
    for schema, leaf, selector in (
        ("tools", "fixture_reader", "read_fixture"),
        ("tools", "fixture_metadata", "describe_fixture"),
        ("other_tools", "fixture_decoy", "decoy_status"),
    ):
        ensure_mcp(client, schema, leaf, args.connection, selector, args.apply)
    for schema, leaf in (
        ("skills", "fixture-summary"),
        ("skills", "fixture-audit"),
        ("other_skills", "fixture-decoy"),
    ):
        ensure_skill(client, schema, leaf, args.apply)
    print("CUJ fixture inventory is ready")


if __name__ == "__main__":
    try:
        main()
    except (ApiError, RuntimeError, ValueError) as error:
        print(f"fixture provisioning failed: {error}", file=sys.stderr)
        sys.exit(1)
