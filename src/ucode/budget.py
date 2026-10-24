"""Workspace-authenticated AI Gateway budget spend-status client."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

from ucode import databricks
from ucode.ui import spinner

_BUDGET_SPEND_STATUS_API_PATH = "/api/ai-gateway/v2/budget-spend-statuses"
_BUDGET_SPEND_STATUS_PAGE_SIZE = 100


@dataclass(frozen=True)
class BudgetLimitStatus:
    """The caller's live spend against one AI Gateway budget threshold."""

    budget_id: str
    scope_type: str
    time_period: str
    percent_consumed: Decimal | None
    spend: Decimal | None = None
    threshold: Decimal | None = None


def _optional_nonnegative_decimal(entry: dict[str, object], key: str) -> Decimal | None:
    value = entry.get(key)
    if value is None:
        return None
    parsed = databricks._parse_decimal(value)
    if parsed is None or not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{key} must be a finite, nonnegative decimal")
    return parsed


def _budget_limit_status(entry: object, budget_id: str) -> BudgetLimitStatus | None:
    if not isinstance(entry, dict):
        return None
    scope_type = entry.get("scope_type")
    time_period = entry.get("time_period")
    if (
        not isinstance(scope_type, str)
        or not scope_type.strip()
        or not isinstance(time_period, str)
        or not time_period.strip()
    ):
        return None

    try:
        percent_consumed = _optional_nonnegative_decimal(entry, "percent_consumed")
        spend = _optional_nonnegative_decimal(entry, "spend")
        threshold = _optional_nonnegative_decimal(entry, "threshold")
    except ValueError:
        return None
    return BudgetLimitStatus(
        budget_id=budget_id,
        scope_type=scope_type,
        time_period=time_period,
        percent_consumed=percent_consumed,
        spend=spend,
        threshold=threshold,
    )


def list_current_user_budget_spend_statuses(
    workspace: str,
    workspace_token: str,
    *,
    timeout: int = 10,
) -> tuple[list[BudgetLimitStatus], str | None]:
    """List the caller's spend against blocking AI Gateway budget thresholds."""
    base_url = databricks.workspace_origin(workspace) + _BUDGET_SPEND_STATUS_API_PATH
    statuses: list[BudgetLimitStatus] = []
    page_token: str | None = None
    seen_page_tokens: set[str] = set()
    while True:
        params: dict[str, int | str] = {"page_size": _BUDGET_SPEND_STATUS_PAGE_SIZE}
        if page_token is not None:
            params["page_token"] = page_token
        with spinner("Checking Unity Gateway spend limits..."):
            payload, reason = databricks._http_get_json(
                f"{base_url}?{urlencode(params)}", workspace_token, timeout=timeout
            )
        if reason is not None:
            return [], reason
        if not isinstance(payload, dict):
            return [], "budget spend status listing returned an unexpected response shape"
        raw_budgets = payload.get("budget_spend_statuses", [])
        if not isinstance(raw_budgets, list):
            return [], "budget spend status listing returned no status collection"

        for raw_budget in raw_budgets:
            if not isinstance(raw_budget, dict):
                return [], "budget spend status listing returned a malformed budget status"
            name = raw_budget.get("name")
            budget_id = name.rsplit("/", 1)[-1] if isinstance(name, str) else ""
            raw_statuses = raw_budget.get("alert_spend_statuses")
            if not budget_id or not isinstance(raw_statuses, list):
                return [], "budget spend status listing returned a malformed budget status"
            for raw_status in raw_statuses:
                status = _budget_limit_status(raw_status, budget_id)
                if status is None:
                    return [], "budget spend status listing returned a malformed alert status"
                statuses.append(status)

        next_page_token = payload.get("next_page_token")
        if next_page_token in (None, ""):
            return statuses, None
        if not isinstance(next_page_token, str):
            return [], "budget spend status listing returned an invalid page token"
        if next_page_token in seen_page_tokens:
            return [], "budget spend status listing repeated a page token"
        seen_page_tokens.add(next_page_token)
        page_token = next_page_token
