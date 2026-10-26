"""Tests for agents/copilot.py."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from ucode import custom_oauth
from ucode.agents import copilot
from ucode.databricks import AnthropicModelCatalog, ModelServiceEntry
from ucode.telemetry import client_platform

WS = "https://example.databricks.com"
PROXY = "http://127.0.0.1:4321"
SONNET = "system.ai.claude-sonnet-5-5"
GPT = "system.ai.gpt-5-4"


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
        ],
    )
    def test_names_decide_the_wire(self, model, wire):
        assert copilot.classify_copilot_model(model) == wire

    @pytest.mark.parametrize(
        ("model", "reason"),
        [
            ("system.ai.gemini-3-flash", "Gemini"),
            ("system.ai.gpt-oss-120b", "Claude and GPT"),
            ("system.ai.meta-llama-3-3-70b-instruct", "Claude and GPT"),
            ("system.ai.gte-large-en", "Claude and GPT"),
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


class TestBuildCopilotCatalog:
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

    def test_uses_the_listing_routes_and_drops_duplicates(self):
        models = copilot.build_copilot_catalog(
            [
                ModelServiceEntry(SONNET, ("anthropic/v1/messages",)),
                ModelServiceEntry("system.ai.gte-large-en", ("mlflow/v1/embeddings",)),
                SONNET,
            ]
        )

        assert models == [copilot.CopilotModel(SONNET, "anthropic")]
        assert models[0].ref == f"dbx-anthropic/{SONNET}"


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
        assert f"dbx-anthropic: databricks-claude-opus-4-7, {SONNET}" in message
        assert f"dbx-responses: {GPT}" in message


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
        ],
    )
    def test_defaults_to_sonnet_then_opus_then_gpt(self, ids, expected):
        assert copilot.launch_model({}, catalog(*ids)).id == expected

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
        assert rewritten[-2:] == ["--model", f"dbx-responses/{GPT}"]

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


def _stub_location(monkeypatch, model_ids, error=None):
    monkeypatch.setattr(copilot, "launch_token", lambda state, workspace, **_: "dbx-token")
    seen = {}

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

    def test_an_empty_schema_says_so(self, monkeypatch):
        _stub_location(monkeypatch, [], error="AI Gateway returned no Anthropic model ids")

        with pytest.raises(RuntimeError, match="no model services to use in main.ucode"):
            copilot.location_catalog({"workspace": WS}, "main.ucode")


class TestCopilotModelLimits:
    @pytest.mark.parametrize(
        ("model", "limits"),
        [
            ("system.ai.claude-sonnet-5-5", (872_000, 128_000)),
            ("system.ai.claude-haiku-5-5", (872_000, 128_000)),
            ("system.ai.claude-haiku-4-5", (136_000, 64_000)),
            ("system.ai.claude-sonnet-4-5", (168_000, 32_000)),
            ("system.ai.claude-opus-4-8", (936_000, 32_000)),
            ("system.ai.claude-opus-6", (936_000, 32_000)),
            ("system.ai.claude-sonnet-4-7", (936_000, 32_000)),
            ("system.ai.claude-opus-4-0", (168_000, 32_000)),
        ],
    )
    def test_claude_limits(self, model, limits):
        assert copilot.copilot_model_limits(model) == limits

    def test_gpt_keeps_copilots_defaults(self):
        assert copilot.copilot_model_limits(GPT) is None


class TestRenderProvidersConfig:
    def test_one_provider_per_wire_in_use_on_the_proxy(self):
        config = copilot.render_providers_config(catalog(SONNET, GPT), PROXY, "secret")

        assert config["providers"] == [
            {
                "name": "dbx-anthropic",
                "type": "anthropic",
                "baseUrl": f"{PROXY}/ai-gateway/anthropic",
                "bearerToken": "secret",
            },
            {
                "name": "dbx-responses",
                "type": "openai",
                "wireApi": "responses",
                "baseUrl": f"{PROXY}/ai-gateway/mlflow/v1",
                "bearerToken": "secret",
            },
        ]

    def test_completions_provider(self):
        config = copilot.render_providers_config(catalog("system.ai.gpt-4-1"), PROXY, "secret")
        assert [(p["name"], p.get("wireApi")) for p in config["providers"]] == [
            ("dbx-completions", "completions")
        ]

    def test_models_carry_claude_settings_and_limits(self):
        config = copilot.render_providers_config(catalog(SONNET, GPT), PROXY, "secret")

        assert config["models"] == [
            {
                "id": SONNET,
                "provider": "dbx-anthropic",
                "wireModel": SONNET,
                "name": SONNET,
                "modelId": "claude-sonnet-5.5",
                "maxPromptTokens": 872_000,
                "maxOutputTokens": 128_000,
            },
            {"id": GPT, "provider": "dbx-responses", "wireModel": GPT, "name": GPT},
        ]

    def test_provider_service_routes_every_provider_by_header(self):
        claude, _ = provider_models(["claude-sonnet-4-6"], "anthropic")
        gpt, _ = provider_models(["gpt-5.5", "gpt-4o"], "openai")

        config = copilot.render_providers_config(
            claude + gpt, PROXY, "secret", provider_service=MPS
        )

        assert config["providers"] == [
            {
                "name": "dbx-anthropic",
                "type": "anthropic",
                "baseUrl": f"{PROXY}/ai-gateway/anthropic",
                "bearerToken": "secret",
                "headers": {"Databricks-Model-Provider-Service": MPS},
            },
            {
                "name": "dbx-responses",
                "type": "openai",
                "wireApi": "responses",
                "baseUrl": f"{PROXY}/ai-gateway/openai/v1",
                "bearerToken": "secret",
                "headers": {"Databricks-Model-Provider-Service": MPS},
            },
            {
                "name": "dbx-completions",
                "type": "openai",
                "wireApi": "completions",
                "baseUrl": f"{PROXY}/ai-gateway/openai/v1",
                "bearerToken": "secret",
                "headers": {"Databricks-Model-Provider-Service": MPS},
            },
        ]

    def test_provider_service_claude_keeps_its_id_with_200k_limits(self):
        claude, _ = provider_models(["claude-sonnet-4-6"], "anthropic")

        config = copilot.render_providers_config(claude, PROXY, "secret", provider_service=MPS)

        assert config["models"] == [
            {
                "id": "claude-sonnet-4-6",
                "provider": "dbx-anthropic",
                "wireModel": "claude-sonnet-4-6",
                "name": "claude-sonnet-4-6",
                "modelId": "claude-sonnet-4.6",
                "maxPromptTokens": 168_000,
                "maxOutputTokens": 32_000,
            }
        ]


class TestBuildRuntimeEnv:
    def test_points_copilot_at_the_registry(self, tmp_path):
        env = copilot.build_runtime_env(tmp_path / "providers.json", catalog(GPT)[0])

        assert env["COPILOT_PROVIDERS_CONFIG"] == str(tmp_path / "providers.json")
        assert env["COPILOT_MODEL"] == f"dbx-responses/{GPT}"
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

    @staticmethod
    def _stub_cli(monkeypatch, *, fail_add=False):
        calls: list[list[str]] = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[2] == "add" and fail_add:
                raise subprocess.CalledProcessError(1, argv)
            stderr = f'Server "{argv[3]}" not found.' if argv[2] == "remove" else ""
            return subprocess.CompletedProcess(argv, 1 if stderr else 0, "", stderr)

        monkeypatch.setattr(copilot.subprocess_cross_os, "run", run)
        return calls

    def test_defers_to_the_copilot_cli_for_a_config_it_cannot_parse(self, monkeypatch):
        calls = self._stub_cli(monkeypatch)
        path = copilot.copilot_mcp_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        jsonc = '{\n  // mine\n  "mcpServers": {"mine": {"x": 1},},\n}\n'
        path.write_text(jsonc, encoding="utf-8")
        copilot.LEGACY_MCP_CONFIG_PATH.write_text(json.dumps({"mcpServers": {"old": self.ENTRY}}))

        removed = copilot.write_user_mcp_servers({"svc": self.ENTRY}, {"gone"})

        assert removed == set()
        assert calls == [
            ["copilot", "mcp", "remove", "gone"],
            ["copilot", "mcp", "remove", "old"],
            ["copilot", "mcp", "add", "old", "--", "ug", "mcp-proxy"],
            ["copilot", "mcp", "remove", "svc"],
            ["copilot", "mcp", "add", "svc", "--", "ug", "mcp-proxy"],
        ]
        assert path.read_text(encoding="utf-8") == jsonc
        assert not copilot.LEGACY_MCP_CONFIG_PATH.exists()

    def test_failed_cli_add_keeps_the_legacy_side_file(self, monkeypatch):
        self._stub_cli(monkeypatch, fail_add=True)
        path = copilot.copilot_mcp_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        copilot.LEGACY_MCP_CONFIG_PATH.write_text(json.dumps({"mcpServers": {"old": self.ENTRY}}))

        with pytest.raises(RuntimeError, match="Failed to add MCP server 'old'"):
            copilot.migrate_legacy_mcp_config()

        assert path.read_text(encoding="utf-8") == "{not json"
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
            "COPILOT_MODEL": f"dbx-responses/{GPT}",
            "COPILOT_OFFLINE": "true",
        }
        assert state["managed_configs"]["copilot"]["keys"] == copilot.MANAGED_KEYS

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

        assert headers == {"user-agent": "admin-agent", "X-Team": "data"}


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
        assert seen["env"]["COPILOT_MODEL"] == f"dbx-anthropic/{SONNET}"
        assert "dbx-token" not in seen["env"].values()
        registry = seen["registry"]
        assert [m["id"] for m in registry["models"]] == [SONNET, GPT]
        secret = registry["providers"][0]["bearerToken"]
        assert secret not in ("", "dbx-token")
        assert secret not in seen["env"].values()
        assert "dbx-token" not in json.dumps(registry)
        # The proxy is live while Copilot runs and refuses other credentials.
        assert seen["wrong_secret_status"] == 401
        assert not os.path.exists(seen["registry_path"])

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
            "-p",
            "hi",
            "--model",
            f"dbx-responses/{GPT}",
        ]
        assert seen["env"]["COPILOT_MODEL"] == f"dbx-responses/{GPT}"

    def test_starts_on_the_requested_launch_model(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        seen = _stub_launch(monkeypatch)

        with pytest.raises(SystemExit):
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions(launch_model=GPT))

        assert seen["env"]["COPILOT_MODEL"] == f"dbx-responses/{GPT}"

    def test_a_wire_model_override_applies_to_the_start_model(self, tmp_path, monkeypatch):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setenv("COPILOT_PROVIDER_WIRE_MODEL", "main.models.my-gpt")
        seen = _stub_launch(monkeypatch)

        with pytest.raises(SystemExit):
            copilot.launch(dict(STATE), [], options=copilot.LaunchOptions(launch_model=GPT))

        wire_models = {m["id"]: m["wireModel"] for m in seen["registry"]["models"]}
        assert wire_models[GPT] == "main.models.my-gpt"
        assert wire_models[SONNET] == SONNET
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
        assert seen["env"]["COPILOT_MODEL"] == "dbx-anthropic/claude-sonnet-5"
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
        assert registry["models"][0]["maxPromptTokens"] == 168_000
        assert seen["env"]["COPILOT_MODEL"] == "dbx-anthropic/main.ucode.claude-sonnet-4-6"
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
