"""Tests for the AI Gateway spend-limit usage command."""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from rich.console import Console
from rich.text import Text

import ucode.usage as usage_mod
from ucode.budget import BudgetLimitStatus
from ucode.usage import render_budget_limits, render_budget_summary, usage

PER_USER = "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER"
SHARED = "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED"


class TestRenderBudgetSummary:
    def test_shows_original_spend_summary_and_meter(self):
        result = render_budget_summary((Decimal("110.70"), Decimal("5000")))

        assert "[bold]Budget spend:[/bold] [cyan]$110.70 of $5,000.00 (2%)[/cyan]" in result
        assert "[dim][█░░░░░░░░░░░░░░░░░░░░░░░░░░░░░][/dim]" in result
        assert "Usage Budget" not in result
        assert "warehouse" not in result.lower()
        assert "token" not in result.lower()

    def test_shows_zero_total_budget(self):
        result = render_budget_summary((Decimal("5"), Decimal("0")))

        assert "$5.00 of $0.00 (0%)" in result
        assert "[░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]" in result


class TestRenderBudgetLimits:
    @pytest.mark.parametrize(
        ("scope_type", "time_period", "expected_label"),
        [
            (PER_USER, "WEEK", "WEEK personal limit"),
            (
                "ALERT_CONFIGURATION_SCOPE_TYPE_PER_GROUP",
                "MONTH",
                "Monthly scope ALERT_CONFIGURATION_SCOPE_TYPE_PER_GROUP limit",
            ),
            (
                "ALERT_CONFIGURATION_SCOPE_TYPE_PER_GROUP",
                "WEEK",
                "WEEK scope ALERT_CONFIGURATION_SCOPE_TYPE_PER_GROUP limit",
            ),
        ],
    )
    def test_preserves_unknown_enums_with_generic_labels(
        self, scope_type, time_period, expected_label
    ):
        result = render_budget_limits(
            [
                BudgetLimitStatus(
                    "future",
                    scope_type,
                    time_period,
                    Decimal("85.25"),
                    Decimal("10"),
                    Decimal("20"),
                ),
                BudgetLimitStatus("known", SHARED, "DAY", Decimal("0")),
            ]
        )
        text = Text.from_markup(result).plain

        assert expected_label in text
        assert "85.25% used" in text
        assert "0% used" in text
        assert "Daily shared limit" in text
        assert "Monthly shared limit" not in text
        assert "Monthly personal limit" not in text
        if scope_type == PER_USER:
            assert "$10.00 of $20.00 used · $10.00 remaining for period WEEK" in text
            assert "today" not in text
            assert "this month" not in text
        else:
            assert "$" not in text

    def test_displays_unknown_enums_as_literal_text(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus(
                    "future", PER_USER, "[red]WEEK[/red]", None, threshold=Decimal("20")
                )
            ]
        )
        text = Text.from_markup(result)

        assert "[red]WEEK[/red] personal limit" in text.plain
        assert "$20.00 limit for period [red]WEEK[/red]" in text.plain
        assert all(span.style != "red" for span in text.spans)

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

        summary, additional_limits = result.split("[bold]Additional limits[/bold]")
        assert summary.startswith("\n[bold]Your AI Gateway spend limits[/bold]")
        assert "[yellow][██████████████████████████░░░░][/yellow] 87% used" in summary
        assert "[bold]Daily personal limit[/bold]" in summary
        assert "$43.50 of $50.00 used · $6.50 remaining today" in summary
        assert "83.73% used" in summary
        assert "$1,674.62 of $2,000.00 used · $325.38 remaining this month" in summary
        assert "82% used" in summary
        assert "[bold]Monthly shared limit[/bold]" in summary
        assert "budget" not in summary.lower()

        assert "[green][████████████░░░░░░░░░░░░░░░░░░][/green] 40% used" in additional_limits
        assert "Daily personal limit (budget other-da)" in additional_limits
        assert "$20.00 of $50.00 used · $30.00 remaining today" in additional_limits
        assert "Monthly shared limit (budget other-sh)" in additional_limits
        shared_block = additional_limits.split("Monthly shared limit (budget other-sh)", 1)[1]
        assert "$" not in shared_block
        assert "Showing " not in result

    def test_caps_display_at_five_while_preserving_each_category(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus("extra-98", PER_USER, "MONTH", Decimal("98")),
                BudgetLimitStatus("shared-5", SHARED, "DAY", Decimal("5")),
                BudgetLimitStatus("daily-79", PER_USER, "DAY", Decimal("79")),
                BudgetLimitStatus("shared60", SHARED, "MONTH", Decimal("60")),
                BudgetLimitStatus("extra-99", PER_USER, "MONTH", Decimal("99")),
                BudgetLimitStatus("monthly", PER_USER, "MONTH", Decimal("120")),
                BudgetLimitStatus("shared70", SHARED, "MONTH", Decimal("70")),
                BudgetLimitStatus("daily-80", PER_USER, "DAY", Decimal("80")),
                BudgetLimitStatus("shared10", SHARED, "DAY", Decimal("10")),
            ]
        )

        summary, additional_limits = result.split("[bold]Additional limits[/bold]")
        assert summary.count("% used") == 4
        assert "120% used" in summary
        assert "80% used" in summary
        assert "70% used" in summary
        assert "10% used" in summary
        assert "Monthly personal limit" in summary
        assert "Monthly shared limit" in summary
        assert "Daily personal limit" in summary
        assert "Daily shared limit" in summary
        assert additional_limits.count("% used") == 1
        assert "99% used" in additional_limits
        assert "Monthly personal limit (budget extra-99)" in additional_limits
        assert "98% used" not in result
        assert "[dim]Showing 5 of 9 spend limits.[/dim]" in result
        assert "All budgets" not in result
        assert "--all" not in result

    def test_fills_five_slots_from_a_single_category(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus(f"limit-{percent}", PER_USER, "MONTH", Decimal(percent))
                for percent in ("10", "20", "30", "40", "50", "60", "70")
            ]
        )

        summary, additional_limits = result.split("[bold]Additional limits[/bold]")
        assert "70% used" in summary
        assert result.count("% used") == 5
        for percent in ("60", "50", "40", "30"):
            assert f"{percent}% used" in additional_limits
        assert "20% used" not in result
        assert "10% used" not in result
        assert "Showing 5 of 7 spend limits." in result

    def test_preserves_api_order_for_equal_percentages_at_the_cutoff(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus(f"tie-{index}", SHARED, "MONTH", Decimal("50"))
                for index in range(6)
            ]
        )

        assert result.count("% used") == 5
        for index in range(1, 5):
            assert f"budget tie-{index}" in result
        assert "budget tie-5" not in result
        assert "Showing 5 of 6 spend limits." in result

    def test_preserves_a_category_without_percentages_within_the_cap(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus("shared", SHARED, "MONTH", None),
                *[
                    BudgetLimitStatus(f"limit-{percent}", PER_USER, "MONTH", Decimal(percent))
                    for percent in ("10", "20", "30", "40", "50", "60")
                ],
            ]
        )

        assert result.count("% used") == 4
        assert result.count("Usage percentage not reported") == 1
        assert "Monthly shared limit" in result
        assert "60% used" in result
        assert "20% used" not in result
        assert "10% used" not in result
        assert "Showing 5 of 7 spend limits." in result

    @pytest.mark.parametrize("scope_type", [PER_USER, SHARED])
    @pytest.mark.parametrize(
        ("percent", "color"),
        [
            ("0", "green"),
            ("17", "green"),
            ("79.99", "green"),
            ("79.999999999999999999", "green"),
            ("80", "yellow"),
            ("99.99", "yellow"),
            ("99.999999999999999999", "yellow"),
            ("100", "red"),
            ("125", "red"),
        ],
    )
    def test_matches_isaac_meter_colors_using_exact_percentages(self, scope_type, percent, color):
        result = render_budget_limits(
            [BudgetLimitStatus("budget", scope_type, "MONTH", Decimal(percent))]
        )
        text = Text.from_markup(result)
        console = Console()
        meter_start = text.plain.index("[")
        meter_style = text.get_style_at_offset(console, meter_start)
        percent_style = text.get_style_at_offset(console, text.plain.index(f"{percent}% used"))
        heading_style = text.get_style_at_offset(
            console, text.plain.index("Your AI Gateway spend limits")
        )

        assert meter_style.color is not None
        assert meter_style.color.name == color
        assert meter_style.dim is not True
        assert percent_style.color is None
        assert percent_style.dim is not True
        assert heading_style.bold is True
        assert heading_style.color is None

    def test_separates_summary_from_auth_output_without_color_codes(self):
        result = render_budget_limits(
            [BudgetLimitStatus("budget", PER_USER, "MONTH", Decimal("80"))]
        )
        stream = io.StringIO()
        console = Console(file=stream, color_system=None, force_terminal=False, width=160)

        console.print("Databricks auth already available")
        console.print(result)
        output = stream.getvalue()

        assert output.startswith(
            "Databricks auth already available\n\nYour AI Gateway spend limits\n"
        )
        assert "\x1b" not in output
        assert "[bold]" not in output
        assert "[yellow]" not in output
        assert "80% used" in output
        assert "Monthly personal limit" in output

    def test_omits_additional_limits_when_each_category_has_one_limit(self):
        result = render_budget_limits(
            [BudgetLimitStatus("only", PER_USER, "DAY", Decimal("10"), threshold=Decimal("50"))]
        )

        assert "Your AI Gateway spend limits" in result
        assert "Additional limits" not in result
        assert "Showing " not in result
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

        assert "[red][██████████████████████████████][/red] 125% used" in result
        assert "125% used" in result
        assert "$12.50 over the limit today" in result

    @pytest.mark.parametrize("scope_type", [PER_USER, SHARED])
    def test_shows_valid_limits_without_reported_percentages(self, scope_type):
        result = render_budget_limits(
            [BudgetLimitStatus("unknown", scope_type, "MONTH", None, threshold=Decimal("50"))]
        )

        assert "[dim]Usage percentage not reported[/dim]" in result
        scope_label = "personal" if scope_type == PER_USER else "shared"
        assert f"Monthly {scope_label} limit" in result
        assert "% used" not in result
        assert "█" not in result
        if scope_type == PER_USER:
            assert "$50.00 limit this month" in result
            assert "$0.00" not in result
        else:
            assert "$" not in result

    def test_preserves_limits_without_percentages_and_known_zero_usage(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus("unknown", PER_USER, "MONTH", None),
                BudgetLimitStatus("known", SHARED, "MONTH", None),
                BudgetLimitStatus("known", PER_USER, "MONTH", Decimal("0")),
            ]
        )

        summary, additional_limits = result.split("[bold]Additional limits[/bold]")
        assert "0% used" in summary
        assert "Monthly personal limit" in summary
        assert "Monthly shared limit" in summary
        assert "Usage percentage not reported" in summary
        assert "Usage percentage not reported" in additional_limits
        assert "Monthly personal limit (budget unknown)" in additional_limits

    def test_preserves_known_amounts_without_a_percentage(self):
        result = render_budget_limits(
            [BudgetLimitStatus("zero-limit", PER_USER, "DAY", None, Decimal("1"), Decimal("0"))]
        )

        assert "Usage percentage not reported" in result
        assert "$1.00 of $0.00 used · $1.00 over the limit today" in result
        assert "% used" not in result

    def test_preserves_spend_without_a_threshold_or_percentage(self):
        result = render_budget_limits(
            [BudgetLimitStatus("spend-only", PER_USER, "DAY", None, spend=Decimal("5"))]
        )

        assert "Usage percentage not reported" in result
        assert "$5.00 used today" in result
        assert "remaining" not in result
        assert "% used" not in result


