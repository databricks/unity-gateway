"""Component coverage for explicit custom coding-agent attribution."""

from __future__ import annotations

import pytest

from ucode.agents import claude, codex, gemini, kilo, opencode, pi
from ucode.agents.coding_agent import custom_coding_agent_headers
from ucode.constants import (
    CUSTOM_CODING_AGENT_HEADER,
    UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR,
)

WORKSPACE = "https://example.databricks.com"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, {}),
        ({UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR: ""}, {}),
        ({UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR: "   "}, {}),
        (
            {UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR: "my-agent"},
            {CUSTOM_CODING_AGENT_HEADER: "my-agent"},
        ),
        (
            {UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR: "  My Agent/1:custom  "},
            {CUSTOM_CODING_AGENT_HEADER: "My Agent/1:custom"},
        ),
        ({"AI_GATEWAY_CUSTOM_CODING_AGENT": "old-name"}, {}),
    ],
)
def test_custom_coding_agent_headers(env, expected):
    assert custom_coding_agent_headers(env) == expected


@pytest.mark.parametrize(
    "value", ["agent\nInjected: value", "agent\r", "\nagent", "agent\x00", "agent\t", "agent\x7f"]
)
def test_control_characters_warn_on_stderr_and_omit_header(value, capsys):
    assert custom_coding_agent_headers({UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR: value}) == {}
    output = capsys.readouterr()
    assert not output.out
    assert UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR in output.err
    assert "control characters" in output.err
    assert "Injected" not in output.err


def test_helper_reads_process_environment(monkeypatch):
    monkeypatch.setenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, "wrapper")
    assert custom_coding_agent_headers() == {CUSTOM_CODING_AGENT_HEADER: "wrapper"}
    monkeypatch.delenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR)
    assert custom_coding_agent_headers() == {}


def _parse_header_lines(value: str, separator: str) -> dict[str, str]:
    headers = {}
    for line in value.split(separator):
        name, _, header_value = line.partition(":")
        headers[name.strip()] = header_value.strip()
    return headers


@pytest.fixture(params=["claude", "codex", "codex-legacy", "gemini", "opencode", "kilo", "pi"])
def rendered_headers(request, monkeypatch):
    agent = request.param
    module = {
        "claude": claude,
        "codex": codex,
        "codex-legacy": codex,
        "gemini": gemini,
        "opencode": opencode,
        "kilo": kilo,
        "pi": pi,
    }[agent]
    monkeypatch.setattr(module, "ug_version", lambda: "1.2.3")
    monkeypatch.setattr(module, "agent_version", lambda _binary: "4.5.6")
    monkeypatch.delenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, raising=False)
    monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)

    def render():
        if agent == "claude":
            overlay, _ = claude.render_overlay(WORKSPACE, "claude-model")
            return [_parse_header_lines(overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"], "\n")]
        if agent in ("codex", "codex-legacy"):
            renderer = codex.render_overlay if agent == "codex" else codex.render_legacy_overlay
            overlay = renderer(WORKSPACE)
            return [overlay["model_providers"]["Databricks"]["http_headers"]]
        if agent == "gemini":
            overlay = gemini.render_env_overlay(WORKSPACE, "gemini-model", "token")
            return [_parse_header_lines(overlay["GEMINI_CLI_CUSTOM_HEADERS"], ",")]
        if agent == "pi":
            overlay, _ = pi.render_overlay(
                "claude-model",
                "token",
                {"claude": WORKSPACE, "openai": WORKSPACE, "gemini": WORKSPACE},
                {"sonnet": "claude-model"},
                ["gpt-model"],
                ["gemini-model"],
            )
            return [provider["headers"] for provider in overlay["providers"].values()]
        overlay, _ = module.render_overlay(
            "claude-model",
            "token",
            {"anthropic": WORKSPACE, "gemini": WORKSPACE, "oss": WORKSPACE},
            {"anthropic": ["claude-model"], "gemini": ["gemini-model"], "oss": ["oss-model"]},
        )
        return [
            model["headers"]
            for provider in overlay["provider"].values()
            for model in provider["models"].values()
        ]

    return agent, render


def test_adapters_emit_override_without_changing_user_agent(rendered_headers, monkeypatch):
    agent, render = rendered_headers
    baseline = render()
    monkeypatch.setenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, "custom-agent")
    overridden = render()
    expected = UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR if agent == "pi" else "custom-agent"
    assert overridden
    for original, headers in zip(baseline, overridden, strict=True):
        assert headers[CUSTOM_CODING_AGENT_HEADER] == expected
        assert {
            name: value for name, value in headers.items() if name != CUSTOM_CODING_AGENT_HEADER
        } == original
        assert "Databricks-Custom-Client-Surfaces" not in headers


@pytest.mark.parametrize("value", ["", "   ", "bad\nInjected: header"])
def test_adapters_omit_empty_or_invalid_override(rendered_headers, monkeypatch, value):
    _, render = rendered_headers
    baseline = render()
    monkeypatch.setenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, value)
    assert render() == baseline


