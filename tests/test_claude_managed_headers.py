"""Compatibility checks must distinguish attribution from routing requirements."""

import pytest

from ucode.agents import claude


@pytest.mark.parametrize(
    ("existing", "desired"),
    [
        ("x-mode: true", "x-mode: true\nUser-Agent: ug/new"),
        ("x-mode: true\nUser-Agent: isaac", "User-Agent: ug/new\nx-mode: true"),
        ("x-mode: true", "x-mode: true\nDatabricks-Smart-Router-Recipe: recipe"),
        ("X-Mode: true\nX-Tags: source=isaac", "x-tags: source=isaac\nx-mode:\ttrue "),
        ("X-Mode: true\r\n", "x-mode: true\n\n"),
    ],
)
def test_optional_attribution_and_header_formatting_do_not_conflict(existing, desired):
    assert (
        claude._managed_settings_conflicts(
            {"env": {"ANTHROPIC_CUSTOM_HEADERS": existing}},
            {"env": {"ANTHROPIC_CUSTOM_HEADERS": desired}},
            [["env", "ANTHROPIC_CUSTOM_HEADERS"]],
        )
        == []
    )


@pytest.mark.parametrize(
    ("existing", "desired"),
    [
        ("x-databricks-use-coding-agent-mode: false", "x-databricks-use-coding-agent-mode: true"),
        ("X-Databricks-Model-Provider: one", "X-Databricks-Model-Provider: two"),
        ("Authorization: Bearer one", "Authorization: Bearer two"),
        ("X-Tags: source=isaac", "X-Tags: source=other"),
        ("", "x-databricks-use-coding-agent-mode: true"),
        ("x-mode: true\nx-mode: false", "x-mode: true"),
        ("malformed", "malformed\nUser-Agent: ug"),
        ("bad name: value", "bad name: value\nUser-Agent: ug"),
        ("Uſer-Agent: ug", "User-Agent: ug"),
        ("x-mode : true", "x-mode: true"),
        ("x-mode: tr\x00ue", "x-mode: tr\x00ue\nUser-Agent: ug"),
        ({"User-Agent": "ug"}, "User-Agent: ug"),
    ],
)
def test_required_or_unparseable_headers_still_conflict(existing, desired):
    assert claude._managed_settings_conflicts(
        {"env": {"ANTHROPIC_CUSTOM_HEADERS": existing}},
        {"env": {"ANTHROPIC_CUSTOM_HEADERS": desired}},
        [["env", "ANTHROPIC_CUSTOM_HEADERS"]],
    ) == ["env.ANTHROPIC_CUSTOM_HEADERS"]


def test_optional_header_difference_does_not_hide_other_conflicts():
    assert claude._managed_settings_conflicts(
        {"env": {"ANTHROPIC_CUSTOM_HEADERS": "", "ANTHROPIC_BASE_URL": "old"}},
        {"env": {"ANTHROPIC_CUSTOM_HEADERS": "User-Agent: ug", "ANTHROPIC_BASE_URL": "new"}},
        [["env", "ANTHROPIC_CUSTOM_HEADERS"], ["env", "ANTHROPIC_BASE_URL"]],
    ) == ["env.ANTHROPIC_BASE_URL"]
