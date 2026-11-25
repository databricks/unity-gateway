"""Read managed model quota settings without printing credentials or workspace details."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

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
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None
    except urllib.error.URLError:
        return "connection-error", None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "invalid-json", None


def main():
    host = os.environ["UCODE_TEST_WORKSPACE"]
    caller = os.environ["DATABRICKS_CLIENT_ID"]
    from run_integration import mint_m2m_token

    try:
        token = mint_m2m_token(host, caller, os.environ["DATABRICKS_CLIENT_SECRET"])
    except (RuntimeError, urllib.error.URLError):
        print(json.dumps({"quota_diagnostics": "authentication-unavailable"}))
        return
    models = CLAUDE_MODELS if os.environ.get("AGENT") == "claude" else CODEX_MODELS
    for model in models:
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
        privileges = []
        for assignment in (permission_data or {}).get("privilege_assignments", []):
            if assignment.get("principal") == caller:
                privileges.extend(assignment.get("privileges", []))
        print(
            json.dumps(
                {
                    "model": service,
                    "model_service_status": service_status,
                    "model_service_rate_limits": summarize_service(service_data or {}, caller),
                    "permissions_status": permission_status,
                    "caller_privileges": privileges,
                    "serving_endpoint_status": endpoint_status,
                    "serving_endpoint_rate_limits": summarize_endpoint(endpoint_data or {}, caller),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