class TestUsageCommand:
    @pytest.fixture(autouse=True)
    def enable_spend_api(self, monkeypatch):
        monkeypatch.setenv("ENABLE_UG_USAGE_SPEND_API", "1")
        monkeypatch.setattr(
            usage_mod,
            "resolve_current_budget_spend",
            lambda *_: pytest.fail("enabled spend API must not call recommendModel"),
        )

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
        unavailable = BudgetLimitStatus("unknown", SHARED, "MONTH", None)

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
                calls.append(("spend", (workspace, token))) or ([status, unavailable], None)
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
        assert "Usage percentage not reported" in printed[0]
        assert "Monthly shared limit" in printed[0]

    def test_reports_no_limits_after_a_successful_empty_lookup(self, monkeypatch):
        self._stub_auth(monkeypatch, {"workspace": "https://workspace"})
        notes: list[str] = []
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda workspace, token: ([], None),
        )
        monkeypatch.setattr(usage_mod, "print_note", notes.append)
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda *_: pytest.fail("should not print a summary")})(),
        )

        assert usage() == 0
        assert notes == ["No AI Gateway spend limits found for this workspace."]

    def test_shows_all_valid_limits_when_no_percentages_are_reported(self, monkeypatch):
        self._stub_auth(monkeypatch, {"workspace": "https://workspace"})
        printed: list[str] = []
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda workspace, token: (
                [
                    BudgetLimitStatus("personal", PER_USER, "DAY", None),
                    BudgetLimitStatus("shared", SHARED, "MONTH", None),
                ],
                None,
            ),
        )
        monkeypatch.setattr(
            usage_mod,
            "print_note",
            lambda *_: pytest.fail("valid limits must be displayed"),
        )
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda _, text: printed.append(text)})(),
        )

        assert usage() == 0
        assert len(printed) == 1
        assert "Daily personal limit" in printed[0]
        assert "Monthly shared limit" in printed[0]
        assert printed[0].count("Usage percentage not reported") == 2
        assert "% used" not in printed[0]

    @pytest.mark.parametrize(
        "reason",
        [
            "HTTP 404 Not Found: FEATURE_DISABLED: AI Gateway budget spend statuses are not enabled.",
            "HTTP 404 Not Found",
            "HTTP 403 Forbidden: permission denied",
            "HTTP 500 Internal Server Error",
            "network error: timed out",
            "budget spend status listing returned a malformed alert status",
        ],
    )
    def test_surfaces_lookup_failures_instead_of_reporting_no_limits(self, monkeypatch, reason):
        self._stub_auth(monkeypatch, {"workspace": "https://workspace"})
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda workspace, token: ([], reason),
        )
        monkeypatch.setattr(
            usage_mod,
            "print_note",
            lambda *_: pytest.fail("lookup failures must not look like absent budgets"),
        )
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda *_: pytest.fail("should not print a summary")})(),
        )

        with pytest.raises(RuntimeError) as exc_info:
            usage()
        assert str(exc_info.value) == f"Could not retrieve AI Gateway spend limits: {reason}"

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


