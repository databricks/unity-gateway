"""Tests for the AI Gateway spend-limit usage command."""

from __future__ import annotations

from decimal import Decimal

import pytest

import ucode.usage as usage_mod
from ucode.databricks import BudgetLimitStatus
from ucode.usage import render_budget_limits, usage

PER_USER = "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER"
SHARED = "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED"


class TestRenderBudgetLimits:
    def test_shows_category_summary_and_additional_budgets(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus(
                    "daily-main",
                    PER_USER,
                    "DAY",
                    Decimal("87"),
                    Decimal("43.50"),
                    Decimal("50"),
                ),
                BudgetLimitStatus(
                    "monthly-main",
                    PER_USER,
                    "MONTH",
                    Decimal("83.73"),
                    Decimal("1674.62"),
                    Decimal("2000"),
                ),
                BudgetLimitStatus("shared-main", SHARED, "MONTH", Decimal("82")),
                BudgetLimitStatus(
                    "other-da",
                    PER_USER,
                    "DAY",
                    Decimal("40"),
                    Decimal("20"),
                    Decimal("50"),
                ),
                BudgetLimitStatus("other-sh", SHARED, "MONTH", Decimal("20")),
            ]
        )

        summary, all_budgets = result.split("[bold blue]All budgets[/bold blue]")
        assert summary.startswith("[bold blue]Your AI Gateway spend limits[/bold blue]")
        assert "[██████████████████████████░░░░]" in summary
        assert "[cyan]87% used[/cyan]" in summary
        assert "[bold]Daily personal limit[/bold]" in summary
        assert "$43.50 of $50.00 used · $6.50 remaining today" in summary
        assert "[cyan]83.73% used[/cyan]" in summary
        assert "$1,674.62 of $2,000.00 used · $325.38 remaining this month" in summary
        assert "[cyan]82% used[/cyan]" in summary
        assert "[bold]Monthly shared limit[/bold]" in summary
        assert "budget" not in summary.lower()

        assert "[cyan]40% used[/cyan]" in all_budgets
        assert "Daily personal limit (budget other-da)" in all_budgets
        assert "$20.00 of $50.00 used · $30.00 remaining today" in all_budgets
        assert "Monthly shared limit (budget other-sh)" in all_budgets
        shared_block = all_budgets.split("Monthly shared limit (budget other-sh)", 1)[1]
        assert "$" not in shared_block

    def test_omits_all_budgets_when_each_category_has_one_limit(self):
        result = render_budget_limits(
            [BudgetLimitStatus("only", PER_USER, "DAY", Decimal("10"), threshold=Decimal("50"))]
        )

        assert "Your AI Gateway spend limits" in result
        assert "All budgets" not in result
        assert "$0.00 of $50.00 used · $50.00 remaining today" in result

    def test_shows_over_limit_amount_and_clamps_meter(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus(
                    "over",
                    PER_USER,
                    "DAY",
                    Decimal("125"),
                    Decimal("62.50"),
                    Decimal("50"),
                )
            ]
        )

        assert "[██████████████████████████████]" in result
        assert "125% used" in result
        assert "$12.50 over the limit today" in result


class TestUsageCommand:
    @staticmethod
    def _stub_auth(monkeypatch, state):
        monkeypatch.setattr(usage_mod, "load_state", lambda: state)
        monkeypatch.setattr(usage_mod, "apply_pat_environment", lambda value: None)
        monkeypatch.setattr(usage_mod, "ensure_databricks_auth", lambda *args: None)
        monkeypatch.setattr(usage_mod, "get_databricks_token", lambda *args: "workspace-token")

    def test_fetches_and_prints_budget_limits(self, monkeypatch):
        state = {"workspace": "https://workspace", "profile": "test-profile"}
        calls: list[tuple[str, object]] = []
        printed: list[str] = []
        status = BudgetLimitStatus(
            "budget-1",
            PER_USER,
            "DAY",
            Decimal("12.34"),
            Decimal("12.34"),
            Decimal("100"),
        )

        monkeypatch.setattr(usage_mod, "load_state", lambda: state)
        monkeypatch.setattr(
            usage_mod, "apply_pat_environment", lambda value: calls.append(("pat", value))
        )
        monkeypatch.setattr(
            usage_mod,
            "ensure_databricks_auth",
            lambda workspace, profile: calls.append(("auth", (workspace, profile))),
        )
        monkeypatch.setattr(
            usage_mod,
            "get_databricks_token",
            lambda workspace, profile: (
                calls.append(("token", (workspace, profile))) or "workspace-token"
            ),
        )
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda workspace, token: (
                calls.append(("spend", (workspace, token))) or ([status], None)
            ),
        )
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda _, text: printed.append(text)})(),
        )

        assert usage() == 0
        assert calls == [
            ("pat", state),
            ("auth", ("https://workspace", "test-profile")),
            ("token", ("https://workspace", "test-profile")),
            ("spend", ("https://workspace", "workspace-token")),
        ]
        assert len(printed) == 1
        assert "$12.34 of $100.00 used" in printed[0]

    def test_reports_unavailable_budget(self, monkeypatch):
        self._stub_auth(monkeypatch, {"workspace": "https://workspace"})
        notes: list[str] = []
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda workspace, token: ([], "disabled"),
        )
        monkeypatch.setattr(usage_mod, "print_note", notes.append)
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda *_: pytest.fail("should not print a summary")})(),
        )

        assert usage() == 0
        assert notes == [
            "Usage information is unavailable. Ask your account admin to configure an "
            "AI Gateway budget with a blocking limit for this workspace."
        ]

    @pytest.mark.parametrize(
        "reason",
        ["HTTP 401 Unauthorized", "HTTP 403 Forbidden: Invalid access token."],
    )
    def test_rejects_invalid_access_token(self, monkeypatch, reason):
        self._stub_auth(monkeypatch, {"workspace": "https://workspace"})
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda workspace, token: ([], reason),
        )
        monkeypatch.setattr(
            usage_mod,
            "print_note",
            lambda *_: pytest.fail("invalid credentials must not look like absent budget"),
        )

        with pytest.raises(RuntimeError) as exc_info:
            usage()
        assert str(exc_info.value) == "Your access token is expired or invalid."

    def test_requires_a_configured_workspace(self, monkeypatch):
        monkeypatch.setattr(usage_mod, "load_state", lambda: {})

        with pytest.raises(RuntimeError, match="Workspace is not configured"):
            usage()
