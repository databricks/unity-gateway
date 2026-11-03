"""Attribution headers that ucode sends on routes:select calls for Claude and Codex."""

from __future__ import annotations

import io
import json

import pytest

from ucode.smart_routing import codex_routing, routing, v2

WS = "https://example.com"
CLAUDE_MODELS = ["system.ai.claude-opus-4-8", "system.ai.claude-sonnet-5"]
CODEX_MODELS = ["system.ai.gpt-5-6-sol"]


def _capture_select(monkeypatch):
    """Stub the router POST and return the captured request, so tests can read its headers."""
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        option = json.loads(request.data)["route_options"][0]
        return io.BytesIO(json.dumps({"route_selection": [{"route_option": option}]}).encode())

    monkeypatch.setattr(routing.urllib.request, "urlopen", fake_urlopen)
    return captured


def _sent(captured) -> dict[str, str]:
    """Sent headers keyed by lowercase name, since urllib capitalizes them."""
    return {name.lower(): value for name, value in captured["request"].header_items()}


class TestAttributionHeaders:
    def test_claude_copies_harness_surface_headers_and_ids(self):
        headers = routing.attribution_headers(
            "claude",
            session_id="sess-1",
            agent_id="agent-7",
            harness_custom_headers=(
                "x-databricks-use-coding-agent-mode: true\n"
                "User-Agent: ucode/1.0 claude/2.1 (linux; x86_64)\n"
                "Databricks-Custom-Coding-Agent: claude-code\n"
                "X-Unrelated: nope\n"
                "Authorization: Bearer secret-token"
            ),
        )

        assert headers == {
            "x-claude-code-session-id": "sess-1",
            "x-claude-code-agent-id": "agent-7",
            "User-Agent": "ucode/1.0 claude/2.1 (linux; x86_64)",
            "Databricks-Custom-Coding-Agent": "claude-code",
        }

    def test_absent_values_are_omitted_not_sent_empty(self):
        headers = routing.attribution_headers("claude", session_id=None, agent_id="")

        assert set(headers) == {"User-Agent"}
        assert headers["User-Agent"].startswith("ucode/")

    def test_missing_harness_user_agent_falls_back_to_ucode(self):
        headers = routing.attribution_headers(
            "claude",
            session_id="sess-1",
            harness_custom_headers="Databricks-Custom-Client-Surfaces: cli",
        )

        assert headers["User-Agent"].startswith("ucode/")
        assert headers["Databricks-Custom-Client-Surfaces"] == "cli"

    def test_unsendable_id_is_omitted(self):
        headers = routing.attribution_headers("codex", session_id="bad\nvalue")

        assert "session-id" not in headers

    def test_codex_uses_generic_session_and_thread_names(self):
        headers = routing.attribution_headers("codex", session_id="cx-1", agent_id="th-2")

        assert headers["session-id"] == "cx-1"
        assert headers["thread-id"] == "th-2"
        assert "x-claude-code-session-id" not in headers
        assert headers["User-Agent"].startswith("ucode/")


class TestSelectRouteHeaders:
    def test_attribution_is_sent_and_authorization_is_unaffected(self, monkeypatch):
        captured = _capture_select(monkeypatch)

        routing.select_route(
            WS,
            "tok-secret",
            "task",
            [("system.ai.claude-opus-4-8", "claude")],
            router_name="stable",
            attribution_headers={"x-claude-code-session-id": "sess-1"},
        )

        sent = _sent(captured)
        assert sent["x-claude-code-session-id"] == "sess-1"
        assert sent["authorization"] == "Bearer tok-secret"
        assert sent["content-type"] == "application/json"

    def test_ug_headers_override_attribution(self, monkeypatch):
        monkeypatch.setenv(
            "UG_HEADERS", "x-claude-code-session-id: override\nUser-Agent: custom-ua"
        )
        captured = _capture_select(monkeypatch)

        routing.select_route(
            WS,
            "tok",
            "task",
            [("system.ai.claude-opus-4-8", "claude")],
            router_name="stable",
            attribution_headers={
                "x-claude-code-session-id": "sess-1",
                "User-Agent": "ucode/1.0",
            },
        )

        sent = _sent(captured)
        assert sent["x-claude-code-session-id"] == "override"
        assert sent["user-agent"] == "custom-ua"

    def test_no_attribution_sends_no_session_header(self, monkeypatch):
        captured = _capture_select(monkeypatch)

        routing.select_route(
            WS,
            "tok",
            "task",
            [("system.ai.claude-opus-4-8", "claude")],
            router_name="stable",
        )

        assert "x-claude-code-session-id" not in _sent(captured)


