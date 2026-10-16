from ucode.smart_routing.rationale import format_rationale


def test_plain_language_reason():
    selection = {
        "params": {
            "router_version": "task_v3",
            "task_tier": "Low",
            "served_tier": "Low",
            "profiler_status": "ok",
            "evidence_spans": ["Inspect the parser."],
            "task_profile": {"localization_burden": "explicit_target", "scope": "localized"},
        }
    }
    rationale = (
        "Routed to Low because its task-only profile is low: "
        "an explicit target, localized scope, direct reasoning. Request: “Inspect the parser.”."
    )
    assert (
        format_rationale(selection, rationale) == "This task has a clear target and limited scope."
    )


def test_unknown_response_is_preserved():
    rationale = "A custom routing explanation."
    assert format_rationale({}, rationale) == rationale
