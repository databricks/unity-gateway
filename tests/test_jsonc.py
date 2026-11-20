"""Editing one member of a JSON-with-comments object in place."""

from __future__ import annotations

import pytest

from ucode import jsonc

KEY = "chat.agentHost.copilot.runtimePath"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("", '{\n    "k": "v"\n}\n', id="empty-file"),
        pytest.param("{}", '{\n    "k": "v"\n}', id="empty-object"),
        pytest.param("{\n}\n", '{\n    "k": "v"\n}\n', id="vscode-empty"),
        pytest.param('{\n  "a": 1\n}', '{\n  "a": 1,\n  "k": "v"\n}', id="keeps-indent"),
        pytest.param(
            '{\n    // mine\n    "a": 1,\n}\n',
            '{\n    // mine\n    "a": 1,\n    "k": "v",\n}\n',
            id="comment-and-trailing-comma",
        ),
        pytest.param('{\n    "k": "old", "b": 2\n}', '{\n    "k": "v", "b": 2\n}', id="replaces"),
    ],
)
def test_set_member(text, expected):
    assert jsonc.set_member(text, "k", "v") == expected


def test_set_ignores_braces_and_keys_inside_strings_comments_and_nested_values():
    text = '{\n  "a": {"k": [1, {"y": "}"}]},\n  /* "k": 1, } */ "b": "v\\"q",\n  "c": null\n}'

    edited = jsonc.set_member(text, "k", "/path")

    assert edited.startswith(text[: text.rindex("null") + 4])
    assert jsonc.get_member(edited, "k") == "/path"
    assert jsonc.get_member(edited, "a") == {"k": [1, {"y": "}"}]}
    assert jsonc.get_member(edited, "b") == 'v"q'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(f'{{\n    "{KEY}": "/w"\n}}\n', "{\n}\n", id="only"),
        pytest.param(f'{{\n    "{KEY}": "/w",\n}}', "{\n}", id="only-trailing-comma"),
        pytest.param(f'{{\n    "a": 1,\n    "{KEY}": "/w"\n}}', '{\n    "a": 1\n}', id="last"),
        pytest.param(f'{{\n    "{KEY}": "/w",\n    "a": 1\n}}', '{\n    "a": 1\n}', id="first"),
        pytest.param(
            f'{{\n    // keep\n    "a": 1,\n    "{KEY}": "/w",\n}}',
            '{\n    // keep\n    "a": 1\n}',
            id="jsonc",
        ),
        pytest.param('{\n    "a": 1\n}', '{\n    "a": 1\n}', id="absent"),
    ],
)
def test_remove_member(text, expected):
    assert jsonc.remove_member(text, KEY) == expected


def test_set_refuses_a_duplicate_key_instead_of_guessing_which_one_counts():
    with pytest.raises(ValueError, match="more than once"):
        jsonc.set_member('{\n    "k": 1,\n    "k": 2\n}', "k", 3)


def test_remove_takes_a_comma_on_the_next_line_too():
    text = f'{{\n    "{KEY}": "/w"\n    ,\n    "a": 1\n}}'

    edited = jsonc.remove_member(text, KEY)

    assert jsonc.get_member(edited, KEY) is None
    assert jsonc.get_member(edited, "a") == 1


def test_get_member_of_an_absent_key_is_none():
    assert jsonc.get_member('{"a": 1}', "k") is None
    assert jsonc.get_member("", "k") is None


@pytest.mark.parametrize(
    "text",
    ["[1]", "{} x", '{"a" 1}', '{"a": 1 2}', '{"a"::1}', '{"a": 1', '{"a": "x', "/* x", "nope"],
)
def test_refuses_what_isnt_a_json_object(text):
    with pytest.raises(ValueError):
        jsonc.set_member(text, "k", "v")
