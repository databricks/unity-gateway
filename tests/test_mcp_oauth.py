"""Tests for OAuth-client discovery + per-workspace caching (mcp_oauth).

Network-free: the token-endpoint probe is monkeypatched; these cover the
known/unknown/error classification and the cache behaviour.
"""

from __future__ import annotations

import io
import json
import urllib.error

from ucode import mcp_oauth

WS = "https://ws.staging.cloud.databricks.com"
TEST_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"


def test_workspace_cimd_metadata_controls_agent_selected_client(monkeypatch):
    metadata = {
        "client_id_metadata_document_supported": True,
        "token_endpoint_auth_methods_supported": ["none"],
    }
    monkeypatch.setattr(
        mcp_oauth.urllib.request,
        "urlopen",
        lambda req, timeout=0: io.BytesIO(json.dumps(metadata).encode()),
    )
    assert mcp_oauth.workspace_supports_cimd(WS)
    metadata["client_id_metadata_document_supported"] = False
    assert not mcp_oauth.workspace_supports_cimd(WS)
    metadata["client_id_metadata_document_supported"] = True
    metadata["token_endpoint_auth_methods_supported"] = ["client_secret_basic"]
    assert not mcp_oauth.workspace_supports_cimd(WS)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="x", code=code, msg="m", hdrs=None, fp=None)


class TestProbeOauthClient:
    def test_known_client_400_is_available(self, monkeypatch):
        # 400 invalid_request ("Invalid authorization code") => client is registered.
        monkeypatch.setattr(
            mcp_oauth.urllib.request,
            "urlopen",
            lambda req, timeout=0: (_ for _ in ()).throw(_http_error(400)),
        )
        assert mcp_oauth._probe_oauth_client(WS, TEST_CLIENT_ID) is True

    def test_unknown_client_401_is_absent(self, monkeypatch):
        # 401 invalid_client => client not registered on this workspace.
        monkeypatch.setattr(
            mcp_oauth.urllib.request,
            "urlopen",
            lambda req, timeout=0: (_ for _ in ()).throw(_http_error(401)),
        )
        assert mcp_oauth._probe_oauth_client(WS, TEST_CLIENT_ID) is False

    def test_network_error_is_inconclusive(self, monkeypatch):
        # A network failure is inconclusive (None) — the caller falls back to the proxy.
        monkeypatch.setattr(
            mcp_oauth.urllib.request,
            "urlopen",
            lambda req, timeout=0: (_ for _ in ()).throw(OSError("down")),
        )
        assert mcp_oauth._probe_oauth_client(WS, TEST_CLIENT_ID) is None

    def test_transient_status_is_inconclusive(self, monkeypatch):
        # 429/5xx (rate limit / incident), 404, redirects must NOT be read as "registered".
        for code in (429, 500, 503, 404):
            monkeypatch.setattr(
                mcp_oauth.urllib.request,
                "urlopen",
                lambda req, timeout=0, c=code: (_ for _ in ()).throw(_http_error(c)),
            )
            assert mcp_oauth._probe_oauth_client(WS, TEST_CLIENT_ID) is None

    def test_unexpected_success_is_inconclusive(self, monkeypatch):
        # A 2xx for a dummy code is unexpected; don't conclude "registered".
        monkeypatch.setattr(mcp_oauth.urllib.request, "urlopen", lambda req, timeout=0: object())
        assert mcp_oauth._probe_oauth_client(WS, TEST_CLIENT_ID) is None


class TestOauthClientAvailableCache:
    def test_probes_once_then_serves_from_cache(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_oauth, "_CACHE_PATH", tmp_path / "cache.json")
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            mcp_oauth, "_probe_oauth_client", lambda ws, cid: calls.append((ws, cid)) or True
        )
        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is True
        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is True
        assert len(calls) == 1  # second call served from cache, no re-probe

    def test_negative_result_is_reprobed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_oauth, "_CACHE_PATH", tmp_path / "cache.json")
        calls: list[int] = []
        monkeypatch.setattr(
            mcp_oauth, "_probe_oauth_client", lambda ws, cid: calls.append(1) or False
        )
        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is False
        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is False
        assert len(calls) == 2
        assert not (tmp_path / "cache.json").exists()

    def test_old_negative_cache_entry_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_oauth, "_CACHE_PATH", tmp_path / "cache.json")
        (tmp_path / "cache.json").write_text(
            '{"https://ws.staging.cloud.databricks.com": '
            '{"new-client": {"available": false, "checked_at": 9999999999}}}'
        )
        monkeypatch.setattr(mcp_oauth, "_probe_oauth_client", lambda ws, cid: True)

        assert mcp_oauth.oauth_client_available(WS, "new-client") is True

    def test_newly_published_client_is_available_on_next_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_oauth, "_CACHE_PATH", tmp_path / "cache.json")
        results = iter((False, True))
        monkeypatch.setattr(mcp_oauth, "_probe_oauth_client", lambda ws, cid: next(results))

        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is False
        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is True
        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is True

    def test_inconclusive_probe_falls_back_and_is_not_cached(self, monkeypatch, tmp_path):
        # An inconclusive probe (None) => oauth_client_available returns False (proxy),
        # and nothing is cached, so a transient failure isn't sticky for the TTL.
        monkeypatch.setattr(mcp_oauth, "_CACHE_PATH", tmp_path / "cache.json")
        calls: list[int] = []
        monkeypatch.setattr(
            mcp_oauth, "_probe_oauth_client", lambda ws, cid: calls.append(1) or None
        )
        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is False
        assert mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID) is False
        assert len(calls) == 2  # not cached -> re-probed each time

    def test_expired_entry_reprobes(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_oauth, "_CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr(mcp_oauth, "_CACHE_TTL_SECONDS", -1)  # every entry immediately stale
        calls: list[int] = []
        monkeypatch.setattr(
            mcp_oauth, "_probe_oauth_client", lambda ws, cid: calls.append(1) or True
        )
        mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID)
        mcp_oauth.oauth_client_available(WS, TEST_CLIENT_ID)
        assert len(calls) == 2  # re-probed because the cached entry expired
