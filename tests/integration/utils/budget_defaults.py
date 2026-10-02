"""Live budget-defaults setup used by the bounded smart-defaults journeys.

This module deliberately speaks the two public APIs directly. It remains independent of the
application under test: the account token stays in pytest, while the workspace bearer comes
from the disposable ``UserSession`` environment.
"""

from __future__ import annotations

import copy
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

_WORKSPACE_CONFIG_PATH = "/api/ai-gateway/v2/coding-agent-configs"
_RECOMMEND_PATH = _WORKSPACE_CONFIG_PATH + ":recommendModel"
_ACCOUNT_BUDGET_PATH = "/api/2.1/accounts/{account_id}/budgets/{budget_id}"
_PER_USER_SCOPE = "ALERT_CONFIGURATION_SCOPE_TYPE_PER_USER"
_BLOCK_USAGE = "BLOCK_USAGE"
_AIG_RESOURCE_TYPES = {
    "BUDGET_RESOURCE_TYPE_UNITY_AI_GATEWAY",
    "RESOURCE_TYPE_UNITY_AI_GATEWAY",
    "UNITY_AI_GATEWAY",
    "AI_GATEWAY",
}
_AIG_PRODUCT_TYPES = {
    "PRODUCT_TYPE_AI_GATEWAY",
    "PRODUCT_TYPE_UNITY_AI_GATEWAY",
    "AI_GATEWAY",
    "UNITY_AI_GATEWAY",
}
_RECOMMEND_TIMEOUT_SECONDS = 30.0
_BUDGET_PROPAGATION_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.5
_MISSING = object()

_TARGET_ENV_FIELDS = (
    ("workspace", "UCODE_TEST_WORKSPACE"),
    ("workspace_id", "UG_BUDGET_WORKSPACE_ID"),
    ("account_host", "UG_BUDGET_ACCOUNT_HOST"),
    ("account_id", "UG_BUDGET_ACCOUNT_ID"),
    ("budget_id", "UG_BUDGET_ID"),
    ("sonnet_model", "UG_BUDGET_SONNET_MODEL"),
    ("sol_model", "UG_BUDGET_SOL_MODEL"),
    ("luna_model", "UG_BUDGET_LUNA_MODEL"),
    ("account_token", "UG_BUDGET_ACCOUNT_TOKEN"),
)


class BudgetDefaultsError(AssertionError):
    """A live budget prerequisite, API operation, or restoration check failed."""


@dataclass(frozen=True, slots=True)
class BudgetTarget:
    """Immutable live target metadata supplied to the budget fixture by pytest."""

    workspace: str
    workspace_id: str
    account_host: str
    account_id: str
    budget_id: str
    sonnet_model: str
    sol_model: str
    luna_model: str
    account_token: str = dataclass_field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> BudgetTarget:
        """Load the complete target from the environment passed to pytest."""

        environment = os.environ if environment is None else environment
        values: dict[str, str] = {}
        missing: list[str] = []
        for name, key in _TARGET_ENV_FIELDS:
            value = environment.get(key, "").strip()
            if not value:
                missing.append(key)
            else:
                values[name] = value
        if missing:
            raise BudgetDefaultsError("set " + ", ".join(missing) + " for the budget fixture")
        return cls(**values)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so an Authorization header cannot follow another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


@dataclass(frozen=True)
class _AlertTarget:
    index: int
    threshold: str


@dataclass(frozen=True)
class _BudgetSnapshot:
    threshold: str
    semantic: dict[str, Any]


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _positive_decimal(value: object) -> Decimal | None:
    result = _decimal(value)
    return result if result is not None and result > 0 else None


def _decimal_text(value: Decimal) -> str:
    """Render a Decimal without a binary float round-trip or scientific notation."""

    return format(value, "f")


