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

    def test_shows_limits_in_one_list(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus(
                    "a1d38e6f-86cd-4da9-b6f7-bde5be230412",
                    PER_USER,
                    "DAY",
                    Decimal("87"),
                    Decimal("43.50"),
                    Decimal("50"),
                ),
                BudgetLimitStatus(
                    "c59b4827-208a-4b5e-96c3-78e83909ce41",
                    PER_USER,
                    "MONTH",
                    Decimal("83.73"),
                    Decimal("1674.62"),
                    Decimal("2000"),
                ),
                BudgetLimitStatus(
                    "f8c260e1-90b3-4a74-a56d-2598c0fcb6e5", SHARED, "MONTH", Decimal("82")
                ),
                BudgetLimitStatus(
                    "b73a5c28-3e91-4a62-9f54-dae1f2348b67",
                    PER_USER,
                    "DAY",
                    Decimal("40"),
                    Decimal("20"),
                    Decimal("50"),
                ),
                BudgetLimitStatus(
                    "e42f8d19-6c73-4b05-a128-9f3d7c6e2b41", SHARED, "MONTH", Decimal("20")
                ),
            ]
        )

        assert result.startswith(
            "\n[bold][underline]Your Unity Gateway spend limits[/underline][/bold]"
        )
        assert "Additional limits" not in result
        heading, daily, monthly, shared, other_daily, other_shared = result.strip().split("\n\n")
        assert heading == "[bold][underline]Your Unity Gateway spend limits[/underline][/bold]"
        assert "[yellow][██████████████████████████░░░░][/yellow] 87% used" in daily
        assert "[bold]Daily personal limit (budget a1d38e6f)[/bold]" in daily
        assert "$43.50 of $50.00 used · $6.50 remaining today" in daily
        assert "83.73% used" in monthly
        assert "[bold]Monthly personal limit (budget c59b4827)[/bold]" in monthly
        assert "$1,674.62 of $2,000.00 used · $325.38 remaining this month" in monthly
        assert "82% used" in shared
        assert "[bold]Monthly shared limit (budget f8c260e1)[/bold]" in shared
        assert "a1d38e6f-86cd-4da9-b6f7-bde5be230412" not in result
        assert "c59b4827-208a-4b5e-96c3-78e83909ce41" not in result
        assert "f8c260e1-90b3-4a74-a56d-2598c0fcb6e5" not in result
        assert "$" not in shared

        assert "[green][████████████░░░░░░░░░░░░░░░░░░][/green] 40% used" in other_daily
        assert "Daily personal limit (budget b73a5c28)" in other_daily
        assert "b73a5c28-3e91-4a62-9f54-dae1f2348b67" not in result
        assert "$20.00 of $50.00 used · $30.00 remaining today" in other_daily
        assert "20% used" in other_shared
        assert "Monthly shared limit (budget e42f8d19)" in other_shared
        assert "e42f8d19-6c73-4b05-a128-9f3d7c6e2b41" not in result
        assert "$" not in other_shared
        assert "Showing " not in result

    def test_shows_the_five_highest_percentages_without_category_priority(self):
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

        limits = result.strip().split("\n\n")[1:-1]
        assert len(limits) == 5
        for limit, percent in zip(limits, ("120", "99", "98", "80", "79"), strict=True):
            assert f"{percent}% used" in limit
        for limit, budget_id in zip(
            limits, ("monthly", "extra-99", "extra-98", "daily-80", "daily-79"), strict=True
        ):
            assert f"(budget {budget_id})" in limit
        assert "shared limit" not in result
        assert (
            "[dim]Showing 5 of 9 spend limits. Run ug usage --all to see all limits.[/dim]"
            in result
        )

    def test_all_sorts_every_limit_by_percentage_without_category_priority(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus("monthly", PER_USER, "MONTH", Decimal("90")),
                BudgetLimitStatus("first", PER_USER, "MONTH", Decimal("80")),
                BudgetLimitStatus("second", PER_USER, "MONTH", Decimal("80")),
                BudgetLimitStatus("lower", PER_USER, "MONTH", Decimal("20")),
                BudgetLimitStatus("lowest", PER_USER, "MONTH", Decimal("10")),
                BudgetLimitStatus("unknown", PER_USER, "MONTH", None),
                BudgetLimitStatus("shared", SHARED, "DAY", None),
            ],
            show_all=True,
        )

        assert "Additional limits" not in result
        assert result.index("budget monthly") < result.index("budget first")
        assert result.index("budget first") < result.index("budget second")
        assert result.index("budget second") < result.index("budget lower")
        assert result.index("budget lower") < result.index("budget lowest")
        assert result.index("budget lowest") < result.index("budget unknown")
        assert result.index("budget unknown") < result.index("budget shared")
        assert len(result.strip().split("\n\n")) == 8
        assert result.count("% used") == 5
        assert result.count("Usage percentage not reported") == 2
        assert "Showing " not in result
        assert "--all" not in result

    def test_all_does_not_cap_the_number_of_categories(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus(f"budget-{index}", PER_USER, f"PERIOD_{index}", Decimal(index))
                for index in range(6)
            ],
            show_all=True,
        )

        for index in range(6):
            assert f"PERIOD_{index} personal limit" in result
        assert result.count("% used") == 6
        assert "Showing " not in result

    def test_preserves_api_order_for_equal_percentages_at_the_cutoff(self):
        result = render_budget_limits(
            [
                BudgetLimitStatus(
                    f"tie-{index}",
                    PER_USER if index % 2 else SHARED,
                    "DAY" if index < 3 else "MONTH",
                    Decimal("50"),
                )
                for index in range(6)
            ]
        )

        assert result.count("% used") == 5
        for index in range(5):
            assert f"budget tie-{index}" in result
        for index in range(4):
            assert result.index(f"budget tie-{index}") < result.index(f"budget tie-{index + 1}")
        assert "budget tie-5" not in result
        assert "Showing 5 of 6 spend limits." in result

    @pytest.mark.parametrize("show_all", [False, True])
    def test_sorts_missing_percentages_after_reported_percentages(self, show_all):
        result = render_budget_limits(
            [
                BudgetLimitStatus("shared", SHARED, "MONTH", None),
                *[
                    BudgetLimitStatus(f"limit-{percent}", PER_USER, "MONTH", Decimal(percent))
                    for percent in ("10", "20", "30", "40", "50", "60")
                ],
            ],
            show_all=show_all,
        )

        limits = result.strip().split("\n\n")[1:]
        if show_all:
            assert len(limits) == 7
            assert "10% used" in limits[-2]
            assert "Usage percentage not reported" in limits[-1]
            assert "Monthly shared limit (budget shared)" in limits[-1]
            assert "Showing " not in result
        else:
            assert result.count("% used") == 5
            assert "Usage percentage not reported" not in result
            assert "Monthly shared limit" not in result
            assert "10% used" not in result
            assert "Showing 5 of 7 spend limits." in limits.pop()
        for limit, percent in zip(limits[:5], ("60", "50", "40", "30", "20"), strict=True):
            assert f"{percent}% used" in limit

    @pytest.mark.parametrize(
        ("percent", "color"),
        [
            ("0", "green"),
            ("79.999999999999999999", "green"),
            ("80", "yellow"),
            ("99.999999999999999999", "yellow"),
            ("100", "red"),
            ("125", "red"),
        ],
    )
    def test_meter_colors_follow_exact_80_and_100_percent_thresholds(self, percent, color):
        result = render_budget_limits(
            [BudgetLimitStatus("budget", PER_USER, "MONTH", Decimal(percent))]
        )
        text = Text.from_markup(result)
        console = Console()
        meter_start = text.plain.index("[")
        meter_style = text.get_style_at_offset(console, meter_start)
        percent_style = text.get_style_at_offset(console, text.plain.index(f"{percent}% used"))
        heading_style = text.get_style_at_offset(
            console, text.plain.index("Your Unity Gateway spend limits")
        )

        assert meter_style.color is not None
        assert meter_style.color.name == color
        assert meter_style.dim is not True
        assert percent_style.color is None
        assert percent_style.dim is not True
        assert heading_style.bold is True
        assert heading_style.underline is True
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
            "Databricks auth already available\n\nYour Unity Gateway spend limits\n"
        )
        assert "\x1b" not in output
        assert "[bold]" not in output
        assert "[underline]" not in output
        assert "[yellow]" not in output
        assert "80% used" in output
        assert "Monthly personal limit" in output

    def test_shows_a_single_limit_without_a_truncation_note(self):
        result = render_budget_limits(
            [BudgetLimitStatus("only", PER_USER, "DAY", Decimal("10"), Decimal("5"), Decimal("50"))]
        )

        assert "Your Unity Gateway spend limits" in result
        assert "Additional limits" not in result
        assert "Showing " not in result
        assert "Daily personal limit (budget only)" in result
        assert "10% used" in result
        assert "$5.00 of $50.00 used · $45.00 remaining today" in result

    @pytest.mark.parametrize("percent", [None, Decimal("0"), Decimal("90")])
    def test_shows_only_limit_when_personal_spend_is_not_reported(self, percent):
        result = render_budget_limits(
            [BudgetLimitStatus("only", PER_USER, "DAY", percent, threshold=Decimal("50"))]
        )

        assert "$50.00 limit today" in result
        assert "$0.00" not in result
        assert "of $50.00 used" not in result
        assert "remaining" not in result
        if percent is not None:
            assert f"{percent}% used" in result

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
    @pytest.mark.parametrize("threshold", [Decimal("0"), Decimal("50")])
    def test_shows_valid_limits_without_reported_percentages(self, scope_type, threshold):
        result = render_budget_limits(
            [BudgetLimitStatus("unknown", scope_type, "MONTH", None, threshold=threshold)]
        )

        assert "[dim]Usage percentage not reported[/dim]" in result
        scope_label = "personal" if scope_type == PER_USER else "shared"
        assert f"Monthly {scope_label} limit" in result
        assert "% used" not in result
        assert "█" not in result
        if scope_type == PER_USER:
            assert f"${threshold:.2f} limit this month" in result
            assert "used ·" not in result
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

        assert "Additional limits" not in result
        assert "0% used" in result
        assert "Monthly personal limit" in result
        assert "Monthly shared limit" in result
        assert result.count("Usage percentage not reported") == 2
        assert "Monthly personal limit (budget unknown)" in result
        assert result.index("0% used") < result.index("budget unknown")
        assert result.index("budget unknown") < result.index("Monthly shared limit")

    @pytest.mark.parametrize(
        ("spend", "expected_amounts"),
        [
            ("0", "$0.00 of $0.00 used · $0.00 remaining today"),
            ("1", "$1.00 of $0.00 used · $1.00 over the limit today"),
        ],
    )
    def test_shows_zero_threshold_as_fully_used(self, spend, expected_amounts):
        result = render_budget_limits(
            [BudgetLimitStatus("zero-limit", PER_USER, "DAY", None, Decimal(spend), Decimal("0"))]
        )

        assert "[red][██████████████████████████████][/red] 100% used" in result
        assert expected_amounts in result
        assert "Usage percentage not reported" not in result

    @pytest.mark.parametrize("show_all", [False, True])
    def test_ranks_zero_threshold_with_reported_spend_as_fully_used(self, show_all):
        result = render_budget_limits(
            [
                *[
                    BudgetLimitStatus(f"limit-{percent}", PER_USER, "MONTH", Decimal(percent))
                    for percent in ("50", "60", "70", "80", "90")
                ],
                BudgetLimitStatus("zero-limit", PER_USER, "DAY", None, Decimal("0"), Decimal("0")),
            ],
            show_all=show_all,
        )

        limits = result.strip().split("\n\n")[1:]
        assert "100% used" in limits[0]
        assert "Daily personal limit (budget zero-lim)" in limits[0]
        for limit, percent in zip(limits[1:5], ("90", "80", "70", "60"), strict=True):
            assert f"{percent}% used" in limit
        if show_all:
            assert "50% used" in limits[-1]
            assert "Showing " not in result
        else:
            assert "50% used" not in result
            assert "Showing 5 of 6 spend limits." in limits[-1]

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
        assert notes == ["No Unity Gateway spend limits found for this workspace."]

    def test_all_displays_every_returned_limit(self, monkeypatch):
        self._stub_auth(monkeypatch, {"workspace": "https://workspace"})
        printed: list[str] = []
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda workspace, token: (
                [
                    BudgetLimitStatus(f"budget-{index}", PER_USER, "MONTH", Decimal(index))
                    for index in range(8)
                ],
                None,
            ),
        )
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda _, text: printed.append(text)})(),
        )

        assert usage(show_all=True) == 0
        assert len(printed) == 1
        assert printed[0].count("% used") == 8
        assert "Showing " not in printed[0]

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
            'HTTP 400 Bad Request: {"error_code": "FEATURE_DISABLED"}',
        ],
    )
    def test_reports_expanded_view_unavailable_when_endpoint_is_missing_or_disabled(
        self, monkeypatch, reason
    ):
        self._stub_auth(monkeypatch, {"workspace": "https://workspace"})
        notes: list[str] = []
        monkeypatch.setattr(
            usage_mod,
            "list_current_user_budget_spend_statuses",
            lambda workspace, token: ([], reason),
        )
        monkeypatch.setattr(usage_mod, "print_note", notes.append)
        monkeypatch.setattr(
            usage_mod,
            "console",
            type("Console", (), {"print": lambda *_: pytest.fail("should not print a summary")})(),
        )

        assert usage() == 0
        assert notes == [
            "Expanded budget usage is not available for this workspace. "
            "Unset ENABLE_UG_USAGE_SPEND_API to use the standard summary."
        ]

    @pytest.mark.parametrize(
        "reason",
        [
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
        assert str(exc_info.value) == f"Could not retrieve Unity Gateway spend limits: {reason}"

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

    @pytest.mark.parametrize("flag", [None, "0", " 1 "])
    def test_all_requires_the_expanded_view_opt_in(self, monkeypatch, flag):
        if flag is not None:
            monkeypatch.setenv("ENABLE_UG_USAGE_SPEND_API", flag)
        monkeypatch.setattr(
            usage_mod,
            "resolve_current_budget_spend",
            lambda *_: pytest.fail("--all must not return the standard summary"),
        )
        monkeypatch.setattr(
            usage_mod,
            "ensure_databricks_auth",
            lambda *_: pytest.fail("invalid options must be rejected before authentication"),
        )

        with pytest.raises(RuntimeError, match="Set ENABLE_UG_USAGE_SPEND_API=1"):
            usage(show_all=True)

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
        assert "Your Unity Gateway spend limits" not in printed[0]

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
