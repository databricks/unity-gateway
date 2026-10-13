"""Tests for shared constants and their small helpers."""

from __future__ import annotations

from ucode import constants


class TestRequestTagsConstants:
    def test_header_and_env_var_names(self):
        assert constants.AI_GATEWAY_REQUEST_TAGS_HEADER == "Databricks-Ai-Gateway-Request-Tags"
        assert constants.AI_GATEWAY_REQUEST_TAGS_ENV_VAR == "AI_GATEWAY_REQUEST_TAGS"
