"""Plain-language explanations of structured task-router decisions."""

_PROFILE_REASONS = (
    (
        "localization_burden",
        "open_search",
        "This task needs investigation to find what needs attention.",
    ),
    ("scope", "cross_cutting", "This task spans several components."),
    ("environment_burden", "live_external", "This task depends on live systems or data."),
    ("reasoning_depth", "open_ended", "Solving this task requires exploring possible approaches."),
    ("verification_strength", "weak", "There is no clear way to check the result of this task."),
    ("scope", "connected", "This task involves several related components."),
    ("reasoning_depth", "multi_step", "This task involves several dependent steps."),
    ("localization_burden", "bounded_search", "This task needs a focused search."),
)


def format_rationale(selection: dict, rationale: str) -> str:
    """Summarize task_v3 profiles, preserving unfamiliar responses and gateway notices."""
    params = selection.get("params")
    if not isinstance(params, dict) or params.get("router_version") != "task_v3":
        return rationale
    task_tier, served_tier = params.get("task_tier"), params.get("served_tier")
    tiers = ("Low", "Medium", "High")
    if task_tier not in tiers or served_tier not in tiers:
        return rationale
    if tiers.index(served_tier) < tiers.index(task_tier):
        return rationale

    evidence = params.get("evidence_spans")
    if not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], str):
        return rationale
    ending = f" Request: “{evidence[0]}”."
    if task_tier != served_tier:
        ending += f" Your configured model availability requires service at {served_tier}."
    # Only replace the known generated portion; custom gateway notices follow it.
    _, separator, notice = rationale.partition(ending)
    if not separator:
        return rationale

    profile = params.get("task_profile")
    if params.get("profiler_status") == "unavailable":
        if task_tier != "High" or served_tier != "High":
            return rationale
        reason = "The task could not be assessed, so a high-capability model was selected."
    elif params.get("profiler_status") == "ok" and isinstance(profile, dict):
        reason = next(
            (text for field, value, text in _PROFILE_REASONS if profile.get(field) == value),
            "",
        )
        if (
            not reason
            and profile.get("localization_burden") == "explicit_target"
            and profile.get("scope") == "localized"
        ):
            reason = "This task has a clear target and limited scope."
        if not reason:
            return rationale
    else:
        return rationale
    if task_tier != served_tier:
        reason += " A higher-tier model was selected because the requested tier isn't available."
    return " ".join(part for part in (reason, notice.strip()) if part)
