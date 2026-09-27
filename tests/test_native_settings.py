"""Pinned native schema validation, pure composition, and launch policy checks."""

import copy
import unittest

import pytest
import tomlkit

from ucode.native_settings import (
    CLAUDE_HOOK_EVENTS,
    compose_native_settings,
    native_ownership,
    preflight_native_launch,
    required_os_scopes,
    tracing_conflict_paths,
    validate_native_requirements,
    validate_native_settings,
)


class NativeSettingsTests(unittest.TestCase):
    def settings(self, tool, value, raw=None):
        return validate_native_settings(value, "$.config.native_settings", tool, raw or {})

    def test_claude_supported_subset_and_independent_copy(self):
        value = {
            "permissions": {
                "allow": ["Read"],
                "ask": ["Bash"],
                "deny": ["Write"],
                "additionalDirectories": ["/workspace"],
                "disableBypassPermissionsMode": "disable",
            },
            "hooks": {
                event: [
                    {
                        "matcher": "Agent",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/helper",
                                "args": ["argument"],
                                "timeout": 0.5,
                                "statusMessage": "Running",
                                "once": True,
                                "async": False,
                                "asyncRewake": True,
                            }
                        ],
                    }
                ]
                for event in CLAUDE_HOOK_EVENTS
            },
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": False,
                "allowUnsandboxedCommands": False,
            },
            "allowManagedPermissionRulesOnly": True,
            "forceLoginOrgUUID": ["not-a-uuid-but-a-schema-valid-string"],
            "disableWorkflows": True,
            "workflowKeywordTriggerEnabled": False,
            "autoCompactEnabled": False,
            "channelsEnabled": True,
            "allowedChannelPlugins": [{"marketplace": "market", "plugin": "channel"}],
            "spinnerVerbs": {"mode": "append", "verbs": ["Working"]},
            "attribution": {"commit": "", "pr": "", "sessionUrl": False},
            "companyAnnouncements": ["Notice"],
            "otelHeadersHelper": "/telemetry-helper",
            "statusLine": {
                "type": "command",
                "command": "/status",
                "padding": 0,
                "refreshInterval": 1.5,
                "hideVimModeIndicator": True,
            },
        }
        original = copy.deepcopy(value)
        result = self.settings("claude", value)
        self.assertEqual(result, original)
        result["hooks"]["PreToolUse"][0]["hooks"].clear()
        self.assertEqual(value, original)

    def test_claude_unknown_nested_and_reserved_fields(self):
        for value in (
            {"prStatusFooterEnabled": False},
            {"permissions": {"sandbox": {"enabled": True}}},
            {"permissions": {"allowManagedPermissionRulesOnly": True}},
            {"sandbox": {"unknown": True}},
            {"hooks": {"UnknownEvent": []}},
            {"env": {}},
            {"apiKeyHelper": "/helper"},
            {"modelPicker": {}},
            {"managedMcpServers": {}},
            {"skills": {}},
            {"enabledPlugins": {}},
        ):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings("claude", value)

    def test_hook_handler_shapes_and_positive_timeout(self):
        for timeout in (0, -1, True, None, float("nan"), float("inf"), "1"):
            value = {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "/helper", "timeout": timeout}]}
                    ]
                }
            }
            with self.subTest(timeout=timeout), self.assertRaises(RuntimeError):
                self.settings("claude", value)
        for handler in (
            {"type": "prompt", "command": "x"},
            {"type": "command"},
            {"type": "command", "command": "x", "unknown": True},
        ):
            with self.subTest(handler=handler), self.assertRaises(RuntimeError):
                self.settings("claude", {"hooks": {"PreToolUse": [{"hooks": [handler]}]}})

    def test_claude_structured_required_fields(self):
        for value in (
            {"spinnerVerbs": {"mode": "append"}},
            {"spinnerVerbs": {"mode": "invalid", "verbs": []}},
            {"allowedChannelPlugins": [{"plugin": "channel"}]},
            {"allowedChannelPlugins": ["channel@market"]},
            {"statusLine": {"type": "command", "command": "x", "refreshInterval": 0.5}},
            {"permissions": {"disableBypassPermissionsMode": "enable"}},
            {"forceLoginOrgUUID": []},
            {"forceLoginOrgUUID": ""},
        ):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings("claude", value)
        self.assertEqual(
            self.settings("claude", {"forceLoginOrgUUID": "org"}), {"forceLoginOrgUUID": "org"}
        )

    def test_codex_distinct_exporters_and_supported_fields(self):
        value = {
            "otel": {
                "environment": "example",
                "log_user_prompt": False,
                "exporter": {
                    "otlp-http": {
                        "endpoint": "https://logs.invalid",
                        "protocol": "json",
                        "headers": {"X-Header": "value"},
                        "tls": {"ca-certificate": "/ca.pem"},
                    }
                },
                "metrics_exporter": {"otlp-grpc": {"endpoint": "https://metrics.invalid"}},
                "trace_exporter": "none",
            },
            "features": {"hooks": True, "fast_mode": False},
            "tui": {"status_line": ["model-name", "context-remaining"]},
            "model_auto_compact_token_limit": 500000,
            "model_auto_compact_token_limit_scope": "total",
        }
        self.assertEqual(self.settings("codex", value), value)
        for exporter in ("none", "statsig"):
            self.settings("codex", {"otel": {"trace_exporter": exporter}})

    def test_codex_exporter_rejects_invalid_shapes(self):
        exporters = (
            None,
            True,
            "otlp",
            {},
            {"otlp-http": {}, "otlp-grpc": {}},
            {"otlp-http": {"endpoint": "x"}},
            {"otlp-http": {"endpoint": "x", "protocol": "protobuf"}},
            {"otlp-grpc": {"endpoint": "x", "protocol": "json"}},
            {"otlp-grpc": {"endpoint": "x", "headers": {"Authorization": False}}},
            {"otlp-grpc": {"endpoint": "x", "tls": {"ca_certificate": "/ca"}}},
            {"otlp-grpc": {"endpoint": "x", "tls": {"ca-certificate": "relative"}}},
        )
        for exporter in exporters:
            with self.subTest(exporter=exporter), self.assertRaises(RuntimeError):
                self.settings("codex", {"otel": {"trace_exporter": exporter}})

    def test_codex_reserved_and_unsupported_paths(self):
        for value in (
            {"features": {"unknown": True}},
            {"otel": {"span_attributes": {}}},
            {"tui": {"unknown": []}},
            {"model": "model"},
            {"model_provider": "provider"},
            {"model_providers": {}},
            {"profiles": {}},
            {"model_catalog_json": "/catalog"},
            {"env": {}},
            {"mcp_servers": {}},
            {"skills": {}},
            {"shell_environment_policy": {"set": {"KEY": "value"}}},
        ):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings("codex", value)

    def test_strict_scalar_and_toml_types(self):
        for value in (True, 1.0, None, "100", 2**63, -(2**63) - 1):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings("codex", {"model_auto_compact_token_limit": value})
        for value in (-(2**63), 2**63 - 1):
            self.settings("codex", {"model_auto_compact_token_limit": value})
        for value in (0, 1, "false", None):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings("codex", {"features": {"hooks": value}})
        for value in ({1: True}, {"autoCompactEnabled": None}, {"companyAnnouncements": [None]}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings("claude", value)

    def test_requirements_exact_false_only(self):
        value = {"features": {"fast_mode": False}}
        self.assertEqual(validate_native_requirements(value, "requirements", "codex", {}), value)
        for bad in (True, 0, "false", None):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                validate_native_requirements({"features": {"fast_mode": bad}}, "r", "codex", {})
        for value in ({"features": {"hooks": False}}, {"feature_requirements": {}}, {"other": {}}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                validate_native_requirements(value, "r", "codex", {})
        with self.assertRaises(RuntimeError):
            validate_native_requirements({}, "r", "claude", {})

    def test_raw_tracing_and_resolved_tracing_conflicts(self):
        for tool, value in (
            ("claude", {"otelHeadersHelper": "/helper"}),
            ("codex", {"otel": {"trace_exporter": "none"}}),
        ):
            with self.subTest(tool=tool), self.assertRaises(RuntimeError):
                self.settings(tool, value, {"tracing": {"enabled": True}})
            self.settings(tool, value, {"tracing": {"enabled": False}})
            with self.subTest(tool=tool), self.assertRaises(RuntimeError):
                preflight_native_launch(
                    tool, value, os_managed_supported=True, resolved_tracing_enabled=True
                )
        for tracing in (True, None, {"enabled": 1}, {"unknown": False}):
            with self.subTest(tracing=tracing), self.assertRaises(RuntimeError):
                self.settings("claude", {}, {"tracing": tracing})
        value = {"otel": {"exporter": "none", "metrics_exporter": "statsig"}}
        self.settings("codex", value, {"tracing": {"enabled": True}})
        self.assertEqual(tracing_conflict_paths("codex", value), ())

    def test_scope_classification(self):
        for value in (
            {"permissions": {"deny": []}},
            {"hooks": {"StopFailure": []}},
            {"sandbox": {"enabled": False}},
            {"disableWorkflows": False},
            {"forceLoginOrgUUID": "org"},
            {"channelsEnabled": False},
        ):
            with self.subTest(value=value):
                self.assertEqual(required_os_scopes("claude", value), {"managed_settings"})
        self.assertFalse(required_os_scopes("claude", {"attribution": {"pr": ""}}))
        self.assertFalse(required_os_scopes("codex", {"tui": {"status_line": []}}))
        self.assertEqual(
            required_os_scopes(
                "codex", {"features": {"hooks": False}}, {"features": {"fast_mode": False}}
            ),
            {"managed_settings", "requirements"},
        )

    def test_preflight_platform_relay_and_org_auth(self):
        policy = {"permissions": {"deny": ["Bash"]}}
        with self.assertRaises(RuntimeError):
            preflight_native_launch("claude", policy, os_managed_supported=False)
        with self.assertRaises(RuntimeError):
            preflight_native_launch("claude", policy, os_managed_supported=True, relayed=True)
        pin = {"forceLoginOrgUUID": "org"}
        with self.assertRaises(RuntimeError):
            preflight_native_launch("claude", pin, os_managed_supported=True)
        self.assertEqual(
            preflight_native_launch(
                "claude",
                pin,
                os_managed_supported=True,
                relayed=True,
                relayed_managed_supported=True,
                first_party_oauth=True,
            ),
            {"managed_settings"},
        )
        with self.assertRaisesRegex(RuntimeError, "native_requirements"):
            preflight_native_launch(
                "codex", {}, {"features": {"fast_mode": False}}, os_managed_supported=False
            )

    def test_explicit_native_feature_requirement_conflict(self):
        settings = {"features": {"fast_mode": True}}
        requirements = {"features": {"fast_mode": False}}
        with self.assertRaises(RuntimeError):
            self.settings("codex", settings, {"native_requirements": requirements})
        with self.assertRaises(RuntimeError):
            validate_native_requirements(requirements, "r", "codex", {"native_settings": settings})
        with self.assertRaises(RuntimeError):
            preflight_native_launch("codex", settings, requirements, os_managed_supported=True)

    def test_errors_never_include_values(self):
        secret = "must-not-appear-in-error"
        for tool, value in (
            ("codex", {"otel": {"trace_exporter": secret}}),
            ("claude", {"permissions": {"disableBypassPermissionsMode": secret}}),
        ):
            with self.subTest(tool=tool):
                try:
                    self.settings(tool, value)
                except RuntimeError as exc:
                    self.assertNotIn(secret, str(exc))
                    self.assertIn("$.config.native_settings", str(exc))
                else:
                    self.fail("Invalid enum was accepted")

    def test_unsupported_agent(self):
        with self.assertRaises(RuntimeError):
            self.settings("gemini", {})

    def test_invalid_unicode_cannot_reach_toml_serialization(self):
        for value in ({"companyAnnouncements": ["\ud800"]}, {"\udfff": True}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.settings("claude", value)


@pytest.mark.parametrize(
    "tool,base,native",
    [
        ("claude", {"sandbox": False}, {"sandbox": {"enabled": True}}),
        ("claude", {"sandbox": []}, {"sandbox": {"enabled": True}}),
        ("claude", {"attribution": {"commit": {"old": True}}}, {"attribution": {"commit": ""}}),
        (
            "claude",
            {"statusLine": {"type": "other"}},
            {"statusLine": {"type": "command", "command": "x"}},
        ),
        (
            "codex",
            {"otel": {"exporter": "none"}},
            {"otel": {"exporter": {"otlp-grpc": {"endpoint": "x"}}}},
        ),
        (
            "codex",
            {"otel": {"exporter": {"otlp-grpc": {"endpoint": "x"}}}},
            {"otel": {"exporter": "none"}},
        ),
    ],
)
def test_native_composition_rejects_unowned_shape_takeovers(tool, base, native):
    original = copy.deepcopy(base)
    with pytest.raises(RuntimeError, match="unowned native settings"):
        compose_native_settings(tool, base, native, target="managed_settings")
    assert base == original


def test_native_composition_rejects_combined_exporter_variants():
    base = tomlkit.parse('[otel.exporter.otlp-grpc]\nendpoint = "original"\n')
    native = {"otel": {"exporter": {"otlp-http": {"endpoint": "new", "protocol": "json"}}}}
    with pytest.raises(RuntimeError, match="exactly one native exporter variant"):
        compose_native_settings("codex", base, native, target="managed_settings")
    assert base["otel"]["exporter"] == {"otlp-grpc": {"endpoint": "original"}}


def test_native_composition_preserves_untouched_native_siblings():
    base = tomlkit.parse(
        '[features]\nother_native_flag = true\n[otel]\nspan_attributes = { team = "old" }\n'
        'metrics_exporter = "statsig"\n'
    )
    composed = compose_native_settings(
        "codex", base, {"otel": {"exporter": "none"}}, target="managed_settings"
    )
    assert composed == {
        "features": {"other_native_flag": True},
        "otel": {
            "span_attributes": {"team": "old"},
            "metrics_exporter": "statsig",
            "exporter": "none",
        },
    }
    assert "exporter" not in base["otel"]


def test_native_hooks_and_permissions_merge_only_declared_contributions():
    owned = {"type": "command", "command": "/managed-helper"}
    personal = {"type": "prompt", "prompt": "keep"}
    base = {
        "permissions": {"deny": ["Personal"], "unknownNativeRule": True},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [personal]}]},
    }
    native = {
        "permissions": {"deny": ["Managed"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [owned]}]},
    }
    composed = compose_native_settings("claude", base, native, target="managed_settings")
    assert composed["permissions"] == {"deny": ["Personal", "Managed"], "unknownNativeRule": True}
    assert composed["hooks"]["PreToolUse"] == [{"matcher": "Bash", "hooks": [personal, owned]}]
    paths, contributions, exact = native_ownership("claude", native)
    assert paths == [["permissions", "deny"], ["hooks", "PreToolUse"]]
    assert contributions == {
        ("permissions", "deny"): ["Managed"],
        ("hooks", "PreToolUse"): [{"matcher": "Bash", "hooks": [owned]}],
    }
    assert not exact


def test_native_exact_array_classification_preserves_empty_declarations():
    native = {
        "forceLoginOrgUUID": "org",
        "allowedChannelPlugins": [],
        "spinnerVerbs": {"mode": "replace", "verbs": []},
    }
    paths, contributions, exact = native_ownership("claude", native)
    assert ["allowedChannelPlugins"] in paths
    assert contributions[("allowedChannelPlugins",)] == []
    assert exact == {("forceLoginOrgUUID",), ("allowedChannelPlugins",), ("spinnerVerbs", "verbs")}
    assert native_ownership("codex", {"tui": {"status_line": []}})[2] == {("tui", "status_line")}


def test_legacy_codex_rejects_native_settings_before_any_application():
    with pytest.raises(RuntimeError, match="upgrade Codex"):
        preflight_native_launch(
            "codex", {"tui": {"status_line": []}}, os_managed_supported=True, legacy_codex=True
        )
    assert not preflight_native_launch("codex", {}, os_managed_supported=True, legacy_codex=True)
