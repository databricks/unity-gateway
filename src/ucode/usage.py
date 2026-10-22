"""Show the current user's coding-agent budget usage."""

from __future__ import annotations

import os
from decimal import Decimal

from rich.markup import escape

from ucode.budget import BudgetLimitStatus, list_current_user_budget_spend_statuses
from ucode.databricks import (
    apply_pat_environment,
    ensure_databricks_auth,
    get_databricks_token,
    raise_for_invalid_access_token,
    resolve_current_budget_spend,
)
from ucode.state import load_state
from ucode.ui import (
    console,
    format_meter,
    format_usd,
    label,
    muted,
    print_note,
    spinner,
    value,
)

USAGE_SPEND_API_ENV_VAR = "ENABLE_UG_USAGE_SPEND_API"
_PER_USER_SCOPE = "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER"
_DEFAULT_LIMIT_COUNT = 5


def render_budget_summary(budget_spend: tuple[Decimal, Decimal]) -> str:
    """Render the legacy spend summary and meter."""
    spend, total_budget = budget_spend
    fraction = float(spend / total_budget) if total_budget > 0 else 0.0
    summary = f"{format_usd(spend)} of {format_usd(total_budget)} ({fraction:.0%})"
    return "\n".join(
        [
            f"{label('Budget spend:')} {value(summary)}",
            muted(format_meter(fraction)),
        ]
    )


def _format_percent(percent: Decimal) -> str:
    return format(percent.normalize(), "f")


def _limit_label(status: BudgetLimitStatus, *, include_budget_id: bool = False) -> str:
    period = {"DAY": "Daily", "MONTH": "Monthly"}.get(status.time_period, status.time_period)
    scope = {
        _PER_USER_SCOPE: "personal",
        "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED": "shared",
    }.get(status.scope_type, f"scope {status.scope_type}")
    suffix = f" (budget {status.budget_id[:8]})" if include_budget_id else ""
    return f"{period} {scope} limit{suffix}"


def _personal_spend_line(status: BudgetLimitStatus) -> str | None:
    if status.scope_type != _PER_USER_SCOPE:
        return None
    period = {"DAY": "today", "MONTH": "this month"}.get(
        status.time_period, f"for period {escape(status.time_period)}"
    )
    if status.threshold is None:
        return f"{format_usd(status.spend)} used {period}" if status.spend is not None else None
    if status.spend is None:
        return f"{format_usd(status.threshold)} limit {period}"
    spend = status.spend
    remaining = status.threshold - spend
    if remaining >= 0:
        balance = f"{format_usd(remaining)} remaining {period}"
    else:
        balance = f"{format_usd(-remaining)} over the limit {period}"
    return f"{format_usd(spend)} of {format_usd(status.threshold)} used · {balance}"


def _render_limit(status: BudgetLimitStatus, *, include_budget_id: bool = False) -> str:
    if status.percent_consumed is None:
        usage_line = muted("Usage percentage not reported")
    else:
        if status.percent_consumed >= 100:
            color = "red"
        elif status.percent_consumed >= 80:
            color = "yellow"
        else:
            color = "green"
        fraction = float(status.percent_consumed / Decimal(100))
        percent = f"{_format_percent(status.percent_consumed)}% used"
        usage_line = f"[{color}]{format_meter(fraction)}[/{color}] {percent}"
    lines = [
        usage_line,
        label(escape(_limit_label(status, include_budget_id=include_budget_id))),
    ]
    spend_line = _personal_spend_line(status)
    if spend_line is not None:
        lines.append(spend_line)
    return "\n".join(lines)


def render_budget_limits(statuses: list[BudgetLimitStatus]) -> str:
    """Render up to five limits, prioritizing the highest usage in each category."""
    ordered = sorted(
        statuses,
        key=lambda status: (
            status.percent_consumed if status.percent_consumed is not None else Decimal(-1)
        ),
        reverse=True,
    )
    featured: list[BudgetLimitStatus] = []
    additional: list[BudgetLimitStatus] = []
    featured_categories: set[tuple[str, str]] = set()
    for status in ordered:
        category = (status.scope_type, status.time_period)
        if category in featured_categories:
            additional.append(status)
        else:
            featured_categories.add(category)
            featured.append(status)

    featured = featured[:_DEFAULT_LIMIT_COUNT]
    additional = additional[: _DEFAULT_LIMIT_COUNT - len(featured)]
    sections = [label("Your Unity Gateway spend limits")]
    sections.extend(_render_limit(status) for status in featured)
    if additional:
        sections.append(label("Additional limits"))
        sections.extend(_render_limit(status, include_budget_id=True) for status in additional)
    displayed_count = len(featured) + len(additional)
    if displayed_count < len(statuses):
        sections.append(muted(f"Showing {displayed_count} of {len(statuses)} spend limits."))
    return "\n" + "\n\n".join(sections)


def usage() -> int:
    state = load_state()
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("Workspace is not configured. Run `ug configure` first.")

    profile = state.get("profile")
    apply_pat_environment(state)
    ensure_databricks_auth(workspace, profile)
    with spinner("Retrieving Databricks access token..."):
        token = get_databricks_token(workspace, profile)

    if os.environ.get(USAGE_SPEND_API_ENV_VAR) != "1":
        with spinner("Checking budget spend..."):
            budget_spend, reason = resolve_current_budget_spend(workspace, token)
        raise_for_invalid_access_token(workspace, reason)
        if budget_spend is None:
            print_note(
                "Usage information is unavailable. Ask your workspace admin to configure "
                "Unity Gateway Budgets and add it to the workspace configuration."
            )
            return 0
        console.print(render_budget_summary(budget_spend))
        return 0

    budget_limits, reason = list_current_user_budget_spend_statuses(workspace, token)
    raise_for_invalid_access_token(workspace, reason)
    if reason is not None:
        if reason.startswith("HTTP 404 ") or "FEATURE_DISABLED" in reason:
            print_note(
                "Expanded budget usage is not available for this workspace. "
                f"Unset {USAGE_SPEND_API_ENV_VAR} to use the standard summary."
            )
            return 0
        raise RuntimeError(f"Could not retrieve Unity Gateway spend limits: {reason}")

    if not budget_limits:
        print_note("No Unity Gateway spend limits found for this workspace.")
        return 0

    console.print(render_budget_limits(budget_limits))
    return 0
