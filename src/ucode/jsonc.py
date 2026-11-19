"""Edit one top-level member of a JSON-with-comments object (such as VS Code's settings.json) in
place, keeping the rest of the text, comments and trailing commas included, as it was."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class _Member:
    key: str
    key_start: int
    value_start: int
    value_end: int


def _string_end(text: str, start: int) -> int:
    index = start + 1
    while index < len(text) and text[index] != '"':
        index += 2 if text[index] == "\\" else 1
    if index >= len(text):
        raise ValueError("unterminated string")
    return index + 1


def _skip_comment(text: str, index: int) -> int | None:
    """The index after a comment starting at ``index``, or None when there is none."""
    if text.startswith("//", index):
        end = text.find("\n", index)
        return len(text) if end < 0 else end
    if text.startswith("/*", index):
        end = text.find("*/", index + 2)
        if end < 0:
            raise ValueError("unterminated comment")
        return end + 2
    return None


def _members(text: str) -> tuple[list[_Member], int, int]:
    """The top-level object's members, and the indexes of its opening and closing braces."""
    members: list[_Member] = []
    depth = 0
    opening = -1
    key: str | None = None
    key_start = value_start = value_end = -1
    colon = False
    index = 0
    while index < len(text):
        if (after := _skip_comment(text, index)) is not None:
            index = after
            continue
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if depth == 0:
            if char != "{" or opening >= 0:
                raise ValueError("not a JSON object")
            opening, depth, index = index, 1, index + 1
            continue
        end = _string_end(text, index) if char == '"' else index + 1
        if depth == 1 and char == '"' and key is None:
            key, key_start = json.loads(text[index:end]), index
        elif depth == 1 and char == ":" and key is not None and not colon:
            colon = True
        elif depth == 1 and char in ",}":
            if key is not None:
                if value_start < 0:
                    raise ValueError(f"no value for {key}")
                members.append(_Member(key, key_start, value_start, value_end))
            key, value_start, colon = None, -1, False
            if char == "}":
                rest = text[end:]
                while rest.strip():
                    skipped = _skip_comment(rest, len(rest) - len(rest.lstrip()))
                    if skipped is None:
                        raise ValueError("text after the JSON object")
                    rest = rest[skipped:]
                return members, opening, index
        else:
            if key is None or not colon:
                raise ValueError("expected a key and a colon")
            if depth == 1 and (char in ":]" or (value_start >= 0 and index != value_end)):
                raise ValueError(f"malformed value for {key}")
            if value_start < 0:
                value_start = index
            if char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
            value_end = end
        index = end
    if opening < 0:
        return [], -1, -1
    raise ValueError("unterminated object")


def get_member(text: str, key: str) -> object:
    """The value of top-level ``key`` in ``text``, or None when it's absent."""
    for member in _members(text)[0]:
        if member.key == key:
            return json.loads(text[member.value_start : member.value_end])
    return None


def _indent(text: str, members: list[_Member]) -> str:
    """The indent of the members' lines, or VS Code's four spaces."""
    if members:
        line_start = text.rfind("\n", 0, members[0].key_start) + 1
        prefix = text[line_start : members[0].key_start]
        if prefix and not prefix.strip():
            return prefix
    return "    "


def set_member(text: str, key: str, value: object) -> str:
    """``text`` with top-level ``key`` set to ``value``; an empty ``text`` is an empty object.

    Raises ValueError when ``text`` isn't a JSON(C) object."""
    members, opening, closing = _members(text)
    rendered = json.dumps(value)
    for member in members:
        if member.key == key:
            return text[: member.value_start] + rendered + text[member.value_end :]
    indent = _indent(text, members)
    entry = f"{json.dumps(key)}: {rendered}"
    if opening < 0:
        return f"{{\n{indent}{entry}\n}}\n"
    if members:
        end = members[-1].value_end
        return f"{text[:end]},\n{indent}{entry}{text[end:]}"
    inside = text[opening + 1 : closing]
    if not inside.strip():
        inside = ""
    return f"{text[: opening + 1]}{inside}\n{indent}{entry}\n{text[closing:]}"


def remove_member(text: str, key: str) -> str:
    """``text`` without top-level ``key``. Raises ValueError when ``text`` isn't a JSON(C) object."""
    members = _members(text)[0]
    for position, member in enumerate(members):
        if member.key != key:
            continue
        if position + 1 < len(members):
            return text[: member.key_start] + text[members[position + 1].key_start :]
        if position > 0:
            return text[: members[position - 1].value_end] + text[member.value_end :]
        rest = text[member.value_end :]
        stripped = rest.lstrip()
        if stripped.startswith(","):
            rest = stripped[1:]
        start = member.key_start
        while start > 0 and text[start - 1] in " \t":
            start -= 1
        if start > 0 and text[start - 1] == "\n":
            start -= 1
        return text[:start] + rest
    return text