class TestLegacyUsageCommand:
    @pytest.fixture(autouse=True)
    def use_legacy_usage(self, monkeypatch):
        monkeypatch.delenv("ENABLE_UG_USAGE_SPEND_API", raising=False)
        TestUsageCommand._stub_auth(
            monkeypatch, {"workspace": "https://workspace", "profile": "test-profile"}
        )
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda *_: pytest.fail("legacy usage must not call the spend-status endpoint"),
        )

    @pytest.mark.parametrize("flag", [None, "", "0", "true", "yes", "2", " 1 "])
    def test_uses_legacy_budget_summary_without_explicit_opt_in(self, monkeypatch, flag):
        if flag is not None:
            monkeypatch.setenv("ENABLE_UG_USAGE_SPEND_API", flag)
        calls: list[tuple[str, str]] = []
        printed: list[str] = []
        monkeypatch.setattr(
            usage_mod,
            "resolve_current_budget_spend",
            lambda workspace, token: (
                calls.append((workspace, token)) or (Decimal("12.34"), Decimal("100")),
                None,
            ),
        )
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda _, text: printed.append(text)})(),
        )

        assert usage() == 0
        assert calls == [("https://workspace", "workspace-token")]
        assert len(printed) == 1
        assert printed[0].startswith("[bold]Budget spend:[/bold]")
        assert "$12.34 of $100.00 (12%)" in printed[0]
        assert "Your AI Gateway spend limits" not in printed[0]

    @pytest.mark.parametrize(
        "reason", ["disabled", "HTTP 404 Not Found", "network error: timed out"]
    )
    def test_preserves_legacy_unavailable_budget_message(self, monkeypatch, reason):
        notes: list[str] = []
        monkeypatch.setattr(
            usage_mod,
            "resolve_current_budget_spend",
            lambda *_: (None, reason),
        )
        monkeypatch.setattr(usage_mod, "print_note", notes.append)
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda *_: pytest.fail("no budget summary expected")})(),
        )

        assert usage() == 0
        assert notes == [
            "Usage information is unavailable. Ask your workspace admin to configure "
            "Unity Gateway Budgets and add it to the workspace configuration."
        ]

    @pytest.mark.parametrize(
        "reason", ["HTTP 401 Unauthorized", "HTTP 403 Forbidden: Invalid access token."]
    )
    def test_preserves_legacy_invalid_token_errors(self, monkeypatch, reason):
        monkeypatch.setattr(
            usage_mod,
            "resolve_current_budget_spend",
            lambda *_: (None, reason),
        )
        monkeypatch.setattr(
            usage_mod,
            "print_note",
            lambda *_: pytest.fail("invalid credentials must not look like absent budget"),
        )

        with pytest.raises(RuntimeError, match="Your access token is expired or invalid"):
            usage()

    def test_requires_a_configured_workspace(self, monkeypatch):
        monkeypatch.setattr(usage_mod, "load_state", lambda: {})

        with pytest.raises(RuntimeError, match="Workspace is not configured"):
            usage()
