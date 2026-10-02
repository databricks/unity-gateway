"""Unit checks for the live budget-defaults fixture's safety boundaries."""

from __future__ import annotations

import copy
import io
from dataclasses import FrozenInstanceError
from decimal import Decimal
from email.message import Message

import pytest

from tests.integration.utils import budget_defaults as bd

TARGET = bd.BudgetTarget(
    workspace="https://workspace.example.test",
    workspace_id="123456789",
    account_host="https://accounts.example.test",
    account_id="account",
    budget_id="11111111-2222-3333-4444-555555555555",
    sonnet_model="test.models.sonnet",
    sol_model="test.models.sol",
    luna_model="test.models.luna",
    account_token="account-token",
)


def _budget(threshold: str = "100.00") -> dict:
    return {
        "account_id": TARGET.account_id,
        "budget_configuration_id": TARGET.budget_id,
        "workspace_id": int(TARGET.workspace_id),
        "display_name": "coding agents",
        "create_time": 123,
        "update_time": 456,
        "filter": {
            "workspace_id": {"operator": "IN", "values": [int(TARGET.workspace_id)]},
            "tags": [{"key": "owner", "value": {"operator": "IN", "values": ["eng"]}}],
        },
        "resource_type": "BUDGET_RESOURCE_TYPE_UNITY_AI_GATEWAY",
        "alert_configurations": [
            {
                "alert_configuration_id": "alert-old",
                "create_time": 123,
                "scope_type": bd._PER_USER_SCOPE,
                "quantity_threshold": threshold,
                "quantity_type": "LIST_PRICE_DOLLARS_USD",
                "time_period": "MONTH",
                "trigger_type": "CUMULATIVE_SPENDING_EXCEEDED",
                "principal_overrides": [],
                "action_configurations": [
                    {"action_configuration_id": "block-old", "action_type": "BLOCK_USAGE"},
                    {
                        "action_configuration_id": "email-old",
                        "action_type": "EMAIL_NOTIFICATION",
                        "target": "alerts@example.com",
                    },
                ],
            },
            {
                "alert_configuration_id": "shared-alert",
                "scope_type": "ALERT_CONFIGURATION_SCOPE_TYPE_SHARED",
                "quantity_threshold": "999.00",
                "action_configurations": [
                    {
                        "action_configuration_id": "shared-email",
                        "action_type": "EMAIL_NOTIFICATION",
                        "target": "shared@example.com",
                    }
                ],
            },
        ],
    }


class _Session:
    def __init__(self):
        self.env = {"DATABRICKS_BEARER": "workspace-token"}
        self.artifacts: list[tuple[str, object]] = []

    def record(self, name: str, value: object) -> None:
        self.artifacts.append((name, value))


def _fixture(
    session: _Session | None = None,
    *,
    workspace: str | None = None,
    target: bd.BudgetTarget = TARGET,
) -> bd.BudgetDefaults:
    return bd.BudgetDefaults(
        session or _Session(),
        workspace or target.workspace,
        target,
    )


def test_target_workspace_must_match_fixture_workspace():
    with pytest.raises(bd.BudgetDefaultsError, match="budget fixture target"):
        _fixture(workspace="https://other.example.test")


def test_target_loads_all_pytest_fields_and_is_immutable():
    environment = {
        "UCODE_TEST_WORKSPACE": TARGET.workspace,
        "UG_BUDGET_WORKSPACE_ID": TARGET.workspace_id,
        "UG_BUDGET_ACCOUNT_HOST": TARGET.account_host,
        "UG_BUDGET_ACCOUNT_ID": TARGET.account_id,
        "UG_BUDGET_ID": TARGET.budget_id,
        "UG_BUDGET_SONNET_MODEL": TARGET.sonnet_model,
        "UG_BUDGET_SOL_MODEL": TARGET.sol_model,
        "UG_BUDGET_LUNA_MODEL": TARGET.luna_model,
        "UG_BUDGET_ACCOUNT_TOKEN": TARGET.account_token,
    }

    loaded = bd.BudgetTarget.from_environment(environment)

    assert loaded == TARGET
    with pytest.raises(FrozenInstanceError):
        loaded.workspace = "https://other.example.test"