def _canonical_decimal_text(value: Decimal) -> str:
    """Render equivalent decimal values identically for semantic comparisons."""

    text = _decimal_text(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _api_decimal_text(value: Decimal) -> str:
    """Render a threshold representable by the account API's DECIMAL(38,18) field."""

    text = _canonical_decimal_text(value)
    unsigned = text.removeprefix("-")
    integer, _, fraction = unsigned.partition(".")
    if len(fraction) > 18 or len(integer.lstrip("0")) > 20:
        raise BudgetDefaultsError(
            "budget multiplier produced a threshold outside the account API decimal precision"
        )
    return text


def _semantic_decimal_text(value: object) -> object:
    parsed = _decimal(value)
    return _canonical_decimal_text(parsed) if parsed is not None else value


def _semantic_budget(budget: Mapping[str, Any]) -> dict[str, Any]:
    """Copy writable budget semantics while removing server-generated IDs.

    Comparing the account API's update schema proves that replacement retained every writable
    field while allowing response-only metadata and nested IDs to change.
    """

    ignored = {"alert_configuration_id", "action_configuration_id"}

    def clean(value: Any, *, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: clean(v, key=k) for k, v in value.items() if k not in ignored}
        if isinstance(value, list):
            items = [clean(item) for item in value]
            return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
        if key in {"quantity_threshold", "override_threshold"}:
            return _semantic_decimal_text(value)
        return copy.deepcopy(value)

    return clean(_budget_update(budget))


def _different_paths(expected: Any, actual: Any, path: str = "budget") -> list[str]:
    """Return differing JSON paths without exposing their values."""

    if isinstance(expected, dict) and isinstance(actual, dict):
        paths = []
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{path}.{key}"
            if key not in expected or key not in actual:
                paths.append(child)
            else:
                paths.extend(_different_paths(expected[key], actual[key], child))
        return paths
    if isinstance(expected, list) and isinstance(actual, list):
        paths = []
        if len(expected) != len(actual):
            paths.append(path + ".length")
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=False)):
            paths.extend(_different_paths(expected_item, actual_item, f"{path}[{index}]"))
        return paths
    return [] if expected == actual else [path]


def _extract_budget(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("budget"), dict):
        return payload["budget"]
    if isinstance(payload, dict) and "budget_configuration_id" in payload:
        return payload
    raise BudgetDefaultsError("account budget response did not contain a budget object")


