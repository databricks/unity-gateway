"""Plan seven CUJ3 models offline; authenticate only for --validate or --apply."""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from databricks.sdk.errors import DatabricksError, NotFound
from requests.exceptions import RequestException

UC = "/api/2.1/unity-catalog"
SCHEMAS = ("models", "other_models")
MODEL_SCHEMAS = {
    "gpt_luna": "models",
    "claude_haiku": "models",
    "claude_sonnet": "models",
    "kimi": "models",
    "gemini_flash": "models",
    "claude_decoy": "other_models",
    "codex_decoy": "other_models",
}
MODEL_LEAVES = tuple(MODEL_SCHEMAS)
SOURCE = re.compile(r"system\.ai\.[A-Za-z0-9_-]+\Z")
DESTINATION_TYPE = "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL"


def workspace_origin(workspace: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(workspace)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError(
            "--workspace must be an HTTPS workspace origin without credentials"
        ) from None
    if (
        not isinstance(workspace, str)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (port is not None and not 0 < port <= 65535)
        or re.search(r"[\s\\]", workspace)
    ):
        raise ValueError("--workspace must be an HTTPS workspace origin without credentials")
    return f"https://{parsed.netloc}"


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


def bearer_auth(bearer: str):
    def reject_redirect(response, **kwargs):
        if 300 <= response.status_code < 400:
            response.close()
            raise AssertionError(
                f"Workspace request redirected with HTTP {response.status_code}"
            ) from None
        if (
            200 <= response.status_code < 300
            and response.request.method == "GET"
            and not isinstance(response.json(), dict)
        ):
            raise AssertionError("Workspace GET returned a non-object JSON response") from None
        return response

    def authenticate(request):
        request.headers["Authorization"] = f"Bearer {bearer}"
        request.register_hook("response", reject_redirect)
        return request

    return authenticate


def _request(api_client, auth, method: str, path: str, *, query=None, body=None, missing_ok=False):
    try:
        return api_client.do(method, path, query=query, body=body, auth=auth)
    except NotFound:
        if missing_ok:
            return None
        raise RuntimeError("Workspace API request failed") from None
    except DatabricksError:
        raise RuntimeError("Workspace API request failed") from None
    except (RequestException, OSError, ValueError):
        raise RuntimeError("Workspace request failed; check connectivity and TLS") from None


def _get_optional(api_client, auth, path: str):
    return _request(api_client, auth, "GET", path, missing_ok=True)


def workspace_client(origin: str, bearer: str):
    try:
        return WorkspaceClient(
            config=Config(
                host=origin,
                token=bearer,
                auth_type="pat",
                http_timeout_seconds=30,
                retry_timeout_seconds=1,
            )
        )
    except (DatabricksError, RequestException, OSError, ValueError):
        raise RuntimeError("Workspace SDK client initialization failed") from None


def model_body(source: str):
    return {
        "config": {
            "routing": {
                "destinations": [
                    {
                        "name": source,
                        "destination_type": DESTINATION_TYPE,
                        "pay_per_token_config": {"model": f"models/{source}"},
                        "traffic_percentage": 100,
                    }
                ]
            },
        }
    }


def validate_model(existing: dict, fqn: str, source: str):
    message = f"Model service {fqn} has unexpected routing; refusing to overwrite it"
    require(isinstance(existing, dict) and isinstance(existing.get("config"), dict), message)
    routing = existing["config"].get("routing")
    require(isinstance(routing, dict), message)
    require(routing.get("fallback") in (None, {}, {"destinations": []}), message)
    destinations = routing.get("destinations")
    require(isinstance(destinations, list) and len(destinations) == 1, message)
    destination = destinations[0]
    require(isinstance(destination, dict), message)
    target = destination.get("pay_per_token_config")
    traffic = destination.get("traffic_percentage", 100)
    require(
        destination.get("name") == source
        and destination.get("destination_type") == DESTINATION_TYPE
        and isinstance(target, dict)
        and target.get("model") == f"models/{source}"
        and type(traffic) is int
        and traffic == 100
        and all(
            destination.get(field, False) is False
            for field in ("is_deleted", "is_disabled", "disabled")
        ),
        message,
    )


def print_plan(sources: dict[str, str]):
    print("catalog ug_e2e: ensure only if missing")
    for schema in SCHEMAS:
        print(f"schema ug_e2e.{schema}: ensure only if missing")
    for leaf in MODEL_LEAVES:
        schema = MODEL_SCHEMAS[leaf]
        print(f"model service ug_e2e.{schema}.{leaf}: planned -> {sources[leaf]}")
    print(
        "Offline dry plan: no network or writes; no bearer required; existing inventory not checked"
    )


def provision(api_client, sources: dict[str, str], apply: bool, bearer: str):
    auth = bearer_auth(bearer)
    for source in sorted(set(sources.values())):
        registered_model = _get_optional(api_client, auth, f"{UC}/models/{source}")
        require(
            isinstance(registered_model, dict) and registered_model.get("full_name") == source,
            f"Source registered model {source} is missing or has an unexpected full_name; "
            "supply an exact canonical registered-model FQN, not a model-service alias; "
            "no writes performed",
        )
        print(f"source registered model {source}: validated")
    missing = []
    if _get_optional(api_client, auth, f"{UC}/catalogs/ug_e2e") is None:
        missing.append((f"{UC}/catalogs", None, {"name": "ug_e2e", "comment": "UG CUJ fixtures"}))
        print("catalog ug_e2e: would create")
    else:
        print("catalog ug_e2e: existing")
    for schema in SCHEMAS:
        fqn = f"ug_e2e.{schema}"
        if _get_optional(api_client, auth, f"{UC}/schemas/{fqn}") is None:
            missing.append(
                (
                    f"{UC}/schemas",
                    None,
                    {"name": schema, "catalog_name": "ug_e2e", "comment": "UG model fixture scope"},
                )
            )
            print(f"schema {fqn}: would create")
        else:
            print(f"schema {fqn}: existing")
    for leaf in MODEL_LEAVES:
        schema = MODEL_SCHEMAS[leaf]
        fqn = f"ug_e2e.{schema}.{leaf}"
        source = sources[leaf]
        existing = _get_optional(api_client, auth, f"{UC}/model-services/{fqn}")
        if existing is None:
            missing.append(
                (
                    f"{UC}/model-services",
                    {"parent": f"schemas/ug_e2e.{schema}", "model_service_id": leaf},
                    model_body(source),
                )
            )
            print(f"model service {fqn}: would create -> {source}")
        else:
            validate_model(existing, fqn, source)
            print(f"model service {fqn}: validated -> {source}")
    if not apply:
        print(f"Validation: {len(missing)} missing resources; no writes performed")
        require(not missing, "Inventory is incomplete; review the offline plan before --apply")
        return
    for path, params, body in missing:
        _request(api_client, auth, "POST", path, query=params, body=body)
    for leaf in MODEL_LEAVES:
        schema = MODEL_SCHEMAS[leaf]
        fqn = f"ug_e2e.{schema}.{leaf}"
        validate_model(
            _request(api_client, auth, "GET", f"{UC}/model-services/{fqn}"),
            fqn,
            sources[leaf],
        )
    print("Model-only fixture inventory is ready; managed config was not published")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Exact HTTPS workspace origin")
    parser.add_argument(
        "--bearer-env",
        default="DATABRICKS_BEARER",
        help="Environment variable containing an explicit workspace bearer token",
    )
    for leaf in MODEL_LEAVES:
        parser.add_argument(
            f"--{leaf.replace('_', '-')}-source",
            required=True,
            help="Canonical existing system.ai registered-model FQN, not a model-service alias",
        )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--validate",
        action="store_true",
        help="Authenticate and validate existing resources; GET only",
    )
    modes.add_argument("--apply", action="store_true", help="Create missing resources after review")
    args = parser.parse_args()
    origin = workspace_origin(args.workspace)
    sources = {leaf: getattr(args, f"{leaf}_source") for leaf in MODEL_LEAVES}
    require(
        all(SOURCE.fullmatch(source) for source in sources.values()),
        "Every model source must be an existing system.ai model FQN",
    )
    print(f"workspace: {origin}")
    if not args.validate and not args.apply:
        print_plan(sources)
        return
    bearer = os.environ.get(args.bearer_env, "")
    require(bool(bearer.strip()), "Bearer environment variable must contain a workspace token")
    require(
        not any(character.isspace() for character in bearer), "Bearer must not contain whitespace"
    )
    client = workspace_client(origin, bearer)
    provision(client.api_client, sources, args.apply, bearer=bearer)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, ValueError) as error:
        print(f"model fixture provisioning failed: {error}", file=sys.stderr)
        sys.exit(1)
