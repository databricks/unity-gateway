"""Small live fixture for the CUJ5 budget smart-default journeys."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any

_CONFIG_PATH = "/api/ai-gateway/v2/coding-agent-configs"
_RECOMMEND_PATH = _CONFIG_PATH + ":recommendModel"
_BUDGET_PATH = "/api/2.1/accounts/{account_id}/budgets/{budget_id}"
_PER_USER = "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER"
_BLOCK = "BLOCK_USAGE"
_TIMEOUT = 30.0
_POLL = 0.5


@dataclass(frozen=True, slots=True)
class BudgetTarget:
    workspace_id: str
    account_host: str
    account_id: str
    budget_id: str
    sonnet_model: str
    sol_model: str
    luna_model: str
    account_token: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> BudgetTarget:
        return cls(
            os.environ["UG_BUDGET_WORKSPACE_ID"].strip(),
            os.environ["UG_BUDGET_ACCOUNT_HOST"].strip(),
            os.environ["UG_BUDGET_ACCOUNT_ID"].strip(),
            os.environ["UG_BUDGET_ID"].strip(),
            os.environ["UG_BUDGET_SONNET_MODEL"].strip(),
            os.environ["UG_BUDGET_SOL_MODEL"].strip(),
            os.environ["UG_BUDGET_LUNA_MODEL"].strip(),
            os.environ["UG_BUDGET_ACCOUNT_TOKEN"].strip(),
        )


def _budget_update(budget: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the account API's writable budget fields and remove response metadata."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in {"create_time", "update_time"}:
                    continue
                if key == "target" and value.get("action_type") == _BLOCK:
                    continue
                cleaned = clean(item)
                if cleaned is None or cleaned == []:
                    continue
                result[key] = cleaned
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return {
        key: clean(budget[key])
        for key in (
            "account_id",
            "alert_configurations",
            "budget_configuration_id",
            "display_name",
            "filter",
            "resource_type",
        )
        if key in budget
    }


def _request_json(
    method: str,
    url: str,
    token: str,
    body: Mapping[str, Any] | None,
    operation: str,
) -> Any:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise AssertionError(f"{operation} failed: HTTP {error.code}") from None
    return json.loads(raw) if raw else {}


class BudgetDefaults:
    """Temporarily move one existing per-user hard-block threshold."""

    def __init__(self, session: Any, workspace: str, target: BudgetTarget):
        self.session = session
        self.workspace = workspace.rstrip("/")
        self.target = target
        self.sonnet_model = target.sonnet_model
        self.sol_model = target.sol_model
        self.luna_model = target.luna_model
        self._original_threshold: str | None = None
        self._dirty = False
        self._evidence = 0
        self._exact_boundary = False

    def __enter__(self) -> BudgetDefaults:
        self._validate_config()
        current = self._account_budget()
        assert current["budget_configuration_id"] == self.target.budget_id
        assert str(current["account_id"]) == self.target.account_id
        workspace_filter = current["filter"]["workspace_id"]
        assert workspace_filter["operator"] in ("IN", "BUDGET_CONFIGURATION_FILTER_OPERATOR_IN")
        assert [str(value) for value in workspace_filter["values"]] == [self.target.workspace_id]
        _, self._original_threshold = self._alert(current)
        self._record("setup", threshold=self._original_threshold, status="captured")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._dirty:
            self._restore()
        return False

    def _validate_config(self) -> None:
        payload = self._workspace_request("GET", _CONFIG_PATH, None, "managed config")
        config = payload["coding_agent_configs"][0]
        assert config["default_agent"] == "CODING_AGENT_CLAUDE_CODE"

        agents = {entry["agent"]: entry["config"] for entry in config["enabled_agents"]}
        claude = agents["CODING_AGENT_CLAUDE_CODE"]
        codex = agents["CODING_AGENT_CODEX"]
        assert claude["models"]["model_services"] == [self.sonnet_model]
        assert claude["default_models"]["default_model"] == self.sonnet_model
        assert set(codex["models"]["model_services"]) == {self.sol_model, self.luna_model}
        assert codex["default_models"]["default_model"] == self.sol_model

        smart_defaults = config["smart_defaults"]
        assert smart_defaults["budget_id"] == self.target.budget_id
        tiers = smart_defaults["tiers"]
        assert [Decimal(str(tier["spending_percentage"])) for tier in tiers] == [
            Decimal("0.5"),
            Decimal("0.8"),
        ]
        assert [(tier["recommended_agent"], tier["recommended_model"]) for tier in tiers] == [
            ("CODING_AGENT_CODEX", self.sol_model),
            ("CODING_AGENT_CODEX", self.luna_model),
        ]

    def recommendation(self, label: str = "recommendation") -> dict[str, Any]:
        payload = self._workspace_request("POST", _RECOMMEND_PATH, {}, "recommendModel")
        self._record(
            "recommendation",
            label=label,
            current_spend=payload.get("current_spend"),
            effective_threshold=payload.get("effective_threshold"),
            recommended_agent=payload.get("recommended_agent"),
            recommended_model=payload.get("recommended_model"),
        )
        return payload

    def prepare(self, multiplier: Decimal, label: str = "prepare") -> dict[str, Any]:
        before = self.recommendation(label + ":before")
        spend = Decimal(str(before["current_spend"]))
        assert spend > 0, "budget fixture requires existing positive spend"
        self._exact_boundary = multiplier in {Decimal("2"), Decimal("1.25")}
        with localcontext() as context:
            context.prec = 64
            threshold = spend * multiplier
        threshold_text = format(threshold, "f")

        current = self._account_budget()
        index, _ = self._alert(current)
        current["alert_configurations"][index]["quantity_threshold"] = threshold_text
        self._put(current, label, threshold_text)
        recommendation = self._wait_for_recommendation(threshold, label)
        readback_spend = Decimal(str(recommendation["current_spend"]))
        if readback_spend != spend:
            detail = (
                "exact boundary is unverified"
                if self._exact_boundary
                else "recommendation spend changed"
            )
            raise AssertionError(f"{label}: {detail} before inference")
        return recommendation

    def assert_stable(self, before: Mapping[str, Any], label: str = "stable") -> dict[str, Any]:
        after = self.recommendation(label + ":after")
        for key in (
            "current_spend",
            "effective_threshold",
            "recommended_agent",
            "recommended_model",
        ):
            old, new = before.get(key), after.get(key)
            if key in {"current_spend", "effective_threshold"}:
                old, new = Decimal(str(old)), Decimal(str(new))
            if old != new:
                detail = "exact boundary is unverified; " if self._exact_boundary else ""
                raise AssertionError(f"{label}: {detail}recommendation changed {key}")
        return after

    def _restore(self) -> None:
        assert self._original_threshold is not None
        current = self._account_budget()
        index, _ = self._alert(current)
        current["alert_configurations"][index]["quantity_threshold"] = self._original_threshold
        self._put(current, "restore", self._original_threshold)
        _, restored_threshold = self._alert(self._account_budget())
        assert Decimal(restored_threshold) == Decimal(self._original_threshold)
        self._record("restore", threshold=self._original_threshold, status="verified")

    def _wait_for_recommendation(self, threshold: Decimal, label: str) -> dict[str, Any]:
        deadline = time.monotonic() + _TIMEOUT
        while True:
            result = self.recommendation(label + ":readback")
            if Decimal(str(result["effective_threshold"])) == threshold:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{label}: recommendation threshold did not converge")
            time.sleep(min(_POLL, remaining))

    def _account_budget(self) -> dict[str, Any]:
        payload = _request_json(
            "GET", self._account_url(), self.target.account_token, None, "account budget GET"
        )
        return payload.get("budget", payload)

    def _alert(self, budget: Mapping[str, Any]) -> tuple[int, str]:
        matches = []
        for index, alert in enumerate(budget["alert_configurations"]):
            actions = alert.get("action_configurations", [])
            if alert.get("scope_type") == _PER_USER and any(
                action.get("action_type") == _BLOCK for action in actions
            ):
                assert not alert.get("principal_overrides")
                matches.append((index, str(alert["quantity_threshold"])))
        assert len(matches) == 1, "account budget must have one per-user BLOCK_USAGE alert"
        return matches[0]

    def _put(self, budget: Mapping[str, Any], label: str, threshold: str) -> None:
        self._dirty = True
        self._record("mutation", label=label, threshold=threshold, status="write_started")
        _request_json(
            "PUT",
            self._account_url(),
            self.target.account_token,
            {"budget": _budget_update(budget)},
            "account budget PUT",
        )

    def _account_url(self) -> str:
        return self.target.account_host.rstrip("/") + _BUDGET_PATH.format(
            account_id=urllib.parse.quote(self.target.account_id, safe=""),
            budget_id=urllib.parse.quote(self.target.budget_id, safe=""),
        )

    def _workspace_request(
        self, method: str, path: str, body: Mapping[str, Any] | None, operation: str
    ) -> Any:
        token = self.session.env["DATABRICKS_BEARER"]
        return _request_json(method, self.workspace + path, token, body, operation)

    def _record(self, kind: str, **fields: object) -> None:
        self._evidence += 1
        self.session.record(
            f"budget-defaults-{self._evidence:03d}-{kind}.json",
            {"kind": kind, **fields},
        )
