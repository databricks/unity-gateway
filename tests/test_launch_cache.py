"""Unit tests for the opt-in launch cache (``UG_LAUNCH_CACHE=1``)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ucode import cli as cli_mod
from ucode import config_io
from ucode import databricks as db_mod
from ucode import launch_cache as lc

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


def _store(key: str, payload: dict, *, saved_at: float | None = None) -> None:
    entry = {**payload, "version": lc.ENTRY_VERSION, "key": key}
    entry["saved_at"] = time.time() if saved_at is None else saved_at
    lc._cache_dir().mkdir(parents=True, exist_ok=True)
    (lc._cache_dir() / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")


def _entries() -> list[Path]:
    directory = config_io.APP_DIR / lc.CACHE_DIR_NAME
    return list(directory.glob("*.json")) if directory.exists() else []


class TestKey:
    def test_same_inputs_same_key(self):
        assert lc.cache_key(WORKSPACE, "p", "123") == lc.cache_key(WORKSPACE, "p", "123")

    def test_workspace_profile_and_id_isolate(self):
        keys = {
            lc.cache_key(WORKSPACE, "p", "123"),
            lc.cache_key("https://other.databricks.com", "p", "123"),
            lc.cache_key(WORKSPACE, "other", "123"),
            lc.cache_key(WORKSPACE, "p", "456"),
        }
        assert len(keys) == 4

    def test_credentials_change_rotates(self, tmp_path, monkeypatch):
        cfg = tmp_path / "databrickscfg"
        cfg.write_text("[p]\nhost = x\n")
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
        before = lc.cache_key(WORKSPACE, "p", "123")
        cfg.write_text("[p]\nhost = y\n")
        assert lc.cache_key(WORKSPACE, "p", "123") != before

    def test_ug_version_rotates(self, monkeypatch):
        monkeypatch.setattr(lc, "ug_version", lambda: "1")
        before = lc.cache_key(WORKSPACE, "p", "123")
        monkeypatch.setattr(lc, "ug_version", lambda: "2")
        assert lc.cache_key(WORKSPACE, "p", "123") != before

    def test_missing_credentials_file_isolated_from_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "absent"))
        missing = lc.cache_key(WORKSPACE, "p", "123")
        cfg = tmp_path / "cfg"
        cfg.write_text("")
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
        assert lc.cache_key(WORKSPACE, "p", "123") != missing


class TestEntry:
    def test_roundtrip_and_parts(self):
        key = lc.cache_key(WORKSPACE, "p", "123")
        lc.merge_entry(
            key,
            {
                "probe": lc.probe_payload(PROBE),
                "model_services": lc.services_payload(SERVICES),
                "catalog": lc.catalog_payload(CATALOG),
            },
        )
        entry = lc.load_entry(key)
        assert entry is not None
        assert lc.entry_probe(entry) == PROBE
        assert lc.entry_model_services(entry) == SERVICES
        catalog = lc.entry_catalog(entry)
        assert catalog is not None
        assert catalog.model_ids == CATALOG.model_ids
        assert catalog.model_id_to_display_name == CATALOG.model_id_to_display_name

    def test_stale_entry_not_served(self):
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(key, {}, saved_at=time.time() - lc.MAX_AGE_SECONDS - 1)
        assert lc.load_entry(key) is None

    def test_corrupt_entry_not_served(self):
        key = lc.cache_key(WORKSPACE, "p", "123")
        lc._cache_dir().mkdir(parents=True, exist_ok=True)
        (lc._cache_dir() / f"{key}.json").write_text("{not json", encoding="utf-8")
        assert lc.load_entry(key) is None

    def test_entry_for_another_key_not_served(self):
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(key, {}, saved_at=time.time())
        other = lc.cache_key("https://other.databricks.com", "p", "123")
        entry = json.loads((lc._cache_dir() / f"{key}.json").read_text(encoding="utf-8"))
        entry["key"] = other
        (lc._cache_dir() / f"{key}.json").write_text(json.dumps(entry), encoding="utf-8")
        assert lc.load_entry(other) is None
        assert lc.load_entry(key) is None

    def test_failed_probe_never_served(self):
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(
            key,
            {"probe": {"reachable": False, "detail": "HTTP 503", "resource_available": False}},
            saved_at=time.time(),
        )
        entry = lc.load_entry(key)
        assert entry is not None
        assert lc.entry_probe(entry) is None

    def test_clear_cache_removes_every_entry(self):
        lc.merge_entry(lc.cache_key(WORKSPACE, "p", "123"), {})
        lc.merge_entry(lc.cache_key("https://other.databricks.com", "p", "123"), {})
        lc.clear_cache()
        assert lc.load_entry(lc.cache_key(WORKSPACE, "p", "123")) is None
        assert not _entries()

    def test_cache_dir_and_entries_are_private(self):
        key = lc.cache_key(WORKSPACE, "p", "123")
        lc.merge_entry(key, {})
        assert (lc._cache_dir().stat().st_mode & 0o777) == 0o700
        assert (lc._entry_path(key).stat().st_mode & 0o777) == 0o600


class TestPrime:
    def test_prime_serves_probe_and_discovery_without_network(self, monkeypatch):
        key = lc.cache_key(WORKSPACE, "p", "123")
        lc.merge_entry(
            key,
            {
                "probe": lc.probe_payload(PROBE),
                "model_services": lc.services_payload(SERVICES),
            },
        )
        entry = lc.load_entry(key)
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
    def _run(monkeypatch, *, use_launch_cache):
        monkeypatch.setenv("DATABRICKS_BEARER", "bearer")
        monkeypatch.setattr(cli_mod, "get_databricks_token", lambda *_a, **_k: "token")
        monkeypatch.setattr(cli_mod, "probe_unity_gateway_capabilities", lambda *_a, **_k: PROBE)

        def discover(workspace, _token):
            db_mod.prime_model_services_cache(workspace, SERVICES)
            return {"sonnet": "system.ai.claude-sonnet-5"}, [], [], [], None

        monkeypatch.setattr(cli_mod, "discover_model_services", discover)
        reads: list[str] = []
        real_load = lc.load_entry

        def recording_load(key):
            reads.append(key)
            return real_load(key)

        monkeypatch.setattr(cli_mod, "load_entry", recording_load)
        writes: list[str] = []
        real_merge = lc.merge_entry

        def recording_merge(key, payload):
            writes.append(key)
            real_merge(key, payload)

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
        assert writes[0] == lc.cache_key(WORKSPACE, "p", state.get("workspace_id"))
        entry = lc.load_entry(writes[0])
        assert entry is not None
        assert lc.entry_probe(entry) == PROBE
        assert lc.entry_model_services(entry) == SERVICES

    def test_hit_does_not_rewrite_a_served_probe(self, monkeypatch):
        # configure_shared_state resolves the key from its state, where the URL carries no
        # workspace id.
        key = lc.cache_key(WORKSPACE, "p", None)
        _store(
            key,
            {"probe": lc.probe_payload(PROBE), "model_services": lc.services_payload(SERVICES)},
            saved_at=time.time(),
        )
        db_mod.clear_model_services_cache()
        _state, reads, writes = self._run(monkeypatch, use_launch_cache=True)
        assert reads == [key]
        assert writes == []


class TestRefresher:
    @staticmethod
    def _patch_lookups(monkeypatch, *, catalog=CATALOG):
        monkeypatch.setattr(lc, "get_databricks_token", lambda *_a, **_k: "token")
        monkeypatch.setattr(lc, "probe_unity_gateway_capabilities", lambda *_a, **_k: PROBE)
        monkeypatch.setattr(lc, "list_model_service_entries", lambda *_a, **_k: (SERVICES, None))
        monkeypatch.setattr(lc, "list_anthropic_model_catalog", lambda *_a, **_k: catalog)

    def test_refresh_writes_every_part(self, monkeypatch):
        self._patch_lookups(monkeypatch)
        args = ["--refresh", "--workspace", WORKSPACE, "--profile", "p", "--workspace-id", "123"]
        assert lc.main(args) == 0
        entry = lc.load_entry(lc.cache_key(WORKSPACE, "p", "123"))
        assert entry is not None
        assert lc.entry_probe(entry) == PROBE
        assert lc.entry_model_services(entry) == SERVICES
        assert lc.entry_catalog(entry) is not None

    def test_refresh_throttles_one_per_key_per_few_minutes(self, monkeypatch):
        self._patch_lookups(monkeypatch)

        def boom(*_a, **_k):
            raise AssertionError("refresh must be throttled")

        monkeypatch.setattr(lc, "probe_unity_gateway_capabilities", boom)
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(key, {}, saved_at=time.time())
        args = ["--refresh", "--workspace", WORKSPACE, "--profile", "p", "--workspace-id", "123"]
        assert lc.main(args) == 0

    def test_failed_refresh_invalidates_instead_of_serving(self, monkeypatch):
        self._patch_lookups(monkeypatch)

        def failing_probe(*_a, **_k):
            raise RuntimeError("gateway unavailable")

        monkeypatch.setattr(lc, "probe_unity_gateway_capabilities", failing_probe)
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(key, {"catalog": lc.catalog_payload(CATALOG)}, saved_at=time.time() - 400)
        args = ["--refresh", "--workspace", WORKSPACE, "--profile", "p", "--workspace-id", "123"]
        assert lc.main(args) == 1
        assert lc.load_entry(key) is None

    def test_spawn_is_detached_without_the_agent_stdio(self, monkeypatch):
        spawned: dict = {}
        monkeypatch.setattr(
            "ucode.os_compatibility.subprocess_cross_os.popen",
            lambda argv, **kwargs: spawned.update(argv=argv, kwargs=kwargs) or MagicMock(),
        )
        lc.spawn_refresher(WORKSPACE, "p", "123")
        assert spawned["kwargs"]["stdin"] is subprocess.DEVNULL
        assert spawned["kwargs"]["stdout"] is subprocess.DEVNULL
        assert spawned["kwargs"]["stderr"] is subprocess.DEVNULL
        assert spawned["kwargs"]["start_new_session"] is True
        assert spawned["argv"][:5] == [
            sys.executable,
            "-P",
            "-m",
            "ucode.launch_cache",
            "--refresh",
        ]


class TestLaunchPath:
    @contextmanager
    def _launch(self, list_catalog, spawn):
        with (
            patch("ucode.cli.ensure_bootstrap_dependencies"),
            patch("ucode.cli.load_state", return_value=dict(STATE)),
            patch("ucode.cli.ensure_provider_state", return_value=dict(STATE)),
            patch("ucode.cli._fetch_managed_config", return_value=(None, False)),
            patch("ucode.cli.get_databricks_token", return_value="token"),
            patch("ucode.cli.configure_shared_state", return_value=dict(STATE)),
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
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(
            key,
            {"catalog": lc.catalog_payload(CATALOG)},
            saved_at=time.time() - lc.REFRESH_THROTTLE_SECONDS - 1,
        )
        list_catalog = MagicMock(side_effect=AssertionError("cached launch must not fetch catalog"))
        spawn = MagicMock()
        with self._launch(list_catalog, spawn):
            result = runner.invoke(cli_mod.app, ["claude"])
        assert result.exit_code == 0, result.output
        spawn.assert_called_once_with(WORKSPACE, "p", "123")
        list_catalog.assert_not_called()

    def test_hit_within_throttle_does_not_refresh(self):
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(key, {"catalog": lc.catalog_payload(CATALOG)}, saved_at=time.time())
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
        entry = lc.load_entry(lc.cache_key(WORKSPACE, "p", "123"))
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
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(key, {}, saved_at=time.time())
        with (
            patch("ucode.cli.install_databricks_cli"),
            patch("ucode.cli.install_tool_binary"),
            patch("ucode.cli.configure_workspace_command"),
        ):
            result = runner.invoke(cli_mod.app, ["configure"])
        assert result.exit_code == 0, result.output
        assert lc.load_entry(key) is None

    def test_login_invalidates(self, monkeypatch):
        key = lc.cache_key(WORKSPACE, "p", "123")
        _store(key, {}, saved_at=time.time())
        monkeypatch.setattr(db_mod, "databricks_cli_path", lambda: "databricks")
        monkeypatch.setattr(db_mod, "find_profile_name_for_workspace", lambda *_a, **_k: "p")
        monkeypatch.setattr(db_mod, "run", lambda *_a, **_k: None)
        db_mod.run_databricks_login(WORKSPACE, "p")
        assert lc.load_entry(key) is None