def _extract_configs(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [value for value in payload if isinstance(value, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("coding_agent_configs", "configs", "items"):
        values = payload.get(key)
        if isinstance(values, list):
            return [value for value in values if isinstance(value, dict)]
    # A few API versions return one config directly on a GET by resource name.
    return [payload] if "enabled_agents" in payload else []


def _budget_update(budget: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only fields accepted by the account budget update request."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            ignored = {"create_time", "update_time"}
            if value.get("action_type") == _BLOCK_USAGE:
                ignored.add("target")
            result = {}
            for key, item in value.items():
                if key in ignored:
                    continue
                cleaned = clean(item)
                if cleaned is None or cleaned == []:
                    continue
                result[key] = cleaned
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return copy.deepcopy(value)

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


def _safe_error(operation: str, error: BaseException) -> BudgetDefaultsError:
    if isinstance(error, urllib.error.HTTPError):
        return BudgetDefaultsError(f"{operation} failed: HTTP {error.code}")
    if isinstance(error, urllib.error.URLError):
        return BudgetDefaultsError(f"{operation} failed: network error")
    if isinstance(error, TimeoutError):
        return BudgetDefaultsError(f"{operation} failed: timeout")
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return BudgetDefaultsError(f"{operation} failed: invalid JSON response")
    return BudgetDefaultsError(f"{operation} failed: request error")


class BudgetDefaults:
    """Context manager for bounded, reversible live smart-defaults budget changes.

    ``prepare`` changes one existing per-user hard-block threshold to a multiple of the already
    accumulated spend.  It never creates spend and never touches the managed CodingAgentConfig.
    Every successful or failed write is restored on context exit using a fresh account read.
    """

    def __init__(
        self,
        session: Any,
        workspace: str,
        target: BudgetTarget,
    ):
        self.session = session
        self.workspace = workspace.rstrip("/")
        self.target = target
        self.target_workspace = target.workspace
        self.workspace_id = target.workspace_id
        self.budget_id = target.budget_id
        self.sonnet_model = target.sonnet_model
        self.sol_model = target.sol_model
        self.luna_model = target.luna_model
        self._require_workspace()
        self._snapshot: _BudgetSnapshot | None = None
        self._dirty = False
        self._entered = False
        self._evidence_index = 0

    def __enter__(self) -> BudgetDefaults:
        self._entered = True
        self.validate_config()
        current = self._validated_budget()
        self._snapshot = _BudgetSnapshot(
            threshold=_target_for(current).threshold,
            semantic=_semantic_budget(current),
        )
        self._record(
            "snapshot",
            "setup",
            budget_id=self.budget_id,
            threshold=self._snapshot.threshold,
            status="captured",
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self._dirty:
            return False
        try:
            self._restore()
        except BaseException as error:
            # Restoration is part of the test's contract.  A failed cleanup must never be hidden
            # by the task error that caused context exit.
            raise BudgetDefaultsError("budget fixture restoration failed") from error
        return False

    def validate_config(self) -> dict[str, Any]:
        """Read and validate the raw managed CodingAgentConfig expected by this journey."""

        self._require_workspace()
        payload = self._workspace_request("GET", _WORKSPACE_CONFIG_PATH, None, "managed config")
        configs = _extract_configs(payload)
        if len(configs) != 1:
            raise BudgetDefaultsError(
                f"expected exactly one managed CodingAgentConfig, found {len(configs)}"
            )
        config = configs[0]
        if config.get("default_agent") != "CODING_AGENT_CLAUDE_CODE":
            raise BudgetDefaultsError(
                "managed config default_agent is not CODING_AGENT_CLAUDE_CODE"
            )
        if "spend_tiers" in config:
            raise BudgetDefaultsError(
                "managed config uses legacy spend_tiers; expected smart_defaults"
            )

        raw_agents = config.get("enabled_agents")
        if not isinstance(raw_agents, list):
            raise BudgetDefaultsError("managed config has no enabled_agents list")
        agents: dict[str, dict[str, Any]] = {}
        for entry in raw_agents:
            if not isinstance(entry, dict):
                raise BudgetDefaultsError("managed config contains a malformed enabled agent")
            name = entry.get("agent")
            if name == "CODING_AGENT_CLAUDE_CODE":
                key = "claude"
            elif name == "CODING_AGENT_CODEX":
                key = "codex"
            else:
                raise BudgetDefaultsError(f"managed config contains unexpected agent {name!r}")
            if key in agents:
                raise BudgetDefaultsError(f"managed config repeats enabled agent {name!r}")
            agents[key] = _as_dict(entry.get("config"))
        if set(agents) != {"claude", "codex"}:
            raise BudgetDefaultsError("managed config must enable exactly Claude Code and Codex")

        claude_models = _as_dict(agents["claude"].get("models")).get("model_services")
        claude_defaults = _as_dict(agents["claude"].get("default_models"))
        if (
            claude_models != [self.sonnet_model]
            or claude_defaults.get("default_model") != self.sonnet_model
        ):
            raise BudgetDefaultsError("managed config Claude defaults are not Sonnet-only")
        if (
            "default_sonnet_model" in claude_defaults
            and claude_defaults.get("default_sonnet_model") != self.sonnet_model
        ):
            raise BudgetDefaultsError("managed config Claude default_sonnet_model is incorrect")

        codex_models = _as_dict(agents["codex"].get("models")).get("model_services")
        codex_defaults = _as_dict(agents["codex"].get("default_models"))
        if not isinstance(codex_models, list) or set(codex_models) != {
            self.sol_model,
            self.luna_model,
        }:
            raise BudgetDefaultsError("managed config Codex defaults are not Sol/Luna")
        if codex_defaults.get("default_model") != self.sol_model:
            raise BudgetDefaultsError("managed config Codex defaults are not Sol/Luna")

        for name, agent in agents.items():
            if _as_dict(agent.get("smart_routing")).get("enabled") is not False:
                raise BudgetDefaultsError(f"managed config {name} smart_routing is not disabled")
            if _as_dict(agent.get("tracing")).get("enabled") is not False:
                raise BudgetDefaultsError(f"managed config {name} tracing is not disabled")

        smart_defaults = config.get("smart_defaults")
        if not isinstance(smart_defaults, dict):
            raise BudgetDefaultsError("managed config has no smart_defaults policy")
        if smart_defaults.get("budget_id") != self.budget_id:
            raise BudgetDefaultsError("managed config smart_defaults uses an unexpected budget")
        tiers = smart_defaults.get("tiers")
        if not isinstance(tiers, list) or len(tiers) != 2:
            raise BudgetDefaultsError("managed config smart_defaults must contain two tiers")
        expected = [
            (Decimal("0.5"), "CODING_AGENT_CODEX", self.sol_model),
            (Decimal("0.8"), "CODING_AGENT_CODEX", self.luna_model),
        ]
        for index, (tier, (fraction, agent, model)) in enumerate(zip(tiers, expected, strict=True)):
            if not isinstance(tier, dict) or _decimal(tier.get("spending_percentage")) != fraction:
                raise BudgetDefaultsError(f"smart_defaults tier {index} has the wrong percentage")
            if tier.get("recommended_agent") != agent or tier.get("recommended_model") != model:
                raise BudgetDefaultsError(
                    f"smart_defaults tier {index} has the wrong recommendation"
                )
        return config

    def recommendation(self, label: str = "recommendation") -> dict[str, Any]:
        """Return the raw ``recommendModel`` object, retaining decimal strings verbatim."""

        self._require_workspace()
        payload = self._workspace_request("POST", _RECOMMEND_PATH, {}, "recommendModel")
        if not isinstance(payload, dict):
            raise BudgetDefaultsError("recommendModel returned a non-object response")
        self._record(
            "recommendation",
            label,
            current_spend=payload.get("current_spend"),
            effective_threshold=payload.get("effective_threshold"),
            recommended_agent=payload.get("recommended_agent"),
            recommended_model=payload.get("recommended_model"),
        )
        return payload

    def prepare(self, multiplier: Decimal, label: str = "prepare") -> dict[str, Any]:
        """Set the existing hard-block threshold to ``current_spend * multiplier``.

        The raw recommendation is read before the account write, then polled until the gateway
        reports the exact Decimal threshold.  Missing, zero, NaN, and infinite spend/threshold
        values are prerequisites failures; this fixture never manufactures spend.
        """

        if not self._entered:
            raise BudgetDefaultsError("BudgetDefaults.prepare must run inside its context manager")
        factor = _positive_decimal(multiplier)
        if factor is None:
            raise BudgetDefaultsError("budget multiplier must be a finite positive Decimal")

        before = self.recommendation(label + ":before")
        spend = _positive_decimal(before.get("current_spend"))
        existing_threshold = _positive_decimal(before.get("effective_threshold"))
        if spend is None:
            raise BudgetDefaultsError(
                "budget fixture requires existing positive spend; it will not generate spend"
            )
        if existing_threshold is None:
            raise BudgetDefaultsError(
                "budget fixture requires a finite positive existing effective threshold"
            )
        with localcontext() as context:
            context.prec = max(64, len(spend.as_tuple().digits) + len(factor.as_tuple().digits) + 8)
            target_threshold = spend * factor
        if not target_threshold.is_finite() or target_threshold <= 0:
            raise BudgetDefaultsError("budget multiplier produced a non-finite threshold")
        target_text = _api_decimal_text(target_threshold)

        current = self._validated_budget()
        target = _target_for(current)
        candidate = copy.deepcopy(current)
        alerts = candidate["alert_configurations"]
        alerts[target.index]["quantity_threshold"] = target_text
        expected_semantic = _semantic_budget(candidate)
        self._put_budget(_budget_update(candidate), label, target_text)
        self._wait_for_budget(expected_semantic, label)
        return self._wait_for_recommendation(target_threshold, label)

    def assert_stable(self, before: Mapping[str, Any], label: str = "stable") -> dict[str, Any]:
        """Reread raw recommendation fields and require spend/threshold/agent/model stability."""

        after = self.recommendation(label + ":after")
        fields = (
            "current_spend",
            "effective_threshold",
            "recommended_agent",
            "recommended_model",
        )
        for field in fields:
            old = before.get(field, _MISSING)
            new = after.get(field, _MISSING)
            if field in {"current_spend", "effective_threshold"}:
                old_decimal = _decimal(old)
                new_decimal = _decimal(new)
                if old_decimal is not None and new_decimal is not None:
                    old, new = old_decimal, new_decimal
            if old != new:
                raise BudgetDefaultsError(f"{label}: recommendation changed {field}")
        return after

    def validate_budget(self) -> dict[str, Any]:
        """Read and validate the target account budget without changing it."""

        return self._validated_budget()

    def _restore(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            raise BudgetDefaultsError("budget fixture is dirty but has no original snapshot")
        current = self._validated_budget()
        target = _target_for(current)
        candidate = copy.deepcopy(current)
        candidate["alert_configurations"][target.index]["quantity_threshold"] = snapshot.threshold
        self._put_budget(_budget_update(candidate), "restore", snapshot.threshold)
        self._wait_for_budget(snapshot.semantic, "restore")
        self._record("restore", "restore", threshold=snapshot.threshold, status="verified")

    def _wait_for_recommendation(self, threshold: Decimal, label: str) -> dict[str, Any]:
        deadline = time.monotonic() + _RECOMMEND_TIMEOUT_SECONDS
        polls = 0
        max_polls = int(_RECOMMEND_TIMEOUT_SECONDS / _POLL_INTERVAL_SECONDS) + 2
        last: dict[str, Any] | None = None
        while True:
            polls += 1
            last = self.recommendation(label + ":readback")
            observed = _decimal(last.get("effective_threshold"))
            if observed is not None and observed == threshold:
                return last
            remaining = deadline - time.monotonic()
            if remaining <= 0 or polls >= max_polls:
                observed_text = "<missing>" if observed is None else str(observed)
                raise BudgetDefaultsError(
                    f"{label}: recommendModel threshold did not converge to {_decimal_text(threshold)} "
                    f"(last {observed_text})"
                )
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    def _wait_for_budget(self, expected: Mapping[str, Any], label: str) -> dict[str, Any]:
        deadline = time.monotonic() + _BUDGET_PROPAGATION_TIMEOUT_SECONDS
        polls = 0
        max_polls = int(_BUDGET_PROPAGATION_TIMEOUT_SECONDS / _POLL_INTERVAL_SECONDS) + 2
        current: dict[str, Any] | None = None
        actual: dict[str, Any] | None = None
        while True:
            polls += 1
            current = self._validated_budget()
            actual = _semantic_budget(current)
            if actual == dict(expected):
                self._record(
                    "mutation" if label != "restore" else "restore",
                    label,
                    threshold=_target_for(current).threshold,
                    status="verified",
                )
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0 or polls >= max_polls:
                threshold = _target_for(current).threshold
                paths = ", ".join(_different_paths(dict(expected), actual)[:10])
                raise BudgetDefaultsError(
                    f"{label}: account budget replacement did not converge within "
                    f"{_BUDGET_PROPAGATION_TIMEOUT_SECONDS:g}s (last threshold {threshold}; "
                    f"different paths: {paths or '<none>'})"
                )
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    def _validated_budget(self) -> dict[str, Any]:
        payload = self._account_request("GET", self._account_url(), None, "account budget GET")
        budget = _extract_budget(payload)
        _validate_budget(budget, self.target)
        return budget

    def _put_budget(self, budget: Mapping[str, Any], label: str, threshold: str) -> None:
        url = self._account_url()
        # Set this before request construction/network I/O: a lost response or serialization error
        # still causes context exit to attempt restoration.
        self._dirty = True
        self._record(
            "mutation",
            label,
            threshold=threshold,
            alert_count=len(budget.get("alert_configurations", [])),
            status="write_started",
        )
        self._account_request(
            "PUT",
            url,
            {"budget": copy.deepcopy(dict(budget))},
            "account budget PUT",
        )

    def _account_url(self) -> str:
        return self.target.account_host + _ACCOUNT_BUDGET_PATH.format(
            account_id=urllib.parse.quote(self.target.account_id, safe=""),
            budget_id=urllib.parse.quote(self.budget_id, safe=""),
        )

    def _workspace_request(
        self, method: str, path: str, body: Mapping[str, Any] | None, operation: str
    ) -> object:
        token = self._workspace_token()
        return _request_json(
            method,
            self.workspace + path,
            token,
            body,
            operation,
        )

    def _account_request(
        self, method: str, url: str, body: Mapping[str, Any] | None, operation: str
    ) -> object:
        return _request_json(method, url, self.target.account_token, body, operation)

    def _workspace_token(self) -> str:
        env = getattr(self.session, "env", None)
        token = env.get("DATABRICKS_BEARER", "") if isinstance(env, Mapping) else ""
        if not isinstance(token, str) or not token.strip():
            raise BudgetDefaultsError("budget fixture requires session.env['DATABRICKS_BEARER']")
        return token.strip()

    def _require_workspace(self) -> None:
        if self.workspace != self.target_workspace:
            raise BudgetDefaultsError(
                f"budget fixture target is {self.target_workspace}; got {self.workspace or '<empty>'}"
            )

    def _record(self, kind: str, label: str, **fields: Any) -> None:
        record = getattr(self.session, "record", None)
        if not callable(record):
            return
        self._evidence_index += 1
        clean_fields = {
            key: (None if value is None else str(value))
            for key, value in fields.items()
            if key not in {"token", "env", "authorization"}
        }
        record(
            f"budget-defaults-{self._evidence_index:03d}-{kind}.json",
            {"kind": kind, "label": label, **clean_fields},
        )


def _target_for(budget: Mapping[str, Any]) -> _AlertTarget:
    alerts = budget.get("alert_configurations")
    if not isinstance(alerts, list):
        raise BudgetDefaultsError("account budget has no alert_configurations list")
    matches: list[_AlertTarget] = []
    for index, alert in enumerate(alerts):
        if not isinstance(alert, dict) or alert.get("scope_type") != _PER_USER_SCOPE:
            continue
        actions = alert.get("action_configurations")
        if not isinstance(actions, list):
            continue
        if not any(
            isinstance(action, dict) and action.get("action_type") == _BLOCK_USAGE
            for action in actions
        ):
            continue
        overrides = alert.get("principal_overrides")
        if isinstance(overrides, list) and overrides:
            raise BudgetDefaultsError("account budget has applicable principal_overrides")
        threshold = alert.get("quantity_threshold")
        parsed = _positive_decimal(threshold)
        if parsed is None:
            raise BudgetDefaultsError("per-user BLOCK_USAGE alert has no finite positive threshold")
        matches.append(_AlertTarget(index, str(threshold)))
    if len(matches) != 1:
        raise BudgetDefaultsError(
            f"account budget must have exactly one unambiguous per-user BLOCK_USAGE alert; found {len(matches)}"
        )
    return matches[0]


def _validate_budget(budget: Mapping[str, Any], target: BudgetTarget) -> None:
    if budget.get("budget_configuration_id") not in (None, target.budget_id):
        raise BudgetDefaultsError("account budget response has an unexpected budget id")
    if budget.get("account_id") is not None:
        if str(budget.get("account_id")) != target.account_id:
            raise BudgetDefaultsError("account budget belongs to a different account")
    owner_workspace = budget.get("workspace_id")
    if owner_workspace is not None and str(owner_workspace) != target.workspace_id:
        raise BudgetDefaultsError("account budget belongs to a different workspace")

    filters = budget.get("filter")
    workspace_filter = _as_dict(filters).get("workspace_id")
    workspace_clause = _as_dict(workspace_filter)
    values = workspace_clause.get("values")
    normalized_values = [str(value) for value in values] if isinstance(values, list) else []
    operator = workspace_clause.get("operator")
    if operator not in ("IN", "BUDGET_CONFIGURATION_FILTER_OPERATOR_IN"):
        raise BudgetDefaultsError("account budget has an unexpected workspace filter operator")
    if normalized_values != [target.workspace_id]:
        raise BudgetDefaultsError("account budget is not filtered to the expected workspace")

    resource = budget.get("resource_type")
    product = budget.get("product_type")
    if resource is not None:
        if str(resource).upper() not in _AIG_RESOURCE_TYPES:
            raise BudgetDefaultsError("account budget is not an AI Gateway resource budget")
    elif product is None or str(product).upper() not in _AIG_PRODUCT_TYPES:
        raise BudgetDefaultsError("account budget has no AI Gateway resource/product type")
    _target_for(budget)


def _request_json(
    method: str,
    url: str,
    token: str,
    body: Mapping[str, Any] | None,
    operation: str,
) -> object:
    try:
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        with _NO_REDIRECT_OPENER.open(request, timeout=30) as response:
            status = response.getcode()
            raw = response.read()
        if not 200 <= status < 300:
            raise BudgetDefaultsError(f"{operation} failed: HTTP {status}")
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))
    except BudgetDefaultsError:
        raise
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as error:
        raise _safe_error(operation, error) from None
    except Exception as error:
        # Do not stringify arbitrary exception text: urllib implementations can include request
        # headers or response bodies, either of which could contain a bearer.
        raise _safe_error(operation, error) from None


__all__ = [
    "BudgetTarget",
    "BudgetDefaults",
    "BudgetDefaultsError",
]
