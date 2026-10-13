"""Show the current user's coding-agent budget usage."""

from __future__ import annotations

from decimal import Decimal

from ucode.databricks import (
    BudgetLimitStatus,
    apply_pat_environment,
    ensure_databricks_auth,
    get_databricks_token,
    list_current_user_budget_spend_statuses,
    raise_for_invalid_access_token,
)
from ucode.state import load_state
from ucode.ui import (
    console,
    format_meter,
    format_usd,
    heading,
    label,
    muted,
    print_note,
    spinner,
    value,
)

_PER_USER_SCOPE = "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER"


def _format_percent(percent: Decimal) -> str:
    return format(percent.normalize(), "f")


def _limit_label(status: BudgetLimitStatus, *, include_budget_id: bool = False) -> str:
    period = "Daily" if status.time_period == "DAY" else "Monthly"
    scope = "personal" if status.scope_type == _PER_USER_SCOPE else "shared"
    suffix = f" (budget {status.budget_id[:8]})" if include_budget_id else ""
    return f"{period} {scope} limit{suffix}"


def _personal_spend_line(status: BudgetLimitStatus) -> str | None:
    if status.scope_type != _PER_USER_SCOPE or status.threshold is None:
        return None
    spend = status.spend or Decimal(0)
    period = "today" if status.time_period == "DAY" else "this month"
    remaining = status.threshold - spend
    if remaining >= 0:
        balance = f"{format_usd(remaining)} remaining {period}"
    else:
        balance = f"{format_usd(-remaining)} over the limit {period}"
    return f"{format_usd(spend)} of {format_usd(status.threshold)} used · {balance}"


def _render_limit(status: BudgetLimitStatus, *, include_budget_id: bool = False) -> str:
    fraction = float(status.percent_consumed / Decimal(100))
    percent = f"{_format_percent(status.percent_consumed)}% used"
    lines = [
        f"{muted(format_meter(fraction))} {value(percent)}",
        label(_limit_label(status, include_budget_id=include_budget_id)),
    ]
    spend_line = _personal_spend_line(status)
    if spend_line is not None:
        lines.append(spend_line)
    return "\n".join(lines)


def render_budget_limits(statuses: list[BudgetLimitStatus]) -> str:
    """Render a category summary followed by any additional budget limits."""
    ordered = sorted(statuses, key=lambda status: status.percent_consumed, reverse=True)
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

    sections = [heading("Your AI Gateway spend limits")]
    sections.extend(_render_limit(status) for status in featured)
    if additional:
        sections.append(heading("All budgets"))
        sections.extend(_render_limit(status, include_budget_id=True) for status in additional)
    return "\n\n".join(sections)


def usage() -> int:
    state = load_state()
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("Workspace is not configured. Run `ucode configure` first.")

    profile = state.get("profile")
    apply_pat_environment(state)
    ensure_databricks_auth(workspace, profile)
    with spinner("Retrieving Databricks access token..."):
        token = get_databricks_token(workspace, profile)

    with spinner("Checking AI Gateway spend limits..."):
        budget_limits, reason = list_current_user_budget_spend_statuses(workspace, token)
    raise_for_invalid_access_token(workspace, reason)

    if not budget_limits:
        print_note(
            "Usage information is unavailable. Ask your account admin to configure an "
            "AI Gateway budget with a blocking limit for this workspace."
        )
        return 0

    console.print(render_budget_limits(budget_limits))
    return 0