class TestHarnessCallers:
    def test_claude_subagent_routing_sends_session_agent_and_surface(self, monkeypatch):
        monkeypatch.setenv(
            "ANTHROPIC_CUSTOM_HEADERS",
            "User-Agent: ucode/9.9 claude/2.1\nDatabricks-Custom-Client-Surfaces: cli",
        )
        captured = _capture_select(monkeypatch)

        v2.route_claude_subagent(
            {
                "session_id": "claude-sess",
                "agent_id": "sub-1",
                "tool_name": "Agent",
                "tool_input": {"prompt": "find entry points", "description": "explore"},
            },
            workspace=WS,
            token="tok",
            available_models=CLAUDE_MODELS,
        )

        sent = _sent(captured)
        assert sent["x-claude-code-session-id"] == "claude-sess"
        assert sent["x-claude-code-agent-id"] == "sub-1"
        assert sent["user-agent"] == "ucode/9.9 claude/2.1"
        assert sent["databricks-custom-client-surfaces"] == "cli"
        assert "databricks-custom-coding-agent" not in sent
        assert sent["authorization"] == "Bearer tok"

    @pytest.mark.parametrize("agent_id", [None, "agent-parent"])
    def test_claude_mod_payload_attributes_the_calling_subagent(self, monkeypatch, agent_id):
        captured = _capture_select(monkeypatch)
        # The native mod's payload: agent_id is the calling loop's, absent on the main loop.
        payload = {
            "session_id": "claude-sess",
            "tool_use_id": "toolu_1",
            "tool_name": "Agent",
            "tool_input": {"prompt": "find entry points", "subagent_type": "Explore"},
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id

        output = v2.route_claude_subagent(
            payload, workspace=WS, token="tok", available_models=CLAUDE_MODELS
        )

        assert output is not None
        assert output["model"] == CLAUDE_MODELS[0]
        assert output["updatedInput"]["prompt"] == "find entry points"
        sent = _sent(captured)
        assert sent["x-claude-code-session-id"] == "claude-sess"
        assert sent.get("x-claude-code-agent-id") == agent_id

    def test_claude_first_prompt_routing_has_no_session_header(self, monkeypatch):
        captured = _capture_select(monkeypatch)

        v2._request_claude_routing_decision(WS, "tok", "prompt", CLAUDE_MODELS)

        sent = _sent(captured)
        assert "x-claude-code-session-id" not in sent
        assert "databricks-custom-coding-agent" not in sent
        assert sent["user-agent"].startswith("ucode/")

    @pytest.mark.parametrize(
        ("session_id", "agent_id", "expected_thread_id"),
        [
            # A spawn from the root has no agent_id; the root thread ID is the session ID.
            ("codex-sess", None, "codex-sess"),
            ("codex-sess", "child-thread", "child-thread"),
            (None, None, None),
        ],
    )
    def test_codex_spawn_routing_attributes_the_calling_thread(
        self, monkeypatch, session_id, agent_id, expected_thread_id
    ):
        captured = _capture_select(monkeypatch)
        payload = {
            "tool_name": "spawn_agent",
            "tool_input": {"message": "do the thing", "task_name": "worker"},
        }
        if session_id is not None:
            payload["session_id"] = session_id
        if agent_id is not None:
            payload["agent_id"] = agent_id

        codex_routing.route_pre_tool_use(
            payload, workspace=WS, token="tok", available_models=CODEX_MODELS
        )

        sent = _sent(captured)
        assert sent.get("session-id") == session_id
        assert sent.get("thread-id") == expected_thread_id
        assert sent["user-agent"].startswith("ucode/")
        assert sent["databricks-custom-coding-agent"] == "codex"

    def test_ug_headers_override_codex_coding_agent(self, monkeypatch):
        monkeypatch.setenv("UG_HEADERS", "Databricks-Custom-Coding-Agent: other-agent")
        captured = _capture_select(monkeypatch)

        codex_routing.route_pre_tool_use(
            {
                "session_id": "codex-sess",
                "tool_name": "spawn_agent",
                "tool_input": {"message": "do the thing", "task_name": "worker"},
            },
            workspace=WS,
            token="tok",
            available_models=CODEX_MODELS,
        )

        assert _sent(captured)["databricks-custom-coding-agent"] == "other-agent"

    def test_codex_request_without_session_omits_session_header(self, monkeypatch):
        captured = _capture_select(monkeypatch)

        codex_routing.request_routing_decision(WS, "tok", "task", CODEX_MODELS)

        sent = _sent(captured)
        assert "session-id" not in sent
        assert "thread-id" not in sent
        assert sent["databricks-custom-coding-agent"] == "codex"
