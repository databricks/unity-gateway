"""Read managed model quota settings without printing credentials or workspace details."""

import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

CODEX_MODELS = ("gpt-6-sol", "gpt-6-1-sol", "gpt-6-luna", "gpt-5-6-sol")
CLAUDE_MODELS = (
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "deepseek-v4-1-flash-1017",
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-fable-5-1",
)
RATE_FIELDS = ("key", "renewal_period", "requests", "tokens", "calls", "calls_per_minute")


def summarize_service(service, caller):
    result = []
    for limit in (service.get("config") or {}).get("rate_limits") or []:
        row = {key: limit[key] for key in RATE_FIELDS if key in limit}
        if "principal" in limit:
            row["applies_to_caller"] = limit["principal"] == caller
        result.append(row)
    return result


def summarize_endpoint(endpoint, caller):
    limits = (endpoint.get("rate_limits") or []) + (
        (endpoint.get("ai_gateway") or {}).get("rate_limits") or []
    )
    return summarize_service({"config": {"rate_limits": limits}}, caller)


def read_api(host, token, path):
    request = urllib.request.Request(
        host.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, None
    except http.client.IncompleteRead:
        return "incomplete-read", None
    except OSError:
        return "connection-error", None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "invalid-json", None


def provider_limits(host, token, provider, caller):
    path = "/api/2.1/unity-catalog/model-provider-services/" + urllib.parse.quote(
        provider, safe="."
    )
    status, payload = read_api(host, token, path)
    config = (payload or {}).get("config") or {}
    return {
        "status": status,
        "rate_limits_present": "rate_limits" in config if payload is not None else None,
        "rate_limits": summarize_service(payload or {}, caller),
    }


def probe_model(host, token, model, agent):
    if agent not in ("codex", "claude"):
        raise ValueError(f"Unknown probe agent: {agent}")
    path = (
        "/ai-gateway/codex/v1/responses"
        if agent == "codex"
        else "/ai-gateway/anthropic/v1/messages"
    )
    body = (
        {
            "model": "system.ai." + model,
            "input": "Reply OK.",
            "max_output_tokens": 16,
            "store": False,
        }
        if agent == "codex"
        else {
            "model": "system.ai." + model,
            "messages": [{"role": "user", "content": "Reply OK."}],
            "max_tokens": 16,
        }
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if agent == "claude":
        headers.update(
            {"Anthropic-Version": "2023-06-01", "x-databricks-use-coding-agent-mode": "true"}
        )

    def send(index):
        request = urllib.request.Request(
            host.rstrip("/") + path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        started = monotonic()
        status = "transport-error"
        response_headers = {}
        read_error = None
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                response_headers = response.headers
                response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            response_headers = error.headers or {}
            error.close()
        except http.client.IncompleteRead:
            read_error = "incomplete-read"
        except OSError:
            read_error = "transport-error"
        return {
            "model": "system.ai." + model,
            "agent": agent,
            "request": index,
            "status": status,
            "error": read_error,
            "latency_ms": round((monotonic() - started) * 1000),
            "headers": {
                name.lower(): value
                for name, value in response_headers.items()
                if name.lower() in {"retry-after", "x-request-id", "server"}
                or name.lower().startswith("x-ratelimit-")
            },
        }

    with ThreadPoolExecutor(max_workers=3) as executor:
        return list(executor.map(send, range(1, 4)))


def main(*, provider_only=False):
    host = os.environ["UCODE_TEST_WORKSPACE"]
    caller = os.environ["DATABRICKS_CLIENT_ID"]
    from run_integration import mint_m2m_token

    try:
        token = mint_m2m_token(host, caller, os.environ["DATABRICKS_CLIENT_SECRET"])
    except (RuntimeError, OSError, ValueError):
        print(json.dumps({"quota_diagnostics": "authentication-unavailable"}))
        return
    if provider_only:
        print(json.dumps(provider_limits(host, token, os.environ["UG_COWORK_PROVIDER"], caller)))
        return
    models = [(model, "codex") for model in CODEX_MODELS] + [
        (model, "claude") for model in CLAUDE_MODELS
    ]
    for model, agent in models:
        service = "system.ai." + model
        service_path = "/api/2.1/unity-catalog/model-services/" + urllib.parse.quote(service)
        service_status, service_data = read_api(host, token, service_path)
        permissions_path = (
            "/api/2.1/unity-catalog/effective-permissions/model_service/"
            + urllib.parse.quote(service)
            + "?"
            + urllib.parse.urlencode({"principal": caller})
        )
        permission_status, permission_data = read_api(host, token, permissions_path)
        endpoint_status, endpoint_data = read_api(
            host, token, "/api/2.0/serving-endpoints/" + urllib.parse.quote(model)
        )
        config = (service_data or {}).get("config") or {}
        rate_limits_present = "rate_limits" in config if service_data is not None else None
        privileges = []
        if isinstance(permission_data, dict):
            for assignment in permission_data.get("privilege_assignments") or []:
                if isinstance(assignment, dict) and assignment.get("principal") == caller:
                    privileges.extend(assignment.get("privileges") or [])
        print(
            json.dumps(
                {
                    "model": service,
                    "model_service_status": service_status,
                    "model_service_rate_limits_present": rate_limits_present,
                    "model_service_rate_limits": summarize_service(service_data or {}, caller),
                    "permissions_status": permission_status,
                    "caller_privileges": privileges,
                    "serving_endpoint_status": endpoint_status,
                    "serving_endpoint_rate_limits": summarize_endpoint(endpoint_data or {}, caller),
                }
            ),
            flush=True,
        )
        for result in probe_model(host, token, model, agent):
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--provider-only"]:
        main(provider_only=True)
    elif not sys.argv[1:]:
        main()
    else:
        raise SystemExit("Use --provider-only or no arguments")
