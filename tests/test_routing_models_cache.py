from __future__ import annotations

import json
import stat
import sys

import pytest

from ucode import config_io
from ucode.databricks import AnthropicModelCatalog
from ucode.smart_routing.routing_models_cache import (
    RoutingModel,
    read_routing_models,
    routing_models_cache_path,
    routing_models_from_catalog,
    write_routing_models,
)

WS = "https://example.databricks.com"
OTHER_WS = "https://other.databricks.com"
MULTI_WS = "https://multi.databricks.com?w=123"
PROVIDER = "main.agents.anthropic_provider"
OTHER_PROVIDER = "main.agents.bedrock_provider"

MODELS = [
    RoutingModel("us.anthropic.claude-opus-4-6-v1", "Claude Opus 4.6", 1_000_000),
    RoutingModel("anthropic.claude-haiku-4-5@20251001", None, None),
    RoutingModel("databricks-claude-sonnet-4-5", "Sonnet", 200_000),
]


def test_cache_lives_under_app_dir():
    assert routing_models_cache_path() == config_io.APP_DIR / "routing-models.json"


def test_round_trip_preserves_order_and_raw_ids():
    assert write_routing_models(WS, PROVIDER, MODELS) is True

    assert read_routing_models(WS, PROVIDER) == MODELS


def test_none_provider_is_its_own_entry():
    write_routing_models(WS, None, MODELS[:1])
    write_routing_models(WS, PROVIDER, MODELS[1:])

    assert read_routing_models(WS, None) == MODELS[:1]
    assert read_routing_models(WS, PROVIDER) == MODELS[1:]


def test_provider_named_like_default_key_does_not_collide():
    write_routing_models(WS, None, MODELS[:1])
    write_routing_models(WS, "default", MODELS[1:])

    assert read_routing_models(WS, None) == MODELS[:1]
    assert read_routing_models(WS, "default") == MODELS[1:]


def test_entries_are_isolated_by_workspace():
    write_routing_models(WS, PROVIDER, MODELS[:1])
    write_routing_models(OTHER_WS, PROVIDER, MODELS[1:])
    write_routing_models(MULTI_WS, PROVIDER, MODELS[2:])

    assert read_routing_models(WS, PROVIDER) == MODELS[:1]
    assert read_routing_models(OTHER_WS, PROVIDER) == MODELS[1:]
    assert read_routing_models(MULTI_WS, PROVIDER) == MODELS[2:]
    assert read_routing_models("https://multi.databricks.com", PROVIDER) is None


def test_entries_are_isolated_by_provider():
    write_routing_models(WS, PROVIDER, MODELS[:1])
    write_routing_models(WS, OTHER_PROVIDER, MODELS[1:])

    assert read_routing_models(WS, PROVIDER) == MODELS[:1]
    assert read_routing_models(WS, OTHER_PROVIDER) == MODELS[1:]
    assert read_routing_models(WS, "main.agents.unknown") is None
    assert read_routing_models(WS, None) is None


def test_rewrite_replaces_only_its_own_entry():
    write_routing_models(WS, PROVIDER, MODELS)
    write_routing_models(WS, OTHER_PROVIDER, MODELS)
    write_routing_models(WS, PROVIDER, MODELS[:1])

    assert read_routing_models(WS, PROVIDER) == MODELS[:1]
    assert read_routing_models(WS, OTHER_PROVIDER) == MODELS


def test_empty_list_is_cached_distinct_from_missing():
    write_routing_models(WS, PROVIDER, [])

    assert read_routing_models(WS, PROVIDER) == []


def test_missing_file_reads_as_none():
    assert not routing_models_cache_path().exists()

    assert read_routing_models(WS, PROVIDER) is None
    assert read_routing_models(WS, None) is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        b"\xff\xfe\x00garbage",
        "[]",
        json.dumps({"version": 999, "workspaces": {WS: {"default": [{"id": "m"}]}}}),
        json.dumps({"version": 1, "workspaces": []}),
        json.dumps({"version": 1, "workspaces": {WS: "nope"}}),
        json.dumps({"version": 1, "workspaces": {WS: {"default": "nope"}}}),
    ],
)
def test_corrupt_or_unknown_file_reads_as_none(content):
    path = routing_models_cache_path()
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")

    assert read_routing_models(WS, None) is None


def test_write_recovers_from_corrupt_file():
    routing_models_cache_path().write_text("{not json", encoding="utf-8")

    assert write_routing_models(WS, None, MODELS) is True
    assert read_routing_models(WS, None) == MODELS


