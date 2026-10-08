"""Claude extra-body composition independent of Smart Router."""

import json

import pytest

from ucode.agents.claude_settings import merge_extra_body


def test_extra_body_merge_preserves_unowned_fields_and_handles_escaping():
    merged = merge_extra_body(
        '{"caller":{"keep":true},"session_setting":"old"}',
        {"session_setting": 'custom<&"value'},
    )
    assert json.loads(merged) == {"caller": {"keep": True}, "session_setting": 'custom<&"value'}


def test_extra_body_without_existing_value():
    assert json.loads(merge_extra_body(None, {"session_setting": "value"})) == {
        "session_setting": "value"
    }


@pytest.mark.parametrize("raw", ["not-json", "[]", "null", "42"])
def test_extra_body_rejects_malformed_or_non_object_values(raw):
    with pytest.raises(
        RuntimeError, match="CLAUDE_CODE_EXTRA_BODY must contain a valid JSON object"
    ):
        merge_extra_body(raw, {"session_setting": "value"})
