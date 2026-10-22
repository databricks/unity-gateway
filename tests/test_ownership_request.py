"""Contract-derived, no-inference tests of captured HTTP context projection."""

from __future__ import annotations

import copy
import json

import pytest

from ucode.smart_routing.ownership_request import project_request


def _native_body() -> dict:
    """Independent fixture with the pinned native request's ordinary wire shape."""
    return {
        "model": "fixture-model",
        "system": [
            {"type": "text", "text": "System: do not change permissions."},
            {
                "type": "text",
                "text": "Project: preserve the public interface.\nRole: complete the assigned slice.",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<system-reminder>Hidden hook instruction.</system-reminder>",
                    },
                    {"type": "text", "text": "User: keep ownership here; café λ 😀."},
                ],
            }
        ],
        "tools": [
            {
                "name": "Inspect",
                "description": "Inspect only the assigned file; permission is required.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "A scoped path."}},
                    "additionalProperties": False,
                    "required": [],
                },
            }
        ],
        "metadata": {"user_id": "transport-only-identity"},
        "max_tokens": 32000,
        "thinking": {"type": "adaptive"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "output_config": {"effort": "high"},
        "stream": True,
    }


def test_complete_request_preserves_all_instruction_sources_and_current_prompt():
    body = _native_body()
    before = copy.deepcopy(body)
    result = project_request(body)
    assert set(result) == {"context", "context_complete", "attachments"}
    assert result["context_complete"] is True
    assert result["attachments"] == []
    assert all(set(entry) == {"role", "content"} for entry in result["context"])
    for block in body["system"]:
        assert {"role": "system", "content": block["text"]} in result["context"]
    for block in body["messages"][0]["content"]:
        assert {"role": "user", "content": block["text"]} in result["context"]
    tool = next(
        json.loads(row["content"])["tool_definition"]
        for row in result["context"]
        if row["content"].startswith('{"tool_definition"')
    )
    assert tool == body["tools"][0]
    assert body == before
    assert "transport-only-identity" not in json.dumps(result)


@pytest.mark.parametrize("recipe", ["task_v3", "DISABLED", None])
def test_gateway_recipe_metadata_does_not_change_projected_context(recipe):
    body = _native_body()
    expected = project_request(body)
    body["smart_router_recipe_name"] = recipe
    before = copy.deepcopy(body)
    assert project_request(body) == expected
    assert body == before


@pytest.mark.parametrize("field", ["caller_field", "smart_router_recipe_name_extra", "extra_body"])
def test_extra_body_passthrough_is_not_blanket_transport_metadata(field):
    body = _native_body()
    body["smart_router_recipe_name"] = "task_v3"
    body[field] = {"instruction": "Caller semantics cannot be silently omitted."}
    result = project_request(body)
    assert result["context_complete"] is False
    assert result["attachments"] == [{"name": f"request.{field}", "available": False}]


def test_recipe_name_inside_message_is_not_treated_as_root_transport_metadata():
    body = _native_body()
    body["messages"][0]["smart_router_recipe_name"] = "task_v3"
    result = project_request(body)
    assert result["context_complete"] is False
    assert result["attachments"] == [
        {"name": "messages[0].smart_router_recipe_name", "available": False}
    ]


def test_history_tool_calls_results_and_empty_values_remain_ordered():
    body = _native_body()
    body["messages"] += [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will inspect the assigned file."},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "Inspect",
                    "input": {"flag": False, "items": [], "optional": None},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "is_error": False,
                    "content": [{"type": "text", "text": "Complete result café λ."}],
                }
            ],
        },
        {"role": "user", "content": "Now correct that same file."},
    ]
    result = project_request(body)
    assert result["context_complete"] is True
    tail = result["context"][-5:]
    assert tail[0] == {"role": "assistant", "content": "I will inspect the assigned file."}
    assert json.loads(tail[1]["content"]) == body["messages"][1]["content"][1]
    assert tail[2]["role"] == "tool"
    assert json.loads(tail[2]["content"]) == {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "is_error": False,
    }
    assert tail[3] == {"role": "tool", "content": "Complete result café λ."}
    assert tail[4] == {"role": "user", "content": "Now correct that same file."}


def test_native_system_message_and_output_settings_are_preserved():
    body = _native_body()
    body["messages"].append(
        {
            "role": "system",
            "content": "Runtime caller instructions.",
            "output_config": {"effort": "medium"},
        }
    )
    result = project_request(body)
    assert result["context_complete"] is True
    assert result["context"][-2:] == [
        {"role": "system", "content": "Runtime caller instructions."},
        {
            "role": "system",
            "content": json.dumps({"output_config": {"effort": "medium"}}, sort_keys=True),
        },
    ]
    body["messages"][-1]["output_config"] = "invalid"
    result = project_request(body)
    assert result["context_complete"] is False
    assert result["attachments"] == [{"name": "messages[1].output_config", "available": False}]


def test_private_thinking_and_signatures_never_enter_projection():
    body = _native_body()
    body["messages"].insert(
        0,
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "PRIVATE-REASONING",
                    "signature": "PRIVATE-SIGNATURE",
                },
                {"type": "redacted_thinking", "data": "PRIVATE-REDACTED"},
                {"type": "text", "text": "Public answer", "signature": "PRIVATE-TEXT-SIGNATURE"},
            ],
        },
    )
    result = project_request(body)
    assert result["context_complete"] is True
    assert "PRIVATE-" not in json.dumps(result)
    assert {"role": "assistant", "content": "Public answer"} in result["context"]