def test_malformed_rows_are_skipped():
    routing_models_cache_path().write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {
                    WS: {
                        "providers": {
                            PROVIDER: [
                                {"id": "good", "display_name": "Good", "max_input_tokens": 10},
                                {"id": ""},
                                {"display_name": "no id"},
                                "not-a-row",
                                {"id": "odd", "display_name": 5, "max_input_tokens": True},
                                {"id": "zero", "max_input_tokens": 0},
                            ]
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert read_routing_models(WS, PROVIDER) == [
        RoutingModel("good", "Good", 10),
        RoutingModel("odd"),
        RoutingModel("zero"),
    ]


def test_file_stores_only_model_metadata():
    write_routing_models(WS, PROVIDER, MODELS)

    data = json.loads(routing_models_cache_path().read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "workspaces": {
            WS: {
                "providers": {
                    PROVIDER: [
                        {
                            "id": "us.anthropic.claude-opus-4-6-v1",
                            "display_name": "Claude Opus 4.6",
                            "max_input_tokens": 1_000_000,
                        },
                        {"id": "anthropic.claude-haiku-4-5@20251001"},
                        {
                            "id": "databricks-claude-sonnet-4-5",
                            "display_name": "Sonnet",
                            "max_input_tokens": 200_000,
                        },
                    ]
                }
            }
        },
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_file_is_user_only():
    write_routing_models(WS, None, MODELS)

    assert stat.S_IMODE(routing_models_cache_path().stat().st_mode) == 0o600


def test_dry_run_skips_write(monkeypatch):
    monkeypatch.setattr(config_io, "_dry_run", True)

    assert write_routing_models(WS, None, MODELS) is False
    assert not routing_models_cache_path().exists()


def test_unwritable_cache_returns_false(monkeypatch):
    def fail(path, payload):
        raise RuntimeError("disk full")

    monkeypatch.setattr(config_io, "atomic_write_json", fail)

    assert write_routing_models(WS, None, MODELS) is False


def test_empty_workspace_is_never_cached():
    assert write_routing_models("", None, MODELS) is False
    assert read_routing_models("", None) is None


def test_models_from_catalog_keep_order_and_treat_zero_limit_as_unknown():
    catalog = AnthropicModelCatalog(
        model_ids=["us.anthropic.claude-opus-4-6-v1", "anthropic.claude-haiku-4-5@20251001"],
        model_id_to_display_name={"us.anthropic.claude-opus-4-6-v1": "Claude Opus 4.6"},
        model_id_to_limits={
            "us.anthropic.claude-opus-4-6-v1": (1_000_000, 64_000),
            "anthropic.claude-haiku-4-5@20251001": (0, 0),
        },
    )

    assert routing_models_from_catalog(catalog) == [
        RoutingModel("us.anthropic.claude-opus-4-6-v1", "Claude Opus 4.6", 1_000_000),
        RoutingModel("anthropic.claude-haiku-4-5@20251001"),
    ]


def test_concurrent_writers_for_different_providers_keep_both_entries():
    import threading

    providers = [f"main.agents.provider_{index}" for index in range(8)]
    start = threading.Barrier(len(providers))

    def write(provider):
        start.wait()
        for _ in range(10):
            assert write_routing_models(WS, provider, [RoutingModel(provider)]) is True

    threads = [threading.Thread(target=write, args=(provider,)) for provider in providers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for provider in providers:
        assert read_routing_models(WS, provider) == [RoutingModel(provider)]


def test_write_gives_up_while_another_writer_holds_the_lock(monkeypatch):
    from ucode.os_compatibility.file_lock_cross_os import (
        acquire_exclusive_file_lock,
        release_file_lock,
    )
    from ucode.smart_routing import routing_models_cache

    monkeypatch.setattr(routing_models_cache, "_LOCK_TIMEOUT_SECONDS", 0.1)
    path = routing_models_cache_path()
    with open(path.with_name(f"{path.name}.lock"), "a+", encoding="utf-8") as held:
        acquire_exclusive_file_lock(held)
        try:
            assert write_routing_models(WS, PROVIDER, MODELS) is False
        finally:
            release_file_lock(held)

    assert write_routing_models(WS, PROVIDER, MODELS) is True


def test_harnesses_keep_separate_lists_for_one_provider():
    from ucode.smart_routing.routing_models_cache import HARNESS_CODEX

    write_routing_models(WS, PROVIDER, MODELS[:1])
    write_routing_models(WS, PROVIDER, MODELS[1:], harness=HARNESS_CODEX)

    assert read_routing_models(WS, PROVIDER) == MODELS[:1]
    assert read_routing_models(WS, PROVIDER, harness=HARNESS_CODEX) == MODELS[1:]
    assert routing_models_cache_path(HARNESS_CODEX).name == "codex-routing-models.json"


def test_snapshots_are_content_addressed_and_never_rewritten():
    from ucode.smart_routing.routing_models_cache import write_routing_models_snapshot

    first = write_routing_models_snapshot(MODELS)
    again = write_routing_models_snapshot(MODELS)
    other = write_routing_models_snapshot(MODELS[:1])

    assert first == again
    assert first != other
    assert json.loads(first.read_text()) == [
        {
            "id": "us.anthropic.claude-opus-4-6-v1",
            "display_name": "Claude Opus 4.6",
            "max_input_tokens": 1_000_000,
        },
        {"id": "anthropic.claude-haiku-4-5@20251001"},
        {
            "id": "databricks-claude-sonnet-4-5",
            "display_name": "Sonnet",
            "max_input_tokens": 200_000,
        },
    ]
