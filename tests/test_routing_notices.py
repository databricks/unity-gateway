"""Display-only filtering of routing reasons for both agents' notices."""

from ucode.smart_routing import routing


def test_first_prompt_notice_hides_request_and_preserves_availability_reason():
    policy = " Your configured model availability requires service at T3."
    reason = "A narrow task. Request: “Fix the parser”." + policy

    assert routing.format_switch_message("model-x", reason) == routing.format_switch_message(
        "model-x", "A narrow task." + policy
    )


def test_subagent_notice_preserves_prompt_and_raw_rationale():
    reason = "A narrow task. Request: “Fix the parser”."
    decision = routing.RoutingDecision("model-x", "model-x", rationale=reason)

    message = decision.display_message(is_subagent=True, prompt="Fix the parser")

    assert "Prompt : Fix the parser" in message
    assert "Reason : A narrow task." in message
    assert "Request:" not in message
    assert decision.rationale == reason