def test_account_request_uses_target_host_and_token(monkeypatch):
    monkeypatch.setenv("UG_BUDGET_ACCOUNT_HOST", "https://unrelated.example")
    fixture = _fixture()
    requests = []

    def request(*args):
        requests.append(args)
        return {"budget": _budget()}

    monkeypatch.setattr(bd, "_request_json", request)
    fixture._account_request("GET", fixture._account_url(), None, "budget read")
    assert requests == [
        (
            "GET",
            TARGET.account_host + "/api/2.1/accounts/account/budgets/" + TARGET.budget_id,
            TARGET.account_token,
            None,
            "budget read",
        )
    ]
    assert fixture.session.env == {"DATABRICKS_BEARER": "workspace-token"}


def test_prepare_keeps_decimal_threshold_precision(monkeypatch):
    session = _Session()
    fixture = _fixture(session)
    fixture._entered = True
    budget = _budget()
    sent: list[dict] = []
    recommendation = {
        "current_spend": "12.34567890123456789",
        "effective_threshold": "100.000000000000000000",
        "recommended_agent": "CODING_AGENT_CODEX",
        "recommended_model": TARGET.sol_model,
    }
    monkeypatch.setattr(fixture, "recommendation", lambda label: recommendation)
    monkeypatch.setattr(fixture, "_validated_budget", lambda: copy.deepcopy(budget))
    monkeypatch.setattr(fixture, "_put_budget", lambda value, label, threshold: sent.append(value))
    monkeypatch.setattr(
        fixture,
        "_wait_for_budget",
        lambda expected, label: expected,
    )
    monkeypatch.setattr(
        fixture,
        "_wait_for_recommendation",
        lambda threshold, label: {"effective_threshold": bd._decimal_text(threshold)},
    )

    result = fixture.prepare(Decimal("2.5"), "sol-boundary")

    assert sent[0]["alert_configurations"][0]["quantity_threshold"] == "30.864197253086419725"
    assert sent[0]["alert_configurations"][1]["quantity_threshold"] == "999.00"
    assert result["effective_threshold"] == "30.864197253086419725"


def test_prepare_rejects_threshold_beyond_account_decimal_scale(monkeypatch):
    session = _Session()
    fixture = _fixture(session)
    fixture._entered = True
    monkeypatch.setattr(
        fixture,
        "recommendation",
        lambda label: {"current_spend": "12.345678901234567891", "effective_threshold": "100"},
    )
    monkeypatch.setattr(fixture, "_validated_budget", lambda: _budget())
    with pytest.raises(bd.BudgetDefaultsError, match="decimal precision"):
        fixture.prepare(Decimal("2.5"), "too-precise")


def test_threshold_limits_respect_decimal_38_18_integer_capacity():
    maximum = Decimal("99999999999999999999.999999999999999999")
    assert bd._api_decimal_text(maximum) == str(maximum)
    with pytest.raises(bd.BudgetDefaultsError, match="decimal precision"):
        bd._api_decimal_text(Decimal("100000000000000000000"))


def test_update_body_keeps_nested_ids_and_drops_response_only_fields():
    original = _budget()
    original["alert_configurations"][0]["action_configurations"][0]["target"] = ""
    candidate = bd._budget_update(
        {**original, "alert_configurations": copy.deepcopy(original["alert_configurations"])}
    )
    candidate["alert_configurations"][0]["quantity_threshold"] = "40.0"

    assert candidate["display_name"] == original["display_name"]
    assert candidate["filter"] == original["filter"]
    assert candidate["resource_type"] == original["resource_type"]
    assert candidate["alert_configurations"][0]["alert_configuration_id"] == "alert-old"
    assert (
        candidate["alert_configurations"][0]["action_configurations"][0]["action_configuration_id"]
        == "block-old"
    )
    assert "create_time" not in candidate
    assert "update_time" not in candidate
    assert "workspace_id" not in candidate
    assert "create_time" not in candidate["alert_configurations"][0]
    assert "principal_overrides" not in candidate["alert_configurations"][0]
    assert "target" not in candidate["alert_configurations"][0]["action_configurations"][0]
    assert (
        candidate["alert_configurations"][0]["action_configurations"][1]["target"]
        == "alerts@example.com"
    )

    # Server metadata and regenerated nested IDs do not affect semantic preservation checks.
    returned = copy.deepcopy(original)
    returned["alert_configurations"][0]["quantity_threshold"] = "40.0"
    returned["update_time"] = 789
    returned["alert_configurations"][0]["alert_configuration_id"] = "alert-new"
    returned["alert_configurations"][0]["create_time"] = 789
    returned["alert_configurations"][0]["action_configurations"][0]["create_time"] = 789
    returned["alert_configurations"][0]["action_configurations"][0]["action_configuration_id"] = (
        "block-new"
    )
    returned["alert_configurations"][0]["action_configurations"].reverse()
    returned.pop("workspace_id")
    returned["product_type"] = "PRODUCT_TYPE_UNRELATED"
    expected = copy.deepcopy(original)
    expected["alert_configurations"][0]["quantity_threshold"] = "40.0"
    assert bd._semantic_budget(returned) == bd._semantic_budget(expected)
    assert "create_time" not in bd._semantic_budget(returned)
    assert bd._semantic_budget(original)["alert_configurations"][0]["quantity_threshold"] == "100"


