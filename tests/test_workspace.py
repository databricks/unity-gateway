"""Tests for workspace.py: workspace URLs, keys, and multi-workspace host detection."""

from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib import error as urllib_error

import pytest

import ucode.workspace as workspace_mod
from ucode.workspace import (
    host_type,
    resolve_workspace,
    split_workspace_key,
    split_workspace_url,
    state_workspace_key,
    validate_workspace_id,
    workspace_key,
)

SHARED = "https://acme.cloud.databricks.com"
CLASSIC = "https://dbc-1234.cloud.databricks.com"


def _host_types(**types: str | None):
    return patch.object(workspace_mod, "host_type", side_effect=lambda host: types.get(host))


class TestSplitWorkspaceUrl:
    @pytest.mark.parametrize("param", ["w", "o", "workspace_id"])
    def test_reads_each_selector_form(self, param):
        parsed = split_workspace_url(f"{SHARED}/?{param}=123")
        assert (parsed.host, parsed.workspace_id, parsed.extra_parts) == (SHARED, "123", False)

    def test_bare_host_adds_scheme_and_strips_slash(self):
        parsed = split_workspace_url("acme.cloud.databricks.com/")
        assert (parsed.host, parsed.workspace_id, parsed.extra_parts) == (SHARED, None, False)

    def test_reports_path_query_and_fragment(self):
        assert split_workspace_url(f"{CLASSIC}/browse").extra_parts
        assert split_workspace_url(f"{CLASSIC}/?o=1&l=en").extra_parts
        assert split_workspace_url(f"{CLASSIC}#notebook").extra_parts

    def test_keeps_an_explicit_port(self):
        assert split_workspace_url("http://127.0.0.1:8080").host == "http://127.0.0.1:8080"

    def test_rejects_two_different_selectors(self):
        with pytest.raises(RuntimeError, match="more than one workspace"):
            split_workspace_url(f"{SHARED}/?w=1&o=2")

    def test_same_selector_twice_is_fine(self):
        assert split_workspace_url(f"{SHARED}/?w=1&o=1").workspace_id == "1"


class TestWorkspaceKey:
    def test_round_trip(self):
        key = workspace_key(SHARED, "123")
        assert key == f"{SHARED}?w=123"
        assert split_workspace_key(key) == (SHARED, "123")

    def test_uuid_key_round_trips(self):
        key = f"{SHARED}?w=1b4e28ba-2fa1-11d2-883f-0016d3cca427"
        assert split_workspace_key(key) == (SHARED, "1b4e28ba-2fa1-11d2-883f-0016d3cca427")

    def test_malformed_id_key_is_not_split(self):
        assert split_workspace_key(f"{SHARED}?w=abc") == (f"{SHARED}?w=abc", None)

    def test_bare_host_is_its_own_key(self):
        assert workspace_key(CLASSIC, None) == CLASSIC
        assert split_workspace_key(CLASSIC) == (CLASSIC, None)

    def test_non_canonical_query_is_not_split(self):
        assert split_workspace_key(f"{CLASSIC}/?o=1") == (f"{CLASSIC}/?o=1", None)

    def test_state_workspace_key(self):
        assert state_workspace_key({"workspace": SHARED, "workspace_id": "9"}) == f"{SHARED}?w=9"
        assert state_workspace_key({"workspace": CLASSIC}) == CLASSIC
        assert state_workspace_key({}) is None


class TestValidateWorkspaceId:
    def test_accepts_numeric_ids(self):
        assert validate_workspace_id(" 6051921418418893 ") == "6051921418418893"

    def test_rejects_disaster_recovery_uuid(self):
        with pytest.raises(RuntimeError, match="disaster-recovery"):
            validate_workspace_id("1b4e28ba-2fa1-11d2-883f-0016d3cca427")

    @pytest.mark.parametrize("value", ["abc", "9007199254740992", "-1", "\u00b2"])
    def test_rejects_other_values(self, value):
        with pytest.raises(RuntimeError, match="not a valid numeric workspace id"):
            validate_workspace_id(value)

    def test_largest_accepted_id(self):
        assert validate_workspace_id("9007199254740991") == "9007199254740991"


