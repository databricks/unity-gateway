"""Offline fault injection for workspace ownership/restoration; never imported by live tests."""

import base64
import copy

import pytest

from tests.e2e_integration.helpers.workspace import ApiError, Workspace


@pytest.fixture
def workspace(monkeypatch):
    artifacts, calls = {}, []
    w = Workspace(
        "https://example.test", "secret", lambda name, value: artifacts.update({name: value})
    )
    config = {
        "name": "coding-agent-configs/test",
        "spec_version": 1,
        "enabled_agents": [
            {"agent": agent, "config": {"smart_routing": {"enabled": True}}}
            for agent in ("CODING_AGENT_CLAUDE_CODE", "CODING_AGENT_CODEX")
        ],
    }
    state = {"config": config, "claim": None}

    def request(method, path, body=None):
        calls.append((method, path, body))
        if path.endswith("/import"):
            if state["claim"]:
                raise ApiError(400, "RESOURCE_ALREADY_EXISTS")
            assert body["overwrite"] is False
            state["claim"] = body["content"]
        elif "/export?" in path:
            if state["claim"] is None:
                raise ApiError(404, "RESOURCE_DOES_NOT_EXIST")
            return {"content": state["claim"]}
        elif path.endswith("/delete"):
            assert body == {"path": w.RESERVATION, "recursive": False}
            state["claim"] = None
        elif method == "PATCH":
            assert path.endswith("?update_mask=enabled_agents")
            state["config"]["enabled_agents"] = copy.deepcopy(body["enabled_agents"])
        else:
            assert method == "GET" and path.endswith("/coding-agent-configs")
            return {"coding_agent_configs": [copy.deepcopy(state["config"])]}
        return {}

    monkeypatch.setattr(w, "request", request)
    return w, state, artifacts, calls


@pytest.mark.parametrize("task_failed", [True, False])
def test_cuj_workspace_restores_both_flags_and_releases(workspace, task_failed):
    w, state, artifacts, calls = workspace
    original = copy.deepcopy(state["config"])
    try:
        with w.reserved():
            w.disable_routing()
            assert all(
                not row["config"]["smart_routing"]["enabled"]
                for row in state["config"]["enabled_agents"]
            )
            if task_failed:
                raise ValueError("file task failed")
    except ValueError:
        assert task_failed
    assert state["config"] == original
    assert state["claim"] is None
    assert artifacts["workspace"]["state"] == "released"
    assert len([c for c in calls if c[0] == "PATCH"]) == 2


def test_cuj_workspace_contention_does_not_touch_policy(workspace):
    w, state, _, calls = workspace
    state["claim"] = "another runner"
    with pytest.raises(RuntimeError, match="already assigned"):
        with w.reserved():
            pytest.fail("Must fail before entering the CUJ")
    assert len(calls) == 1
    assert state["claim"] == "another runner"


@pytest.mark.parametrize("conflict", ["config", "ownership", "local_cleanup"])
def test_cuj_workspace_quarantines_unverified_cleanup(workspace, conflict):
    w, state, artifacts, calls = workspace
    with pytest.raises(AssertionError):
        with w.reserved():
            if conflict == "config":
                state["config"]["spec_version"] = 99
            elif conflict == "ownership":
                state["claim"] = base64.b64encode(b"someone else").decode()
            else:
                w.quarantined = True
    assert state["claim"] is not None
    assert artifacts["workspace"]["state"] == "QUARANTINED"
    assert not any(c[0] == "PATCH" or c[1].endswith("/delete") for c in calls)


def test_cuj_workspace_ambiguous_publication_restores_but_keeps_reservation(workspace, monkeypatch):
    w, state, artifacts, _ = workspace
    original = copy.deepcopy(state["config"])
    request = w.request
    failed = False

    def lost_response(method, path, body=None):
        nonlocal failed
        result = request(method, path, body)
        if method == "PATCH" and not failed:
            failed = True
            raise TimeoutError("response lost after commit")
        return result

    monkeypatch.setattr(w, "request", lost_response)
    with pytest.raises(AssertionError, match="quarantined"):
        with w.reserved():
            w.disable_routing()
    assert state["config"] == original
    assert state["claim"] is not None
    assert artifacts["workspace"]["state"] == "QUARANTINED"


def test_cuj_workspace_read_only_policy_ignores_server_timestamps():
    assert Workspace.policy({"name": "x", "update_time": "today", "enabled_agents": []}) == {
        "name": "x",
        "enabled_agents": [],
    }


def test_cuj_workspace_release_does_not_confuse_a_new_owner(workspace, monkeypatch):
    w, state, artifacts, calls = workspace
    request = w.request
    next_claim = base64.b64encode(b"next runner").decode()

    def race(method, path, body=None):
        result = request(method, path, body)
        if path.endswith("/delete"):
            state["claim"] = next_claim
        return result

    monkeypatch.setattr(w, "request", race)
    with w.reserved():
        pass
    assert artifacts["workspace"]["state"] == "released"
    assert state["claim"] == next_claim
    assert len([c for c in calls if c[1].endswith("/delete")]) == 1
