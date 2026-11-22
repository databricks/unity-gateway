"""Unit tests for the opt-in launch cache (``UG_LAUNCH_CACHE=1``)."""

from __future__ import annotations

import base64
import io
import json
import sys
import time
from contextlib import contextmanager
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ucode import cli as cli_mod
from ucode import databricks as db_mod
from ucode import launch_cache as lc
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
)

runner = CliRunner()

WORKSPACE = "https://example.databricks.com"
STATE = {
    "workspace": WORKSPACE,
    "profile": "p",
    "workspace_id": "123",
    "available_tools": ["claude"],
    "claude_models": {"sonnet": "databricks-claude-sonnet-4"},
}

SERVICES = [db_mod.ModelServiceEntry("system.ai.claude-sonnet-5", ("chat",))]
PROBE = db_mod.GatewayProbe(True, "reachable, accessible model service returned", True)
CATALOG = db_mod.AnthropicModelCatalog(
    model_ids=["system.ai.claude-sonnet-5"],
    model_id_to_display_name={"system.ai.claude-sonnet-5": "Claude Sonnet 5"},
)


class _Response(io.BytesIO):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.status = 200
        self.headers = Message()

    def geturl(self):
        return WORKSPACE


@pytest.fixture(autouse=True)
def _launch_cache_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UG_LAUNCH_CACHE", "1")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "databrickscfg"))


def _identity(**overrides):
    identity = {
        "workspace": WORKSPACE,
        "workspace_id": "123",
        "profile": "p",
        "use_pat": False,
        "custom_oauth": None,
        "config_fingerprint": lc.config_fingerprint(),
        "ug_version": lc.ug_version(),
    }
    identity.update(overrides)
    return identity


def refresh_args(identity=None):
    encoded = base64.urlsafe_b64encode(json.dumps(identity or _identity()).encode()).decode()
    return ["--refresh", "--identity", encoded]


def _key(**kwargs) -> str:
    return lc.cache_key(_identity(**kwargs))