@pytest.mark.parametrize(
    "invalid", ["budget", "account", "workspace", "owner", "resource", "override", "ambiguous"]
)
def test_budget_preconditions_reject_unsafe_targets(invalid):
    budget = _budget()
    if invalid == "budget":
        budget["budget_configuration_id"] = "another-budget"
    elif invalid == "account":
        budget["account_id"] = "another-account"
    elif invalid == "workspace":
        budget["filter"]["workspace_id"]["values"].append("another-workspace")
    elif invalid == "owner":
        budget["workspace_id"] = "another-workspace"
    elif invalid == "resource":
        budget["resource_type"] = "BUDGET_RESOURCE_TYPE_WORKSPACE"
    elif invalid == "override":
        budget["alert_configurations"][0]["principal_overrides"] = [
            {"principal_id": "someone", "override_threshold": "200"}
        ]
    else:
        budget["alert_configurations"].append(copy.deepcopy(budget["alert_configurations"][0]))
    with pytest.raises(bd.BudgetDefaultsError):
        bd._validate_budget(budget, TARGET)


def test_budget_classification_prefers_valid_resource_over_legacy_product():
    budget = _budget()
    budget["product_type"] = "PRODUCT_TYPE_UNRELATED"

    bd._validate_budget(budget, TARGET)


def test_budget_classification_accepts_valid_legacy_product_without_resource():
    budget = _budget()
    budget.pop("resource_type")
    budget["product_type"] = "PRODUCT_TYPE_AI_GATEWAY"

    bd._validate_budget(budget, TARGET)


@pytest.mark.parametrize(
    "classification",
    [
        {
            "resource_type": "BUDGET_RESOURCE_TYPE_WORKSPACE",
            "product_type": "PRODUCT_TYPE_AI_GATEWAY",
        },
        {"product_type": "PRODUCT_TYPE_WORKSPACE"},
        {},
    ],
)
def test_budget_classification_rejects_invalid_or_missing_authority(classification):
    budget = _budget()
    budget.pop("resource_type")
    budget.update(classification)

    with pytest.raises(bd.BudgetDefaultsError, match="AI Gateway"):
        bd._validate_budget(budget, TARGET)


@pytest.mark.parametrize("spend", ["0", "-1", "NaN", "Infinity", None])
def test_prepare_rejects_missing_positive_spend_before_a_write(monkeypatch, spend):
    fixture = _fixture()
    fixture._entered = True
    monkeypatch.setattr(
        fixture,
        "recommendation",
        lambda label: {"current_spend": spend, "effective_threshold": "100"},
    )
    with pytest.raises(bd.BudgetDefaultsError, match="existing positive spend"):
        fixture.prepare(Decimal("2"))
    assert not fixture._dirty


def test_context_restores_after_first_put_fails(monkeypatch):
    session = _Session()
    fixture = _fixture(session)
    budget = _budget()
    monkeypatch.setattr(fixture, "validate_config", lambda: {})
    monkeypatch.setattr(fixture, "_validated_budget", lambda: copy.deepcopy(budget))
    monkeypatch.setattr(fixture, "_wait_for_budget", lambda expected, label: expected)
    puts: list[dict] = []

    def account_request(method, url, body, operation):
        if method == "PUT":
            puts.append(body["budget"])
            if len(puts) == 1:
                raise RuntimeError("account-token-must-not-escape")
        return {}

    monkeypatch.setattr(fixture, "_account_request", account_request)
    with pytest.raises(RuntimeError, match="account-token-must-not-escape"):
        with fixture as active:
            active._put_budget(bd._budget_update(budget), "write-failure", "40.0")

    assert len(puts) == 2
    assert puts[1]["alert_configurations"][0]["quantity_threshold"] == "100.00"


