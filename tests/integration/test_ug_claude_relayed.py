"""CUJ: a relayed (subscription-relay) Claude session reaching both its subscription models and,
via the hybrid re-route, Databricks-hosted models."""

import json
import urllib.request

import pytest
from utils.evidence import FileTask

pytestmark = [pytest.mark.live, pytest.mark.claude]

# Anthropic gateway model catalog; a Databricks-hosted id here is namespace-qualified, which is
# what the relayed proxy re-routes to gateway auth (bare Anthropic ids stay on the relay path).
ANTHROPIC_MODELS_PATH = "/ai-gateway/anthropic/v1/models"


def _databricks_hosted_model(workspace: str, token: str) -> str:
    """A gateway-served, namespace-qualified model id (prefer the cheapest Claude tier).

    Discovered from the live gateway catalog rather than hardcoded, mirroring how the explicit-model
    CUJs record a real discovered model. Excludes `anthropic-aigw-*` aliases: they need their
    provider-service header to route, which the Databricks re-route drops, so a direct call 404s."""
    request = urllib.request.Request(
        workspace + ANTHROPIC_MODELS_PATH, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        ids = [entry.get("id") for entry in json.load(response).get("data", [])]
    hosted = [
        model for model in ids if model and "." in model and not model.startswith("anthropic-aigw-")
    ]
    haiku = [model for model in hosted if "haiku" in model]
    model = (haiku or hosted or [""])[0]
    assert model, f"gateway served no Databricks-hosted model id to route; catalog was {ids}"
    return model


def test_ug_claude_relayed_serves_subscription_and_databricks_models(
    live_session, workspace, claude_relayed_provider, claude_oauth_token
):
    """Scenario: launch Claude relayed through a subscription-relay MPS and submit a headless
    prompt on two models — a bare Anthropic id the subscription serves, then a Databricks-hosted
    `system.ai` id.

    Expected: the relayed launch runs headless (the OAuth token stands in for the browser login).
    The bare model completes the file task over the relay path (route=relay); the Databricks-hosted
    model completes it too because the loopback proxy re-routes that request to gateway auth
    (route=databricks). One relayed session reaches both.
    """
    session = live_session
    databricks_model = _databricks_hosted_model(workspace, session.env["DATABRICKS_BEARER"])
    session.record(
        "model.json", {"model": databricks_model, "source": "gateway /v1/models discovery"}
    )

    # Establish the workspace with the real CLI; each launch below overrides the provider.
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspaces",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    # Headless relayed launches: the OAuth token stands in for the browser subscription login, and
    # the diagnostics flag makes the proxy log its per-request route decision to stderr.
    session.env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_oauth_token
    session.env["UCODE_RELAYED_PROXY_DIAGNOSTICS"] = "1"

    def relayed_file_task(model):
        task = FileTask(session)
        result = session.run(
            "claude",
            "--provider",
            claude_relayed_provider,
            "--model",
            model,
            "--",
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            timeout=240,
        )
        task.assert_headless_answer("claude", result)
        return result

    # Relay path: a bare Anthropic id, served directly by the subscription.
    relay = relayed_file_task("haiku")
    assert '"route":"relay"' in relay.stderr, (
        f"Relayed launch did not use the subscription relay path for a bare model:\n{relay.stderr}"
    )

    # Hybrid re-route: a Databricks-hosted id, re-routed to gateway auth in the same relayed mode.
    databricks = relayed_file_task(databricks_model)
    assert '"route":"databricks"' in databricks.stderr, (
        "Relayed proxy did not re-route the Databricks-hosted model to gateway auth; "
        f"no route=databricks diagnostic in:\n{databricks.stderr}"
    )