def _store(key: str, payload: dict, *, checked_at: float | None = None, identity=None) -> None:
    checked_at = time.time() if checked_at is None else checked_at
    entry = {
        **payload,
        "version": lc.ENTRY_VERSION,
        "key": key,
        "identity": identity or _identity(),
        "checked_at": checked_at,
        "generation": lc._read_generation(),
        **{f"{part}_checked_at": checked_at for part in payload},
    }
    lc._cache_dir().mkdir(parents=True, exist_ok=True)
    (lc._cache_dir() / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")


class TestKey:
    def test_workspace_profile_and_id_isolate(self):
        keys = {
            _key(),
            _key(workspace="https://other.databricks.com"),
            _key(profile="other"),
            _key(workspace_id="456"),
        }
        assert len(keys) == 4

    def test_credentials_change_rotates(self, tmp_path, monkeypatch):
        cfg = tmp_path / "databrickscfg"
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
        before = lc.cache_key(lc.entry_identity({"workspace": WORKSPACE}))
        cfg.write_text("[p]\nhost = x\n")
        after = lc.cache_key(lc.entry_identity({"workspace": WORKSPACE}))
        assert after != before

    def test_ug_version_rotates(self):
        before = lc.cache_key(_identity(ug_version="1"))
        assert lc.cache_key(_identity(ug_version="2")) != before

    def test_auth_mode_and_oauth_isolate(self):
        keys = {
            _key(),
            _key(use_pat=True),
            _key(custom_oauth={"client_id": "c"}),
        }
        assert len(keys) == 3


class TestEntry:
    def test_roundtrip_and_parts(self):
        key = _key()
        lc.merge_entry(
            key,
            _identity(),
            {
                "probe": lc.probe_payload(PROBE),
                "model_services": lc.services_payload(SERVICES),
                "catalog": lc.catalog_payload(CATALOG),
            },
        )
        entry = lc.load_entry(key, _identity())
        assert entry is not None
        assert lc.entry_probe(entry) == PROBE
        assert lc.entry_model_services(entry) == SERVICES
        catalog = lc.entry_catalog(entry)
        assert catalog is not None
        assert catalog.model_ids == CATALOG.model_ids
        assert catalog.model_id_to_display_name == CATALOG.model_id_to_display_name

    def test_stale_entry_not_served(self):
        key = _key()
        _store(key, {}, checked_at=time.time() - lc.MAX_AGE_SECONDS - 1)
        assert lc.load_entry(key) is None

    def test_corrupt_entry_not_served(self):
        key = _key()
        lc._cache_dir().mkdir(parents=True, exist_ok=True)
        (lc._cache_dir() / f"{key}.json").write_text("{not json", encoding="utf-8")
        assert lc.load_entry(key) is None

    def test_entry_for_another_key_or_identity_not_served(self):
        key = _key()
        _store(key, {}, checked_at=time.time())
        other = lc.cache_key(_identity(workspace="https://other.databricks.com"))
        entry = json.loads((lc._cache_dir() / f"{key}.json").read_text(encoding="utf-8"))
        entry["key"] = other
        (lc._cache_dir() / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")
        assert lc.load_entry(other) is None
        assert lc.load_entry(key) is None
        # A matching key with a foreign identity must not serve either.
        entry = json.loads((lc._cache_dir() / f"{key}.json").read_text(encoding="utf-8"))
        entry["identity"]["profile"] = "someone-else"
        (lc._cache_dir() / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")
        assert lc.load_entry(key, _identity()) is None

    def test_failed_probe_never_served(self):
        key = _key()
        _store(
            key,
            {"probe": {"reachable": False, "detail": "HTTP 503", "resource_available": False}},
            checked_at=time.time(),
        )
        entry = lc.load_entry(key)
        assert entry is not None
        assert lc.entry_probe(entry) is None

    def test_merging_one_part_leaves_the_others_ages_alone(self):
        key = _key()
        _store(
            key,
            {
                "probe": lc.probe_payload(PROBE),
                "model_services": lc.services_payload(SERVICES),
                "catalog": lc.catalog_payload(CATALOG),
            },
        )
        # Age only the probe and services parts, as a catalog-only merge leaves them.
        old = time.time() - lc.MAX_AGE_SECONDS - 1
        entry = lc._read_entry(key)
        entry["probe_checked_at"] = old
        entry["model_services_checked_at"] = old
        (lc._cache_dir() / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")
        lc.merge_entry(key, _identity(), {"catalog": lc.catalog_payload(CATALOG)})
        entry = lc.load_entry(key, _identity())
        assert entry is not None
        assert lc.entry_catalog(entry) is not None
        assert lc.entry_probe(entry) is None
        assert lc.entry_model_services(entry) is None

    def test_parts_without_an_age_read_as_stale(self):
        key = _key()
        _store(key, {"probe": lc.probe_payload(PROBE)})
        entry = lc._read_entry(key)
        del entry["probe_checked_at"]
        (lc._cache_dir() / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")
        loaded = lc.load_entry(key, _identity())
        assert loaded is not None
        assert lc.entry_probe(loaded) is None

    def test_an_entry_written_after_a_clear_is_dead_on_read(self):
        key = _key()
        _store(key, {}, checked_at=time.time())
        lc.clear_cache()
        assert lc.load_entry(key) is None
        # A pre-clear refresher's write lands with the old generation: dead on read,
        # whatever order its write landed in against the clear's unlink.
        lc.merge_entry(key, _identity(), {"catalog": lc.catalog_payload(CATALOG)}, generation=0)
        assert lc.load_entry(key) is None
        assert lc.entry_catalog(lc._read_entry(key) or {}) is None
        # A write under the current generation is alive.
        lc.merge_entry(
            key,
            _identity(),
            {"catalog": lc.catalog_payload(CATALOG)},
            generation=lc._read_generation(),
        )
        assert lc.load_entry(key) is not None

    def test_clear_cache_removes_every_entry(self):
        lc.merge_entry(_key(), _identity(), {})
        lc.merge_entry(
            _key(workspace="https://other.databricks.com"),
            _identity(workspace="https://other.databricks.com"),
            {},
        )
        lock = lc._cache_dir() / f"{_key()}.lock"
        lock.touch()
        lc.clear_cache()
        assert lc.load_entry(_key()) is None
        assert not list(lc._cache_dir().glob("*.json"))
        assert not lock.exists()

    def test_clear_cache_leaves_a_held_lock_and_prunes_dead_ones(self):
        key = _key()
        lc.merge_entry(key, _identity(), {})
        lock_path = lc._cache_dir() / f"{key}.lock"
        with lock_path.open("a+b") as lock_file:
            acquire_exclusive_file_lock(lock_file)
            lc.clear_cache()
            # A live refresher holds this key; pruning the lock would let a second
            # refresher for it run alongside.
            assert lock_path.exists()
        lc.clear_cache()
        assert not lock_path.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_cache_dir_and_entries_are_private(self):
        key = _key()
        lc.merge_entry(key, _identity(), {})
        assert (lc._cache_dir().stat().st_mode & 0o777) == 0o700
        assert (lc._entry_path(key).stat().st_mode & 0o777) == 0o600


class TestRefresher:
    @pytest.fixture(autouse=True)
    def lookups(self, monkeypatch):
        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", lambda *_a, **_k: "token")
        monkeypatch.setattr(lc, "probe_unity_gateway_capabilities", lambda *_a, **_k: PROBE)
        monkeypatch.setattr(lc, "list_model_service_entries", lambda *_a, **_k: (SERVICES, None))
        monkeypatch.setattr(lc, "list_anthropic_model_catalog", lambda *_a, **_k: CATALOG)

    @pytest.mark.parametrize("failure", ["unavailable-probe", "login-required"])
    def test_failed_refresh_invalidates(self, monkeypatch, failure):
        if failure == "unavailable-probe":
            monkeypatch.setattr(
                lc,
                "probe_unity_gateway_capabilities",
                MagicMock(return_value=db_mod.GatewayProbe(False, "HTTP 503", False)),
            )
        else:
            monkeypatch.setattr(
                lc,
                "fetch_databricks_token_untimed",
                MagicMock(side_effect=db_mod.DatabricksLoginRequiredError("no cached credentials")),
            )
        _store(
            _key(),
            {"catalog": lc.catalog_payload(CATALOG)},
            checked_at=time.time() - lc.REFRESH_THROTTLE_SECONDS - 1,
        )
        assert lc.main(refresh_args()) == 1
        assert lc.load_entry(_key()) is None

    def test_refresh_rejects_a_credentials_file_that_changed_since_spawn(self, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("stale identity must not refetch")

        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", boom)
        identity = _identity()
        changed = {
            **identity,
            "config_fingerprint": {"path": "other", "exists": True, "sha256": "z"},
        }
        _store(lc.cache_key(changed), {}, identity=changed)
        assert lc.main(refresh_args(identity=changed)) == 1
        # The stale-identity refresher neither serves nor rewrites the old identity's entry.
        assert lc.load_entry(lc.cache_key(changed), changed) is not None

    def test_custom_oauth_refresh_without_a_saved_login_gives_up(self, monkeypatch):
        monkeypatch.setenv("ENABLE_CUSTOM_OAUTH_FROM_CLI", "1")
        monkeypatch.setattr(lc, "has_valid_databricks_auth", lambda *_a, **_k: False)

        def boom(*_a, **_k):
            raise AssertionError("the refresher must not fetch without a saved login")

        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", boom)
        custom_oauth = {"client_id": "cid", "redirect_url": "http://localhost", "scopes": ["a"]}
        key = _key(custom_oauth=custom_oauth)
        _store(key, {}, checked_at=time.time() - lc.REFRESH_THROTTLE_SECONDS - 1)
        assert lc.main(refresh_args(identity=_identity(custom_oauth=custom_oauth))) == 1
        assert lc.load_entry(key) is None

    def test_refresher_requests_carry_the_workspace_selector(self, monkeypatch):
        monkeypatch.setattr(
            lc, "probe_unity_gateway_capabilities", db_mod.probe_unity_gateway_capabilities
        )
        monkeypatch.setattr(db_mod, "_use_pooled_client", lambda: False)
        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", lambda *_a, **_k: "token")
        monkeypatch.setattr(lc, "list_model_service_entries", lambda *_a, **_k: (SERVICES, None))
        monkeypatch.setattr(lc, "list_anthropic_model_catalog", lambda *_a, **_k: CATALOG)
        requests: list = []

        def record(request, *, timeout=10):
            requests.append(request)
            return _Response(b'{"model_services": [{"id": "system.ai.claude-sonnet-5"}]}')

        monkeypatch.setattr(db_mod, "_http_open", record)
        assert lc.main([*refresh_args(), "--workspace-id", "123"]) == 0
        assert requests
        assert all(request.get_header("X-databricks-workspace-id") == "123" for request in requests)


class TestLaunchMerge:
    def test_a_merge_is_skipped_while_a_refresher_holds_the_key(self):
        key = _key()
        _store(key, {"probe": lc.probe_payload(PROBE)}, checked_at=time.time())
        before = lc._read_entry(key)
        lock_path = lc._cache_dir() / f"{key}.lock"
        with lock_path.open("a+b") as lock_file:
            acquire_exclusive_file_lock(lock_file)
            # A mid-flight refresher holds the key: the launch skips the write instead of
            # bumping `checked_at` under its read-fetch-invalidate. The skip is immediate —
            # the launch path never waits on the refresher.
            started = time.monotonic()
            assert not lc.try_merge_entry(
                key, _identity(), {"catalog": lc.catalog_payload(CATALOG)}
            )
            assert time.monotonic() - started < 1
        assert lc._read_entry(key) == before
        # Once the key is free, the same merge goes through.
        assert lc.try_merge_entry(key, _identity(), {"catalog": lc.catalog_payload(CATALOG)})
        assert lc.entry_catalog(lc._read_entry(key)) is not None


@contextmanager
def _launch(state):
    with (
        patch("ucode.cli.ensure_bootstrap_dependencies"),
        patch("ucode.cli.load_state", return_value=dict(state)),
        patch("ucode.cli.ensure_provider_state", return_value=dict(state)),
        patch("ucode.cli._fetch_managed_config", return_value=(None, False)),
        patch("ucode.cli.get_databricks_token", return_value="token"),
        patch(
            "ucode.cli.resolve_launch_model",
            return_value=(dict(state), "databricks-claude-sonnet-4"),
        ),
        patch("ucode.cli.configure_tool", return_value=dict(state)),
        patch("ucode.cli.refresh_downloaded_skills_on_launch"),
        patch("ucode.cli.launch_agent"),
        patch("ucode.cli.spawn_refresher"),
    ):
        yield


class TestLaunchJourney:
    """A launch over a recorded HTTP layer: the workspace lookups a miss and a hit make."""

    @staticmethod
    def _journey_http(monkeypatch, requests: list[str]) -> None:
        monkeypatch.setattr(db_mod, "_use_pooled_client", lambda: False)

        def open_fake(request, *, timeout=10):
            url = request.full_url
            requests.append(url)
            if "unity-catalog/model-services" in url:
                body = {"model_services": [{"name": "model-services/system.ai.claude-sonnet-5"}]}
            else:
                body = {
                    "data": [{"id": "system.ai.claude-sonnet-5", "display_name": "Claude Sonnet 5"}]
                }
            return _Response(json.dumps(body).encode("utf-8"))

        monkeypatch.setattr(db_mod, "_http_open", open_fake)

    @staticmethod
    def _lookup_count(requests: list[str]) -> int:
        return sum("model-services" in url or "/anthropic/" in url for url in requests)

    def test_cold_launch_makes_the_lookups_and_a_hit_makes_none(self, monkeypatch):
        # A URL without a workspace id, like configure saves for one: the launch's
        # pre-configure and post-configure identities must agree with its entry.
        state = {**STATE, "workspace_id": ""}
        # The bearer keeps the auth check off the network and off the Databricks CLI.
        monkeypatch.setenv("DATABRICKS_BEARER", "bearer")
        cold: list[str] = []
        self._journey_http(monkeypatch, cold)
        with _launch(state):
            result = runner.invoke(cli_mod.app, ["claude"])
            assert result.exit_code == 0, result.output
            # The cold launch made the three lookups — probe, discovery walk and picker
            # catalog — and persisted all three parts for the next one.
            assert self._lookup_count(cold) == 3
            assert sum("model-services" in url for url in cold) == 2
            assert sum("/anthropic/" in url for url in cold) == 1
            identity = _identity(workspace_id="")
            entry = lc.load_entry(_key(workspace_id=""), identity)
            assert entry is not None
            assert lc.entry_probe(entry) is not None
            assert lc.entry_model_services(entry) is not None
            assert lc.entry_catalog(entry) is not None
            cli_mod.launch_agent.assert_called_once()

        # The next launch starts Claude from the cache: no lookup requests at all.
        warm: list[str] = []
        self._journey_http(monkeypatch, warm)
        with _launch(state):
            result = runner.invoke(cli_mod.app, ["claude"])
            assert result.exit_code == 0, result.output
            assert self._lookup_count(warm) == 0
            cli_mod.launch_agent.assert_called_once()
