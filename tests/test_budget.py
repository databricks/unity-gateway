"""Tests for the workspace-authenticated AI Gateway budget spend-status client."""

from __future__ import annotations

import os
from decimal import Decimal
from urllib.parse import parse_qs

import pytest

import ucode.databricks as db_mod
from ucode.budget import BudgetLimitStatus, list_current_user_budget_spend_statuses

WS = "https://example.databricks.com"
ACCOUNT_ID = "11d77e21-5e05-4196-af72-423257f74974"
BUDGET_ID = "c6563b45-df9a-4b19-afb2-d42dc2b52576"
SECOND_BUDGET_ID = "8fe84d4d-ed51-4a44-8ec3-b688717366c6"
BUDGET_NAME = f"accounts/{ACCOUNT_ID}/budget-spend-statuses/{BUDGET_ID}"
SECOND_BUDGET_NAME = f"accounts/{ACCOUNT_ID}/budget-spend-statuses/{SECOND_BUDGET_ID}"


class TestListCurrentUserBudgetSpendStatuses:
    @pytest.fixture(autouse=True)
    def reject_auth_subprocesses(self, monkeypatch):
        def unexpected_run(*args, **kwargs):
            pytest.fail("Spend lookup must use existing workspace credentials without CLI auth")

        monkeypatch.setattr(db_mod, "run", unexpected_run)

    def test_uses_workspace_credentials_and_parses_personal_and_shared_limits(self, monkeypatch):
        calls = []

        def fake_get(url, token, timeout=10):
            calls.append((url, token))
            return {
                "budget_spend_statuses": [
                    {
                        "name": BUDGET_NAME,
                        "alert_spend_statuses": [
                            {
                                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                                "time_period": "DAY",
                                "spend": "43.50",
                                "threshold": "50",
                                "percent_consumed": "87",
                            },
                            {
                                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
                                "time_period": "MONTH",
                                "percent_consumed": "82.25",
                            },
                        ],
                    }
                ]
            }, None

        monkeypatch.setattr(db_mod, "_http_get_json", fake_get)

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert reason is None
        assert statuses == [
            BudgetLimitStatus(
                budget_id=BUDGET_ID,
                scope_type="ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                time_period="DAY",
                percent_consumed=Decimal("87"),
                spend=Decimal("43.50"),
                threshold=Decimal("50"),
            ),
            BudgetLimitStatus(
                budget_id=BUDGET_ID,
                scope_type="ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
                time_period="MONTH",
                percent_consumed=Decimal("82.25"),
            ),
        ]
        assert len(calls) == 1
        request_url, request_token = calls[0]
        assert request_url.startswith(f"{WS}/api/ai-gateway/v2/budget-spend-statuses?")
        params = parse_qs(request_url.split("?", 1)[1])
        assert params == {"page_size": ["100"]}
        assert request_token == "workspace-token"

    @pytest.mark.parametrize("deprecated_budget_id", [None, SECOND_BUDGET_ID])
    def test_extracts_the_id_from_name_and_ignores_deprecated_budget_id(
        self, monkeypatch, deprecated_budget_id
    ):
        payload = {
            "budget_spend_statuses": [
                {
                    "name": BUDGET_NAME,
                    "budget_id": deprecated_budget_id,
                    "alert_spend_statuses": [
                        {
                            "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
                            "time_period": "DAY",
                            "percent_consumed": "85.25",
                        }
                    ],
                }
            ]
        }
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert reason is None
        assert statuses == [
            BudgetLimitStatus(
                BUDGET_ID, "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED", "DAY", Decimal("85.25")
            )
        ]

    @pytest.mark.parametrize(
        "name", [None, "", 42, [], {}, f"accounts/{ACCOUNT_ID}/budget-spend-statuses/"]
    )
    def test_rejects_invalid_names_or_empty_budget_ids(self, monkeypatch, name):
        payload = {
            "budget_spend_statuses": [
                {"name": name, "budget_id": BUDGET_ID, "alert_spend_statuses": []}
            ]
        }
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason == "budget spend status listing returned a malformed budget status"

    @pytest.mark.parametrize("last_token", [None, ""])
    def test_pages_through_empty_pages_until_the_token_is_absent_or_empty(
        self, monkeypatch, last_token
    ):
        pages = iter(
            [
                {
                    "budget_spend_statuses": [],
                    "next_page_token": "page-2+/=",
                },
                {
                    "budget_spend_statuses": [
                        {
                            "name": SECOND_BUDGET_NAME,
                            "alert_spend_statuses": [
                                {
                                    "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                                    "time_period": "MONTH",
                                    "percent_consumed": "0",
                                    "threshold": "100",
                                }
                            ],
                        }
                    ],
                    "next_page_token": last_token,
                },
            ]
        )
        urls = []

        def fake_get(url, token, timeout=10):
            assert token == "workspace-token"
            urls.append(url)
            return next(pages), None

        monkeypatch.setattr(db_mod, "_http_get_json", fake_get)

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert reason is None
        assert [status.budget_id for status in statuses] == [SECOND_BUDGET_ID]
        assert len(urls) == 2
        assert parse_qs(urls[1].split("?", 1)[1]) == {
            "page_size": ["100"],
            "page_token": ["page-2+/="],
        }

    def test_rejects_a_repeated_page_token(self, monkeypatch):
        def fake_get(url, token, timeout=10):
            return {"budget_spend_statuses": [], "next_page_token": "same"}, None

        monkeypatch.setattr(db_mod, "_http_get_json", fake_get)

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason == "budget spend status listing repeated a page token"

    def test_accepts_an_omitted_empty_status_collection(self, monkeypatch):
        def fake_get(url, token, timeout=10):
            return {}, None

        monkeypatch.setattr(db_mod, "_http_get_json", fake_get)

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason is None

    @pytest.mark.parametrize(
        ("scope_type", "amounts", "spend", "threshold"),
        [
            ("ALERT_CONFIGURATION_SCOPE_TYPE_SHARED", {}, None, None),
            ("ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER", {"threshold": "50"}, None, Decimal("50")),
            (
                "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                {"spend": "1", "threshold": "0"},
                Decimal("1"),
                Decimal("0"),
            ),
        ],
    )
    def test_accepts_an_omitted_percentage(
        self, monkeypatch, scope_type, amounts, spend, threshold
    ):
        payload = {
            "budget_spend_statuses": [
                {
                    "name": BUDGET_NAME,
                    "alert_spend_statuses": [
                        {"scope_type": scope_type, "time_period": "MONTH", **amounts}
                    ],
                }
            ]
        }
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert reason is None
        assert statuses == [
            BudgetLimitStatus(BUDGET_ID, scope_type, "MONTH", None, spend, threshold)
        ]

    @pytest.mark.parametrize(
        "null_fields",
        [
            ("percent_consumed",),
            ("spend",),
            ("threshold",),
            ("percent_consumed", "spend", "threshold"),
        ],
    )
    def test_accepts_null_optional_fields_like_missing_fields(self, monkeypatch, null_fields):
        complete = {
            "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
            "time_period": "MONTH",
            "percent_consumed": "85.25",
            "spend": "43.50",
            "threshold": "50",
        }
        missing = {key: value for key, value in complete.items() if key not in null_fields}
        explicit_nulls = {**complete, **dict.fromkeys(null_fields)}
        payload = {
            "budget_spend_statuses": [
                {
                    "name": BUDGET_NAME,
                    "alert_spend_statuses": [complete, missing, explicit_nulls],
                }
            ]
        }
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        expected_optional = {
            key: None if key in null_fields else Decimal(complete[key])
            for key in ("percent_consumed", "spend", "threshold")
        }
        expected = BudgetLimitStatus(
            BUDGET_ID,
            "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
            "MONTH",
            **expected_optional,
        )
        assert reason is None
        assert statuses == [
            BudgetLimitStatus(
                BUDGET_ID,
                "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                "MONTH",
                Decimal("85.25"),
                Decimal("43.50"),
                Decimal("50"),
            ),
            expected,
            expected,
        ]

    @pytest.mark.parametrize("include_known", [False, True])
    @pytest.mark.parametrize(
        ("scope_type", "time_period"),
        [
            ("ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER", "WEEK"),
            ("ALERT_CONFIGURATION_SCOPE_TYPE_PER_GROUP", "MONTH"),
            ("ALERT_CONFIGURATION_SCOPE_TYPE_PER_GROUP", "WEEK"),
        ],
    )
    def test_preserves_unknown_enums_without_losing_known_statuses(
        self, monkeypatch, include_known, scope_type, time_period
    ):
        known_status = {
            "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
            "time_period": "DAY",
            "percent_consumed": "0",
        }
        future_status = {
            "scope_type": scope_type,
            "time_period": time_period,
            "percent_consumed": "85.25",
        }
        payload = {
            "budget_spend_statuses": [
                {
                    "name": BUDGET_NAME,
                    "alert_spend_statuses": (
                        [known_status, future_status] if include_known else [future_status]
                    ),
                }
            ]
        }
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert reason is None
        expected = [BudgetLimitStatus(BUDGET_ID, scope_type, time_period, Decimal("85.25"))]
        if include_known:
            expected.insert(
                0,
                BudgetLimitStatus(
                    BUDGET_ID, "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED", "DAY", Decimal("0")
                ),
            )
        assert statuses == expected

    @pytest.mark.parametrize("field", ["scope_type", "time_period"])
    @pytest.mark.parametrize("invalid_value", ["", " ", None, 1, [], {}])
    def test_rejects_missing_or_malformed_enum_values(self, monkeypatch, field, invalid_value):
        alert_status = {
            "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
            "time_period": "DAY",
            "percent_consumed": "1",
            field: invalid_value,
        }
        payload = {
            "budget_spend_statuses": [{"name": BUDGET_NAME, "alert_spend_statuses": [alert_status]}]
        }
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason == "budget spend status listing returned a malformed alert status"

    @pytest.mark.parametrize("field", ["percent_consumed", "spend", "threshold"])
    @pytest.mark.parametrize(
        "invalid_value", ["-1", "-0.01", "NaN", "Infinity", "-Infinity", "invalid"]
    )
    def test_rejects_invalid_decimals_even_alongside_valid_statuses(
        self, monkeypatch, field, invalid_value
    ):
        valid_status = {
            "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
            "time_period": "DAY",
            "percent_consumed": "0",
        }
        payload = {
            "budget_spend_statuses": [
                {
                    "name": BUDGET_NAME,
                    "alert_spend_statuses": [
                        valid_status,
                        {**valid_status, field: invalid_value},
                    ],
                }
            ]
        }
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason == "budget spend status listing returned a malformed alert status"

    @pytest.mark.parametrize("amount", ["0", "85.25"])
    def test_accepts_nonnegative_decimal_strings_for_all_amounts(self, monkeypatch, amount):
        payload = {
            "budget_spend_statuses": [
                {
                    "name": BUDGET_NAME,
                    "alert_spend_statuses": [
                        {
                            "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                            "time_period": "DAY",
                            "percent_consumed": amount,
                            "spend": amount,
                            "threshold": amount,
                        }
                    ],
                }
            ]
        }
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert reason is None
        assert statuses == [
            BudgetLimitStatus(
                BUDGET_ID,
                "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                "DAY",
                Decimal(amount),
                Decimal(amount),
                Decimal(amount),
            )
        ]

    @pytest.mark.parametrize(
        "alert_status",
        [
            {"time_period": "DAY", "percent_consumed": "1"},
            {
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                "percent_consumed": "1",
            },
            {
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                "time_period": "DAY",
                "percent_consumed": "invalid",
            },
            {
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                "time_period": "DAY",
                "percent_consumed": "NaN",
            },
            {
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                "time_period": "DAY",
                "percent_consumed": "1",
                "spend": "invalid",
            },
        ],
    )
    def test_rejects_malformed_alert_statuses(self, monkeypatch, alert_status):
        def fake_get(url, token, timeout=10):
            return {
                "budget_spend_statuses": [
                    {"name": BUDGET_NAME, "alert_spend_statuses": [alert_status]}
                ]
            }, None

        monkeypatch.setattr(db_mod, "_http_get_json", fake_get)

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason == "budget spend status listing returned a malformed alert status"

    @pytest.mark.parametrize("workspace", [WS, f"{WS}/", f"{WS}/?o=123"])
    def test_uses_workspace_origin_and_preserves_configured_auth(self, monkeypatch, workspace):
        monkeypatch.setenv("DATABRICKS_HOST", "https://another-workspace")
        monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "workspace-profile")
        monkeypatch.setenv("DATABRICKS_TOKEN", "existing-pat")
        calls = []

        def fake_get(url, token, timeout=10):
            calls.append((url, token, timeout))
            return {}, None

        monkeypatch.setattr(db_mod, "_http_get_json", fake_get)

        statuses, reason = list_current_user_budget_spend_statuses(
            workspace, "workspace-token", timeout=7
        )

        assert statuses == []
        assert reason is None
        assert calls == [
            (f"{WS}/api/ai-gateway/v2/budget-spend-statuses?page_size=100", "workspace-token", 7)
        ]
        assert os.environ["DATABRICKS_HOST"] == "https://another-workspace"
        assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "workspace-profile"
        assert os.environ["DATABRICKS_TOKEN"] == "existing-pat"

    def test_propagates_api_failures_without_account_fallback(self, monkeypatch):
        failure = "network error: timed out"
        calls = []

        def fake_get(url, token, timeout=10):
            calls.append((url, token))
            return None, failure

        monkeypatch.setattr(db_mod, "_http_get_json", fake_get)

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason == failure
        assert calls == [
            (f"{WS}/api/ai-gateway/v2/budget-spend-statuses?page_size=100", "workspace-token")
        ]

    @pytest.mark.parametrize("page_token", [42, [], {}])
    def test_rejects_invalid_page_tokens(self, monkeypatch, page_token):
        monkeypatch.setattr(
            db_mod,
            "_http_get_json",
            lambda *args, **kwargs: ({"next_page_token": page_token}, None),
        )

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason == "budget spend status listing returned an invalid page token"

    @pytest.mark.parametrize(
        ("payload", "failure"),
        [
            (None, "an unexpected response shape"),
            ([], "an unexpected response shape"),
            ({"budget_spend_statuses": {}}, "no status collection"),
            ({"budget_spend_statuses": [None]}, "a malformed budget status"),
            ({"budget_spend_statuses": [{"name": BUDGET_NAME}]}, "a malformed budget status"),
            (
                {"budget_spend_statuses": [{"budget_id": BUDGET_ID, "alert_spend_statuses": []}]},
                "a malformed budget status",
            ),
        ],
    )
    def test_rejects_malformed_responses(self, monkeypatch, payload, failure):
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: (payload, None))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert statuses == []
        assert reason == f"budget spend status listing returned {failure}"

    @pytest.mark.parametrize("failure", [None, "HTTP 503 Service Unavailable"])
    def test_preserves_statuses_only_after_all_pages_succeed(self, monkeypatch, failure):
        pages = iter(
            [
                (
                    {
                        "budget_spend_statuses": [
                            {
                                "name": BUDGET_NAME,
                                "alert_spend_statuses": [
                                    {
                                        "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER",
                                        "time_period": "DAY",
                                        "percent_consumed": "0",
                                    }
                                ],
                            }
                        ],
                        "next_page_token": "page-2",
                    },
                    None,
                ),
                ({}, failure),
            ]
        )
        monkeypatch.setattr(db_mod, "_http_get_json", lambda *args, **kwargs: next(pages))

        statuses, reason = list_current_user_budget_spend_statuses(WS, "workspace-token")

        assert reason == failure
        assert [status.budget_id for status in statuses] == ([] if failure else [BUDGET_ID])
        assert next(pages, None) is None
