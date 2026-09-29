"""Tests for shared constants and their small helpers."""

from __future__ import annotations

from ucode import constants


class TestRequestTagsHeaderValue:
    def test_returns_stripped_value_from_explicit_env(self):
        env = {"AI_GATEWAY_REQUEST_TAGS": '  {"source":"my-app"}  '}
        assert constants.request_tags_header_value(env) == '{"source":"my-app"}'

    def test_returns_none_when_absent(self):
        assert constants.request_tags_header_value({}) is None

    def test_returns_none_when_blank(self):
        assert constants.request_tags_header_value({"AI_GATEWAY_REQUEST_TAGS": "   "}) is None

    def test_reads_process_environment_by_default(self, monkeypatch):
        monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", '{"source":"my-app"}')
        assert constants.request_tags_header_value() == '{"source":"my-app"}'

    def test_process_environment_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)
        assert constants.request_tags_header_value() is None

    def test_header_and_env_var_names(self):
        assert constants.AI_GATEWAY_REQUEST_TAGS_HEADER == "Databricks-Ai-Gateway-Request-Tags"
        assert constants.AI_GATEWAY_REQUEST_TAGS_ENV_VAR == "AI_GATEWAY_REQUEST_TAGS"