def test_assert_stable_allows_server_decimal_formatting_but_detects_model_change(monkeypatch):
    session = _Session()
    fixture = _fixture(session)
    responses = iter(
        [
            {
                "current_spend": "40.0",
                "effective_threshold": "100.00",
                "recommended_agent": "CODING_AGENT_CODEX",
                "recommended_model": TARGET.sol_model,
            },
            {
                "current_spend": "40.000000000000",
                "effective_threshold": "100.000000000000",
                "recommended_agent": "CODING_AGENT_CODEX",
                "recommended_model": TARGET.sol_model,
            },
        ]
    )
    monkeypatch.setattr(fixture, "recommendation", lambda label: next(responses))
    before = next(responses)
    assert fixture.assert_stable(before, "before-task")["recommended_model"] == TARGET.sol_model

    changed = {
        **before,
        "recommended_model": TARGET.luna_model,
    }
    monkeypatch.setattr(fixture, "recommendation", lambda label: changed)
    with pytest.raises(bd.BudgetDefaultsError, match="recommended_model"):
        fixture.assert_stable(before, "changed")


def test_sensitive_http_error_contains_only_status(monkeypatch):
    secret = "account-token-must-not-escape"

    class FailingOpener:
        def open(self, request, timeout):
            raise urllib_error(
                request.full_url + "?token=" + secret,
                500,
                secret,
            )

    class _HTTPError(bd.urllib.error.HTTPError):
        pass

    def urllib_error(url, code, reason):
        return _HTTPError(url, code, reason, Message(), io.BytesIO(secret.encode()))

    monkeypatch.setattr(bd, "_NO_REDIRECT_OPENER", FailingOpener())
    with pytest.raises(bd.BudgetDefaultsError) as error:
        bd._request_json("GET", "https://account.example.test/budget", secret, None, "budget read")
    assert str(error.value) == "budget read failed: HTTP 500"
    assert secret not in str(error.value)


def test_validate_config_requires_current_smart_defaults(monkeypatch):
    session = _Session()
    fixture = _fixture(session)
    config = {
        "default_agent": "CODING_AGENT_CLAUDE_CODE",
        "enabled_agents": [
            {
                "agent": "CODING_AGENT_CLAUDE_CODE",
                "config": {
                    "models": {"model_services": [TARGET.sonnet_model]},
                    "default_models": {
                        "default_model": TARGET.sonnet_model,
                    },
                    "smart_routing": {"enabled": False},
                    "tracing": {"enabled": False},
                },
            },
            {
                "agent": "CODING_AGENT_CODEX",
                "config": {
                    "models": {"model_services": [TARGET.sol_model, TARGET.luna_model]},
                    "default_models": {"default_model": TARGET.sol_model},
                    "smart_routing": {"enabled": False},
                    "tracing": {"enabled": False},
                },
            },
        ],
        "smart_defaults": {
            "budget_id": TARGET.budget_id,
            "tiers": [
                {
                    "spending_percentage": 0.5,
                    "recommended_agent": "CODING_AGENT_CODEX",
                    "recommended_model": TARGET.sol_model,
                },
                {
                    "spending_percentage": 0.8,
                    "recommended_agent": "CODING_AGENT_CODEX",
                    "recommended_model": TARGET.luna_model,
                },
            ],
        },
    }
    monkeypatch.setattr(
        fixture,
        "_workspace_request",
        lambda method, path, body, operation: {"coding_agent_configs": [config]},
    )

    assert fixture.validate_config() == config

    legacy = copy.deepcopy(config)
    legacy["spend_tiers"] = legacy.pop("smart_defaults")
    monkeypatch.setattr(
        fixture,
        "_workspace_request",
        lambda method, path, body, operation: {"coding_agent_configs": [legacy]},
    )
    with pytest.raises(bd.BudgetDefaultsError, match="spend_tiers"):
        fixture.validate_config()
