"""Tests for how OpenCode and Kilo pick models and check the installed version."""

from __future__ import annotations

import pytest

from ucode.agents import kilo, opencode

AGENTS = [pytest.param(opencode, "OpenCode", id="opencode"), pytest.param(kilo, "Kilo", id="kilo")]
MODELS = {"anthropic": ["claude-sonnet"], "gemini": ["gemini-2"], "oss": ["kimi-k2"]}
CHOOSE = "Choose a discovered model id or pass a provider/model selector."
KILO_HINT = (
    "Run `ug configure --agent kilo` to discover models, then choose a "
    "discovered model id or pass a provider/model selector."
)
AUTH = "for renewable Databricks authentication."


class TestVersionGate:
    @pytest.mark.parametrize(
        ("module", "installed", "message", "package"),
        [
            (
                opencode,
                "1.0.219",
                f"OpenCode 1.0.219 is too old. ucode requires OpenCode 1.0.220 or newer {AUTH}",
                "opencode-ai@1",
            ),
            (
                kilo,
                "7.3.0",
                f"Kilo 7.3.0 is too old. ucode requires Kilo 7.3.1 or newer {AUTH}",
                "@kilocode/cli@7",
            ),
        ],
    )
    def test_too_old_message_is_exact(self, monkeypatch, module, installed, message, package):
        monkeypatch.setattr(module, "agent_version", lambda binary: installed)

        assert module._minimum_version_message() == message
        assert module.minimum_version_error() == (
            f"{message} Update it with `npm install -g {package}`."
        )

    @pytest.mark.parametrize(
        ("module", "installed"),
        [
            (opencode, "1.0.220"),
            (opencode, "2.0.14"),
            (opencode, "unknown"),
            (kilo, "7.3.1"),
            (kilo, "8.0.0"),
            (kilo, "unknown"),
        ],
    )
    def test_no_error_at_above_or_unparseable_version(self, monkeypatch, module, installed):
        monkeypatch.setattr(module, "agent_version", lambda binary: installed)

        assert module.minimum_version_error() is None

    @pytest.mark.parametrize("module", [opencode, kilo])
    def test_parse_version(self, module):
        assert module._parse_version("v10.20.30-beta") == (10, 20, 30)
        assert module._parse_version("1.2") is None


class TestExplicitModel:
    @pytest.mark.parametrize(("module", "display"), AGENTS)
    def test_empty_model_is_rejected(self, module, display):
        with pytest.raises(RuntimeError) as error:
            module.resolve_explicit_model("  ", {"opencode_models": MODELS})

        assert str(error.value) == f"{display} model must not be empty."

    @pytest.mark.parametrize("module", [opencode, kilo])
    @pytest.mark.parametrize(
        ("model", "resolved"),
        [
            ("claude-sonnet", "databricks-anthropic/claude-sonnet"),
            (" gemini-2 ", "databricks-google/gemini-2"),
            ("kimi-k2", "databricks-oss/kimi-k2"),
        ],
    )
    def test_bare_id_gets_its_managed_provider(self, module, model, resolved):
        assert module.resolve_explicit_model(model, {"opencode_models": MODELS}) == resolved

    @pytest.mark.parametrize("module", [opencode, kilo])
    def test_first_bucket_wins_and_a_prefixed_id_is_not_prefixed_again(self, module):
        both = {"anthropic": ["shared"], "gemini": ["shared"], "oss": ["shared"]}
        later = {"gemini": ["shared"], "oss": ["shared"]}
        listed = {"oss": ["databricks-oss/x"]}

        assert module.resolve_explicit_model("shared", {"opencode_models": both}) == (
            "databricks-anthropic/shared"
        )
        assert module.resolve_explicit_model("shared", {"opencode_models": later}) == (
            "databricks-google/shared"
        )
        assert module._resolve_model_selector("databricks-oss/x", listed) == "databricks-oss/x"

    @pytest.mark.parametrize(("module", "display"), AGENTS)
    def test_unknown_bare_id_with_models_listed(self, module, display):
        with pytest.raises(RuntimeError) as error:
            module.resolve_explicit_model("missing", {"opencode_models": MODELS})

        assert str(error.value) == f"{display} model 'missing' is not configured. {CHOOSE}"

    def test_unknown_bare_id_without_models_hints_at_configure_for_kilo_only(self):
        with pytest.raises(RuntimeError) as kilo_error:
            kilo.resolve_explicit_model("missing", {})
        with pytest.raises(RuntimeError) as opencode_error:
            opencode.resolve_explicit_model("missing", {})

        assert str(kilo_error.value) == f"Kilo model 'missing' is not configured. {KILO_HINT}"
        assert str(opencode_error.value) == f"OpenCode model 'missing' is not configured. {CHOOSE}"

    @pytest.mark.parametrize(("module", "display"), AGENTS)
    def test_selector_needs_provider_and_model(self, module, display):
        with pytest.raises(RuntimeError) as error:
            module.resolve_explicit_model("databricks-oss/", {"opencode_models": MODELS})

        assert str(error.value) == f"{display} model selector must use provider/model form."

    @pytest.mark.parametrize(("module", "display"), AGENTS)
    def test_managed_provider_with_a_model_it_does_not_list(self, module, display):
        with pytest.raises(RuntimeError) as error:
            module.resolve_explicit_model(
                "databricks-google/claude-sonnet", {"opencode_models": MODELS}
            )

        assert str(error.value) == (
            f"{display} model 'databricks-google/claude-sonnet' is not configured "
            "for managed provider 'databricks-google'."
        )

    @pytest.mark.parametrize("module", [opencode, kilo])
    @pytest.mark.parametrize(
        "selector",
        ["databricks-oss/kimi-k2", "openrouter/anthropic/claude-sonnet", "databricks-openai/gpt-5"],
    )
    def test_selectors_that_pass_through(self, module, selector):
        # databricks-openai passes through because it is not a managed provider.
        assert module.resolve_explicit_model(selector, {"opencode_models": MODELS}) == selector


class TestDefaultModel:
    @pytest.mark.parametrize("module", [opencode, kilo])
    def test_buckets_are_tried_in_order(self, module):
        assert module.default_model({"opencode_models": MODELS}) == "claude-sonnet"
        assert module.default_model({"opencode_models": {**MODELS, "anthropic": []}}) == "gemini-2"
        assert module.default_model({"opencode_models": {"oss": ["kimi-k2", "glm-5"]}}) == "kimi-k2"
        assert (
            module.default_model({"opencode_models": {"gemini": ["gemini-2", "g-3"]}}) == "gemini-2"
        )
        assert module.default_model({"opencode_models": {"anthropic": ["a", "b"]}}) == "a"
        assert module.default_model({}) is None

    @pytest.mark.parametrize(
        ("value", "expected"), [("admin-pick", "admin-pick"), ("", ""), (5, "claude-sonnet")]
    )
    def test_opencode_takes_a_string_default_from_state(self, value, expected):
        state = {"opencode_default_model": value, "opencode_models": MODELS}

        assert opencode.default_model(state) == expected

    @pytest.mark.parametrize("key", ["opencode_default_model", "kilo_default_model"])
    def test_kilo_ignores_a_default_in_state(self, key):
        assert kilo.default_model({key: "admin-pick", "opencode_models": MODELS}) == "claude-sonnet"
        assert kilo.default_model({key: "admin-pick"}) is None

    @pytest.mark.parametrize(("module", "display"), AGENTS)
    def test_launch_without_any_model_names_the_agent(self, module, display):
        with pytest.raises(RuntimeError) as error:
            module._configure_launch({})

        assert str(error.value) == f"No {display} model is configured."
