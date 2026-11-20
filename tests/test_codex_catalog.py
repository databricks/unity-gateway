"""Offline catalog construction and subprocess boundary tests (no installed CLI required)."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ucode import databricks as db_mod
from ucode.agents import codex_catalog as catalog


@pytest.fixture
def bundled():
    return [
        {
            "slug": slug,
            "display_name": slug,
            "base_instructions": f"Prompt for {slug}",
            "model_messages": {"instructions_template": f"Template for {slug}"},
            "future_field": {"nested": [1]},
            "tool_mode": None if slug == "gpt-5.2" else "code_mode_only",
            "input_modalities": ["text", "image"],
            "context_window": 272000,
            "priority": 99,
            "visibility": "hide",
            "supported_reasoning_levels": [{"effort": "high", "description": "Deep"}],
        }
        for slug in ("gpt-5.2", "gpt-5.6-sol", "gpt-6-astra")
    ]


@pytest.mark.parametrize("multi_agent_tools_in_code_mode", [False, True, None])
def test_gpt_metadata_preserved_with_admin_identity_and_order(
    bundled, multi_agent_tools_in_code_mode
):
    for model in bundled:
        model["multi_agent_tools_in_code_mode"] = multi_agent_tools_in_code_mode
        model["multi_agent_version"] = "v2"
    original = copy.deepcopy(bundled)
    names = ["system.ai.gpt-6-astra", "gpt-5-6-sol", "system.ai.gpt-5-2"]
    result = catalog.build_codex_catalog(bundled, names + names)["models"]
    models = [model for model in result if model["visibility"] == "list"]
    assert any(model["visibility"] == "hide" for model in result)
    assert all(
        model["multi_agent_tools_in_code_mode"] is multi_agent_tools_in_code_mode
        for model in result
    )
    assert all(model["multi_agent_version"] == "v2" for model in result)
    assert [m["slug"] for m in models] == names
    for priority, (model, native) in enumerate(zip(models, reversed(bundled), strict=True)):
        for key, value in native.items():
            if key not in {
                "slug",
                "display_name",
                "visibility",
                "priority",
            }:
                assert model[key] == value
        assert model["priority"] == priority
        assert model["visibility"] == "list"
        assert model["display_name"] == native["display_name"]
        assert model["supported_in_api"] is True
    models[0]["future_field"]["nested"].append(2)
    assert bundled == original


def test_hosted_services_get_readable_labels_and_keep_full_slugs(bundled):
    names = ["system.ai.kimi-k3", "main.team.custom", "plain"]
    models = catalog.build_codex_catalog(bundled, names)["models"]
    assert [(m["slug"], m["display_name"]) for m in models] == [
        ("system.ai.kimi-k3", "Kimi K3"),
        ("main.team.custom", "Custom"),
        ("plain", "Plain"),
    ]


def test_duplicate_labels_fall_back_to_full_ids_but_not_hidden_aliases(bundled):
    names = ["system.ai.kimi-k3", "main.team.kimi-k3", "system.ai.other", "system.ai.gpt-6-astra"]
    models = catalog.build_codex_catalog(bundled, names)["models"]
    assert [m["display_name"] for m in models[:4]] == [
        "system.ai.kimi-k3",
        "main.team.kimi-k3",
        "Other",
        "gpt-6-astra",
    ]
    assert models[4]["slug"] == "gpt-6-astra"
    assert models[4]["visibility"] == "hide"


def test_provider_labels_keep_dotted_versions_and_full_slugs(bundled):
    names = ["main.default.lill-oai.gpt-6.1-sol", "main.default.lill-oai.gpt-6.1-terra"]
    models = catalog.build_codex_catalog(bundled, names)["models"]

    assert [(m["slug"], m["display_name"]) for m in models] == list(
        zip(names, ["GPT 6.1 Sol", "GPT 6.1 Terra"], strict=True)
    )


def test_only_selected_gpt_models_get_hidden_routing_aliases(bundled):
    names = ["system.ai.gpt-6-astra", "system.ai.gpt-5-2", "gpt-5.2"]
    models = catalog.build_codex_catalog(bundled, names)["models"]
    assert [m["slug"] for m in models if m["visibility"] == "list"] == names
    assert [m["slug"] for m in models if m["visibility"] == "hide"] == ["gpt-6-astra"]
    assert "gpt-5.6-sol" not in {m["slug"] for m in models}


@pytest.mark.parametrize("name", ["gpt-99", "system.ai.gpt-99", "databricks-gpt-99"])
def test_missing_gpt_uses_generic_compatibility_metadata(bundled, name):
    warnings = []

    models = catalog.build_codex_catalog(bundled, [name], warn=warnings.append)["models"]

    model = models[0]
    assert model["slug"] == name
    assert model["base_instructions"] == bundled[0]["base_instructions"]
    assert model["tool_mode"] is None
    assert model["input_modalities"] == ["text"]
    assert model["context_window"] == 32768
    assert model["default_reasoning_level"] == "medium"
    assert model["supported_reasoning_levels"] == [
        {"effort": "low", "description": "Low reasoning"},
        {"effort": "medium", "description": "Medium reasoning"},
        {"effort": "high", "description": "High reasoning"},
    ]
    assert warnings == [
        f"Codex is missing metadata for managed GPT model '{name}', so UG is falling "
        "back to default metadata. Try updating Codex with `ug codex update`."
    ]


@pytest.mark.parametrize("name", ["system.ai.gpt-4o", "gpt-4.1", "databricks-gpt-4o-mini"])
def test_missing_pre_reasoning_gpt_keeps_none_only_reasoning(bundled, name):
    model = catalog.build_codex_catalog(bundled, [name])["models"][0]

    assert model["default_reasoning_level"] == "none"
    assert model["supported_reasoning_levels"] == [
        {"effort": "none", "description": "Default model behavior"}
    ]


def test_missing_managed_gpt_keeps_hidden_native_alias(bundled):
    models = catalog.build_codex_catalog(bundled, ["system.ai.gpt-6-1-sol"])["models"]

    assert [(model["slug"], model["visibility"]) for model in models] == [
        ("system.ai.gpt-6-1-sol", "list"),
        ("gpt-6.1-sol", "hide"),
    ]
    assert models[1]["default_reasoning_level"] == "medium"
    assert models[1]["supported_reasoning_levels"] == [
        {"effort": "low", "description": "Low reasoning"},
        {"effort": "medium", "description": "Medium reasoning"},
        {"effort": "high", "description": "High reasoning"},
    ]
    assert models[1]["supported_reasoning_levels"] is not models[0]["supported_reasoning_levels"]


def test_legacy_gpt_alias_uses_native_metadata(bundled):
    models = catalog.build_codex_catalog(bundled, ["databricks-gpt-5-6-sol"])["models"]
    assert models[0]["base_instructions"] == bundled[1]["base_instructions"]
    assert models[0]["slug"] == "databricks-gpt-5-6-sol"
    assert models[1]["slug"] == "gpt-5.6-sol"
    assert models[1]["visibility"] == "hide"


def test_custom_services_use_destination_metadata_and_keep_request_identity(bundled):
    names = [
        "mlops_ont.models.codegen-gpt-6-luna",
        "main.team.opaque",
        "main.team.gpt-4o",
        "main.team.another",
    ]
    targets = dict(
        zip(
            names,
            [["gpt-6-astra"], ["system.ai.kimi-k3"], ["gpt-6.1-sol"], ["gpt-6-astra"]],
            strict=True,
        )
    )
    original = copy.deepcopy(bundled)

    models = catalog.build_codex_catalog(bundled, names, model_ids=targets)["models"]

    assert [model["slug"] for model in models] == names
    for key, value in bundled[2].items():
        if key not in {"slug", "display_name", "priority", "visibility", "supported_in_api"}:
            assert models[0][key] == value
    assert models[1]["default_reasoning_level"] == "max"
    assert models[1]["input_modalities"] == ["text", "image"]
    assert models[2]["default_reasoning_level"] == "medium"
    assert models[3]["display_name"] == "Another"
    assert models[0]["display_name"] != models[3]["display_name"]
    assert [level["effort"] for level in models[2]["supported_reasoning_levels"]] == [
        "low",
        "medium",
        "high",
    ]
    assert bundled == original


@pytest.mark.parametrize("target", ["gpt-4o", "unknown-deployment"])
def test_destination_identity_overrides_misleading_gpt_service_name(bundled, target):
    name = "main.team.gpt-6-astra"
    model = catalog.build_codex_catalog(bundled, [name], model_ids={name: [target]})["models"][0]

    assert model["slug"] == name
    assert model["default_reasoning_level"] == "none"
    assert model["context_window"] == 32768


@pytest.mark.parametrize("target", ["system.ai.databricks-gpt-6-astra", "databricks-gpt-6-astra"])
def test_destination_model_resource_uses_native_metadata(bundled, target):
    name = "main.team.opaque"
    models = catalog.build_codex_catalog(bundled, [name], model_ids={name: [target]})["models"]

    assert [model["slug"] for model in models] == [name]
    assert models[0]["base_instructions"] == bundled[2]["base_instructions"]
    assert models[0]["supported_reasoning_levels"] == bundled[2]["supported_reasoning_levels"]
    assert models[0]["context_window"] == bundled[2]["context_window"]


def test_multiple_destinations_with_same_model_use_native_metadata(monkeypatch, bundled):
    service = {
        "config": {
            "routing": {
                "destinations": [
                    {
                        "traffic_percentage": 50,
                        "external_model_config": {"target": {"model": "gpt-6-astra"}},
                    },
                    {
                        "traffic_percentage": 50,
                        "pay_per_token_config": {
                            "model": "models/system.ai.databricks-gpt-6-astra"
                        },
                    },
                ]
            }
        }
    }
    monkeypatch.setattr(catalog, "get_model_service", lambda *a, **kw: (service, None))
    monkeypatch.setattr(
        catalog,
        "_run_catalog_command",
        lambda *a: subprocess.CompletedProcess([], 0, json.dumps({"models": bundled})),
    )

    result = catalog.prepare_codex_catalog(
        "/selected/codex",
        ["main.team.opaque"],
        workspace="https://selected",
        get_token=lambda: "token",
    )

    assert result["models"][0]["slug"] == "main.team.opaque"
    assert result["models"][0]["base_instructions"] == bundled[2]["base_instructions"]


def test_mixed_destinations_use_only_shared_capabilities(bundled):
    first = {
        **bundled[1],
        "default_reasoning_level": "high",
        "supported_reasoning_levels": catalog._reasoning_levels(["low", "medium", "high", "max"]),
        "context_window": 64000,
        "max_context_window": 128000,
        "effective_context_window_percent": 80,
        "additional_speed_tiers": ["fast"],
        "experimental_supported_tools": ["clock", "other"],
        "supports_search_tool": True,
        "support_verbosity": True,
        "node_repl_disabled": True,
        "use_responses_lite": True,
        "service_tiers": [{"id": "priority"}],
    }
    second = {
        **bundled[2],
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": catalog._reasoning_levels(["medium", "high", "xhigh"]),
        "context_window": 96000,
        "max_context_window": 192000,
        "effective_context_window_percent": 75,
        "input_modalities": ["text"],
        "additional_speed_tiers": ["fast", "other"],
        "experimental_supported_tools": ["clock"],
        "use_responses_lite": True,
        "service_tiers": [{"id": "priority"}, {"id": "other"}],
    }
    baseline = {**bundled[0], "default_service_tier": "priority", "node_repl_disabled": False}
    native = [baseline, first, second]
    original = copy.deepcopy(native)
    name = "main.team.opaque"

    result = catalog.build_codex_catalog(
        native,
        [name],
        model_ids={name: [first["slug"], second["slug"]]},
    )["models"]

    assert [model["slug"] for model in result] == [name]
    model = result[0]
    assert model["default_reasoning_level"] == "medium"
    assert [level["effort"] for level in model["supported_reasoning_levels"]] == ["medium", "high"]
    assert model["input_modalities"] == ["text"]
    assert (model["context_window"], model["max_context_window"]) == (64000, 128000)
    assert model["effective_context_window_percent"] == 75
    assert model["additional_speed_tiers"] == ["fast"]
    assert model["experimental_supported_tools"] == ["clock"]
    assert model["service_tiers"] == [{"id": "priority"}]
    assert model["use_responses_lite"] is True
    assert model["supports_search_tool"] is False
    assert model["support_verbosity"] is False
    assert model["default_verbosity"] is None
    assert model["default_service_tier"] is None
    assert model["node_repl_disabled"] is True
    assert model["tool_mode"] is None
    assert model["multi_agent_version"] is None
    assert model["base_instructions"] == baseline["base_instructions"]
    assert native == original


@pytest.mark.parametrize(
    "targets,expected_default,expected_efforts",
    [
        (["gpt-6-astra", "gpt-5.6-sol"], "high", ["high"]),
        (["gpt-99", "gpt-98"], "medium", ["low", "medium", "high"]),
        (["gpt-99", "system.ai.kimi-k3"], "low", ["low", "high"]),
    ],
)
def test_mixed_reasoning_uses_shared_native_or_fallback_levels(
    bundled, targets, expected_default, expected_efforts
):
    name = "main.team.opaque"
    model = catalog.build_codex_catalog(bundled, [name], model_ids={name: targets})["models"][0]

    assert model["default_reasoning_level"] == expected_default
    assert [level["effort"] for level in model["supported_reasoning_levels"]] == expected_efforts


def test_mixed_destinations_without_shared_effort_fail_actionably(bundled):
    with pytest.raises(RuntimeError, match="no reasoning effort supported by every destination"):
        catalog.build_codex_catalog(
            bundled, ["main.team.opaque"], model_ids={"main.team.opaque": ["gpt-6-astra", "gpt-4o"]}
        )


def test_mixed_destinations_without_shared_input_fail_actionably(bundled):
    bundled[1]["input_modalities"] = ["text"]
    bundled[2]["input_modalities"] = ["image"]
    with pytest.raises(RuntimeError, match="no input modality supported by every destination"):
        catalog.build_codex_catalog(
            bundled,
            ["main.team.opaque"],
            model_ids={"main.team.opaque": ["gpt-5.6-sol", "gpt-6-astra"]},
        )


def test_mixed_primary_and_fallback_models_are_resolved(monkeypatch, bundled):
    def destination(model):
        return {"external_model_config": {"target": {"model": model}}}

    service = {
        "config": {
            "routing": {
                "destinations": [destination("gpt-6-astra")],
                "fallback": {"destinations": [destination("gpt-5.6-sol")]},
                "budget_fallbacks": [{"destination": destination("gpt-99")}],
            }
        }
    }
    monkeypatch.setattr(catalog, "get_model_service", lambda *a, **kw: (service, None))
    monkeypatch.setattr(
        catalog,
        "_run_catalog_command",
        lambda *a: subprocess.CompletedProcess([], 0, json.dumps({"models": bundled})),
    )

    result = catalog.prepare_codex_catalog(
        "/selected/codex",
        ["main.team.opaque"],
        workspace="https://selected",
        get_token=lambda: "token",
    )

    model = result["models"][0]
    assert model["slug"] == "main.team.opaque"
    assert [level["effort"] for level in model["supported_reasoning_levels"]] == ["high"]
    assert model["default_reasoning_level"] == "high"


def test_prepare_catalog_resolves_custom_destinations_with_selected_credentials(
    monkeypatch, bundled
):
    names = ["mlops_ont.models.codegen-gpt-6-luna", "main.team.opaque", "main.team.oss"]
    native = {**bundled[2], "slug": "gpt-6-luna", "default_reasoning_level": "high"}
    services = [
        {"external_model_config": {"target": {"model": "gpt-6-luna"}}},
        {"pay_per_token_config": {"model": "models/system.ai.databricks-gpt-6-1-sol"}},
        {"pay_per_token_config": {"model": "models/system.ai.databricks-kimi-k3"}},
    ]
    requests = []
    auth_calls = []
    validated = []

    def get_json(url, token, *, timeout):
        requests.append((url, token, timeout))
        destination = services[len(requests) - 1]
        return {"config": {"routing": {"destinations": [destination]}}}, None

    def get_token():
        auth_calls.append(True)
        return "selected-token"

    def run(binary, args, home):
        if args == ["debug", "models", "--bundled"]:
            return subprocess.CompletedProcess([], 0, json.dumps({"models": [*bundled, native]}))
        validated.append(json.loads((Path(home) / "catalog.json").read_text()))

    monkeypatch.setattr(db_mod, "_http_get_json", get_json)
    monkeypatch.setattr(catalog, "_run_catalog_command", run)

    result = catalog.prepare_codex_catalog(
        "/selected/codex", names + names[:1], workspace="https://selected", get_token=get_token
    )

    assert [(url, token) for url, token, _timeout in requests] == [
        (f"https://selected/api/2.1/unity-catalog/model-services/{name}", "selected-token")
        for name in names
    ]
    assert all(0 < timeout <= 5 for _url, _token, timeout in requests)
    assert auth_calls == [True]
    assert validated == [result]
    assert [model["slug"] for model in result["models"]] == names
    assert result["models"][0]["default_reasoning_level"] == "high"
    assert result["models"][0]["base_instructions"] == native["base_instructions"]
    assert result["models"][1]["default_reasoning_level"] == "medium"
    assert result["models"][2]["default_reasoning_level"] == "max"


@pytest.mark.parametrize(
    "service,reason",
    [
        (None, "HTTP 403"),
        ({"config": {}}, None),
    ],
    ids=["unreadable", "unresolved"],
)
def test_unresolved_destinations_warn_without_guessing_metadata(
    monkeypatch, bundled, service, reason
):
    warnings = []
    monkeypatch.setattr(db_mod, "_http_get_json", lambda *a, **kw: (service, reason))
    monkeypatch.setattr(catalog, "print_warning", warnings.append)
    monkeypatch.setattr(
        catalog,
        "_run_catalog_command",
        lambda *a: subprocess.CompletedProcess([], 0, json.dumps({"models": bundled})),
    )

    result = catalog.prepare_codex_catalog(
        "/selected/codex",
        ["main.team.gpt-6-astra"],
        workspace="https://selected",
        get_token=lambda: "token",
    )

    assert result["models"][0]["default_reasoning_level"] == "none"
    assert len(warnings) == 1
    assert "Could not resolve Codex metadata" in warnings[0]
    assert (reason or "backing model identities") in warnings[0]


def test_native_models_do_not_require_destination_lookup(monkeypatch, bundled):
    monkeypatch.setattr(
        catalog,
        "_run_catalog_command",
        lambda *a: subprocess.CompletedProcess([], 0, json.dumps({"models": bundled})),
    )

    result = catalog.prepare_codex_catalog(
        "/selected/codex",
        ["system.ai.gpt-6-astra", "gpt-5.2"],
        workspace="https://selected",
        get_token=lambda: pytest.fail("unneeded authentication"),
    )

    assert result["models"][0]["base_instructions"] == bundled[2]["base_instructions"]


@pytest.mark.parametrize("error", [RuntimeError("expired"), OSError("unavailable")])
def test_service_metadata_auth_failure_warns_and_keeps_catalog(monkeypatch, bundled, error):
    def token():
        raise error

    warnings = []
    monkeypatch.setattr(catalog, "print_warning", warnings.append)
    monkeypatch.setattr(
        catalog, "get_model_service", lambda *a, **kw: pytest.fail("lookup without credentials")
    )
    monkeypatch.setattr(
        catalog,
        "_run_catalog_command",
        lambda *a: subprocess.CompletedProcess([], 0, json.dumps({"models": bundled})),
    )

    result = catalog.prepare_codex_catalog(
        "/selected/codex", ["main.team.opaque"], workspace="https://selected", get_token=token
    )

    assert result["models"][0]["slug"] == "main.team.opaque"
    assert "check your workspace login" in warnings[0]


def test_service_metadata_lookups_share_time_budget(monkeypatch, bundled):
    clock = iter([100, 102, 106])
    monkeypatch.setattr(catalog, "monotonic", lambda: next(clock))
    calls = []

    def service(name, workspace, token, *, timeout):
        calls.append((name, timeout))
        return None, "network timeout"

    warnings = []
    monkeypatch.setattr(catalog, "get_model_service", service)
    monkeypatch.setattr(catalog, "print_warning", warnings.append)
    monkeypatch.setattr(
        catalog,
        "_run_catalog_command",
        lambda *a: subprocess.CompletedProcess([], 0, json.dumps({"models": bundled})),
    )

    result = catalog.prepare_codex_catalog(
        "/selected/codex",
        ["main.team.first", "main.team.second"],
        workspace="https://selected",
        get_token=lambda: "token",
    )

    assert calls == [("main.team.first", 3)]
    assert len(result["models"]) == 2
    assert "time budget exhausted" in warnings[1]


def test_generic_non_gpt_uses_conservative_ordinary_tool_defaults(bundled):
    models = catalog.build_codex_catalog(bundled, ["main.team.custom", "another"])["models"]
    first = models[0]
    assert first["base_instructions"] == bundled[0]["base_instructions"]
    assert first["model_messages"] == bundled[0]["model_messages"]
    assert first["future_field"] == bundled[0]["future_field"]
    assert first["tool_mode"] is None
    assert first["input_modalities"] == ["text"]
    assert first["context_window"] == first["max_context_window"] == 32768
    assert first["default_reasoning_level"] == "none"
    assert first["supported_reasoning_levels"] == [
        {"effort": "none", "description": "Default model behavior"}
    ]
    assert first["default_reasoning_summary"] == "none"
    assert first["supports_search_tool"] is False
    assert first["use_responses_lite"] is False
    assert first["experimental_supported_tools"] == []
    assert first["upgrade"] is None
    first["input_modalities"].append("image")
    assert models[1]["input_modalities"] == ["text"]
    assert catalog._HOSTED_DEFAULTS["input_modalities"] == ["text"]


@pytest.mark.parametrize(
    "name,modalities,efforts",
    [
        ("system.ai.glm-5-2", ["text"], ["high", "max"]),
        ("glm-5-3", ["text"], ["low", "high", "max"]),
        ("system.ai.glm-5-3-flash", ["text", "image"], ["low", "high", "max"]),
        ("system.ai.kimi-k3", ["text", "image"], ["low", "high", "max"]),
        ("system.ai.deepseek-v4-1-flash", ["text", "image"], ["low", "high", "xhigh", "max"]),
        ("system.ai.deepseek-v4-1-flash-1017", ["text", "image"], ["low", "high", "xhigh", "max"]),
    ],
)
def test_known_hosted_capabilities(bundled, name, modalities, efforts):
    model = catalog.build_codex_catalog(bundled, [name])["models"][0]
    assert model["input_modalities"] == modalities
    assert [level["effort"] for level in model["supported_reasoning_levels"]] == efforts
    assert model["context_window"] == 1048576
    assert model["default_reasoning_level"] == efforts[-1]


def test_unknown_uc_names_do_not_infer_gpt_or_hosted_capabilities(bundled):
    model = catalog.build_codex_catalog(bundled, ["custom.schema.kimi-k3"])["models"][0]
    assert model["input_modalities"] == ["text"]
    assert model["context_window"] == 32768


def test_native_non_gpt_metadata_takes_precedence(bundled):
    native = {**bundled[0], "slug": "kimi-k3", "context_window": 123456}
    model = catalog.build_codex_catalog([*bundled, native], ["system.ai.kimi-k3"])["models"][0]
    assert model["context_window"] == 123456


def test_baseline_is_any_ordinary_tool_model_not_a_pinned_slug(bundled):
    # gpt-5.2 gone; the remaining ordinary-tool model serves as the skeleton.
    without_gpt52 = [{**bundled[1], "tool_mode": None}, bundled[2]]
    model = catalog.build_codex_catalog(without_gpt52, ["kimi-k3"])["models"][0]
    assert model["base_instructions"] == without_gpt52[0]["base_instructions"]
    assert model["tool_mode"] is None


def test_missing_baseline_does_not_affect_gpt(bundled):
    without_baseline = bundled[1:]  # leaves only code-mode models, no ordinary-tool skeleton
    assert catalog.build_codex_catalog(without_baseline, ["gpt-6-astra"])["models"]
    with pytest.raises(RuntimeError, match="no ordinary-tool bundled model"):
        catalog.build_codex_catalog(without_baseline, ["kimi-k3"])


def test_extract_and_validate_with_same_binary_in_isolation(monkeypatch, bundled):
    calls = []
    homes = []

    def run(argv, **kwargs):
        calls.append(argv)
        home = Path(kwargs["env"]["CODEX_HOME"])
        homes.append(home)
        assert kwargs["cwd"] == str(home)
        assert home.exists()
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 30
        assert kwargs["check"] is True
        if len(calls) == 1:
            assert argv == ["/selected/codex", "debug", "models", "--bundled"]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"models": bundled}))
        assert argv[:2] == ["/selected/codex", "-c"]
        assert argv[-2:] == ["debug", "models"]
        path = Path(json.loads(argv[2].split("=", 1)[1]))
        assert path.parent == home
        assert json.loads(path.read_text())["models"][0]["slug"] == "system.ai.gpt-6-astra"
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setattr(catalog.subprocess, "run", run)
    result = catalog.prepare_codex_catalog("/selected/codex", ["system.ai.gpt-6-astra"])
    assert result["models"][0]["base_instructions"] == bundled[-1]["base_instructions"]
    assert len(calls) == 2
    assert homes[0] == homes[1]
    assert not homes[0].exists()


def test_unscoped_routing_catalog_retains_native_menu(monkeypatch, bundled):
    original = copy.deepcopy(bundled)
    monkeypatch.setattr(
        catalog,
        "_run_catalog_command",
        lambda *a: subprocess.CompletedProcess([], 0, json.dumps({"models": bundled})),
    )

    result = catalog.prepare_codex_catalog(
        "codex", ["system.ai.gpt-6-astra", "system.ai.glm-5-3-flash"], include_bundled=True
    )["models"]

    assert result[: len(bundled)] == original
    assert [m["slug"] for m in result[len(bundled) :]] == ["system.ai.glm-5-3-flash"]
    assert result[-1]["visibility"] == "list"
    assert result[-1]["priority"] > max(m["priority"] for m in bundled)
    assert bundled == original


def test_validate_discovered_catalog_with_same_binary_in_isolation(monkeypatch):
    discovered = {
        "models": [
            {
                "slug": "system.ai.gpt-6-astra",
                "future_field": {"nested": [1, "keep"]},
                "visibility": "list",
            }
        ],
        "future_catalog_field": {"enabled": True},
    }
    original = copy.deepcopy(discovered)
    calls = []
    homes = []

    def run(argv, **kwargs):
        calls.append(argv)
        home = Path(kwargs["env"]["CODEX_HOME"])
        homes.append(home)
        assert argv[0] == "/selected/codex"
        assert argv[-2:] == ["debug", "models"]
        assert kwargs["cwd"] == str(home)
        assert kwargs["env"]["CODEX_HOME"] != "/user/codex-home"
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 30
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        setting = json.loads(argv[2].split("=", 1)[1])
        candidate = Path(setting)
        assert candidate.parent == home
        assert json.loads(candidate.read_text()) == discovered
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setenv("CODEX_HOME", "/user/codex-home")
    monkeypatch.setattr(catalog.subprocess, "run", run)

    assert catalog.validate_codex_catalog("/selected/codex", discovered) is None

    assert len(calls) == 1
    assert calls[0][1] == "-c"
    assert discovered == original
    assert not homes[0].exists()


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("codex not found"),
        subprocess.TimeoutExpired("codex", 30),
        subprocess.CalledProcessError(1, ["codex"], stderr="catalog secret"),
    ],
    ids=["missing-binary", "timeout", "rejected-catalog"],
)
def test_validate_discovered_catalog_failure_is_actionable_without_payload(monkeypatch, error):
    catalog_payload = {"models": [{"slug": "system.ai.secret-model"}]}

    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(catalog.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="Update Codex") as raised:
        catalog.validate_codex_catalog("/selected/codex", catalog_payload)

    assert "secret-model" not in str(raised.value)
    assert "catalog secret" not in str(raised.value)


def test_missing_gpt_warning_is_printed_after_validation(monkeypatch, bundled):
    calls = []
    warnings = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"models": bundled}))
        assert warnings == []
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setattr(catalog.subprocess, "run", run)
    monkeypatch.setattr(catalog, "print_warning", warnings.append)

    result = catalog.prepare_codex_catalog("codex", ["system.ai.gpt-99"])

    assert result["models"][0]["slug"] == "system.ai.gpt-99"
    assert warnings == [
        "Codex is missing metadata for managed GPT model 'system.ai.gpt-99', so UG is "
        "falling back to default metadata. Try updating Codex with `ug codex update`."
    ]


@pytest.mark.parametrize(
    "stdout",
    [
        "bad json",
        "null",
        "[]",
        "{}",
        '{"models": []}',
        '{"models": [null]}',
        '{"models": [{"slug": 1}]}',
        '{"models": [{"slug": ""}]}',
    ],
)
def test_invalid_bundled_catalog_blocks_without_validation(monkeypatch, stdout):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout)

    monkeypatch.setattr(catalog.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Gateway discovery was not used"):
        catalog.prepare_codex_catalog("codex", ["kimi-k3"])
    assert len(calls) == 1


@pytest.mark.parametrize("fail_validation", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired("codex", 30),
        subprocess.CalledProcessError(1, "codex"),
    ],
)
def test_subprocess_failure_never_falls_back(monkeypatch, bundled, fail_validation, error):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if fail_validation and len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"models": bundled}))
        raise error

    monkeypatch.setattr(catalog.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Upgrade the active Codex"):
        catalog.prepare_codex_catalog("codex", ["kimi-k3"])
    assert len(calls) == (2 if fail_validation else 1)


class TestRunCatalogCommand:
    def test_decodes_utf8_output_even_when_locale_defaults_to_cp1252(self, tmp_path, monkeypatch):
        # Force the dependency's omitted encoding to behave like Windows on every host.
        real_run = catalog.subprocess.run

        def run_with_cp1252_default(*args, **kwargs):
            if kwargs.get("text") and "encoding" not in kwargs:
                kwargs["encoding"] = "cp1252"
            return real_run(*args, **kwargs)

        monkeypatch.setattr(catalog.subprocess, "run", run_with_cp1252_default)
        script = r"import sys; sys.stdout.buffer.write('“fast” — ok'.encode('utf-8'))"
        result = catalog._run_catalog_command(sys.executable, ["-c", script], str(tmp_path))
        assert result.stdout == "“fast” — ok"


class TestConfiguredPaths:
    def test_lists_catalog_when_a_static_model_list_drives_codex(self):
        from ucode.agents.codex import CODEX_MODEL_CATALOG_PATH, configured_paths

        paths = configured_paths({"codex_static_models": ["system.ai.gpt-5-6-sol"]})
        assert str(CODEX_MODEL_CATALOG_PATH) in paths

    def test_omits_catalog_without_a_static_list_or_under_a_provider(self):
        from ucode.agents.codex import CODEX_MODEL_CATALOG_PATH, configured_paths

        assert str(CODEX_MODEL_CATALOG_PATH) not in configured_paths({})
        under_provider = configured_paths(
            {
                "codex_static_models": ["system.ai.gpt-5-6-sol"],
                "provider_services": {"codex": "main.ucode.ci_openai_mps"},
            }
        )
        assert str(CODEX_MODEL_CATALOG_PATH) not in under_provider
