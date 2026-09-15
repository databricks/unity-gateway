"""Offline catalog construction and subprocess boundary tests (no installed CLI required)."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

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


def test_gpt_metadata_preserved_with_admin_identity_and_order(bundled):
    original = copy.deepcopy(bundled)
    names = ["system.ai.gpt-6-astra", "gpt-5-6-sol", "system.ai.gpt-5-2"]
    result = catalog.build_codex_catalog(bundled, names + names)["models"]
    models = [model for model in result if model["visibility"] == "list"]
    assert [m["slug"] for m in models] == names
    for priority, (model, native) in enumerate(zip(models, reversed(bundled), strict=True)):
        for key, value in native.items():
            if key not in {"slug", "display_name", "visibility", "priority"}:
                assert model[key] == value
        assert model["priority"] == priority
        assert model["visibility"] == "list"
        assert model["display_name"] == names[priority]
        assert model["supported_in_api"] is True
    models[0]["future_field"]["nested"].append(2)
    assert bundled == original


def test_only_selected_gpt_models_get_hidden_routing_aliases(bundled):
    names = ["system.ai.gpt-6-astra", "system.ai.gpt-5-2", "gpt-5.2"]
    models = catalog.build_codex_catalog(bundled, names)["models"]
    assert [m["slug"] for m in models if m["visibility"] == "list"] == names
    assert [m["slug"] for m in models if m["visibility"] == "hide"] == ["gpt-6-astra"]
    assert "gpt-5.6-sol" not in {m["slug"] for m in models}


@pytest.mark.parametrize("name", ["gpt-99", "system.ai.gpt-99", "databricks-gpt-99"])
def test_missing_gpt_metadata_is_an_actionable_error(bundled, name):
    with pytest.raises(RuntimeError, match="no bundled metadata.*Upgrade"):
        catalog.build_codex_catalog(bundled, [name])


def test_legacy_gpt_alias_uses_native_metadata(bundled):
    models = catalog.build_codex_catalog(bundled, ["databricks-gpt-5-6-sol"])["models"]
    assert models[0]["base_instructions"] == bundled[1]["base_instructions"]
    assert models[0]["slug"] == "databricks-gpt-5-6-sol"
    assert models[1]["slug"] == "gpt-5.6-sol"
    assert models[1]["visibility"] == "hide"


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


def test_missing_baseline_does_not_affect_gpt(bundled):
    without_baseline = bundled[1:]
    assert catalog.build_codex_catalog(without_baseline, ["gpt-6-astra"])["models"]
    with pytest.raises(RuntimeError, match="gpt-5.2 metadata"):
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
