"""Public workspace APIs and an exclusive, fail-fast workspace reservation.

The reservation protects cooperating CUJ runners, not arbitrary administrators.
The config API has no compare-and-swap: use a dedicated test workspace with no
other writers. Unexpected edits quarantine the reservation rather than overwrite.
"""

import base64
import copy
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from uuid import uuid4


class ApiError(RuntimeError):
    def __init__(self, status, code):
        super().__init__(f"Workspace API failed: HTTP {status}, {code}")
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Workspace API redirected; refusing to forward credentials")


class Workspace:
    RESERVATION = "/Shared/ug-e2e-workspace-reservation"
    OUTPUT_FIELDS = {
        "workspace_id",
        "create_time",
        "created_user_id",
        "update_time",
        "updated_user_id",
        "retrieved_time",
    }

    def __init__(self, url, bearer, record):
        self.url = url
        self._bearer = bearer
        self.record = record
        self.owner = uuid4().hex
        self.claim = f"# Databricks notebook source\n# ug-e2e-owner: {self.owner}\n"
        self.original = None
        self.expected = None
        self.pending = None
        self.quarantined = False

    def request(self, method, path, body=None):
        assert path.startswith("/") and not path.startswith("//")
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._bearer}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as error:
            # Server messages may echo credentials; retain only status/code.
            try:
                code = json.loads(error.read()).get("error_code", "UNKNOWN")
            except (ValueError, AttributeError):
                code = "UNKNOWN"
            raise ApiError(error.code, code) from None

    @classmethod
    def policy(cls, config):
        return {key: value for key, value in config.items() if key not in cls.OUTPUT_FIELDS}

    def config(self):
        result = self.request("GET", "/api/ai-gateway/v2/coding-agent-configs")
        configs = result.get("coding_agent_configs", [])
        assert len(configs) == 1 and not result.get("next_page_token"), (
            "CUJ requires exactly one published CodingAgentConfig"
        )
        config = self.policy(configs[0])
        assert re.fullmatch(r"coding-agent-configs/[\w-]+", config.get("name", ""))
        return config

    def assert_owned(self):
        assert self.claim_content() == self.claim, (
            "Workspace reservation changed; refusing mutation or release"
        )

    def claim_content(self):
        query = urllib.parse.urlencode({"path": self.RESERVATION, "format": "SOURCE"})
        result = self.request("GET", "/api/2.0/workspace/export?" + query)
        return base64.b64decode(result["content"]).decode()

    def assert_unchanged(self):
        self.assert_owned()
        assert self.config() == self.expected, "Published policy changed outside this CUJ"

    def wait_visible(self, expected, previous):
        deadline = time.monotonic() + 60
        while True:
            actual = self.config()
            if actual == expected:
                return
            assert actual == previous, "Unexpected admin edit; refusing to overwrite policy"
            assert time.monotonic() < deadline, "Published config did not become visible"
            time.sleep(0.5)

    def publish_agents(self, agents):
        self.assert_unchanged()
        target = {**self.expected, "enabled_agents": copy.deepcopy(agents)}
        self.pending = target  # A timed-out PATCH may still have committed.
        self.request(
            "PATCH",
            f"/api/ai-gateway/v2/{target['name']}?update_mask=enabled_agents",
            {"name": target["name"], "enabled_agents": agents},
        )
        self.wait_visible(target, self.expected)
        self.expected, self.pending = target, None

    def disable_routing(self):
        agents = copy.deepcopy(self.expected["enabled_agents"])
        for agent in agents:
            agent["config"]["smart_routing"]["enabled"] = False
        self.publish_agents(agents)

    def restore(self):
        self.assert_owned()
        if self.original is None:
            return
        actual = self.config()
        assert actual in (self.original, self.expected, self.pending), (
            "Unexpected policy; reservation quarantined. Restore manually after investigating."
        )
        if actual != self.original:
            self.expected = actual
            self.publish_agents(self.original["enabled_agents"])
        self.wait_visible(self.original, self.original)

    @contextmanager
    def reserved(self):
        self.record(
            "workspace",
            {
                "workspace": self.url,
                "owner": self.owner,
                "reservation": self.RESERVATION,
                "state": "claiming",
            },
        )
        # Fixed workspace-wide path, create-only. Never wait, steal, or use a TTL.
        try:
            self.request(
                "POST",
                "/api/2.0/workspace/import",
                {
                    "path": self.RESERVATION,
                    "format": "SOURCE",
                    "language": "PYTHON",
                    "overwrite": False,
                    "content": base64.b64encode(self.claim.encode()).decode(),
                },
            )
        except ApiError as error:
            if error.code == "RESOURCE_ALREADY_EXISTS":
                raise RuntimeError(
                    f"Workspace already assigned/quarantined at {self.RESERVATION}; "
                    "use a separately assigned workspace or resolve its owner. No task was started."
                ) from None
            raise
        clean = False
        try:
            self.assert_owned()
            self.original = self.expected = self.config()
            self.record(
                "workspace",
                {
                    "workspace": self.url,
                    "owner": self.owner,
                    "reservation": self.RESERVATION,
                    "state": "reserved",
                },
            )
            yield self
        finally:
            try:
                # Ambiguous writes can still commit after a timeout: never recycle.
                self.quarantined = self.quarantined or self.pending is not None
                self.restore()
                assert not self.quarantined, (
                    f"Workspace quarantined at {self.RESERVATION}; verify cleanup manually"
                )
                self.assert_owned()
                self.request(
                    "POST",
                    "/api/2.0/workspace/delete",
                    {
                        "path": self.RESERVATION,
                        "recursive": False,
                    },
                )
                try:
                    remaining = self.claim_content()
                except ApiError as error:
                    if error.code != "RESOURCE_DOES_NOT_EXIST":
                        raise
                else:
                    # Another runner can claim immediately after our deletion.
                    # Verify OUR claim is gone without touching that new owner.
                    assert remaining != self.claim, "Reservation deletion was not verified"
                clean = True
            finally:
                self.record(
                    "workspace",
                    {
                        "workspace": self.url,
                        "owner": self.owner,
                        "reservation": self.RESERVATION,
                        "state": "released" if clean else "QUARANTINED",
                    },
                )

    def model_ids(self, agent):
        from .evidence import canonical_model

        models, seen, token = set(), set(), None
        for _ in range(100):
            params = {"parent": "schemas/system.ai", "page_size": "100"}
            if token:
                params["page_token"] = token
            result = self.request(
                "GET", "/api/2.1/unity-catalog/model-services?" + urllib.parse.urlencode(params)
            )
            models.update(
                row["name"].removeprefix("model-services/")
                for row in result.get("model_services", [])
            )
            token = result.get("next_page_token")
            if not token:
                break
            assert token not in seen, "Catalog pagination repeated a token"
            seen.add(token)
        else:
            raise AssertionError("Catalog pagination exceeded 100 pages")
        prefix = "system.ai.claude-" if agent == "claude" else "system.ai.gpt-"
        models = {model for model in models if model.startswith(prefix)}
        if agent == "claude":
            result = self.request("GET", "/ai-gateway/anthropic/v1/models?limit=1000")
            assert not result.get("has_more"), "Anthropic catalog pagination needs implementation"
            models &= {canonical_model(row["id"]) for row in result["data"]}
        assert models, "Live catalog is empty"
        return models
