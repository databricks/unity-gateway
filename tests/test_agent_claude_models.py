"""Claude-specific catalog and picker presentation contracts."""

import pytest

from ucode.agents.claude_models import picker_label


@pytest.mark.parametrize(
    ("model", "label"),
    [
        ("main.default.lilly-personal-anthropic.claude-haiku-5-5", "Claude Haiku 5.5"),
        ("main.default.lilly-personal-anthropic.claude-opus-5-5", "Claude Opus 5.5"),
        ("main.default.lilly-personal-anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5"),
        ("system.ai.claude-haiku-4-5", "Claude Haiku 4.5"),
        ("gpt-5.5", "GPT 5.5"),
        ("system.ai.gpt-5.5", "GPT 5.5"),
    ],
)
def test_picker_label(model, label):
    assert picker_label(model) == label
