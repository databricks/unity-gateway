"""Read published workspace configuration and live model catalogs through the SDK."""

import re

from databricks.sdk import WorkspaceClient

from .constants import CLAUDE, CODEX
from .evidence import canonical_model


class Workspace:
    OUTPUT_FIELDS = {
        "workspace_id",
        "create_time",
        "created_user_id",
        "update_time",
        "updated_user_id",
        "retrieved_time",
    }

    def __init__(self, client: WorkspaceClient):
        self.client = client
        self.url = client.config.host.rstrip("/")

    @classmethod
    def policy(cls, config):
        return {key: value for key, value in config.items() if key not in cls.OUTPUT_FIELDS}

    @staticmethod
    def agent_configs(config):
        """Each enabled agent's settings, keyed by its CodingAgent value."""
        entries = config["enabled_agents"]
        configs = {entry["agent"]: entry["config"] for entry in entries}
        assert len(configs) == len(entries), (
            f"Duplicate enabled agents: {[entry['agent'] for entry in entries]}"
        )
        return configs

    def config(self):
        result = self.client.api_client.do("GET", "/api/ai-gateway/v2/coding-agent-configs")
        configs = result.get("coding_agent_configs", [])
        assert len(configs) == 1 and not result.get("next_page_token"), (
            "CUJ requires exactly one published CodingAgentConfig"
        )
        config = self.policy(configs[0])
        assert re.fullmatch(r"coding-agent-configs/[\w-]+", config.get("name", ""))
        return config

    def assert_unchanged(self, expected):
        assert self.config() == expected, "Published workspace configuration changed during the CUJ"

    def model_ids(self, agent):
        if agent == CLAUDE:
            prefix = "system.ai.claude-"
        elif agent == CODEX:
            prefix = "system.ai.gpt-"
        else:
            raise ValueError(f"Unsupported agent: {agent!r}")

        models, seen, token = set(), set(), None
        for _ in range(100):
            params = {"parent": "schemas/system.ai", "page_size": "100"}
            if token:
                params["page_token"] = token
            result = self.client.api_client.do(
                "GET", "/api/2.1/unity-catalog/model-services", query=params
            )
            models.update(
                row["name"].removeprefix("model-services/")
                for row in result.get("model_services", [])
            )
            token = result.get("next_page_token")
            if not token:
                break
            assert token not in seen, "Catalog pagination repeated a token"
            seen.add(token)
        else:
            raise AssertionError("Catalog pagination exceeded 100 pages")
        models = {model for model in models if model.startswith(prefix)}
        if agent == CLAUDE:
            result = self.client.api_client.do(
                "GET", "/ai-gateway/anthropic/v1/models", query={"limit": 1000}
            )
            assert not result.get("has_more"), "Anthropic catalog pagination needs implementation"
            models &= {canonical_model(row["id"]) for row in result["data"]}
        assert models, "Live catalog is empty"
        return models
