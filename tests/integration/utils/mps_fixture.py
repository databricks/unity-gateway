"""Stable Unity Catalog Model Provider Service fixtures for live CUJs."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Literal

_MODEL_PROVIDER_SERVICES_PATH = "/api/2.1/unity-catalog/model-provider-services"
_UNITY_CATALOG_PATH = "/api/2.1/unity-catalog"
_REQUEST_TIMEOUT_SECONDS = 30
_SCHEMA_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\Z")
_DUMMY_API_KEYS = {
    "anthropic": "dummy-anthropic-api-key",
    "openai": "dummy-openai-api-key",
}
_STABLE_LEAVES = {
    "anthropic": "ug_4882_claude_anthropic_all",
    "openai": "ug_4882_codex_openai",
}

ProviderKind = Literal["anthropic", "openai"]


class MpsFixtureError(AssertionError):
    """A bounded MPS fixture request or validation failed."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class MpsProvider:
    """The safe provider inputs a test passes to ug."""

    provider: str
    model: str
    allow_all_targets: bool = False


class MpsFixture:
    """Get or create stable direct Anthropic/OpenAI MPS fixtures.

    An empty schema deliberately leaves the existing runner-selected provider untouched.
    Configured resources use stable names and remain in the test schema so repeated runs can
    reuse them without requiring provider keys. Existing resources are validated before reuse;
    this helper never updates or deletes a resource owned by the integration schema.
    """

    def __init__(self, workspace: str, schema: str | None, bearer: str):
        self._workspace = workspace.rstrip("/")
        self._schema = schema.strip() if schema else ""
        self._bearer = bearer
        if self._schema and not _SCHEMA_PATTERN.fullmatch(self._schema):
            raise MpsFixtureError(
                "UG_INTEGRATION_MPS_FIXTURE_SCHEMA must be a catalog.schema identifier."
            )
        if self._schema and not self._bearer:
            raise MpsFixtureError("MPS fixture provisioning requires DATABRICKS_BEARER.")

    def provider_for(
        self,
        kind: ProviderKind,
        *,
        fallback_provider: str,
        model: str,
    ) -> MpsProvider:
        """Return the configured provider, creating it only when the stable MPS is absent."""
        if not self._schema:
            return MpsProvider(provider=fallback_provider, model=model)
        if kind not in _STABLE_LEAVES:
            raise MpsFixtureError(f"Unsupported MPS fixture provider kind: {kind!r}")
        if not model.strip():
            raise MpsFixtureError(
                f"UG_INTEGRATION_{kind.upper()}_PROVIDER_MODEL is required for MPS provisioning."
            )

        catalog, schema = self._schema.split(".", maxsplit=1)
        leaf = _STABLE_LEAVES[kind]
        fqn = f"{catalog}.{schema}.{leaf}"
        resource = f"model-provider-services/{fqn}"
        try:
            existing = self._request("GET", self._resource_url(resource))
        except MpsFixtureError as error:
            if error.status != 404:
                raise
            api_key = _DUMMY_API_KEYS[kind]
            try:
                self._request(
                    "POST",
                    self._create_url(catalog, schema, leaf),
                    body=self._body(kind, model, api_key),
                    redactions=(api_key,),
                )
            except MpsFixtureError as create_error:
                if create_error.status != 409:
                    raise
                # Another runner may have created the stable fixture between our GET and POST.
                # Read the authoritative resource and validate it instead of updating it.
            existing = self._request("GET", self._resource_url(resource))
            self._validate_resource(existing, resource, kind, model)
        else:
            self._validate_resource(existing, resource, kind, model)
        return MpsProvider(provider=fqn, model=model, allow_all_targets=kind == "anthropic")

    def _create_url(self, catalog: str, schema: str, leaf: str) -> str:
        query = urllib.parse.urlencode(
            {
                "parent": f"schemas/{catalog}.{schema}",
                "model_provider_service_id": leaf,
            }
        )
        return f"{self._workspace}{_MODEL_PROVIDER_SERVICES_PATH}?{query}"

    def _resource_url(self, resource: str) -> str:
        escaped = urllib.parse.quote(resource, safe="/.")
        return f"{self._workspace}{_UNITY_CATALOG_PATH}/{escaped}"

    @staticmethod
    def _body(kind: ProviderKind, model: str, api_key: str) -> dict:
        if kind == "anthropic":
            provider_type = "EXTERNAL_MODEL_PROVIDER_TYPE_ANTHROPIC"
            provider = {"anthropic": {"direct": {"apiKey": {"plaintext": api_key}}}}
            native_api_type = "anthropic/v1/messages"
        else:
            provider_type = "EXTERNAL_MODEL_PROVIDER_TYPE_OPENAI"
            provider = {"openai": {"direct": {"apiKey": {"plaintext": api_key}}}}
            native_api_type = "openai/v1/responses"
        return {
            "config": {
                "providerType": provider_type,
                **provider,
                "allowAllTargets": kind == "anthropic",
                "targets": [{"model": model, "nativeApiTypes": [native_api_type]}],
            },
            "comment": "Unity Gateway AIGTWY-4882 integration MPS",
        }

    @staticmethod
    def _value(mapping: dict, snake: str, camel: str, default=None):
        if snake in mapping:
            return mapping[snake]
        return mapping.get(camel, default)

    def _validate_resource(
        self, payload: dict, resource: str, kind: ProviderKind, model: str
    ) -> None:
        if not isinstance(payload, dict):
            raise MpsFixtureError(f"MPS response for {resource} was not an object.")
        if payload.get("name") != resource:
            raise MpsFixtureError(
                f"MPS response returned an unexpected resource name: {payload.get('name')!r}"
            )
        config = payload.get("config")
        if not isinstance(config, dict):
            raise MpsFixtureError(f"MPS {resource} has no provider config.")

        expected_type = (
            "EXTERNAL_MODEL_PROVIDER_TYPE_ANTHROPIC"
            if kind == "anthropic"
            else "EXTERNAL_MODEL_PROVIDER_TYPE_OPENAI"
        )
        provider_type = self._value(config, "provider_type", "providerType")
        if provider_type != expected_type:
            raise MpsFixtureError(
                f"MPS {resource} has provider type {provider_type!r}; expected {expected_type!r}."
            )
        provider_config = config.get(kind)
        if not isinstance(provider_config, dict) or not isinstance(
            provider_config.get("direct"), dict
        ):
            raise MpsFixtureError(f"MPS {resource} is not a direct {kind} provider.")
        allow_all_targets = self._value(config, "allow_all_targets", "allowAllTargets", False)
        if allow_all_targets is not (kind == "anthropic"):
            raise MpsFixtureError(
                f"MPS {resource} has allow_all_targets={allow_all_targets!r}; "
                f"expected {kind == 'anthropic'}."
            )

        targets = config.get("targets")
        if not isinstance(targets, list):
            raise MpsFixtureError(f"MPS {resource} has no target catalog.")
        native_api_type = "anthropic/v1/messages" if kind == "anthropic" else "openai/v1/responses"
        matching = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            if self._value(target, "model", "model") != model:
                continue
            native_api_types = self._value(target, "native_api_types", "nativeApiTypes", [])
            if isinstance(native_api_types, list):
                matching.extend(native_api_types)
        if native_api_type not in matching:
            raise MpsFixtureError(
                f"MPS {resource} does not expose model {model!r} with native API "
                f"{native_api_type!r}."
            )

    def _request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        *,
        body: dict | None = None,
        redactions: tuple[str, ...] = (),
    ) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._bearer}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw_body = response.read(16 * 1024)
        except urllib.error.HTTPError as error:
            raw_body = error.read(16 * 1024)
            detail = _redacted_preview(raw_body, redactions)
            raise MpsFixtureError(
                f"{method} {url} returned HTTP {error.code}: {detail}", status=error.code
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise MpsFixtureError(f"{method} {url} failed: {error}") from error

        if status < 200 or status >= 300:
            detail = _redacted_preview(raw_body, redactions)
            raise MpsFixtureError(f"{method} {url} returned HTTP {status}: {detail}", status=status)
        if not raw_body:
            return {}
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MpsFixtureError(f"{method} {url} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise MpsFixtureError(f"{method} {url} returned a non-object JSON response")
        return payload


def _redacted_preview(raw_body: bytes, redactions: tuple[str, ...]) -> str:
    """Return bounded diagnostics with provider credentials removed."""
    preview = raw_body.decode("utf-8", errors="replace")
    for secret in redactions:
        if secret:
            preview = preview.replace(secret, "<redacted>")
    return preview[:1000]
