"""Unit tests for the opt-in launch cache (``UG_LAUNCH_CACHE=1``)."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ucode import cli as cli_mod
from ucode import config_io
from ucode import databricks as db_mod
from ucode import launch_cache as lc
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
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
        **{f"{part}_checked_at": checked_at for part in payload},
    }
    lc._cache_dir().mkdir(parents=True, exist_ok=True)
    (lc._cache_dir() / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")


def _entries() -> list[Path]:
    directory = config_io.APP_DIR / lc.CACHE_DIR_NAME
    return list(directory.glob("*.json")) if directory.exists() else []


class TestKey:
    def test_same_inputs_same_key(self):
        assert _key() == _key()

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

    def test_clear_cache_removes_every_entry(self):
        lc.merge_entry(_key(), _identity(), {})
        lc.merge_entry(
            _key(workspace="https://other.databricks.com"),
            _identity(workspace="https://other.databricks.com"),
            {},
        )
        lc.clear_cache()
        assert lc.load_entry(_key()) is None
        assert not _entries()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_cache_dir_and_entries_are_private(self):
        key = _key()
        lc.merge_entry(key, _identity(), {})
        assert (lc._cache_dir().stat().st_mode & 0o777) == 0o700
        assert (lc._entry_path(key).stat().st_mode & 0o777) == 0o600


class TestPrime:
    def test_prime_serves_probe_and_discovery_without_network(self, monkeypatch):
        key = _key()
        lc.merge_entry(
            key,
            _identity(),
            {
                "probe": lc.probe_payload(PROBE),
                "model_services": lc.services_payload(SERVICES),
            },
        )
        entry = lc.load_entry(key, _identity())
        assert entry is not None

        def no_network(*_args, **_kwargs):
            raise AssertionError("launch cache hit must not make workspace requests")

        monkeypatch.setattr(db_mod, "_http_get_json", no_network)
        db_mod.clear_gateway_probe_cache()
        db_mod.clear_model_services_cache()
        lc.prime_from_entry(WORKSPACE, "token", entry)
        assert db_mod.probe_unity_gateway_capabilities(WORKSPACE, "token") == PROBE
        claude, codex, gemini, oss, reason = db_mod.discover_model_services(WORKSPACE, "token")
        assert claude == {"sonnet": "system.ai.claude-sonnet-5"}
        assert codex == gemini == oss == []
        assert reason is None


class TestConfigureIsolation:
    @staticmethod
    def _run(monkeypatch, *, use_launch_cache, hit=False):
        monkeypatch.setenv("DATABRICKS_BEARER", "bearer")
        monkeypatch.setattr(cli_mod, "get_databricks_token", lambda *_a, **_k: "token")
        if hit:
            # A hit must serve the probe and the discovery from the primed in-process
            # caches; any workspace request means the cache failed to serve.
            def no_network(*_args, **_kwargs):
                raise AssertionError("launch cache hit must not make workspace requests")

            monkeypatch.setattr(db_mod, "_http_get_json", no_network)
            monkeypatch.setattr(
                cli_mod, "probe_unity_gateway_capabilities", db_mod.probe_unity_gateway_capabilities
            )
            monkeypatch.setattr(cli_mod, "discover_model_services", db_mod.discover_model_services)
            # The UC listing only buckets claude models, so configure falls back to the
            # gateway listing for codex; that fallback is outside the cache's scope.
            monkeypatch.setattr(cli_mod, "discover_codex_models", lambda *_a, **_k: ([], None))
        else:
            monkeypatch.setattr(
                cli_mod, "probe_unity_gateway_capabilities", lambda *_a, **_k: PROBE
            )

            def discover(workspace, _token):
                # The listing runs in the background: outlast the probe so the cold save can
                # only happen after the join.
                time.sleep(0.1)
                db_mod.prime_model_services_cache(workspace, SERVICES)
                return {"sonnet": "system.ai.claude-sonnet-5"}, [], [], [], None

            monkeypatch.setattr(cli_mod, "discover_model_services", discover)
        reads: list[str] = []
        real_load = lc.load_entry

        def recording_load(key, identity=None):
            reads.append(key)
            return real_load(key, identity)

        monkeypatch.setattr(cli_mod, "load_entry", recording_load)
        writes: list[str] = []
        real_merge = lc.merge_entry

        def recording_merge(key, identity, payload):
            writes.append(key)
            real_merge(key, identity, payload)

        monkeypatch.setattr(cli_mod, "merge_entry", recording_merge)
        state = cli_mod.configure_shared_state(
            WORKSPACE, profile="p", tools=["claude"], use_launch_cache=use_launch_cache
        )
        return state, reads, writes

    def test_configure_never_reads_or_writes_the_cache(self, monkeypatch):
        db_mod.clear_model_services_cache()
        _state, reads, writes = self._run(monkeypatch, use_launch_cache=False)
        assert reads == []
        assert writes == []

    def test_launch_read_serves_and_cold_lookup_writes(self, monkeypatch):
        db_mod.clear_model_services_cache()
        state, reads, writes = self._run(monkeypatch, use_launch_cache=True)
        assert len(reads) == 1
        assert len(writes) == 1
        assert writes[0] == lc.cache_key(lc.entry_identity(state))
        entry = lc.load_entry(writes[0])
        assert entry is not None
        assert lc.entry_probe(entry) == PROBE
        assert lc.entry_model_services(entry) == SERVICES

    def test_hit_serves_probe_and_discovery_without_a_request(self, monkeypatch):
        # configure_shared_state resolves the key from its state, where the URL carries no
        # workspace id; its identity also reflects the isolated credentials file.
        key = _key(workspace_id="")
        _store(
            key,
            {"probe": lc.probe_payload(PROBE), "model_services": lc.services_payload(SERVICES)},
            checked_at=time.time(),
            identity=_identity(workspace_id=""),
        )
        db_mod.clear_gateway_probe_cache()
        db_mod.clear_model_services_cache()
        state, reads, writes = self._run(monkeypatch, use_launch_cache=True, hit=True)
        assert reads == [key]
        assert writes == []
        assert state["claude_models"] == {"sonnet": "system.ai.claude-sonnet-5"}


class TestRefresher:
    @staticmethod
    def _patch_lookups(monkeypatch, *, catalog=CATALOG):
        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", lambda *_a, **_k: "token")
        monkeypatch.setattr(lc, "probe_unity_gateway_capabilities", lambda *_a, **_k: PROBE)
        monkeypatch.setattr(lc, "list_model_service_entries", lambda *_a, **_k: (SERVICES, None))
        monkeypatch.setattr(lc, "list_anthropic_model_catalog", lambda *_a, **_k: catalog)

    def test_refresh_writes_every_part(self, monkeypatch):
        self._patch_lookups(monkeypatch)
        assert lc.main(refresh_args()) == 0
        entry = lc.load_entry(_key(), _identity())
        assert entry is not None
        assert lc.entry_probe(entry) == PROBE
        assert lc.entry_model_services(entry) == SERVICES
        assert lc.entry_catalog(entry) is not None

    def test_refresh_throttles_one_per_key_per_few_minutes(self, monkeypatch):
        self._patch_lookups(monkeypatch)

        def boom(*_a, **_k):
            raise AssertionError("refresh must be throttled")

        monkeypatch.setattr(lc, "probe_unity_gateway_capabilities", boom)
        key = _key()
        _store(key, {}, checked_at=time.time())
        assert lc.main(refresh_args()) == 0

    def test_failed_refresh_invalidates_instead_of_serving(self, monkeypatch):
        self._patch_lookups(monkeypatch)

        def failing_probe(*_a, **_k):
            raise RuntimeError("gateway unavailable")

        monkeypatch.setattr(lc, "probe_unity_gateway_capabilities", failing_probe)
        key = _key()
        _store(
            key,
            {"catalog": lc.catalog_payload(CATALOG)},
            checked_at=time.time() - lc.REFRESH_THROTTLE_SECONDS - 1,
        )
        assert lc.main(refresh_args()) == 1
        assert lc.load_entry(key) is None

    def test_refresh_rejects_a_credentials_file_that_changed_since_spawn(self, monkeypatch):
        self._patch_lookups(monkeypatch)

        def boom(*_a, **_k):
            raise AssertionError("stale identity must not refetch")

        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", boom)
        identity = _identity()
        changed = {
            **identity,
            "config_fingerprint": {"path": "other", "exists": True, "sha256": "z"},
        }
        assert lc.main(refresh_args(identity=changed)) == 1
        assert lc.load_entry(lc.cache_key(changed)) is None

    def test_refresh_never_reauths(self, monkeypatch):
        self._patch_lookups(monkeypatch)
        seen: dict = {}

        def token(_workspace, _profile, **kwargs):
            seen.update(kwargs)
            return "token"

        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", token)
        assert lc.main(refresh_args()) == 0
        assert seen.get("allow_reauth") is False

    def test_failed_token_fetch_invalidates(self, monkeypatch):
        self._patch_lookups(monkeypatch)

        def failing(*_a, **_k):
            raise RuntimeError("auth expired")

        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", failing)
        key = _key()
        _store(key, {}, checked_at=time.time() - lc.REFRESH_THROTTLE_SECONDS - 1)
        assert lc.main(refresh_args()) == 1
        assert lc.load_entry(key) is None

    def test_custom_oauth_refresh_reuses_the_saved_login(self, monkeypatch):
        self._patch_lookups(monkeypatch)
        monkeypatch.setenv("ENABLE_CUSTOM_OAUTH_FROM_CLI", "1")
        fetched: list = []

        def token(_workspace, profile, **kwargs):
            fetched.append((profile, kwargs.get("allow_reauth")))
            return "token"

        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", token)
        monkeypatch.setattr(lc, "has_valid_databricks_auth", lambda *_a, **_k: True)
        custom_oauth = {"client_id": "cid", "redirect_url": "http://localhost", "scopes": ["a"]}
        identity = _identity(custom_oauth={**custom_oauth, "profile": "ug-oauth-p"})
        assert lc.main(refresh_args(identity=identity)) == 0
        assert fetched == [("ug-oauth-p", False)]

    def test_custom_oauth_refresh_falls_back_to_the_derived_profile(self, monkeypatch):
        self._patch_lookups(monkeypatch)
        monkeypatch.setenv("ENABLE_CUSTOM_OAUTH_FROM_CLI", "1")
        fetched: list = []

        def token(_workspace, profile, **kwargs):
            fetched.append((profile, kwargs.get("allow_reauth")))
            return "token"

        monkeypatch.setattr(lc, "fetch_databricks_token_untimed", token)
        monkeypatch.setattr(lc, "has_valid_databricks_auth", lambda *_a, **_k: True)
        custom_oauth = {"client_id": "cid", "redirect_url": "http://localhost", "scopes": ["a"]}
        identity = _identity(custom_oauth=custom_oauth)
        assert lc.main(refresh_args(identity=identity)) == 0
        assert fetched == [("ug-oauth-example.databricks.com-cid", False)]

    def test_custom_oauth_refresh_without_a_saved_login_gives_up(self, monkeypatch):
        self._patch_lookups(monkeypatch)
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

    def test_refresh_never_caches_an_empty_catalog(self, monkeypatch):
        empty = db_mod.AnthropicModelCatalog(model_ids=[], model_id_to_display_name={})
        self._patch_lookups(monkeypatch, catalog=empty)
        assert lc.main(refresh_args()) == 0
        entry = lc.load_entry(_key(), _identity())
        assert entry is not None
        assert lc.entry_catalog(entry) is None

    def test_failed_catalog_refresh_drops_the_cached_catalog(self, monkeypatch):
        empty = db_mod.AnthropicModelCatalog(model_ids=[], model_id_to_display_name={})
        self._patch_lookups(monkeypatch, catalog=empty)
        key = _key()
        _store(
            key,
            {"catalog": lc.catalog_payload(CATALOG)},
            checked_at=time.time() - lc.REFRESH_THROTTLE_SECONDS - 1,
        )
        assert lc.main(refresh_args()) == 0
        entry = lc.load_entry(_key(), _identity())
        assert entry is not None
        assert lc.entry_catalog(entry) is None
        assert lc.entry_probe(entry) is not None

    def test_a_failed_refresh_does_not_drop_a_superseded_entry(self, monkeypatch):
        self._patch_lookups(monkeypatch)
        key = _key()

        def supersede_then_fail(*_a, **_k):
            # Another refresher finished while this one was fetching.
            lc.merge_entry(
                key,
                _identity(),
                {
                    "probe": lc.probe_payload(PROBE),
                    "model_services": lc.services_payload(SERVICES),
                },
            )
            raise RuntimeError("transient failure")

        monkeypatch.setattr(lc, "list_anthropic_model_catalog", supersede_then_fail)
        assert lc.main(refresh_args()) == 1
        entry = lc.load_entry(key, _identity())
        assert entry is not None
        assert lc.entry_probe(entry) == PROBE

    def test_concurrent_refreshes_serialize_per_key(self, monkeypatch):
        self._patch_lookups(monkeypatch)
        key = _key()
        # Fresh: once the lock is ours, the re-check throttles without any fetch.
        _store(key, {}, checked_at=time.time())
        lock_path = lc._cache_dir() / f"{key}.lock"
        with lock_path.open("a+b") as lock_file:
            acquire_exclusive_file_lock(lock_file)
            done: list = []
            thread = threading.Thread(target=lambda: done.append(lc.main(refresh_args())))
            thread.start()
            time.sleep(0.3)
            assert not done  # still waiting for the key's lock
            release_file_lock(lock_file)
        thread.join(timeout=10)
        assert done == [0]

    def test_refresher_applies_tls_settings(self, monkeypatch):
        applied: list = []

        def apply():
            applied.append(True)
            return "os"

        monkeypatch.setattr(lc.tls, "apply", apply)
        self._patch_lookups(monkeypatch)
        assert lc.main(refresh_args()) == 0
        assert applied == [True]

    def test_refresher_requests_carry_the_workspace_selector(self, monkeypatch):
        class _Response(io.BytesIO):
            def __init__(self, body: bytes):
                super().__init__(body)
                self.status = 200
                self.headers = Message()

            def geturl(self):
                return WORKSPACE

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

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

    def test_spawn_is_detached_without_the_agent_stdio(self, monkeypatch):
        spawned: dict = {}
        monkeypatch.setattr(
            "ucode.os_compatibility.subprocess_cross_os.popen",
            lambda argv, **kwargs: spawned.update(argv=argv, kwargs=kwargs) or MagicMock(),
        )
        identity = _identity()
        lc.spawn_refresher(identity)
        assert spawned["kwargs"]["stdin"] is subprocess.DEVNULL
        assert spawned["kwargs"]["stdout"] is subprocess.DEVNULL
        assert spawned["kwargs"]["stderr"] is subprocess.DEVNULL
        if sys.platform == "win32":
            assert spawned["kwargs"]["creationflags"] == subprocess.CREATE_NO_WINDOW
        else:
            assert spawned["kwargs"]["start_new_session"] is True
        assert spawned["argv"][:4] == [sys.executable, "-P", "-m", "ucode.launch_cache"]
        assert spawned["argv"][4:6] == ["--refresh", "--identity"]
        assert json.loads(base64.urlsafe_b64decode(spawned["argv"][6].encode())) == identity


class TestLaunchPath:
    @contextmanager
    def _launch(self, list_catalog, spawn, *, configured_state=None):
        configured_state = configured_state or STATE
        with (
            patch("ucode.cli.ensure_bootstrap_dependencies"),
            patch("ucode.cli.load_state", return_value=dict(STATE)),
            patch("ucode.cli.ensure_provider_state", return_value=dict(STATE)),
            patch("ucode.cli._fetch_managed_config", return_value=(None, False)),
            patch("ucode.cli.get_databricks_token", return_value="token"),
            patch("ucode.cli.configure_shared_state", return_value=dict(configured_state)),
            patch(
                "ucode.cli.resolve_launch_model",
                return_value=(dict(STATE), "databricks-claude-sonnet-4"),
            ),
            patch("ucode.cli.configure_tool", return_value=dict(STATE)),
            patch("ucode.cli.refresh_downloaded_skills_on_launch"),
            patch("ucode.cli.launch_agent"),
            patch("ucode.cli.list_anthropic_model_catalog", list_catalog),
            patch("ucode.cli.spawn_refresher", spawn),
        ):
            yield

    def test_hit_serves_catalog_without_a_lookup_and_starts_refresher(self):
        _store(
            _key(),
            {"catalog": lc.catalog_payload(CATALOG)},
            checked_at=time.time() - lc.REFRESH_THROTTLE_SECONDS - 1,
        )
        list_catalog = MagicMock(side_effect=AssertionError("cached launch must not fetch catalog"))
        spawn = MagicMock()
        with self._launch(list_catalog, spawn):
            result = runner.invoke(cli_mod.app, ["claude"])
        assert result.exit_code == 0, result.output
        spawn.assert_called_once_with(_identity())
        list_catalog.assert_not_called()

    def test_configure_switching_the_profile_drops_the_cached_catalog(self):
        switched = {**STATE, "profile": "other"}
        _store(
            _key(),
            {"catalog": lc.catalog_payload(CATALOG)},
            checked_at=time.time() - lc.REFRESH_THROTTLE_SECONDS - 1,
        )
        list_catalog = MagicMock(return_value=CATALOG)
        spawn = MagicMock()
        with self._launch(list_catalog, spawn, configured_state=switched):
            result = runner.invoke(cli_mod.app, ["claude"])
        assert result.exit_code == 0, result.output
        # The cached catalog belonged to the pre-configure profile, so the launch
        # fetched its own and recorded it under the configured identity.
        list_catalog.assert_called_once()
        entry = lc.load_entry(lc.cache_key(lc.entry_identity(switched)))
        assert entry is not None
        assert lc.entry_catalog(entry) is not None

    def test_hit_within_throttle_does_not_refresh(self):
        _store(_key(), {"catalog": lc.catalog_payload(CATALOG)}, checked_at=time.time())
        list_catalog = MagicMock(return_value=CATALOG)
        spawn = MagicMock()
        with self._launch(list_catalog, spawn):
            result = runner.invoke(cli_mod.app, ["claude"])
        assert result.exit_code == 0, result.output
        spawn.assert_not_called()
        list_catalog.assert_not_called()

    def test_miss_fetches_and_records_the_catalog(self):
        list_catalog = MagicMock(return_value=CATALOG)
        spawn = MagicMock()
        with self._launch(list_catalog, spawn):
            result = runner.invoke(cli_mod.app, ["claude"])
        assert result.exit_code == 0, result.output
        list_catalog.assert_called_once()
        entry = lc.load_entry(_key())
        assert entry is not None
        assert lc.entry_catalog(entry) is not None
        # The results were just fetched; the refresher only runs for older entries.
        spawn.assert_not_called()

    def test_flag_off_matches_today_and_writes_nothing(self, monkeypatch):
        monkeypatch.delenv("UG_LAUNCH_CACHE")
        list_catalog = MagicMock(return_value=CATALOG)
        spawn = MagicMock()
        with self._launch(list_catalog, spawn):
            result = runner.invoke(cli_mod.app, ["claude"])
        assert result.exit_code == 0, result.output
        list_catalog.assert_called_once()
        spawn.assert_not_called()
        assert not _entries()


class TestInvalidation:
    def test_configure_invalidates(self):
        key = _key()
        _store(key, {}, checked_at=time.time())
        with (
            patch("ucode.cli.install_databricks_cli"),
            patch("ucode.cli.install_tool_binary"),
            patch("ucode.cli.configure_workspace_command"),
        ):
            result = runner.invoke(cli_mod.app, ["configure"])
        assert result.exit_code == 0, result.output
        assert lc.load_entry(key) is None

    def test_configure_dry_run_keeps_the_cache(self):
        key = _key()
        _store(key, {}, checked_at=time.time())
        with (
            patch("ucode.cli.install_databricks_cli"),
            patch("ucode.cli.install_tool_binary"),
            patch("ucode.cli.configure_workspace_command"),
        ):
            result = runner.invoke(cli_mod.app, ["configure", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert lc.load_entry(key) is not None

    def test_login_invalidates(self, monkeypatch):
        key = _key()
        _store(key, {}, checked_at=time.time())
        monkeypatch.setattr(db_mod, "databricks_cli_path", lambda: "databricks")
        monkeypatch.setattr(db_mod, "find_profile_name_for_workspace", lambda *_a, **_k: "p")
        monkeypatch.setattr(db_mod, "run", lambda *_a, **_k: None)
        db_mod.run_databricks_login(WORKSPACE, "p")
        assert lc.load_entry(key) is None