def test_schema_fields_named_thinking_and_signature_are_caller_data():
    body = _native_body()
    schema = body["tools"][0]["input_schema"]
    schema["properties"] = {
        "thinking": {"type": "boolean"},
        "signature": {"const": "public contract"},
    }
    result = project_request(body)
    assert result["context_complete"] is True
    tool = next(
        json.loads(row["content"])["tool_definition"]
        for row in result["context"]
        if row["content"].startswith('{"tool_definition"')
    )
    assert tool["input_schema"] == schema


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "source": {"type": "base64", "data": "UNREADABLE"}},
        {"type": "document", "source": {"type": "file", "file_id": "UNREADABLE"}},
        {"type": "future_semantic_type", "text": "Must not silently accept this."},
        {"type": "text", "text": None},
        {"type": "text", "text": "Readable sibling", "new_instruction": "Unprojected instruction"},
        None,
    ],
)
def test_nontext_unreadable_and_unknown_blocks_are_incomplete(block):
    body = _native_body()
    body["messages"][0]["content"].append(block)
    result = project_request(body)
    assert result["context_complete"] is False
    assert result["attachments"]
    assert all(item["available"] is False for item in result["attachments"])
    assert result["context"][-1]["content"] in (
        "User: keep ownership here; café λ 😀.",
        "Readable sibling",
    )
    assert "UNREADABLE" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("thread", {"id": "inherited", "parent_id": "previous"}),
        ("system", {"type": "inherit"}),
        ("tools", {"type": "inherit"}),
        ("context_management", {"edits": [{"type": "clear_tool_uses_20250919"}]}),
        ("new_caller_instructions", "Unknown top-level semantic field."),
        ("messages", []),
        ("messages", [{"role": "unknown", "content": "Missing semantics"}]),
        ("messages", [{"role": "user", "content": "Text", "clear_at": "later"}]),
    ],
)
def test_inherited_or_unrecognized_request_semantics_are_incomplete(field, value):
    body = _native_body()
    body[field] = value
    assert project_request(body)["context_complete"] is False


def test_nontext_nested_tool_result_does_not_hide_missing_attachment():
    body = _native_body()
    body["messages"].append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": [
                        {"type": "text", "text": "Caption"},
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "https://invalid.example/image"},
                        },
                    ],
                }
            ],
        }
    )
    result = project_request(body)
    assert result["context_complete"] is False
    assert {"role": "tool", "content": "Caption"} in result["context"]
    assert result["attachments"] == [
        {"name": "messages[1].content[0].content[1]", "available": False}
    ]


def test_tool_choice_output_schema_and_large_text_are_preserved_without_truncation():
    body = _native_body()
    body["tool_choice"] = {"type": "tool", "name": "Inspect", "disable_parallel_tool_use": True}
    body["output_config"]["format"] = {
        "type": "json_schema",
        "schema": {"type": "object", "description": "Use the caller's answer format."},
    }
    prompt = "完整 caller context\n" * 20000
    body["messages"].append({"role": "user", "content": prompt})
    result = project_request(body)
    assert result["context_complete"] is True
    assert result["context"][-1] == {"role": "user", "content": prompt}
    for field in ("tool_choice", "output_config"):
        assert any(
            row["role"] == "system"
            and row["content"]
            == json.dumps({field: body[field]}, ensure_ascii=False, sort_keys=True)
            for row in result["context"]
        )


@pytest.mark.parametrize("body", [None, [], {"messages": None}, {"messages": [None]}])
def test_malformed_body_is_explicitly_incomplete(body):
    result = project_request(body)
    assert result["context_complete"] is False
    assert result["attachments"]


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "Read", "description": "Missing schema"},
        {"name": "Read", "input_schema": None},
        {"type": "computer_20250124", "name": "computer", "display_width_px": 1024},
        None,
    ],
)
def test_unknown_or_unreadable_tool_definition_is_incomplete(tool):
    body = _native_body()
    body["tools"] = [tool]
    result = project_request(body)
    assert result["context_complete"] is False
    assert result["attachments"] == [{"name": "tools[0]", "available": False}]
    assert {"role": "user", "content": "User: keep ownership here; café λ 😀."} in result["context"]


def test_plain_system_and_text_tool_results_need_no_attachments_or_tool_definitions():
    body = {
        "system": "Caller system policy.",
        "messages": [
            {"role": "user", "content": "Prior task"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": "Readable tool result",
                    },
                    {"type": "tool_result", "tool_use_id": "call-2", "content": []},
                ],
            },
            {"role": "user", "content": "Current prompt is separate from the root-intent prompt."},
        ],
    }
    result = project_request(body)
    assert result["context_complete"] is True
    assert result["context"][0] == {"role": "system", "content": body["system"]}
    assert {"role": "tool", "content": "Readable tool result"} in result["context"]
    assert result["context"][-1]["content"] == body["messages"][-1]["content"]


def test_nontext_system_block_and_bad_tool_input_are_explicitly_incomplete():
    body = _native_body()
    body["system"].append({"type": "reference", "id": "missing-system"})
    body["messages"].append(
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-1", "name": "Read", "input": None}],
        }
    )
    result = project_request(body)
    assert result["context_complete"] is False
    assert result["attachments"] == [
        {"name": "system[2]", "available": False},
        {"name": "messages[1].content[0]", "available": False},
    ]