def test_adapters_remove_override_on_next_render(rendered_headers, monkeypatch):
    _, render = rendered_headers
    baseline = render()
    monkeypatch.setenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, "first-agent")
    assert all(CUSTOM_CODING_AGENT_HEADER in headers for headers in render())
    monkeypatch.delenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR)
    assert render() == baseline


@pytest.mark.parametrize("agent", [claude, codex])
def test_admin_header_overrides_environment_case_insensitively(agent, monkeypatch):
    monkeypatch.setenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, "session-agent")
    managed_headers = {CUSTOM_CODING_AGENT_HEADER.lower(): "admin-agent"}
    if agent is claude:
        overlay, _ = claude.render_overlay(
            WORKSPACE, "claude-model", managed_http_headers=managed_headers
        )
        headers = _parse_header_lines(overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"], "\n")
    else:
        overlay = codex.render_overlay(WORKSPACE, managed_http_headers=managed_headers)
        headers = overlay["model_providers"]["Databricks"]["http_headers"]
    assert [
        value
        for name, value in headers.items()
        if name.casefold() == CUSTOM_CODING_AGENT_HEADER.casefold()
    ] == ["admin-agent"]


def test_claude_merge_replaces_and_removes_override_but_preserves_other_headers(monkeypatch):
    existing = "X-Caller: keep\ndatabricks-custom-coding-agent: old"
    monkeypatch.setenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, "new-agent")
    overlay, _ = claude.render_overlay(WORKSPACE, "claude-model")
    merged = claude._merge_anthropic_custom_headers(
        existing, overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    )
    assert _parse_header_lines(merged, "\n")[CUSTOM_CODING_AGENT_HEADER] == "new-agent"
    monkeypatch.delenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR)
    overlay, _ = claude.render_overlay(WORKSPACE, "claude-model")
    merged = claude._merge_anthropic_custom_headers(
        merged, overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    )
    headers = _parse_header_lines(merged, "\n")
    assert headers["X-Caller"] == "keep"
    assert not any(name.casefold() == CUSTOM_CODING_AGENT_HEADER.casefold() for name in headers)


def test_gemini_warns_and_omits_comma_delimited_override(monkeypatch, capsys):
    monkeypatch.setenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, "agent,Injected:value")
    overlay = gemini.render_env_overlay(WORKSPACE, "gemini-model", "token")
    assert CUSTOM_CODING_AGENT_HEADER not in overlay["GEMINI_CLI_CUSTOM_HEADERS"]
    assert "Injected" not in overlay["GEMINI_CLI_CUSTOM_HEADERS"]
    assert "Gemini CLI" in capsys.readouterr().err


@pytest.mark.parametrize("identity", ["COLLIDING_ENV_VAR", "!do-not-execute"])
def test_pi_uses_environment_reference_not_a_dynamic_literal(identity, monkeypatch):
    monkeypatch.setenv(UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR, identity)
    monkeypatch.setenv("COLLIDING_ENV_VAR", "unrelated-value")
    overlay, _ = pi.render_overlay(
        "claude-model", "token", {"claude": WORKSPACE}, {"sonnet": "claude-model"}, [], []
    )
    header = overlay["providers"]["databricks-claude"]["headers"][CUSTOM_CODING_AGENT_HEADER]
    assert header == UNITY_GATEWAY_CUSTOM_CODING_AGENT_ENV_VAR
    assert pi.build_runtime_env("token")[header] == identity
