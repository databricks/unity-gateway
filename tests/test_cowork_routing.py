"""Cowork prompt routing for configured Unity Gateway integrations."""

from ucode.agents.desktop import cowork_routing


def test_routing_guidance_names_configured_integrations():
    guidance = cowork_routing.routing_guidance(
        ["system-ai-github", "system-ai-atlassian", "custom-service"]
    )

    assert "GitHub (`unity-gateway:system-ai-github`)" in guidance
    assert "Atlassian, Jira, and Confluence (`unity-gateway:system-ai-atlassian`)" in guidance
    assert "Custom Service (`unity-gateway:custom-service`)" in guidance
    assert "before saying the connector is unavailable" in guidance


def test_hook_output_injects_guidance_before_user_prompt():
    output = cowork_routing.hook_output(
        {"hook_event_name": "UserPromptSubmit"}, ["system-ai-github"]
    )

    hook_output = output["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "UserPromptSubmit"
    assert "unity-gateway:system-ai-github" in hook_output["additionalContext"]


def test_hook_output_ignores_other_events():
    assert (
        cowork_routing.hook_output({"hook_event_name": "SessionStart"}, ["system-ai-github"]) == {}
    )
