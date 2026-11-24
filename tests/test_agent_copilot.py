"""Tests for agents/copilot.py."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import httpx
import pytest

from ucode import custom_oauth
from ucode.agents import copilot, model_sources
from ucode.databricks import (
    AnthropicModelCatalog,
    DatabricksLoginRequiredError,
    ModelServiceEntry,
)
from ucode.telemetry import client_platform

WS = "https://example.databricks.com"
PROXY = "http://127.0.0.1:4321"
SONNET = "system.ai.claude-sonnet-5-5"
GPT = "system.ai.gpt-5-4"
SONNET_REF = "databricks/claude-sonnet-5-5"
GPT_REF = "databricks-responses/gpt-5-4"


def catalog(*ids: str) -> list[copilot.CopilotModel]:
    return copilot.build_copilot_catalog(ids)


class TestCopilotSpec:
    def test_binary(self):
        assert copilot.SPEC["binary"] == "copilot"

    def test_package(self):
        assert copilot.SPEC["package"] == "@github/copilot"

    def test_display(self):
        assert copilot.SPEC["display"] == "GitHub Copilot CLI"

    def test_config_path_is_ucode_env_file(self):
        assert copilot.SPEC["config_path"].name == "ucode.env"


class TestClassifyCopilotModel:
    @pytest.mark.parametrize(
        ("model", "wire"),
        [
            ("system.ai.claude-sonnet-5-5", "anthropic"),
            ("system.ai.claude-sonnet-5-5[1m]", "anthropic"),
            ("databricks-claude-sonnet-4-6", "anthropic"),
            ("main.models.claude-opus-4-7", "anthropic"),
            ("system.ai.gpt-5-4", "responses"),
            ("system.ai.gpt-6-1-sol", "responses"),
            ("databricks-gpt-5-3-codex", "responses"),
            ("system.ai.gpt-4-1", "completions"),
            ("system.ai.gpt-6.1-sol", "responses"),
            ("databricks-gpt-4o", "completions"),
            ("system.ai.gpt-4o-mini", "completions"),
            ("gpt-5.4", "responses"),
            ("claude-sonnet-4.5", "anthropic"),
            ("system.ai.glm-5-3", "completions"),
            ("system.ai.kimi-k3-1-pro", "completions"),
            ("system.ai.deepseek-v4-1-flash-1017", "completions"),
            ("databricks-glm-5-3-flash-scale", "completions"),
            ("system.ai.kimi-k3-neo", "completions"),
            ("system.ai.glm-9", "completions"),
        ],
    )
    def test_names_decide_the_wire(self, model, wire):
        assert copilot.classify_copilot_model(model) == wire

    @pytest.mark.parametrize(
        ("model", "reason"),
        [
            ("system.ai.gemini-3-flash", "Gemini"),
            ("system.ai.gpt-oss-120b", "Claude, GPT, GLM, Kimi and DeepSeek"),
            ("system.ai.meta-llama-3-3-70b-instruct", "Claude, GPT"),
            ("system.ai.gte-large-en", "Claude, GPT"),
            ("system.ai.claude-opus-4-1", "thinking"),
            ("anthropic-aigw-12e8d1ee-system.ai.gpt-5-6-sol", "alias"),
        ],
    )
    def test_excluded_models_say_why(self, model, reason):
        assert copilot.classify_copilot_model(model) is None
        assert reason in (copilot.copilot_exclusion_reason(model) or "")

    def test_advertised_routes_only_exclude(self):
        assert copilot.classify_copilot_model(SONNET, ["mlflow/v1/embeddings"]) is None
        # gpt-5-3-codex lists only openai/codex Responses, which the gateway also serves on mlflow.
        codex_routes = ["openai/v1/responses", "codex/v1/responses"]
        assert (
            copilot.classify_copilot_model("system.ai.gpt-5-3-codex", codex_routes) == "responses"
        )

    def test_open_models_need_chat_completions(self):
        deepseek = "system.ai.deepseek-v4-1-flash-1017"
        routes = ["mlflow/v1/chat/completions", "mlflow/v1/responses"]

        assert copilot.classify_copilot_model(deepseek, routes) == "completions"
        assert copilot.classify_copilot_model(deepseek, ["mlflow/v1/responses"]) is None


class TestBuildCopilotCatalog:
    def test_provider_wildcards_reach_the_registry_with_qualified_ids(self, monkeypatch):
        services = {
            "main.default.lilly-personal-anthropic": (
                "anthropic",
                ["claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"],
            ),
            "main.default.lill-oai": ("openai", ["gpt-6.1-sol", "gpt-6.1-terra"]),
        }
        monkeypatch.setattr(
            model_sources,
            "get_model_provider_service",
            lambda name, workspace, token: ({"provider_type": services[name][0]}, None),
        )
        monkeypatch.setattr(
            model_sources,
            "list_provider_service_models",
            lambda workspace, token, name, provider: (services[name][1], None),
        )
        models, _ = model_sources.resolve_model_sources(
            [SONNET, *(f"{service}.*" for service in services)], "copilot", WS, "token"
        )
        state = {"copilot_static_models": models}
        result = copilot.render_providers_config(copilot.copilot_catalog(state), PROXY, "secret")

        expected = {SONNET: "anthropic"}
        expected.update(
            (f"{service}.{model}", "anthropic" if provider == "anthropic" else "responses")
            for service, (provider, names) in services.items()
            for model in names
        )
        assert {entry["wireModel"] for entry in result["models"]} == set(expected)
        assert copilot.unservable_static_models(state) == []
        for entry in result["models"]:
            assert entry["id"] == entry["wireModel"].removeprefix("system.ai.")
            assert entry["provider"] == copilot.PROVIDER_NAMES[expected[entry["wireModel"]]]
            if expected[entry["wireModel"]] == "anthropic":
                assert entry["modelId"].startswith("claude-")
                assert entry["maxPromptTokens"] == 872_000

    def test_orders_claude_by_family_newest_first_then_gpt(self):
        models = catalog(
            "system.ai.gpt-5-4",
            "system.ai.claude-haiku-4-5",
            "system.ai.claude-sonnet-4-6",
            "system.ai.gpt-6-1-sol",
            "system.ai.claude-sonnet-5-5",
            "system.ai.claude-opus-5-5",
            "system.ai.claude-fable-5-1",
            "system.ai.gemini-3-flash",
        )

        assert [m.id for m in models] == [
            "system.ai.claude-fable-5-1",
            "system.ai.claude-opus-5-5",
            "system.ai.claude-sonnet-5-5",
            "system.ai.claude-sonnet-4-6",
            "system.ai.claude-haiku-4-5",
            "system.ai.gpt-6-1-sol",
            "system.ai.gpt-5-4",
        ]

    def test_open_models_come_after_gpt_on_chat_completions(self):
        models = catalog(
            "system.ai.kimi-k3",
            "system.ai.deepseek-v4-1-flash-1017",
            "system.ai.gpt-4o",
            "system.ai.glm-5-3",
            SONNET,
        )

        assert [m.id for m in models] == [
            SONNET,
            "system.ai.gpt-4o",
            "system.ai.deepseek-v4-1-flash-1017",
            "system.ai.glm-5-3",
            "system.ai.kimi-k3",
        ]

    def test_uses_the_listing_routes_and_drops_duplicates(self):
        models = copilot.build_copilot_catalog(
            [
                ModelServiceEntry(SONNET, ("anthropic/v1/messages",)),
                ModelServiceEntry("system.ai.gte-large-en", ("mlflow/v1/embeddings",)),
                SONNET,
            ]
        )

        assert models == [copilot.CopilotModel(SONNET, "anthropic")]
        assert models[0].ref == SONNET_REF


class TestCatalogFromListings:
    def test_prefers_the_model_services_listing_per_family(self):
        entries = [ModelServiceEntry(GPT), ModelServiceEntry("system.ai.gemini-3-flash")]
        fallback = [SONNET, "databricks-gpt-5-5"]

        assert copilot.catalog_from_listings(entries, fallback) == catalog(SONNET, GPT)

    def test_a_listing_with_only_unusable_models_keeps_the_gateway_families(self):
        entries = [ModelServiceEntry("system.ai.gemini-3-flash")]

        assert copilot.catalog_from_listings(entries, [SONNET, GPT]) == catalog(SONNET, GPT)

    def test_falls_back_to_the_gateway_listings(self):
        assert copilot.catalog_from_listings(None, ["databricks-claude-sonnet-4-6"]) == catalog(
            "databricks-claude-sonnet-4-6"
        )

    def test_prefers_the_anthropic_route_listing_with_its_limits(self):
        gateway = AnthropicModelCatalog(
            model_ids=[
                "system.ai.claude-opus-4-8",
                "anthropic-aigw-481f0baa-system.ai.gpt-6-luna",
                "anthropic-aigw-73ea02b2-system.ai.glm-5-2",
                "system.ai.claude-opus-4-1",
            ],
            model_id_to_display_name={},
            model_id_to_limits={
                "system.ai.claude-opus-4-8": (1_000_000, 128_000),
                "anthropic-aigw-481f0baa-system.ai.gpt-6-luna": (922_000, 128_000),
            },
        )

        models = copilot.catalog_from_listings([ModelServiceEntry(GPT)], [SONNET], gateway)

        assert [(m.id, m.wire, m.listed_limits) for m in models] == [
            ("system.ai.claude-opus-4-8", "anthropic", (1_000_000, 128_000)),
            ("system.ai.gpt-6-luna", "responses", (922_000, 128_000)),
        ]

    def test_open_models_come_from_the_model_services_listing(self):
        gateway = AnthropicModelCatalog(
            model_ids=[
                "system.ai.claude-opus-4-8",
                "anthropic-aigw-ba377e31-system.ai.kimi-k3",
                "anthropic-aigw-73ea02b2-system.ai.glm-5-2",
            ],
            model_id_to_display_name={},
        )
        entries = [
            ModelServiceEntry("system.ai.glm-5-2", ("mlflow/v1/chat/completions",)),
            ModelServiceEntry(
                "system.ai.deepseek-v4-1-flash-1017", ("mlflow/v1/chat/completions",)
            ),
            ModelServiceEntry("system.ai.kimi-k3-neo", ("mlflow/v1/chat/completions",)),
        ]

        models = copilot.catalog_from_listings(entries, [SONNET], gateway)

        assert [(m.id, m.wire) for m in models] == [
            ("system.ai.claude-opus-4-8", "anthropic"),
            ("system.ai.deepseek-v4-1-flash-1017", "completions"),
            ("system.ai.glm-5-2", "completions"),
            ("system.ai.kimi-k3-neo", "completions"),
        ]

    def test_open_models_dont_stand_in_for_a_missing_gpt_family(self):
        entries = [ModelServiceEntry(SONNET), ModelServiceEntry("system.ai.glm-5-2")]

        models = copilot.catalog_from_listings(entries, [SONNET, GPT, "system.ai.kimi-k3"])

        assert [m.id for m in models] == [SONNET, GPT, "system.ai.glm-5-2"]

    def test_an_anthropic_route_listing_of_only_open_models_falls_back(self):
        gateway = AnthropicModelCatalog(
            model_ids=["anthropic-aigw-ba377e31-system.ai.kimi-k3"], model_id_to_display_name={}
        )

        models = copilot.catalog_from_listings([ModelServiceEntry(GPT)], [SONNET], gateway)

        assert models == catalog(SONNET, GPT)

    def test_an_anthropic_route_listing_without_usable_models_falls_back(self):
        gateway = AnthropicModelCatalog(
            model_ids=[], model_id_to_display_name={}, error_msg="HTTP 404"
        )

        assert copilot.catalog_from_listings(
            [ModelServiceEntry(GPT)], [SONNET], gateway
        ) == catalog(SONNET, GPT)

    def test_no_answer_keeps_the_saved_catalog(self):
        assert copilot.catalog_from_listings(None, []) is None

    def test_an_answer_with_nothing_usable_is_an_empty_catalog(self):
        assert (
            copilot.catalog_from_listings([ModelServiceEntry("system.ai.gemini-3-flash")], []) == []
        )


class TestCopilotCatalog:
    def test_managed_list_wins_and_skips_what_copilot_cannot_use(self):
        state = {
            "copilot_static_models": [GPT, "system.ai.gemini-3-flash"],
            "copilot_catalog": copilot.catalog_state(catalog(SONNET)),
        }

        assert copilot.copilot_catalog(state) == catalog(GPT)
        assert copilot.unservable_static_models(state) == [
            (
                "system.ai.gemini-3-flash",
                copilot.copilot_exclusion_reason("system.ai.gemini-3-flash"),
            )
        ]

    def test_managed_list_takes_the_limits_discovery_saved(self):
        discovered = [
            copilot.CopilotModel(SONNET, "anthropic"),
            copilot.CopilotModel(GPT, "responses", (922_000, 128_000)),
        ]
        state = {
            "copilot_static_models": [GPT],
            "copilot_catalog": copilot.catalog_state(discovered),
        }

        [model] = copilot.copilot_catalog(state)

        assert model.listed_limits == (922_000, 128_000)
        assert copilot.catalog_model_limits(model) == (794_000, 128_000)

    def test_managed_model_without_saved_limits_keeps_copilots_default(self):
        state = {
            "copilot_static_models": [GPT],
            "copilot_catalog": copilot.catalog_state(catalog(SONNET)),
        }

        [model] = copilot.copilot_catalog(state)

        assert copilot.catalog_model_limits(model) is None

    def test_an_unusable_managed_list_falls_back_to_discovery(self):
        state = {
            "copilot_static_models": ["system.ai.gemini-3-flash"],
            "copilot_catalog": copilot.catalog_state(catalog(SONNET)),
        }

        assert copilot.copilot_catalog(state) == catalog(SONNET)
        assert copilot.unservable_static_models(state) == []

    def test_saved_catalog(self):
        saved = copilot.catalog_state(catalog(SONNET, GPT))

        assert saved["models"] == [SONNET, GPT]
        assert saved["discovered_at"].endswith("+00:00")
        assert copilot.copilot_catalog({"copilot_catalog": saved}) == catalog(SONNET, GPT)

    def test_saved_catalog_keeps_the_listed_limits(self):
        models = [
            copilot.CopilotModel(SONNET, "anthropic"),
            copilot.CopilotModel(GPT, "responses", (922_000, 128_000)),
        ]

        saved = copilot.catalog_state(models)
        restored = copilot.copilot_catalog({"copilot_catalog": saved})

        assert saved["limits"] == {GPT: [922_000, 128_000]}
        assert [m.listed_limits for m in restored] == [None, (922_000, 128_000)]

    def test_malformed_saved_limits_are_ignored(self):
        saved = dict(copilot.catalog_state(catalog(GPT)), limits={GPT: [0, 1], SONNET: "x"})

        restored = copilot.copilot_catalog({"copilot_catalog": saved})

        assert [copilot.catalog_model_limits(m) for m in restored] == [None]

    def test_an_empty_saved_catalog_does_not_revive_older_lists(self):
        state = {"copilot_catalog": copilot.catalog_state([]), "codex_models": [GPT]}
        assert copilot.copilot_catalog(state) == []

    def test_state_from_before_the_catalog_uses_the_claude_and_gpt_lists(self):
        state = {
            "claude_models": {"sonnet": SONNET},
            "codex_models": [GPT, "system.ai.gpt-oss-120b"],
        }
        assert copilot.copilot_catalog(state) == catalog(SONNET, GPT)


class TestResolveCopilotModel:
    MODELS = catalog(SONNET, GPT, "databricks-claude-opus-4-7")

    @pytest.mark.parametrize(
        "requested",
        [
            SONNET,
            "claude-sonnet-5-5",
            "CLAUDE-Sonnet-5-5",
            f"{SONNET}[1m]",
            SONNET_REF,
            f"dbx-anthropic/{SONNET}",
        ],
    )
    def test_accepts_ids_short_names_and_refs(self, requested):
        assert copilot.resolve_copilot_model(requested, self.MODELS).id == SONNET

    def test_accepts_the_databricks_prefix(self):
        assert copilot.resolve_copilot_model("claude-opus-4-7", self.MODELS).id == (
            "databricks-claude-opus-4-7"
        )

    def test_prefers_the_system_ai_id(self):
        models = catalog("databricks-claude-sonnet-5-5", SONNET)
        assert copilot.resolve_copilot_model("claude-sonnet-5-5", models).id == SONNET

    def test_unknown_model_lists_what_is_available(self):
        with pytest.raises(RuntimeError) as error:
            copilot.resolve_copilot_model("system.ai.gemini-3-flash", self.MODELS)

        message = str(error.value)
        assert "Gemini" in message
        assert f"databricks: databricks-claude-opus-4-7, {SONNET}" in message
        assert f"databricks-responses: {GPT}" in message


class TestLaunchModel:
    MODELS = catalog(SONNET, "system.ai.claude-opus-5-5", GPT)

    def test_requested_model_wins(self):
        state = {"copilot_default_model": SONNET}
        assert copilot.launch_model(state, self.MODELS, requested=GPT).id == GPT

    def test_managed_default_then_list(self):
        assert copilot.launch_model({"copilot_default_model": GPT}, self.MODELS).id == GPT
        assert copilot.launch_model({"copilot_static_models": [GPT]}, catalog(GPT)).id == GPT

    @pytest.mark.parametrize(
        ("ids", "expected"),
        [
            ((GPT, "system.ai.claude-opus-5-5", SONNET), SONNET),
            ((GPT, "system.ai.claude-opus-5-5"), "system.ai.claude-opus-5-5"),
            (("system.ai.claude-fable-5", GPT), GPT),
            (("system.ai.kimi-k3", "system.ai.gpt-4o"), "system.ai.gpt-4o"),
            (("system.ai.kimi-k3", "system.ai.claude-fable-5"), "system.ai.claude-fable-5"),
            (("system.ai.kimi-k3",), "system.ai.kimi-k3"),
        ],
    )
    def test_defaults_to_sonnet_then_opus_then_gpt(self, ids, expected):
        assert copilot.launch_model({}, catalog(*ids)).id == expected

    def test_a_provider_service_starts_on_gpt_before_an_open_model(self):
        models, _ = copilot.build_catalog(
            [("gpt-4o", ()), ("deepseek-v4-1-flash", ()), ("glm-5-2", ())],
            provider_type="openai",
        )

        assert copilot.launch_model({}, models, managed_list=False).id == "gpt-4o"

    def test_an_unavailable_requested_model_warns_and_falls_through(self, capsys):
        model = copilot.launch_model({}, self.MODELS, requested="system.ai.gemini-3-flash")

        assert model.id == SONNET
        assert "gemini-3-flash isn't available in ug copilot" in capsys.readouterr().out

    def test_default_model_is_quiet(self, capsys):
        assert copilot.default_model({"copilot_default_model": "missing"}) is None

    def test_a_managed_models_list_picks_the_start_model(self):
        assert copilot.launch_model({"copilot_models": [GPT]}, self.MODELS).id == GPT

    def test_an_unservable_managed_entry_is_skipped_without_a_second_warning(self, capsys):
        state = {"copilot_static_models": ["system.ai.gemini-3-flash", GPT]}

        assert copilot.launch_model(state, catalog(GPT)).id == GPT
        assert "isn't available" not in capsys.readouterr().out

    def test_a_repeated_candidate_warns_once(self, capsys):
        state = {"copilot_default_model": "system.ai.missing"}

        copilot.launch_model(state, self.MODELS, requested="system.ai.missing")

        assert capsys.readouterr().out.count("isn't available") == 1
        state = {"copilot_default_model": "missing", "codex_models": [GPT]}
        assert copilot.default_model(state) == GPT
        assert capsys.readouterr().out == ""


class TestRewriteModelArgs:
    MODELS = catalog(SONNET, GPT)

    @pytest.mark.parametrize(
        "args",
        [["--model", GPT], [f"--model={GPT}"], ["-m", "gpt-5-4"], ["--model", SONNET, "-m", GPT]],
    )
    def test_rewrites_to_the_registry_ref(self, args):
        rewritten, selected = copilot.rewrite_model_args(["-p", "hi", *args], self.MODELS)

        assert selected == copilot.CopilotModel(GPT, "responses")
        assert rewritten[:2] == ["-p", "hi"]
        assert rewritten[-2:] == ["--model", GPT_REF]

    def test_leaves_arguments_after_the_separator(self):
        rewritten, selected = copilot.rewrite_model_args(["--", "--model", "x"], self.MODELS)
        assert (rewritten, selected) == (["--", "--model", "x"], None)

    def test_unknown_model_raises(self):
        with pytest.raises(RuntimeError, match="isn't available"):
            copilot.rewrite_model_args(["--model", "system.ai.claude-opus-4-1"], self.MODELS)


class TestCopilotCatalogModelId:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("system.ai.claude-sonnet-5-5", "claude-sonnet-5.5"),
            ("system.ai.claude-opus-4-7[1m]", "claude-opus-4.7"),
            ("system.ai.claude-sonnet-5", "claude-sonnet-5"),
            ("system.ai.claude-haiku-4-5", "claude-haiku-4.5"),
            ("system.ai.claude-haiku-5-5", "claude-sonnet-5.5"),
            ("databricks-claude-sonnet-4-6", "claude-sonnet-4.6"),
            ("claude-sonnet-4-5-20250929", "claude-sonnet-4.5"),
            ("us.anthropic.claude-sonnet-4-6", "claude-sonnet-4.6"),
            ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4.5"),
            ("global.anthropic.claude-opus-4-8", "claude-opus-4.8"),
            ("anthropic.claude-opus-4-8", "claude-opus-4.8"),
            ("anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4.5"),
        ],
    )
    def test_maps_claude_ids(self, model, expected):
        assert copilot.copilot_catalog_model_id(model) == expected

    def test_other_models_have_no_catalog_id(self):
        assert copilot.copilot_catalog_model_id(GPT) is None


class TestClassifyProviderModel:
    @pytest.mark.parametrize(
        ("model", "provider_type", "wire"),
        [
            ("claude-sonnet-5", "anthropic", "anthropic"),
            ("us.anthropic.claude-sonnet-4-6", "amazon_bedrock", "anthropic"),
            ("anthropic.claude-opus-4-8", "bedrock_mantle", "anthropic"),
            ("gpt-5.5", "openai", "responses"),
            ("gpt-5-nano", "openai", "responses"),
            ("o3-mini", "openai", "responses"),
            ("gpt-4o", "azure_openai", "completions"),
            ("my-gpt4o-deployment", "azure_openai", "completions"),
            ("claude-opus-4-8", "microsoft_foundry", "anthropic"),
            ("gpt-5-nano", "microsoft_foundry", "responses"),
            ("deepseek", "microsoft_foundry", "completions"),
        ],
    )
    def test_provider_type_and_id_decide_the_wire(self, model, provider_type, wire):
        assert copilot.classify_model(model, provider_type=provider_type) == (wire, None)

    def test_declared_non_chat_apis_exclude(self):
        wire, why = copilot.classify_model(
            "embed-v-4-0", ["openai/v1/embeddings"], provider_type="openai"
        )

        assert wire is None
        assert "isn't a chat model" in why
        assert copilot.classify_model(
            "gpt-5.2-chat", ["openai/v1/chat/completions"], provider_type="azure_openai"
        ) == ("responses", None)

    @pytest.mark.parametrize(
        ("model", "provider_type", "reason"),
        [
            ("us.anthropic.claude-sonnet-4-6", "openai", None),
            ("amazon.titan-text-express-v1", "amazon_bedrock", "Claude models"),
        ],
    )
    def test_unusable_models_say_why(self, model, provider_type, reason):
        wire, why = copilot.classify_model(model, provider_type=provider_type)
        if reason is None:
            assert wire == "completions"
        else:
            assert wire is None
            assert reason in why


def provider_models(models, provider_type, api_types=None):
    api_types = api_types or {}
    return copilot.build_catalog(
        ((model, api_types.get(model, ())) for model in models), provider_type=provider_type
    )


class TestBuildProviderCatalog:
    def test_keeps_usable_models_in_catalog_order_and_reports_the_rest(self):
        models, skipped = provider_models(
            ["gpt-4o", "text-embedding-3-small", "gpt-5.5", "gpt-5.5"],
            "openai",
            {"text-embedding-3-small": ["openai/v1/embeddings"]},
        )

        assert [(m.id, m.wire) for m in models] == [
            ("gpt-5.5", "responses"),
            ("gpt-4o", "completions"),
        ]
        assert skipped == [
            ("text-embedding-3-small", "it isn't a chat model (it serves openai/v1/embeddings)")
        ]

    def test_foundry_claude_uses_anthropic_whatever_it_declares(self):
        chat = ["openai/v1/chat/completions"]
        models, skipped = provider_models(
            ["deepseek", "claude-opus-4-8"],
            "microsoft_foundry",
            {"deepseek": chat, "claude-opus-4-8": chat},
        )

        assert [(m.id, m.wire) for m in models] == [
            ("claude-opus-4-8", "anthropic"),
            ("deepseek", "completions"),
        ]
        assert skipped == []

    def test_orders_dotted_provider_versions_newest_first(self):
        models, _ = provider_models(
            ["gpt-5", "gpt-5.6", "gpt-5.1", "gpt-5.5", "o1", "o4-mini", "o3"], "openai"
        )

        assert [m.id for m in models] == [
            "gpt-5.6",
            "gpt-5.5",
            "gpt-5.1",
            "gpt-5",
            "o4-mini",
            "o3",
            "o1",
        ]


def _stub_provider_service(monkeypatch, service, *, listed=(), listing_error=None):
    monkeypatch.setattr(copilot, "launch_token", lambda state, workspace, **_: "dbx-token")
    seen = {}

    def resolve(tool, name, workspace, token):
        seen["resolve"] = (tool, name, workspace, token)
        return (dict(service, name=name), None) if service else (None, f"'{name}' was not found.")

    def list_models(workspace, token, service_name, provider_type):
        seen["listing"] = (service_name, provider_type)
        return list(listed), listing_error

    monkeypatch.setattr(copilot, "resolve_provider_service", resolve)
    monkeypatch.setattr(copilot, "list_provider_service_models", list_models)
    return seen


MPS = "main.default.anthropic_mps"


class TestProviderCatalog:
    def test_uses_the_declared_targets(self, monkeypatch):
        seen = _stub_provider_service(
            monkeypatch,
            {"provider_type": "anthropic", "targets": ["claude-sonnet-5", "claude-opus-5"]},
        )

        models = copilot.provider_service_catalog({"workspace": WS}, MPS, None)

        assert [m.id for m in models] == ["claude-opus-5", "claude-sonnet-5"]
        assert seen["resolve"] == ("copilot", MPS, WS, "dbx-token")
        assert "anthropic_listing" not in seen

    def test_allow_all_adds_the_gateway_listing_to_the_start_model(self, monkeypatch):
        seen = _stub_provider_service(
            monkeypatch,
            {"provider_type": "amazon_bedrock", "targets": [], "allow_all_targets": True},
            listed=["anthropic.claude-opus-4-8"],
        )

        models = copilot.provider_service_catalog(
            {"workspace": WS}, MPS, "us.anthropic.claude-sonnet-4-6"
        )

        assert [m.id for m in models] == [
            "anthropic.claude-opus-4-8",
            "us.anthropic.claude-sonnet-4-6",
        ]
        assert seen["listing"] == (MPS, "amazon_bedrock")

    @pytest.mark.parametrize("tool", ["copilot", "vscode"])
    def test_allow_all_adds_the_managed_default_for_the_cli_and_vscode(self, monkeypatch, tool):
        _stub_provider_service(
            monkeypatch,
            {"provider_type": "anthropic", "targets": [], "allow_all_targets": True},
            listed=["claude-opus-5"],
        )
        state = {"workspace": WS, "copilot_default_model": "claude-sonnet-5"}

        models = copilot.provider_service_catalog(state, MPS, None, tool=tool)

        assert sorted(m.id for m in models) == ["claude-opus-5", "claude-sonnet-5"]

    def test_allow_all_openai_takes_a_provider_ref_as_the_start_model(self, monkeypatch):
        seen = _stub_provider_service(
            monkeypatch,
            {"provider_type": "openai", "targets": [], "allow_all_targets": True},
            listed=["gpt-5.5", "gpt-4o"],
        )

        models = copilot.provider_service_catalog({"workspace": WS}, MPS, "dbx-responses/gpt-5.5")

        assert [(m.id, m.wire) for m in models] == [
            ("gpt-5.5", "responses"),
            ("gpt-4o", "completions"),
        ]
        assert seen["listing"] == (MPS, "openai")

    def test_allow_all_starts_on_the_requested_model_when_listing_fails(self, monkeypatch, capsys):
        _stub_provider_service(
            monkeypatch,
            {"provider_type": "openai", "targets": [], "allow_all_targets": True},
            listing_error="HTTP 503 Service Unavailable",
        )

        models = copilot.provider_service_catalog({"workspace": WS}, MPS, "gpt-5.5")

        assert [m.id for m in models] == ["gpt-5.5"]
        assert "HTTP 503" in capsys.readouterr().out

    def test_allow_all_without_a_start_model_registers_the_listing(self, monkeypatch):
        _stub_provider_service(
            monkeypatch,
            {"provider_type": "anthropic", "targets": [], "allow_all_targets": True},
            listed=["claude-opus-5", "claude-sonnet-5"],
        )

        models = copilot.provider_service_catalog({"workspace": WS}, MPS, None)

        assert [m.id for m in models] == ["claude-opus-5", "claude-sonnet-5"]
        assert copilot.launch_model({}, models, warn=False).id == "claude-sonnet-5"

    def test_allow_all_with_an_empty_listing_asks_for_a_model(self, monkeypatch):
        _stub_provider_service(
            monkeypatch, {"provider_type": "anthropic", "targets": [], "allow_all_targets": True}
        )

        with pytest.raises(RuntimeError, match="has no models Copilot can use.*--model"):
            copilot.provider_service_catalog({"workspace": WS}, MPS, None)

    def test_no_usable_model_lists_the_reasons(self, monkeypatch):
        _stub_provider_service(
            monkeypatch,
            {
                "provider_type": "openai",
                "targets": ["text-embedding-3-large"],
                "target_api_types": {"text-embedding-3-large": ("openai/v1/embeddings",)},
            },
        )

        with pytest.raises(RuntimeError, match=r"text-embedding-3-large \(it isn't a chat model"):
            copilot.provider_service_catalog({"workspace": WS}, MPS, None)

    def test_warns_about_skipped_models(self, monkeypatch, capsys):
        _stub_provider_service(
            monkeypatch,
            {
                "provider_type": "openai",
                "targets": ["gpt-5.5", "text-embedding-3-large"],
                "target_api_types": {"text-embedding-3-large": ("openai/v1/embeddings",)},
            },
        )

        copilot.provider_service_catalog({"workspace": WS}, MPS, None)

        assert "text-embedding-3-large" in capsys.readouterr().out

    def test_invalid_service_raises_its_error(self, monkeypatch):
        _stub_provider_service(monkeypatch, None)

        with pytest.raises(RuntimeError, match="was not found"):
            copilot.provider_service_catalog({"workspace": WS}, MPS, None)


def _model_service(*models):
    """A UC model service whose routing has one live pay-per-token destination per model."""
    return {
        "config": {
            "routing": {
                "destinations": [
                    {"traffic_percentage": 100, "pay_per_token_config": {"model": f"models/{m}"}}
                    for m in models
                ]
            }
        }
    }


def _stub_location(monkeypatch, model_ids, error=None, services=None):
    monkeypatch.setattr(copilot, "launch_token", lambda state, workspace, **_: "dbx-token")
    seen = {"looked_up": []}

    def model_service(name, workspace, token, *, timeout):
        seen["looked_up"].append(name)
        service = (services or {}).get(name)
        return (service, None) if service else (None, "not found")

    monkeypatch.setattr(copilot, "get_model_service", model_service)

    def anthropic_catalog(workspace, token, *, parent_schema):
        seen["parent_schema"] = parent_schema
        return AnthropicModelCatalog(
            model_ids=list(model_ids), model_id_to_display_name={}, error_msg=error
        )

    monkeypatch.setattr(copilot, "list_anthropic_model_catalog", anthropic_catalog)
    return seen


class TestLocationCatalog:
    def test_registers_the_schemas_services_by_full_name(self, monkeypatch):
        seen = _stub_location(
            monkeypatch,
            [
                "anthropic-aigw-1a2b3c4d-main.ucode.claude_haiku",
                "main.ucode.claude-sonnet-4-6",
                "anthropic-aigw-0f0f0f0f-main.ucode.gpt-5-6-luna",
                "main.ucode.claude-opus-4-1",
            ],
        )

        models = copilot.location_catalog({"workspace": WS}, "main.ucode")

        assert [(m.id, m.wire) for m in models] == [
            ("main.ucode.claude-sonnet-4-6", "anthropic"),
            ("main.ucode.claude_haiku", "anthropic"),
            ("main.ucode.gpt-5-6-luna", "responses"),
        ]
        assert seen["parent_schema"] == "main.ucode"

    def test_a_service_named_like_no_model_takes_its_claude_settings_from_the_model_behind_it(
        self, monkeypatch
    ):
        seen = _stub_location(
            monkeypatch,
            [
                "anthropic-aigw-1a2b3c4d-main.ucode.claude_sonnet",
                "main.ucode.gpt_luna",
                "main.ucode.claude-haiku-4-5",
            ],
            services={
                "main.ucode.claude_sonnet": _model_service(
                    "system.ai.databricks-claude-sonnet-4-6"
                ),
                "main.ucode.gpt_luna": _model_service("system.ai.databricks-gpt-6-luna"),
            },
        )

        models = copilot.location_catalog({"workspace": WS}, "main.ucode")
        entries = {
            entry["wireModel"]: entry
            for entry in copilot.render_providers_config(models, PROXY, "secret")["models"]
        }

        # Copilot sends `temperature` to a model it can't place, which newer Claude models reject.
        assert entries["main.ucode.claude_sonnet"]["modelId"] == "claude-sonnet-4.6"
        assert entries["main.ucode.claude_sonnet"]["maxPromptTokens"] == 872_000
        assert "modelId" not in entries["main.ucode.gpt_luna"]
        assert entries["main.ucode.claude-haiku-4-5"]["modelId"] == "claude-haiku-4.5"
        assert sorted(seen["looked_up"]) == ["main.ucode.claude_sonnet", "main.ucode.gpt_luna"]

    def test_a_service_lookup_that_fails_keeps_the_service_as_listed(self, monkeypatch):
        _stub_location(monkeypatch, ["main.ucode.claude_sonnet"])

        [model] = copilot.location_catalog({"workspace": WS}, "main.ucode")

        assert model.served_model is None
        [entry] = copilot.render_providers_config([model], PROXY, "secret")["models"]
        assert "modelId" not in entry

    def test_an_empty_schema_says_so(self, monkeypatch):
        _stub_location(monkeypatch, [], error="Unity Gateway returned no Anthropic model ids")

        with pytest.raises(RuntimeError, match="no model services to use in main.ucode"):
            copilot.location_catalog({"workspace": WS}, "main.ucode")


OVERFLOW = (
    b'{"error_code":"BAD_REQUEST","message":"{\\"error\\":\\"Input exceeds the model\'s maximum'
    b' context length. Reduce the length of the input messages.\\"}"}'
)


class TestRecognizableContextOverflow:
    def test_puts_copilots_words_before_the_gateways(self):
        rewritten = copilot.recognizable_context_overflow(OVERFLOW)

        assert rewritten == OVERFLOW.replace(b"Input exceeds", b"prompt is too long: Input exceeds")
        inner = json.loads(json.loads(rewritten)["message"])
        assert inner["error"].startswith("prompt is too long: Input exceeds")

    def test_the_runtime_wording_too(self):
        body = (
            b'{"message":"The input (1100000 tokens) is longer than the model\'s context length'
            b' (1048576 tokens)."}'
        )

        assert copilot.recognizable_context_overflow(body) == body.replace(
            b"The input", b"prompt is too long: The input"
        )

    @pytest.mark.parametrize(
        "body",
        [
            b'{"error":{"message":"prompt is too long: 210000 tokens > 200000 maximum"}}',
            b'{"error_code":"BAD_REQUEST","message":"max_tokens exceeds the limit"}',
            b"\xff\xfe",
        ],
    )
    def test_other_bodies_are_relayed_unchanged(self, body):
        assert copilot.recognizable_context_overflow(body) is None


class TestCopilotModelLimits:
    @pytest.mark.parametrize(
        ("model", "tokens"),
        [
            ("system.ai.claude-sonnet-5-5", (1_000_000, 128_000)),
            ("system.ai.claude-haiku-5-5", (1_000_000, 128_000)),
            ("system.ai.claude-opus-4-8", (1_000_000, 128_000)),
            ("system.ai.claude-opus-6", (1_000_000, 128_000)),
            ("system.ai.claude-sonnet-4-6", (1_000_000, 128_000)),
            ("system.ai.claude-haiku-4-5", (200_000, 32_000)),
            ("system.ai.claude-sonnet-4-5", (200_000, 32_000)),
            ("system.ai.claude-opus-4-5", (200_000, 32_000)),
            ("system.ai.claude-opus-4-1", (200_000, 32_000)),
            ("system.ai.claude-opus-4-0", (200_000, 32_000)),
        ],
    )
    def test_claude_token_limits_by_name(self, model, tokens):
        assert copilot.claude_token_limits(model) == tokens

    def test_gpt_has_no_limits_by_name(self):
        assert copilot.claude_token_limits(GPT) is None

    def test_claude_prompt_leaves_room_for_the_output(self):
        model = copilot.CopilotModel(SONNET, "anthropic")

        assert copilot.catalog_model_limits(model) == (872_000, 128_000)

    def test_listed_limits_win_over_the_name(self):
        model = copilot.CopilotModel(SONNET, "anthropic", (200_000, 64_000))

        assert copilot.catalog_model_limits(model) == (136_000, 64_000)

    def test_gpt_prompt_leaves_room_for_the_output(self):
        model = copilot.CopilotModel(GPT, "responses", (922_000, 128_000))

        assert copilot.catalog_model_limits(model) == (794_000, 128_000)

    def test_gpt_without_listed_limits_keeps_copilots_defaults(self):
        assert copilot.catalog_model_limits(copilot.CopilotModel(GPT, "responses")) is None

    @pytest.mark.parametrize(
        "model",
        ["system.ai.glm-5-3", "system.ai.kimi-k3-1-pro", "deepseek-v4-1-flash", "glm-9"],
    )
    def test_open_models_keep_the_prompt_well_inside_their_window(self, model):
        limits = copilot.catalog_model_limits(copilot.CopilotModel(model, "completions"))

        assert limits == (687_000, 32_000)

    def test_a_listed_output_that_fills_the_window_is_ignored(self):
        model = copilot.CopilotModel(SONNET, "anthropic", (64_000, 64_000))

        assert copilot.catalog_model_limits(model) is None


class TestRenderProvidersConfig:
    def test_readable_labels_preserve_model_and_wire_identity(self):
        models = catalog("main.team.provider.anthropic.claude-sonnet-5", GPT)

        entries = copilot.render_providers_config(models, PROXY, "secret")["models"]

        assert {entry["wireModel"]: entry["name"] for entry in entries} == {
            "main.team.provider.anthropic.claude-sonnet-5": "Claude Sonnet 5",
            GPT: "GPT 5.4",
        }
        assert [entry["id"] for entry in entries] == [
            "main.team.provider.anthropic.claude-sonnet-5",
            "gpt-5-4",
        ]
        assert entries[0]["modelId"] == "claude-sonnet-5"

    def test_direct_provider_retains_raw_region_distinctions(self):
        ids = ["us.anthropic.claude-sonnet-5", "eu.anthropic.claude-sonnet-5"]
        models = [copilot.CopilotModel(model_id, "anthropic") for model_id in ids]

        entries = copilot.render_providers_config(models, PROXY, "secret", provider_service=MPS)[
            "models"
        ]

        assert [entry["name"] for entry in entries] == ids
        assert [entry["wireModel"] for entry in entries] == ids

    def test_duplicate_labels_keep_distinct_exact_ids(self):
        ids = ["main.team_a.provider.openai.gpt-5.5", "main.team_b.provider.openai.gpt-5.5"]
        models = [copilot.CopilotModel(model_id, "responses") for model_id in ids]

        entries = copilot.render_providers_config(models, PROXY, "secret")["models"]

        assert [entry["name"] for entry in entries] == ids
        assert [entry["id"] for entry in entries] == ids
        assert [entry["wireModel"] for entry in entries] == ids

    def test_one_provider_per_wire_in_use_on_the_proxy(self):
        config = copilot.render_providers_config(catalog(SONNET, GPT), PROXY, "secret")

        assert config["providers"] == [
            {
                "name": "databricks",
                "type": "anthropic",
                "baseUrl": f"{PROXY}/ai-gateway/anthropic",
                "bearerToken": "secret",
            },
            {
                "name": "databricks-responses",
                "type": "openai",
                "wireApi": "responses",
                "baseUrl": f"{PROXY}/ai-gateway/mlflow/v1",
                "bearerToken": "secret",
            },
        ]

    def test_completions_provider(self):
        config = copilot.render_providers_config(catalog("system.ai.gpt-4-1"), PROXY, "secret")
        assert [(p["name"], p.get("wireApi")) for p in config["providers"]] == [
            ("databricks-completions", "completions")
        ]

    def test_models_carry_claude_settings_and_limits(self):
        config = copilot.render_providers_config(catalog(SONNET, GPT), PROXY, "secret")

        assert config["models"] == [
            {
                "id": "claude-sonnet-5-5",
                "provider": "databricks",
                "wireModel": SONNET,
                "name": "Claude Sonnet 5.5",
                "modelId": "claude-sonnet-5.5",
                "maxPromptTokens": 872_000,
                "maxOutputTokens": 128_000,
            },
            {
                "id": "gpt-5-4",
                "provider": "databricks-responses",
                "wireModel": GPT,
                "name": "GPT 5.4",
            },
        ]

    def test_open_models_get_limits_and_no_catalog_id(self):
        config = copilot.render_providers_config(catalog("system.ai.kimi-k3"), PROXY, "secret")

        assert config["models"] == [
            {
                "id": "kimi-k3",
                "provider": "databricks-completions",
                "wireModel": "system.ai.kimi-k3",
                "name": "Kimi K3",
                "maxPromptTokens": 687_000,
                "maxOutputTokens": 32_000,
            }
        ]

    def test_provider_service_routes_every_provider_by_header(self):
        claude, _ = provider_models(["claude-sonnet-4-6"], "anthropic")
        gpt, _ = provider_models(["gpt-5.5", "gpt-4o"], "openai")

        config = copilot.render_providers_config(
            claude + gpt, PROXY, "secret", provider_service=MPS
        )

        assert config["providers"] == [
            {
                "name": "databricks",
                "type": "anthropic",
                "baseUrl": f"{PROXY}/ai-gateway/anthropic",
                "bearerToken": "secret",
                "headers": {"Databricks-Model-Provider-Service": MPS},
            },
            {
                "name": "databricks-responses",
                "type": "openai",
                "wireApi": "responses",
                "baseUrl": f"{PROXY}/ai-gateway/openai/v1",
                "bearerToken": "secret",
                "headers": {"Databricks-Model-Provider-Service": MPS},
            },
            {
                "name": "databricks-completions",
                "type": "openai",
                "wireApi": "completions",
                "baseUrl": f"{PROXY}/ai-gateway/openai/v1",
                "bearerToken": "secret",
                "headers": {"Databricks-Model-Provider-Service": MPS},
            },
        ]

    def test_provider_service_claude_keeps_its_id_and_its_limits(self):
        claude, _ = provider_models(["claude-sonnet-4-6"], "anthropic")

        config = copilot.render_providers_config(claude, PROXY, "secret", provider_service=MPS)

        assert config["models"] == [
            {
                "id": "claude-sonnet-4-6",
                "provider": "databricks",
                "wireModel": "claude-sonnet-4-6",
                "name": "claude-sonnet-4-6",
                "modelId": "claude-sonnet-4.6",
                "maxPromptTokens": 872_000,
                "maxOutputTokens": 128_000,
            }
        ]

    def test_gpt_gets_its_listed_limits(self):
        model = copilot.CopilotModel(GPT, "responses", (922_000, 128_000))

        config = copilot.render_providers_config([model], PROXY, "secret")

        assert config["models"][0]["maxPromptTokens"] == 794_000
        assert config["models"][0]["maxOutputTokens"] == 128_000

    def test_ids_and_names_stay_full_where_the_short_ones_collide(self):
        opus = "system.ai.claude-opus-5-5"

        config = copilot.render_providers_config(
            catalog(SONNET, "claude-sonnet-5-5", opus), PROXY, "secret"
        )

        assert {m["wireModel"]: (m["id"], m["name"]) for m in config["models"]} == {
            SONNET: (SONNET, SONNET),
            "claude-sonnet-5-5": ("claude-sonnet-5-5", "claude-sonnet-5-5"),
            opus: ("claude-opus-5-5", "Claude Opus 5.5"),
        }
        assert len({f"{m['provider']}/{m['id']}" for m in config["models"]}) == 3


class TestBuildRuntimeEnv:
    def test_points_copilot_at_the_registry(self, tmp_path):
        env = copilot.build_runtime_env(tmp_path / "providers.json", catalog(GPT)[0])

        assert env["COPILOT_PROVIDERS_CONFIG"] == str(tmp_path / "providers.json")
        assert env["COPILOT_MODEL"] == GPT_REF
        assert env["COPILOT_OFFLINE"] == "true"
        assert env["PATH"] == os.environ["PATH"]

    @pytest.mark.parametrize(
        "key",
        [
            "COPILOT_PROVIDERS_CONFIG",
            "COPILOT_PROVIDER_BASE_URL",
            "COPILOT_PROVIDER_BEARER_TOKEN",
            "COPILOT_PROVIDER_API_KEY_COMMAND",
            "COPILOT_PROVIDER_WIRE_MODEL",
        ],
    )
    def test_drops_the_users_own_provider_settings(self, monkeypatch, tmp_path, key):
        monkeypatch.setenv(key, "user-value")
        env = copilot.build_runtime_env(tmp_path / "providers.json", catalog(GPT)[0])
        assert env.get(key) != "user-value"

    def test_tracing_replaces_the_users_own_exporter_settings(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://collector:4318/t")
        monkeypatch.setenv("COPILOT_OTEL_FILE_EXPORTER_PATH", "/tmp/spans")
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=ml")
        otel = copilot.otel_env(PROXY, "secret")

        env = copilot.build_runtime_env(tmp_path / "providers.json", catalog(GPT)[0], otel=otel)

        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"{PROXY}/ai-gateway/otel"
        assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
        assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "x-api-key=secret"
        assert env["COPILOT_OTEL_ENABLED"] == "true"
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in env
        assert "COPILOT_OTEL_FILE_EXPORTER_PATH" not in env
        assert env["OTEL_RESOURCE_ATTRIBUTES"] == "team=ml"

    def test_without_tracing_drops_the_users_own_exporter_settings(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer other")
        monkeypatch.setenv("COPILOT_OTEL_ENABLED", "true")
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=ml")

        env = copilot.build_runtime_env(tmp_path / "providers.json", catalog(GPT)[0])

        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
        assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
        assert "COPILOT_OTEL_ENABLED" not in env
        assert env["OTEL_RESOURCE_ATTRIBUTES"] == "team=ml"


class TestSessionRegistry:
    def test_writes_a_private_registry_removed_at_exit(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)

        with copilot.session_registry({"models": ["x"]}) as path:
            assert json.loads(path.read_text(encoding="utf-8")) == {"models": ["x"]}
            assert path.parent == copilot.COPILOT_CONFIG_DIR
            assert path.name.startswith("ug-providers-")
            if os.name != "nt":
                assert path.stat().st_mode & 0o777 == 0o600

        assert not path.exists()
        assert not path.with_suffix(".lock").exists()

    def test_cleanup_holds_the_registries_mutex(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        real_lock, real_release = copilot._registries_lock, copilot.release_file_lock
        held: list[bool] = []
        released_under_mutex: list[bool] = []

        @contextlib.contextmanager
        def tracking_lock():
            with real_lock():
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()

        def tracking_release(lock_file):
            if Path(lock_file.name).name.startswith(copilot._REGISTRY_PREFIX):
                released_under_mutex.append(bool(held))
            real_release(lock_file)

        monkeypatch.setattr(copilot, "_registries_lock", tracking_lock)
        monkeypatch.setattr(copilot, "release_file_lock", tracking_release)

        with copilot.session_registry({}):
            pass

        assert released_under_mutex == [True]

    def test_removed_when_the_session_fails(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)

        with pytest.raises(OSError), copilot.session_registry({}) as path:
            raise OSError("copilot failed")

        assert not path.exists()

    def test_sweep_keeps_live_sessions_and_removes_dead_ones(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        config_dir = copilot.COPILOT_CONFIG_DIR
        config_dir.mkdir(parents=True)
        dead = config_dir / "ug-providers-dead.json"
        dead.write_text("{}", encoding="utf-8")
        dead.with_suffix(".lock").touch()
        unlocked = config_dir / "ug-providers-crashed-before-lock.json"
        unlocked.write_text("{}", encoding="utf-8")
        users_own = config_dir / "providers.json"
        users_own.write_text("{}", encoding="utf-8")

        with copilot.session_registry({}) as live:
            copilot.sweep_session_registries()
            assert live.exists()

        assert not dead.exists()
        assert not dead.with_suffix(".lock").exists()
        assert not unlocked.exists()
        assert users_own.exists()


class TestMcpServerConfig:
    # ucode registers the `ucode mcp-proxy ...` bridge as a `local` (stdio) MCP
    # server; the proxy refreshes the token, so no URL/bearer header here.
    PROXY_ARGV = ["ucode", "mcp-proxy", "--url", f"{WS}/api/2.0/mcp/functions/system/ai"]

    def test_builds_local_server_entry_from_proxy_argv(self):
        entry = copilot.build_mcp_server_entry(self.PROXY_ARGV)

        assert entry == {
            "type": "local",
            "command": self.PROXY_ARGV[0],
            "args": self.PROXY_ARGV[1:],
            "tools": ["*"],
        }

    def test_writes_mcp_server_without_clobbering_existing_config(self, tmp_path, monkeypatch):
        import ucode.agents.copilot as cp_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "mcp-config.json"
        monkeypatch.setattr(cp_mod, "COPILOT_MCP_CONFIG_PATH", config_file)

        config_file.write_text(
            json.dumps(
                {
                    "other": True,
                    "mcpServers": {"old-server": {"type": "stdio", "command": "old"}},
                }
            ),
            encoding="utf-8",
        )

        removed = cp_mod.write_mcp_server_config("github", self.PROXY_ARGV)

        written = json.loads(config_file.read_text())
        assert removed is False
        assert written["other"] is True
        assert written["mcpServers"]["old-server"] == {"type": "stdio", "command": "old"}
        assert written["mcpServers"]["github"] == {
            "type": "local",
            "command": self.PROXY_ARGV[0],
            "args": self.PROXY_ARGV[1:],
            "tools": ["*"],
        }

    def test_reports_replaced_mcp_server(self, tmp_path, monkeypatch):
        import ucode.agents.copilot as cp_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "mcp-config.json"
        monkeypatch.setattr(cp_mod, "COPILOT_MCP_CONFIG_PATH", config_file)

        config_file.write_text(
            json.dumps({"mcpServers": {"github": {"old": True}}}),
            encoding="utf-8",
        )

        removed = cp_mod.write_mcp_server_config("github", self.PROXY_ARGV)

        assert removed is True
        written = json.loads(config_file.read_text())
        assert written["mcpServers"]["github"]["command"] == self.PROXY_ARGV[0]

    def test_removes_mcp_server_without_clobbering_others(self, tmp_path, monkeypatch):
        import ucode.agents.copilot as cp_mod

        config_file = tmp_path / "mcp-config.json"
        monkeypatch.setattr(cp_mod, "COPILOT_MCP_CONFIG_PATH", config_file)
        config_file.write_text(
            json.dumps(
                {
                    "other": True,
                    "mcpServers": {
                        "github": {"url": "old"},
                        "jira": {"url": "keep"},
                    },
                }
            ),
            encoding="utf-8",
        )

        removed = cp_mod.remove_mcp_server_config("github")

        written = json.loads(config_file.read_text())
        assert removed is True
        assert "github" not in written["mcpServers"]
        assert written["mcpServers"]["jira"] == {"url": "keep"}
        assert written["other"] is True


class TestCopilotUserMcpConfig:
    ENTRY = {"type": "local", "command": "ug", "args": ["mcp-proxy"], "tools": ["*"]}

    def test_honors_copilot_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))

        copilot.write_user_mcp_servers({"svc": self.ENTRY}, set())

        written = json.loads((tmp_path / "copilot-home" / "mcp-config.json").read_text())
        assert written["mcpServers"] == {"svc": self.ENTRY}

    def test_a_config_ug_cannot_parse_is_left_alone(self):
        path = copilot.copilot_mcp_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        jsonc = '{\n  // mine\n  "mcpServers": {"mine": {"x": 1}},\n}\n'
        path.write_text(jsonc, encoding="utf-8")
        copilot.LEGACY_MCP_CONFIG_PATH.write_text(json.dumps({"mcpServers": {"old": self.ENTRY}}))

        with pytest.raises(RuntimeError, match="Remove the comments"):
            copilot.write_user_mcp_servers({"svc": self.ENTRY}, set())

        assert path.read_text(encoding="utf-8") == jsonc
        assert copilot.LEGACY_MCP_CONFIG_PATH.exists()

    def test_failed_write_keeps_the_legacy_side_file(self, monkeypatch):
        def fail(path, payload):
            raise RuntimeError(f"Failed to write config file: {path}")

        monkeypatch.setattr(copilot, "write_json_file", fail)
        copilot.LEGACY_MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        copilot.LEGACY_MCP_CONFIG_PATH.write_text(json.dumps({"mcpServers": {"old": self.ENTRY}}))

        with pytest.raises(RuntimeError, match="Failed to write config file"):
            copilot.migrate_legacy_mcp_config()

        assert copilot.LEGACY_MCP_CONFIG_PATH.exists()

    def test_a_legacy_side_file_that_cannot_be_deleted_raises_a_runtime_error(self, monkeypatch):
        copilot.LEGACY_MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        copilot.LEGACY_MCP_CONFIG_PATH.write_text("{}")

        def unlink(self, missing_ok=False):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "unlink", unlink)

        with pytest.raises(RuntimeError, match="Failed to remove"):
            copilot.remove_legacy_mcp_config()

    def test_empty_legacy_side_file_is_removed_without_a_write(self):
        copilot.LEGACY_MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        copilot.LEGACY_MCP_CONFIG_PATH.write_text("{}")

        copilot.migrate_legacy_mcp_config()

        assert not copilot.LEGACY_MCP_CONFIG_PATH.exists()
        assert not copilot.copilot_mcp_config_path().exists()

    def test_moves_legacy_side_file_servers_into_the_user_config(self):
        path = copilot.copilot_mcp_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": {"mine": {"x": 1}}, "other": True}))
        copilot.LEGACY_MCP_CONFIG_PATH.write_text(
            json.dumps({"mcpServers": {"svc": self.ENTRY, "gone": self.ENTRY}})
        )
        copilot.LEGACY_MCP_BACKUP_PATH.write_text("{}")

        removed = copilot.write_user_mcp_servers({}, {"gone"})

        assert removed == {"gone"}
        written = json.loads(path.read_text())
        assert written["other"] is True
        assert written["mcpServers"] == {"mine": {"x": 1}, "svc": self.ENTRY}
        assert not copilot.LEGACY_MCP_CONFIG_PATH.exists()
        assert not copilot.LEGACY_MCP_BACKUP_PATH.exists()

    def test_migration_is_a_no_op_without_a_legacy_side_file(self):
        copilot.migrate_legacy_mcp_config()

        assert not copilot.copilot_mcp_config_path().exists()


def isolate_copilot_config_paths(monkeypatch, tmp_path):
    import ucode.config_io as config_io
    import ucode.state as state_mod

    app_dir = tmp_path / ".ucode"
    env_path = tmp_path / ".copilot" / "ucode.env"
    monkeypatch.setattr(config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(state_mod, "APP_DIR", app_dir)
    monkeypatch.setattr(state_mod, "STATE_PATH", app_dir / "state.json")
    monkeypatch.setattr(copilot, "COPILOT_ENV_PATH", env_path)
    monkeypatch.setattr(copilot, "COPILOT_BACKUP_PATH", app_dir / "copilot-ucode-env.backup")
    monkeypatch.setattr(copilot, "COPILOT_CONFIG_DIR", env_path.parent)
    return env_path


class TestWriteToolConfig:
    def test_records_the_default_model_and_drops_stale_provider_settings(
        self, tmp_path, monkeypatch
    ):
        env_path = isolate_copilot_config_paths(monkeypatch, tmp_path)
        env_path.parent.mkdir(parents=True)
        env_path.write_text(
            'COPILOT_PROVIDER_TYPE="anthropic"\nCOPILOT_PROVIDER_WIRE_API="responses"\n'
            'USER_SETTING="kept"\n',
            encoding="utf-8",
        )
        state = {"workspace": WS, "codex_models": [GPT]}

        state = copilot.write_tool_config(state, GPT)

        env = copilot.parse_dotenv(env_path)
        assert env == {
            "USER_SETTING": "kept",
            "COPILOT_MODEL": GPT_REF,
            "COPILOT_OFFLINE": "true",
        }
        assert state["managed_configs"]["copilot"]["keys"] == copilot.MANAGED_KEYS

    @pytest.mark.parametrize(("reported", "recorded"), [("1.0.98", "1.0.98"), ("unknown", None)])
    def test_records_the_cli_version_it_found(self, tmp_path, monkeypatch, reported, recorded):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(copilot, "agent_version", lambda binary: reported)

        state = copilot.write_tool_config({"workspace": WS}, "gpt-5")

        assert state.get(copilot.VERSION_STATE_KEY) == recorded

    def test_scrubs_a_token_an_earlier_version_wrote(self, tmp_path, monkeypatch):
        env_path = isolate_copilot_config_paths(monkeypatch, tmp_path)
        env_path.parent.mkdir(parents=True)
        env_path.write_text(
            'COPILOT_PROVIDER_BEARER_TOKEN="old-token"\nOAUTH_TOKEN="old-token"\n',
            encoding="utf-8",
        )

        copilot.write_tool_config({"workspace": WS}, "gpt-5")

        assert "old-token" not in env_path.read_text(encoding="utf-8")

    def test_does_not_back_up_its_own_file_on_reconfigure(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)

        state = copilot.write_tool_config({"workspace": WS}, "gpt-5")
        copilot.write_tool_config(state, "gpt-5")

        assert not copilot.COPILOT_BACKUP_PATH.exists()


class TestScrubStoredCredentials:
    def test_drops_a_token_bearing_backup_so_revert_cannot_restore_it(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        backup = copilot.COPILOT_BACKUP_PATH
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text('COPILOT_PROVIDER_BEARER_TOKEN="old-token"\n', encoding="utf-8")

        copilot.scrub_stored_credentials()

        assert not backup.exists()

    def test_keeps_a_backup_without_credentials(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        backup = copilot.COPILOT_BACKUP_PATH
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text('USER_SETTING="keep"\n', encoding="utf-8")

        copilot.scrub_stored_credentials()

        assert backup.read_text(encoding="utf-8") == 'USER_SETTING="keep"\n'

    def test_leaves_a_credential_free_env_file_untouched(self, tmp_path, monkeypatch):
        env_path = isolate_copilot_config_paths(monkeypatch, tmp_path)
        env_path.parent.mkdir(parents=True)
        env_path.write_text("COPILOT_MODEL=gpt-5\n", encoding="utf-8")

        copilot.scrub_stored_credentials()

        assert env_path.read_text(encoding="utf-8") == "COPILOT_MODEL=gpt-5\n"


class TestProxyHeaders:
    def test_identifies_ucode_and_copilot(self, monkeypatch):
        monkeypatch.setattr(copilot, "agent_version", lambda binary: "1.0.91")
        monkeypatch.setattr(copilot, "ug_version", lambda: "0.9.0")

        headers = copilot.proxy_headers({})

        assert headers["User-Agent"] == f"ucode/0.9.0 copilot/1.0.91 {client_platform()}"

    def test_asks_the_gateway_for_coding_agent_mode(self, monkeypatch):
        monkeypatch.setattr(copilot, "agent_version", lambda binary: "1")

        headers = copilot.proxy_headers({})

        assert headers["x-databricks-use-coding-agent-mode"] == "true"

    def test_adds_request_tags(self, monkeypatch):
        monkeypatch.setattr(copilot, "agent_version", lambda binary: "1")
        monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", '{"team":"ug"}')

        headers = copilot.proxy_headers({})

        assert headers["Databricks-Ai-Gateway-Request-Tags"] == '{"team":"ug"}'

    def test_managed_headers_win_by_name_but_never_replace_authorization(self, monkeypatch):
        monkeypatch.setattr(copilot, "agent_version", lambda binary: "1")
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)

        headers = copilot.proxy_headers(
            {
                "copilot_http_headers": {
                    "user-agent": "admin-agent",
                    "X-Team": "data",
                    "Authorization": "Bearer admin",
                }
            }
        )

        assert headers == {
            "x-databricks-use-coding-agent-mode": "true",
            "user-agent": "admin-agent",
            "X-Team": "data",
        }

    def test_leaves_out_ugs_user_agent_but_not_an_admins(self, monkeypatch):
        def no_version_lookup(binary):
            raise AssertionError("no User-Agent, so no `--version` run")

        monkeypatch.setattr(copilot, "agent_version", no_version_lookup)
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)

        assert copilot.proxy_headers({}, user_agent=False) == {
            "x-databricks-use-coding-agent-mode": "true"
        }
        admin = {"copilot_http_headers": {"User-Agent": "admin-agent"}}
        assert copilot.proxy_headers(admin, user_agent=False)["User-Agent"] == "admin-agent"


class TestTokenProvider:
    def test_uses_the_cli_profile(self, monkeypatch):
        calls = []

        def get_token(workspace, profile=None, *, force_refresh=False):
            calls.append((workspace, profile, force_refresh))
            return "profile-token"

        monkeypatch.setattr(custom_oauth, "get_databricks_token", get_token)

        token = copilot._token_provider({"workspace": WS, "profile": "dev"})(True)

        assert token == "profile-token"
        assert calls == [(WS, "dev", True)]

    def test_uses_custom_oauth_when_configured(self, monkeypatch):
        calls = []

        def get_custom(workspace, client_id, redirect_url, *, scopes, profile, force_refresh):
            calls.append((workspace, client_id, redirect_url, scopes, profile, force_refresh))
            return "custom-token"

        monkeypatch.setattr(custom_oauth, "get_custom_client_token", get_custom)
        state = {
            "workspace": WS,
            "custom_oauth": {
                "client_id": "cid",
                "redirect_url": "http://localhost:8020",
                "scopes": ["all-apis"],
                "profile": "p",
            },
        }

        assert copilot._token_provider(state)(False) == "custom-token"
        assert calls == [(WS, "cid", "http://localhost:8020", ["all-apis"], "p", False)]


class _ExitedProcess:
    def __init__(self, returncode=0, interrupts=0):
        self.returncode = returncode
        self.interrupts = interrupts

    def wait(self):
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt
        return self.returncode


def _stub_launch(monkeypatch, process=None, token="dbx-token"):
    """Run launch against the real proxy with a stubbed Copilot process; return what it saw."""
    seen = {}
    monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *args, **kwargs: token)
    monkeypatch.setattr(copilot, "agent_version", lambda binary: "1")

    def popen(argv, *, env):
        seen["argv"] = argv
        seen["env"] = env
        registry_path = env["COPILOT_PROVIDERS_CONFIG"]
        seen["registry_path"] = registry_path
        seen["registry"] = json.loads(open(registry_path, encoding="utf-8").read())
        provider = seen["registry"]["providers"][0]
        seen["wrong_secret_status"] = httpx.post(
            f"{provider['baseUrl']}/v1/messages",
            headers={"Authorization": "Bearer wrong-secret"},
            content=b"{}",
            timeout=5,
        ).status_code
        if endpoint := env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            api_key = env["OTEL_EXPORTER_OTLP_HEADERS"].removeprefix("x-api-key=")
            seen["metrics_status"] = httpx.post(
                f"{endpoint}/v1/metrics", headers={"x-api-key": api_key}, content=b"", timeout=5
            ).status_code
            seen["unsigned_traces_status"] = httpx.post(
                f"{endpoint}/v1/traces", content=b"", timeout=5
            ).status_code
        return process or _ExitedProcess()

    monkeypatch.setattr(copilot.subprocess_cross_os, "popen", popen)
    return seen


STATE = {"workspace": WS, "copilot_catalog": {"models": [SONNET, GPT]}}


class TestLaunch:
    def test_copilot_gets_the_registry_and_never_the_token(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch, token="dbx-token")

        with pytest.raises(SystemExit) as exit_info:
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions())

        assert exit_info.value.code == 0
        assert seen["env"]["COPILOT_MODEL"] == SONNET_REF
        assert "dbx-token" not in seen["env"].values()
        registry = seen["registry"]
        assert [m["wireModel"] for m in registry["models"]] == [SONNET, GPT]
        secret = registry["providers"][0]["bearerToken"]
        assert secret not in ("", "dbx-token")
        assert secret not in seen["env"].values()
        assert "dbx-token" not in json.dumps(registry)
        # The proxy is live while Copilot runs and refuses other credentials.
        assert seen["wrong_secret_status"] == 401
        assert not os.path.exists(seen["registry_path"])

    def test_tracing_exports_to_the_session_proxy(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)

        with pytest.raises(SystemExit):
            copilot.launch(
                {**STATE, "copilot_otel_tracing": True}, [], options=copilot.LaunchOptions()
            )

        secret = seen["registry"]["providers"][0]["bearerToken"]
        assert seen["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == f"x-api-key={secret}"
        assert seen["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"].startswith("http://127.0.0.1:")
        assert seen["metrics_status"] == 200
        assert seen["unsigned_traces_status"] == 401

    def test_without_tracing_the_proxy_serves_no_otel_path(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)

        with pytest.raises(SystemExit):
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions())

        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in seen["env"]
        assert "COPILOT_OTEL_ENABLED" not in seen["env"]

    def test_explicit_model_is_rewritten_to_the_registry_ref(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)

        with pytest.raises(SystemExit):
            copilot.launch(
                dict(STATE),
                ["-p", "hi", "-m", "gpt-5-4"],
                options=copilot.LaunchOptions(launch_model=SONNET),
            )

        assert seen["argv"] == [
            "copilot",
            "--allow-all-mcp-server-instructions",
            "--session-id",
            seen["argv"][3],
            "-p",
            "hi",
            "--model",
            GPT_REF,
        ]
        assert seen["env"]["COPILOT_MODEL"] == GPT_REF

    def test_the_proxy_rewords_context_overflows_and_drops_reasoning_effort(
        self, tmp_path, monkeypatch
    ):
        from ucode import gateway_proxy

        isolate_copilot_config_paths(monkeypatch, tmp_path)
        _stub_launch(monkeypatch)
        start = gateway_proxy.start_gateway_auth_proxy
        seen = {}

        def start_proxy(*args, **kwargs):
            seen.update(kwargs)
            return start(*args, **kwargs)

        monkeypatch.setattr(gateway_proxy, "start_gateway_auth_proxy", start_proxy)

        with pytest.raises(SystemExit):
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions())

        assert seen["bad_request_rewriter"] is copilot.recognizable_context_overflow
        assert tuple(seen["dropped_chat_completions_fields"]) == ("reasoning_effort",)

    def test_the_proxy_sends_the_session_id_and_marks_subagents(self, tmp_path, monkeypatch):
        from ucode import gateway_proxy

        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)
        start = gateway_proxy.start_gateway_auth_proxy
        proxy = {}

        def start_proxy(*args, **kwargs):
            proxy.update(kwargs)
            return start(*args, **kwargs)

        monkeypatch.setattr(gateway_proxy, "start_gateway_auth_proxy", start_proxy)

        with pytest.raises(SystemExit):
            copilot.launch(dict(STATE), ["-p", "hi"], options=copilot.LaunchOptions())

        session_id = seen["argv"][seen["argv"].index("--session-id") + 1]
        assert str(uuid.UUID(session_id)) == session_id
        assert proxy["added_headers"]["session-id"] == session_id
        subagent = {"x-interaction-type": "conversation-subagent"}
        body = json.dumps({"messages": [{"role": "user", "content": "find the tests"}]}).encode()
        assert proxy["request_headers_adder"](subagent, body)["thread-id"].startswith(
            f"{session_id}-"
        )

    def test_starts_on_the_requested_launch_model(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)

        with pytest.raises(SystemExit):
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions(launch_model=GPT))

        assert seen["env"]["COPILOT_MODEL"] == GPT_REF

    def test_a_wire_model_override_applies_to_the_start_model(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setenv("COPILOT_PROVIDER_WIRE_MODEL", "main.models.my-gpt")
        seen = _stub_launch(monkeypatch)

        with pytest.raises(SystemExit):
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions(launch_model=GPT))

        wire_models = {m["id"]: m["wireModel"] for m in seen["registry"]["models"]}
        assert wire_models["gpt-5-4"] == "main.models.my-gpt"
        assert wire_models["claude-sonnet-5-5"] == SONNET
        assert "COPILOT_PROVIDER_WIRE_MODEL" not in seen["env"]

    def test_unknown_model_fails_before_starting_copilot(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)

        with pytest.raises(RuntimeError, match="isn't available in ug copilot"):
            copilot.launch(dict(STATE), ["--model", "nope"], options=copilot.LaunchOptions())

        assert seen == {}
        assert not list(copilot.COPILOT_CONFIG_DIR.glob("ug-providers-*"))

    def test_warns_about_managed_models_copilot_cannot_use(self, tmp_path, monkeypatch, capsys):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        _stub_launch(monkeypatch)
        state = {"workspace": WS, "copilot_static_models": [GPT, "system.ai.gemini-3-flash"]}

        with pytest.raises(SystemExit):
            copilot.launch(state, [], options=copilot.LaunchOptions())

        assert "Skipping system.ai.gemini-3-flash" in capsys.readouterr().out

    def test_launch_persists_no_state_and_no_token(self, tmp_path, monkeypatch):
        env_path = isolate_copilot_config_paths(monkeypatch, tmp_path)
        _stub_launch(monkeypatch, token="dbx-token")
        saved = []
        monkeypatch.setattr(copilot, "save_state", saved.append)

        with pytest.raises(SystemExit):
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions())

        assert saved == []
        assert not env_path.exists()

    def test_ctrl_c_keeps_waiting_for_copilot(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        _stub_launch(monkeypatch, process=_ExitedProcess(returncode=130, interrupts=2))

        with pytest.raises(SystemExit) as exit_info:
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions())

        assert exit_info.value.code == 130

    def test_proxy_and_registry_stop_when_copilot_cannot_start(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(custom_oauth, "get_databricks_token", lambda *args, **kwargs: "tok")
        monkeypatch.setattr(copilot, "agent_version", lambda binary: "1")
        seen = {}

        def popen(argv, *, env):
            seen["registry"] = env["COPILOT_PROVIDERS_CONFIG"]
            registry = json.loads(open(seen["registry"], encoding="utf-8").read())
            seen["base"] = registry["providers"][0]["baseUrl"]
            raise FileNotFoundError("copilot")

        monkeypatch.setattr(copilot.subprocess_cross_os, "popen", popen)

        with pytest.raises(FileNotFoundError):
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions())

        assert not os.path.exists(seen["registry"])
        with pytest.raises(httpx.ConnectError):
            httpx.post(f"{seen['base']}/v1/messages", content=b"{}", timeout=10)

    def test_provider_service_launch_registers_its_models(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)
        _stub_provider_service(
            monkeypatch,
            {"provider_type": "anthropic", "targets": ["claude-opus-5", "claude-sonnet-5"]},
        )
        state = dict(STATE, copilot_static_models=[GPT])
        state[copilot.LAUNCH_PROVIDER_SERVICE_KEY] = MPS

        with pytest.raises(SystemExit):
            copilot.launch(state, [], options=copilot.LaunchOptions())

        registry = seen["registry"]
        assert [m["id"] for m in registry["models"]] == ["claude-opus-5", "claude-sonnet-5"]
        assert registry["providers"][0]["headers"] == {"Databricks-Model-Provider-Service": MPS}
        assert seen["env"]["COPILOT_MODEL"] == "databricks/claude-sonnet-5"
        assert copilot.LAUNCH_PROVIDER_SERVICE_KEY not in state

    def test_model_location_launch_registers_the_schemas_services(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)
        _stub_location(monkeypatch, ["main.ucode.claude-sonnet-4-6", "main.ucode.claude_haiku"])
        state = dict(STATE, copilot_static_models=[GPT])
        state[copilot.LAUNCH_MODEL_LOCATION_KEY] = "main.ucode"

        with pytest.raises(SystemExit):
            copilot.launch(state, [], options=copilot.LaunchOptions())

        registry = seen["registry"]
        assert [m["id"] for m in registry["models"]] == [
            "main.ucode.claude-sonnet-4-6",
            "main.ucode.claude_haiku",
        ]
        assert "headers" not in registry["providers"][0]
        assert registry["models"][0]["maxPromptTokens"] == 872_000
        assert seen["env"]["COPILOT_MODEL"] == "databricks/main.ucode.claude-sonnet-4-6"
        assert copilot.LAUNCH_MODEL_LOCATION_KEY not in state

    def test_provider_service_launch_rejects_a_model_it_does_not_serve(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)
        _stub_provider_service(
            monkeypatch, {"provider_type": "anthropic", "targets": ["claude-opus-5"]}
        )
        state = dict(STATE)
        state[copilot.LAUNCH_PROVIDER_SERVICE_KEY] = MPS

        with pytest.raises(RuntimeError, match="claude-opus-5"):
            copilot.launch(state, ["--model", SONNET], options=copilot.LaunchOptions())

        assert "argv" not in seen

    def test_no_model_is_a_launch_error(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)

        with pytest.raises(RuntimeError, match="No Copilot model"):
            copilot.launch({"workspace": WS}, [], options=copilot.LaunchOptions())


class TestManagedKeys:
    def test_only_the_model_and_offline_mode(self):
        assert copilot.MANAGED_KEYS == ["COPILOT_MODEL", "COPILOT_OFFLINE"]


class TestMinimumVersion:
    @pytest.mark.parametrize(
        ("version", "blocked"),
        [
            ("GitHub Copilot CLI 1.0.90.", True),
            ("1.0.91", False),
            ("1.1.0", False),
            ("unknown", False),
        ],
    )
    def test_requires_1_0_91(self, monkeypatch, version, blocked):
        monkeypatch.setattr(copilot, "agent_version", lambda binary: version)

        error = copilot.minimum_version_error()

        assert (error is not None) == blocked
        if blocked:
            assert "npm install -g @github/copilot" in error


class TestValidateCmd:
    def test_starts_with_binary(self):
        cmd = copilot.validate_cmd("copilot")
        assert cmd[0] == "copilot"

    def test_has_prompt_flag(self):
        cmd = copilot.validate_cmd("copilot")
        assert "--prompt" in cmd

    def test_lets_mcp_server_instructions_reach_the_model(self):
        cmd = copilot.validate_cmd("copilot")

        assert cmd[:2] == ["copilot", "--allow-all-mcp-server-instructions"]
        assert "--additional-mcp-config" not in cmd


class TestWriteUserMcpServers:
    def test_batched_add_remove_preserves_other_keys(self, tmp_path, monkeypatch):
        path = tmp_path / "mcp-config.json"
        path.write_text(
            json.dumps({"other": 1, "mcpServers": {"mine": {"type": "local"}, "gone": {}}})
        )
        monkeypatch.setattr(copilot, "COPILOT_MCP_CONFIG_PATH", path)

        copilot.write_user_mcp_servers(
            {"svc": copilot.build_mcp_server_entry(["ug", "mcp-proxy", "u"])}, {"gone"}
        )

        doc = json.loads(path.read_text())
        assert doc["other"] == 1
        assert "gone" not in doc["mcpServers"]
        assert doc["mcpServers"]["mine"] == {"type": "local"}
        assert doc["mcpServers"]["svc"]["command"] == "ug"


SESSION = "0b6f3a52-2c5e-4d1e-9a51-1f2b8e4c7d90"


class TestSessionArgs:
    def test_a_new_session_gets_a_new_id_that_copilot_uses_too(self):
        args, session_id = copilot.session_args(["-p", "hi"])

        assert args == ["--session-id", session_id, "-p", "hi"]
        assert str(uuid.UUID(session_id)) == session_id

    @pytest.mark.parametrize(
        "args",
        [
            ["--session-id", SESSION],
            [f"--session-id={SESSION}"],
            ["--resume", SESSION.upper()],
            ["-r", SESSION, "-p", "hi"],
            [f"--resume={SESSION}"],
        ],
    )
    def test_a_session_named_by_uuid_keeps_its_id(self, args):
        assert copilot.session_args(args) == (args, SESSION)

    @pytest.mark.parametrize(
        "args",
        [["--continue"], ["--resume"], ["-r", "-p", "hi"], ["--resume=my-session"], ["sessions"]],
    )
    def test_a_session_picked_otherwise_gets_an_id_of_this_launch(self, args):
        new_args, session_id = copilot.session_args(args)

        assert new_args == args
        assert str(uuid.UUID(session_id)) == session_id
        assert copilot.session_args(args)[1] != session_id


class TestSubagentHeaders:
    SUBAGENT = {"x-interaction-type": "conversation-subagent"}

    def setup_method(self):
        self.adder = copilot.subagent_headers(SESSION)

    def _id(self, body, headers=SUBAGENT):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return self.adder(headers, raw).get("thread-id")

    def test_each_subagent_keeps_one_id_over_its_turns(self):
        first = {
            "messages": [{"role": "user", "content": [{"type": "text", "text": "find tests"}]}]
        }
        later = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "find tests",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
            ]
        }
        other = {"messages": [{"role": "user", "content": "read the docs"}]}

        assert self._id(first) == self._id(later)
        assert self._id(first).startswith(f"{SESSION}-")
        assert self._id(other) != self._id(first)

    def test_reads_the_first_user_message_of_each_api(self):
        prompt = "find tests"
        anthropic = {"system": "s", "messages": [{"role": "user", "content": prompt}]}
        completions = {
            "messages": [
                {"role": "system", "content": "the same for every subagent"},
                {"role": "user", "content": [{"type": "text", "text": prompt}]},
            ]
        }
        responses = {
            "instructions": "s",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
        }

        assert self._id(anthropic) == self._id(completions) == self._id(responses)
        assert self._id({"input": prompt}) == self._id(anthropic)

    @pytest.mark.parametrize(
        ("headers", "body"),
        [
            (
                {"x-interaction-type": "conversation-user"},
                {"messages": [{"role": "user", "content": "hi"}]},
            ),
            ({}, {"messages": [{"role": "user", "content": "hi"}]}),
            (SUBAGENT, b"not json"),
            (SUBAGENT, {"messages": [{"role": "assistant", "content": "hi"}]}),
            (SUBAGENT, ["not", "an", "object"]),
        ],
    )
    def test_no_id_for_the_main_agent_or_a_body_it_cannot_read(self, headers, body):
        assert self._id(body, headers) is None

    def test_a_lone_surrogate_still_gets_an_id(self):
        body = b'{"messages": [{"role": "user", "content": "cut \\ud83d"}]}'

        assert self._id(body).startswith(f"{SESSION}-")

    def test_the_id_is_keyed_for_the_launch(self):
        body = json.dumps({"messages": [{"role": "user", "content": "find tests"}]}).encode()
        one, other = copilot.subagent_headers(SESSION), copilot.subagent_headers(SESSION)

        assert one(self.SUBAGENT, body) == one(self.SUBAGENT, body)
        assert one(self.SUBAGENT, body) != other(self.SUBAGENT, body)

    def test_without_a_launch_session_the_id_is_under_the_requests_own_session(self):
        adder = copilot.subagent_headers()
        body = json.dumps({"messages": [{"role": "user", "content": "find tests"}]}).encode()
        other_session = str(uuid.uuid4())

        mine = adder({**self.SUBAGENT, "x-client-session-id": SESSION}, body)["thread-id"]
        theirs = adder({**self.SUBAGENT, "x-client-session-id": other_session}, body)["thread-id"]

        assert mine.startswith(f"{SESSION}-")
        assert theirs.startswith(f"{other_session}-")
        assert adder(self.SUBAGENT, body) == {}


class TestWithSessionId:
    def test_adds_the_session_id_unless_an_admin_header_sets_it(self):
        assert copilot.with_session_id({"User-Agent": "u"}, SESSION) == {
            "session-id": SESSION,
            "User-Agent": "u",
        }
        assert copilot.with_session_id({"Session-Id": "admin"}, SESSION) == {"Session-Id": "admin"}


VSCODE_RUNTIME_UA = "copilot/1.0.111-0 (linux unknown) term/unknown client/vscode"


class TestSeatRuntimeHeaders:
    @pytest.fixture(autouse=True)
    def _ug_version(self, monkeypatch):
        monkeypatch.setattr(copilot, "ug_version", lambda: "0.9.0")

    @pytest.mark.parametrize(
        ("runtime", "expected"),
        [
            (VSCODE_RUNTIME_UA, "copilot/1.0.111-0 term/unknown client/vscode"),
            ("copilot/1.0.112", "copilot/1.0.112"),
            (None, "copilot/unknown"),
            ("", "copilot/unknown"),
            ("node (linux)", "copilot/unknown"),
            ("other/1 client/vscode", "copilot/unknown other/1 client/vscode"),
        ],
    )
    def test_user_agent_is_ugs_around_the_runtimes_products(self, runtime, expected):
        assert copilot.seat_runtime_user_agent(runtime) == (
            f"ucode/0.9.0 {expected} {client_platform()}"
        )

    def test_each_call_gets_its_own_chats_session_id(self):
        adder = copilot.seat_runtime_headers()
        other_chat = str(uuid.uuid4())

        one = adder({"User-Agent": VSCODE_RUNTIME_UA, "x-client-session-id": SESSION}, b"{}")
        other = adder({"User-Agent": VSCODE_RUNTIME_UA, "x-client-session-id": other_chat}, b"{}")

        assert one == {
            "User-Agent": f"ucode/0.9.0 copilot/1.0.111-0 term/unknown client/vscode "
            f"{client_platform()}",
            "session-id": SESSION,
        }
        assert other["session-id"] == other_chat

    @pytest.mark.parametrize("value", [None, "", "not-a-uuid"])
    def test_a_call_without_a_session_gets_no_session_id(self, value):
        headers = {"User-Agent": VSCODE_RUNTIME_UA}
        if value is not None:
            headers["x-client-session-id"] = value

        added = copilot.seat_runtime_headers()(headers, b"{}")

        assert "session-id" not in added
        assert added["User-Agent"].startswith("ucode/0.9.0 copilot/1.0.111-0 ")

    def test_a_subagent_call_gets_an_id_under_its_chat(self):
        body = json.dumps({"messages": [{"role": "user", "content": "find tests"}]}).encode()
        headers = {
            "User-Agent": VSCODE_RUNTIME_UA,
            "x-client-session-id": SESSION,
            "x-interaction-type": "conversation-subagent",
        }

        added = copilot.seat_runtime_headers()(headers, body)

        assert added["session-id"] == SESSION
        assert added["thread-id"].startswith(f"{SESSION}-")


SEAT_MPS = "main.ai.copilot_seats"
SEAT_SERVICE = {"provider_type": "github_copilot", "targets": ["gpt-5.5"], "relayed": True}


def _stub_seat_launch(monkeypatch, *, returncode=0, host="api.business.githubcopilot.com"):
    from ucode import copilot_relay

    seen: dict = {}
    monkeypatch.setattr(copilot_relay, "cached_copilot_api_host", lambda *_a: host)

    def popen(argv, *, env):
        seen["argv"] = argv
        seen["env"] = env
        # The relay is live while Copilot runs, and refuses a call without a GitHub token.
        seen["unsigned_status"] = httpx.get(f"{env['COPILOT_API_URL']}/meta", timeout=5).status_code
        return _ExitedProcess(returncode)

    monkeypatch.setattr(copilot.subprocess_cross_os, "popen", popen)
    return seen


class TestSeatRelay:
    def test_env_signs_copilot_in_and_sends_its_calls_to_the_relay(self, monkeypatch):
        for key in ("COPILOT_OFFLINE", "COPILOT_MODEL", "COPILOT_PROVIDERS_CONFIG"):
            monkeypatch.setenv(key, "x")
        monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "byok")
        monkeypatch.setenv("GITHUB_MCP_URL_OVERRIDE", "https://elsewhere/mcp")

        env = copilot.seat_relay_env("http://127.0.0.1:9/s", mcp_host="api.githubcopilot.com")

        assert env["COPILOT_API_URL"] == "http://127.0.0.1:9/s"
        assert env["COPILOT_CLI_DISABLE_WEBSOCKET_RESPONSES"] == "true"
        assert env["GITHUB_MCP_URL_OVERRIDE"] == "https://api.githubcopilot.com/mcp/readonly"
        for key in ("COPILOT_OFFLINE", "COPILOT_MODEL", "COPILOT_PROVIDERS_CONFIG"):
            assert key not in env
        assert "COPILOT_PROVIDER_API_KEY" not in env
        assert "GITHUB_MCP_URL_OVERRIDE" not in copilot.seat_relay_env("u", mcp_host=None)

    def test_env_keeps_a_proxy_away_from_the_loopback_relay(self, monkeypatch):
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        env = copilot.seat_relay_env("http://127.0.0.1:9/s", mcp_host=None)
        assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost"

        monkeypatch.setenv("NO_PROXY", "internal.example, .corp.example")
        expected = "internal.example,.corp.example,127.0.0.1,localhost"
        if os.name != "nt":
            # Windows has one case-insensitive variable, so this would replace NO_PROXY.
            monkeypatch.setenv("no_proxy", "localhost")
            expected = "internal.example,.corp.example,localhost,127.0.0.1"
        env = copilot.seat_relay_env("http://127.0.0.1:9/s", mcp_host=None)
        assert env["NO_PROXY"] == env["no_proxy"] == expected

    def test_a_service_lookup_error_is_raised_not_read_as_no_seat_relay(self, monkeypatch):
        _stub_provider_service(monkeypatch, None)

        with pytest.raises(RuntimeError, match="was not found"):
            copilot.uses_seat_relay(dict(STATE), SEAT_MPS)

    def test_launch_runs_copilot_signed_in_through_the_relay(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        _stub_provider_service(monkeypatch, SEAT_SERVICE)
        seen = _stub_seat_launch(monkeypatch, returncode=3)
        state = dict(STATE)
        state[copilot.LAUNCH_PROVIDER_SERVICE_KEY] = SEAT_MPS

        with pytest.raises(SystemExit) as exit_info:
            copilot.launch(state, ["--model", "gpt-5.5"], options=copilot.LaunchOptions())

        assert exit_info.value.code == 3
        env = seen["env"]
        assert env["COPILOT_API_URL"].startswith("http://127.0.0.1:")
        assert "COPILOT_OFFLINE" not in env and "COPILOT_PROVIDERS_CONFIG" not in env
        assert (
            env["GITHUB_MCP_URL_OVERRIDE"] == "https://api.business.githubcopilot.com/mcp/readonly"
        )
        assert "dbx-token" not in env.values()
        assert seen["argv"][:2] == [copilot.SPEC["binary"], *copilot.MCP_LAUNCH_ARGS]
        assert seen["argv"][2] == "--session-id"
        assert seen["argv"][4:] == ["--model", "gpt-5.5"]
        assert seen["unsigned_status"] == 401
        assert copilot.LAUNCH_PROVIDER_SERVICE_KEY not in state

    def test_env_traces_to_the_relay_when_the_workspace_traces_copilot(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer other")
        monkeypatch.setenv("COPILOT_OTEL_FILE_EXPORTER_PATH", "/tmp/spans")

        traced = copilot.seat_relay_env("http://127.0.0.1:9/s", mcp_host=None, tracing=True)
        untraced = copilot.seat_relay_env("http://127.0.0.1:9/s", mcp_host=None)

        assert traced["COPILOT_OTEL_ENABLED"] == "true"
        assert traced["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:9/s/ai-gateway/otel"
        assert traced["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
        # The secret is already in the endpoint's path.
        assert "OTEL_EXPORTER_OTLP_HEADERS" not in traced
        assert "COPILOT_OTEL_FILE_EXPORTER_PATH" not in traced
        assert "COPILOT_OTEL_ENABLED" not in untraced
        assert not any(key.startswith(copilot._INHERITED_OTEL_PREFIXES) for key in untraced)

    def test_keep_inherited_otel_is_exclusive_with_tracing(self):
        with pytest.raises(ValueError):
            copilot.seat_relay_env("u", mcp_host=None, tracing=True, keep_inherited_otel=True)

    def test_relay_serves_copilots_otel_path(self):
        from ucode import copilot_relay

        assert copilot_relay.OTEL_PATH == copilot.OTEL_PATH

    def test_traced_launch_exports_to_the_live_relay(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        _stub_provider_service(monkeypatch, SEAT_SERVICE)
        from ucode import copilot_relay

        monkeypatch.setattr(copilot_relay, "cached_copilot_api_host", lambda *_a: None)
        seen: dict = {}

        def popen(argv, *, env):
            seen["env"] = env
            endpoint = env["OTEL_EXPORTER_OTLP_ENDPOINT"]
            seen["metrics_status"] = httpx.post(
                f"{endpoint}/v1/metrics", content=b"points", timeout=5
            ).status_code
            return _ExitedProcess(0)

        monkeypatch.setattr(copilot.subprocess_cross_os, "popen", popen)
        state = {
            **STATE,
            "copilot_otel_tracing": True,
            copilot.LAUNCH_PROVIDER_SERVICE_KEY: SEAT_MPS,
        }

        with pytest.raises(SystemExit):
            copilot.launch(state, [], options=copilot.LaunchOptions())

        env = seen["env"]
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"{env['COPILOT_API_URL']}/ai-gateway/otel"
        assert seen["metrics_status"] == 200

    def test_launch_warns_about_copilots_own_api_url_setting(self, tmp_path, monkeypatch, capsys):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        home = tmp_path / "copilot-home"
        home.mkdir()
        (home / "settings.json").write_text(json.dumps({"copilotUrl": "https://example.test"}))
        monkeypatch.setenv("COPILOT_HOME", str(home))
        _stub_provider_service(monkeypatch, SEAT_SERVICE)
        _stub_seat_launch(monkeypatch)
        state = {**STATE, copilot.LAUNCH_PROVIDER_SERVICE_KEY: SEAT_MPS}

        with pytest.raises(SystemExit):
            copilot.launch(state, [], options=copilot.LaunchOptions())

        printed = " ".join(capsys.readouterr().out.split())
        assert "`copilotUrl` setting (https://example.test)" in printed

    def test_a_service_that_doesnt_relay_a_seat_launches_offline(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)
        _stub_provider_service(
            monkeypatch, {"provider_type": "anthropic", "targets": ["claude-sonnet-5"]}
        )
        state = {**STATE, copilot.LAUNCH_PROVIDER_SERVICE_KEY: MPS}

        with pytest.raises(SystemExit):
            copilot.launch(state, [], options=copilot.LaunchOptions())

        assert seen["env"]["COPILOT_OFFLINE"] == "true"

    def test_vscode_runtime_runs_through_the_relay_and_keeps_stdout_clear(
        self, tmp_path, monkeypatch, capsys
    ):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(copilot, "launch_token", lambda state, workspace, **_: "dbx-token")
        seen = _stub_seat_launch(monkeypatch, returncode=5)
        sigterm = signal.getsignal(signal.SIGTERM)

        returncode = copilot.run_seat_runtime(
            dict(STATE), SEAT_MPS, "/vscode/copilot-runtime", ["--stdio", "--headless"]
        )

        assert returncode == 5
        assert signal.getsignal(signal.SIGTERM) is sigterm
        assert seen["argv"] == ["/vscode/copilot-runtime", "--stdio", "--headless"]
        assert seen["env"]["COPILOT_API_URL"].startswith("http://127.0.0.1:")
        assert seen["unsigned_status"] == 401
        assert capsys.readouterr().out == ""

    def test_vscode_runtime_keeps_the_otel_settings_vscode_gave_it(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(copilot, "launch_token", lambda state, workspace, **_: "dbx-token")
        monkeypatch.setenv("COPILOT_OTEL_ENABLED", "true")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318/vscode")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/t")
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://byok.example")
        seen = _stub_seat_launch(monkeypatch)

        copilot.run_seat_runtime(dict(STATE), SEAT_MPS, "/vscode/copilot-runtime", [])

        assert seen["env"]["COPILOT_OTEL_ENABLED"] == "true"
        assert seen["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318/vscode"
        assert seen["env"]["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://127.0.0.1:4318/t"
        assert "COPILOT_PROVIDER_BASE_URL" not in seen["env"]

    @pytest.mark.skipif(
        sys.platform != "win32", reason="only Windows leaves a terminated process's children up"
    )
    def test_windows_runtime_stops_when_the_wrapper_is_terminated(self, tmp_path):
        watching, stopped = tmp_path / "watching", tmp_path / "stopped"
        ug = textwrap.dedent(
            f"""
            import subprocess, sys
            from ucode.agents import copilot
            runtime = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            copilot._stop_with_wrapper(runtime)
            open({str(watching)!r}, "w").close()
            runtime.wait()
            open({str(stopped)!r}, "w").close()
            """
        )
        # The `ug.exe` launcher between the wrapper and ug's Python, which outlives the wrapper.
        launcher = (
            f"import subprocess, sys; sys.exit(subprocess.call([sys.executable, '-c', {ug!r}]))"
        )
        wrapper = textwrap.dedent(
            f"""
            import os, subprocess, sys, time
            env = {{**os.environ, {copilot.RUNTIME_WRAPPER_PID_ENV!r}: str(os.getpid())}}
            subprocess.Popen([sys.executable, "-c", {launcher!r}], env=env)
            time.sleep(120)
            """
        )
        process = subprocess.Popen([sys.executable, "-c", wrapper])
        deadline = time.monotonic() + 60
        while not watching.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert watching.exists()

        # How VS Code stops the runtime: it terminates the process it started.
        process.kill()
        process.wait()

        deadline = time.monotonic() + 30
        while not stopped.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert stopped.exists()

    def test_a_wrapper_pid_that_isnt_a_number_starts_no_watch(self, monkeypatch):
        monkeypatch.setattr(copilot.sys, "platform", "win32")
        monkeypatch.setenv(copilot.RUNTIME_WRAPPER_PID_ENV, "not-a-pid")

        def no_thread(*args, **kwargs):
            raise AssertionError("started a watch")

        monkeypatch.setattr(copilot.threading, "Thread", no_thread)

        copilot._stop_with_wrapper(object())

    def test_vscode_runtime_relay_labels_each_call_and_lets_admin_headers_win(
        self, tmp_path, monkeypatch
    ):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(copilot, "launch_token", lambda state, workspace, **_: "dbx-token")
        monkeypatch.setattr(copilot, "ug_version", lambda: "0.9.0")
        _stub_seat_launch(monkeypatch)
        started: dict = {}
        start = copilot.start_seat_relay

        def start_seat_relay(*args, **kwargs):
            started.update(kwargs)
            return start(*args, **kwargs)

        monkeypatch.setattr(copilot, "start_seat_relay", start_seat_relay)
        state = {**STATE, "copilot_http_headers": {"session-id": "admin-session"}}

        assert copilot.run_seat_runtime(state, SEAT_MPS, "/vscode/copilot-runtime", []) == 0

        assert "user-agent" not in {name.casefold() for name in started["headers"]}
        assert started["headers"]["session-id"] == "admin-session"
        added = started["request_headers_adder"](
            {"User-Agent": VSCODE_RUNTIME_UA, "x-client-session-id": SESSION}, b"{}"
        )
        assert added["User-Agent"].startswith("ucode/0.9.0 copilot/1.0.111-0 ")

    def test_vscode_runtime_that_cant_start_reports_it_on_stderr(
        self, tmp_path, monkeypatch, capsys
    ):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(copilot, "launch_token", lambda state, workspace, **_: "dbx-token")

        def popen(argv, *, env):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(copilot.subprocess_cross_os, "popen", popen)

        assert copilot.run_seat_runtime(dict(STATE), SEAT_MPS, "/missing", []) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Couldn't start the Copilot runtime /missing" in captured.err

    @pytest.mark.parametrize(
        "failure",
        [
            DatabricksLoginRequiredError("no cached credentials for https://ws.example"),
            FileNotFoundError("databricks"),
        ],
        ids=["lapsed-login", "missing-cli"],
    )
    def test_vscode_runtime_with_no_databricks_token_prints_the_hint_instead_of_a_traceback(
        self, tmp_path, monkeypatch, capsys, failure
    ):
        isolate_copilot_config_paths(monkeypatch, tmp_path)

        def mint(state, workspace, **_):
            raise failure

        monkeypatch.setattr(copilot, "launch_token", mint)
        started = []
        monkeypatch.setattr(
            copilot.subprocess_cross_os, "popen", lambda argv, *, env: started.append(argv)
        )

        assert copilot.run_seat_runtime(dict(STATE), SEAT_MPS, "/vscode/copilot-runtime", []) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Couldn't start the seat relay for VS Code's Copilot" in captured.err
        assert str(failure) in captured.err
        assert "Run `ug configure --agents copilot`, then restart VS Code." in captured.err
        assert started == []


class TestSeatRelayJourney:
    """`ug copilot` on a seat relay, against a fake gateway, with a client that honours the launch
    environment's proxy variables the way Copilot does."""

    def test_launch_relays_the_signed_in_calls_and_keeps_a_proxy_off_the_relay(
        self, tmp_path, monkeypatch
    ):
        import urllib.error
        import urllib.request
        from unittest import mock

        from tests.test_copilot_relay import _Upstream

        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "no_proxy"):
            monkeypatch.delenv(key, raising=False)
        _stub_provider_service(monkeypatch, SEAT_SERVICE)
        from ucode import copilot_relay

        monkeypatch.setattr(
            copilot_relay, "cached_copilot_api_host", lambda *_a: "api.business.githubcopilot.com"
        )
        gateway = _Upstream()
        proxy = _Upstream()
        seen: dict = {}

        def popen(argv, *, env):
            seen["env"] = env
            # Copilot resolves proxies from its environment: the launch env names a proxy for
            # everything but loopback, so the relay's requests must reach the relay, not the proxy.
            proxied = {
                "HTTP_PROXY": proxy.base_url,
                "NO_PROXY": env["NO_PROXY"],
                "no_proxy": env["no_proxy"],
            }
            with mock.patch.dict(os.environ, proxied):
                opener = urllib.request.build_opener(urllib.request.ProxyHandler())
                request = urllib.request.Request(
                    f"{env['COPILOT_API_URL']}/meta", headers={"Authorization": "Bearer gho_cli"}
                )
                with opener.open(request, timeout=5) as response:
                    seen["meta"] = response.status
                # The relay retries one 403 after a token refresh before giving up.
                gateway.statuses.extend([403, 403])
                try:
                    opener.open(request, timeout=5)
                except urllib.error.HTTPError as error:
                    seen["denied"] = error.code
            return _ExitedProcess(0)

        monkeypatch.setattr(copilot.subprocess_cross_os, "popen", popen)
        state = dict(STATE, workspace=gateway.base_url)
        state[copilot.LAUNCH_PROVIDER_SERVICE_KEY] = SEAT_MPS
        try:
            with pytest.raises(SystemExit) as exit_info:
                copilot.launch(state, [], options=copilot.LaunchOptions())
        finally:
            gateway.close()
            proxy.close()

        assert exit_info.value.code == 0
        env = seen["env"]
        assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost"
        assert seen["meta"] == 200
        # Copilot shows the body of a 400 and hides a 403's.
        assert seen["denied"] == 400
        assert proxy.requests == []
        relayed, denied, retried = gateway.requests
        assert relayed["path"] == "/ai-gateway/github-copilot/v1/meta"
        assert relayed["headers"]["authorization"] == "Bearer gho_cli"
        assert relayed["headers"]["x-databricks-ai-gateway-token"] == "Bearer dbx-token"
        assert relayed["headers"]["databricks-model-provider-service"] == SEAT_MPS
        assert relayed["headers"]["x-databricks-github-copilot-api-host"] == (
            "api.business.githubcopilot.com"
        )
        assert denied["path"] == retried["path"] == relayed["path"]
