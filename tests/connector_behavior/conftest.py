"""Explicit workspace/model inputs for live connector-skill behavior tests."""

import httpx
import pytest

from ucode.databricks import get_databricks_token


def pytest_addoption(parser):
    group = parser.getgroup("connector behavior")
    group.addoption("--workspace", help="Databricks workspace URL")
    group.addoption("--profile", help="Explicit Databricks CLI profile")
    group.addoption("--model", help="Exact Claude model-service name")


def pytest_report_header(config):
    return f"Connector behavior model: {config.getoption('model') or '(required)'}"


@pytest.fixture(scope="session")
def claude_messages(request):
    options = {name: request.config.getoption(name) for name in ("workspace", "profile", "model")}
    for name, value in options.items():
        if not value:
            raise pytest.UsageError(f"Connector behavior tests require --{name}")
    token = get_databricks_token(options["workspace"], options["profile"])
    with httpx.Client(
        base_url=options["workspace"].rstrip("/") + "/ai-gateway/anthropic/",
        headers={"Authorization": f"Bearer {token}", "Anthropic-Version": "2023-06-01"},
        timeout=60,
    ) as client:

        def messages(system, conversation, tools):
            response = client.post(
                "v1/messages",
                json={
                    "model": options["model"],
                    "max_tokens": 1024,
                    "temperature": 0,
                    "system": system,
                    "messages": conversation,
                    "tools": tools,
                },
            )
            response.raise_for_status()
            return response.json()

        yield messages