class TestHostType:
    def _response(self, payload: dict):
        return io.BytesIO(json.dumps(payload).encode())

    def test_reads_host_type_without_a_selector(self):
        with patch.object(
            workspace_mod.urllib_request,
            "urlopen",
            return_value=self._response({"host_type": "UNIFIED"}),
        ) as urlopen:
            assert host_type(SHARED) == "unified"
        request = urlopen.call_args.args[0]
        assert request.full_url == f"{SHARED}/.well-known/databricks-config"
        assert not any(name.lower().startswith("x-databricks") for name in request.headers)

    def test_cached_per_host(self):
        with patch.object(
            workspace_mod.urllib_request,
            "urlopen",
            return_value=self._response({"host_type": "workspace"}),
        ) as urlopen:
            host_type(CLASSIC)
            host_type(CLASSIC)
        assert urlopen.call_count == 1

    def test_unreachable_host_raises_and_is_not_cached(self):
        with patch.object(
            workspace_mod.urllib_request, "urlopen", side_effect=urllib_error.URLError("down")
        ):
            with pytest.raises(workspace_mod.HostUnreachableError, match="down"):
                host_type(CLASSIC)
            assert not workspace_mod.is_unified_host(CLASSIC)
        assert CLASSIC not in workspace_mod._host_types

    def test_bad_response_is_unreachable(self):
        with (
            patch.object(
                workspace_mod.urllib_request,
                "urlopen",
                side_effect=workspace_mod.http.client.IncompleteRead(b""),
            ),
            pytest.raises(workspace_mod.HostUnreachableError),
        ):
            host_type(CLASSIC)

    def test_server_error_is_unreachable(self):
        error = urllib_error.HTTPError(f"{SHARED}/x", 503, "Unavailable", None, None)  # type: ignore[arg-type]
        with (
            patch.object(workspace_mod.urllib_request, "urlopen", side_effect=error),
            pytest.raises(workspace_mod.HostUnreachableError, match="503"),
        ):
            host_type(SHARED)

    def test_host_without_the_document_is_a_single_workspace_host(self):
        error = urllib_error.HTTPError(f"{CLASSIC}/x", 404, "Not Found", None, None)  # type: ignore[arg-type]
        with patch.object(workspace_mod.urllib_request, "urlopen", side_effect=error):
            assert host_type(CLASSIC) is None


class TestResolveWorkspace:
    def test_bare_url_makes_no_request(self):
        with patch.object(workspace_mod, "host_type") as probe:
            assert resolve_workspace(CLASSIC) == CLASSIC
        probe.assert_not_called()

    def test_selector_on_multi_workspace_host_keeps_the_id(self):
        with _host_types(**{SHARED: "unified"}):
            assert resolve_workspace(f"{SHARED}/?o=123") == f"{SHARED}?w=123"

    def test_selector_on_single_workspace_host_keeps_the_saved_form(self, capsys):
        with _host_types(**{CLASSIC: "workspace"}):
            assert resolve_workspace(f"{CLASSIC}/?o=123") == f"{CLASSIC}/?o=123"
        assert "serves a single workspace" in " ".join(capsys.readouterr().out.split())

    def test_unreachable_host_with_an_id_fails(self):
        with (
            patch.object(
                workspace_mod, "host_type", side_effect=workspace_mod.HostUnreachableError("down")
            ),
            pytest.raises(RuntimeError, match="Could not check"),
        ):
            resolve_workspace(f"{SHARED}/?o=123")

    def test_host_without_host_type_is_treated_as_single_workspace(self):
        with _host_types():
            assert resolve_workspace(f"{CLASSIC}/?o=123") == f"{CLASSIC}/?o=123"

    def test_profile_id_on_single_workspace_host_is_dropped_silently(self, capsys):
        with _host_types(**{CLASSIC: "workspace"}):
            assert resolve_workspace(CLASSIC, "123", explicit_id=False) == CLASSIC
        assert capsys.readouterr().out == ""

    def test_profile_id_on_multi_workspace_host(self):
        with _host_types(**{SHARED: "unified"}):
            assert resolve_workspace(SHARED, "123", explicit_id=False) == f"{SHARED}?w=123"

    def test_extra_parts_on_single_workspace_host_are_kept(self, capsys):
        assert resolve_workspace(f"{CLASSIC}/browse") == f"{CLASSIC}/browse"
        assert capsys.readouterr().out == ""

    def test_extra_parts_on_multi_workspace_host_are_dropped_with_a_note(self, capsys):
        with _host_types(**{SHARED: "unified"}):
            assert resolve_workspace(f"{SHARED}/browse?o=123") == f"{SHARED}?w=123"
        assert "not part of the workspace URL" in " ".join(capsys.readouterr().out.split())

    def test_conflicting_ids(self):
        with pytest.raises(RuntimeError, match="conflicts"):
            resolve_workspace(f"{SHARED}/?w=1", "2")

    def test_uuid_on_multi_workspace_host_is_refused(self):
        with _host_types(**{SHARED: "unified"}), pytest.raises(RuntimeError, match="disaster"):
            resolve_workspace(f"{SHARED}/?w=1b4e28ba-2fa1-11d2-883f-0016d3cca427")
