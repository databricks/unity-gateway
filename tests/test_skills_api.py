"""Tests for skills_api.py -- the read-only UC skills API client and workspace walk."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import ucode.skills_api as sa
from ucode.skills_api import SkillRef

WS = "https://example.databricks.com"


def ref(
    securable_name: str,
    bundle_name: str | None = None,
    *,
    catalog: str = "main",
    schema: str = "default",
    description: str | None = None,
) -> SkillRef:
    """A SkillRef whose two names match unless a differing bundle name is given."""
    return SkillRef(
        catalog=catalog,
        schema=schema,
        securable_name=securable_name,
        bundle_name=bundle_name or securable_name,
        description=description,
    )


class TestListSchemaSkills:
    def test_keeps_finalized_skills_only(self, monkeypatch):
        payload = {
            "skills": [
                {
                    "name": "skills/main.default.pii-handling",
                    "bundle_name": "pii-handling",
                    "finalize_time": "2026-06-26T05:58:25Z",
                },
                {
                    "name": "skills/main.default.triage",
                    "bundle_name": "triage",
                    "finalize_time": "2026-06-26T05:58:26Z",
                },
                {"name": "skills/main.default.draft", "bundle_name": "draft"},
            ]
        }
        monkeypatch.setattr(sa, "_http_get_json", lambda url, token, timeout=30: (payload, None))

        refs, reason = sa.list_schema_skills(WS, "token", "main", "default")

        assert reason is None
        assert refs == [ref("pii-handling"), ref("triage")]

    def test_carries_description_when_present(self, monkeypatch):
        payload = {
            "skills": [
                {
                    "name": "skills/main.default.triage",
                    "bundle_name": "triage",
                    "finalize_time": "2026-06-26T05:58:25Z",
                    "description": "Routes tickets by severity.",
                }
            ]
        }
        monkeypatch.setattr(sa, "_http_get_json", lambda url, token, timeout=30: (payload, None))

        refs, _ = sa.list_schema_skills(WS, "token", "main", "default")

        assert refs == [ref("triage", description="Routes tickets by severity.")]

    def test_keeps_both_names_when_bundle_differs_from_securable(self, monkeypatch):
        # bundle_name comes from the bundle's SKILL.md frontmatter, so it can
        # differ from the securable it was created under.
        payload = {
            "skills": [
                {
                    "name": "skills/main.default.task-prioritizer",
                    "bundle_name": "task-triage",
                    "finalize_time": "2026-06-26T05:58:25Z",
                }
            ]
        }
        monkeypatch.setattr(sa, "_http_get_json", lambda url, token, timeout=30: (payload, None))

        refs, reason = sa.list_schema_skills(WS, "token", "main", "default")

        assert reason is None
        assert refs == [ref("task-prioritizer", "task-triage")]

    @pytest.mark.parametrize(
        ("skill", "expected_missing"),
        [
            ({"name": "skills/main.default.pii-handling"}, "bundle_name"),
            ({"name": "skills/main.default.pii-handling", "bundle_name": ""}, "bundle_name"),
            ({"bundle_name": "orphan"}, "name"),
            ({}, "name or bundle_name"),
        ],
        ids=["no-bundle-name", "blank-bundle-name", "no-resource-name", "neither"],
    )
    def test_skips_and_warns_when_a_name_is_missing(self, skill, expected_missing, monkeypatch):
        # Finalize owns bundle_name and `name` is immutable from creation, so a
        # finalized skill missing either is an anomaly worth surfacing.
        payload = {"skills": [{**skill, "finalize_time": "2026-06-26T05:58:25Z"}]}
        monkeypatch.setattr(sa, "_http_get_json", lambda url, token, timeout=30: (payload, None))
        warnings = []
        monkeypatch.setattr(sa, "print_warning", warnings.append)

        refs, reason = sa.list_schema_skills(WS, "token", "main", "default")

        assert reason is None
        assert refs == []
        assert len(warnings) == 1
        assert f"no {expected_missing}." in warnings[0]

    def test_unfinalized_skill_is_skipped_without_a_warning(self, monkeypatch):
        # An unfinalized skill simply has no bundle yet, which is not an anomaly.
        payload = {"skills": [{"name": "skills/main.default.draft"}]}
        monkeypatch.setattr(sa, "_http_get_json", lambda url, token, timeout=30: (payload, None))
        warnings = []
        monkeypatch.setattr(sa, "print_warning", warnings.append)

        refs, reason = sa.list_schema_skills(WS, "token", "main", "default")

        assert reason is None
        assert refs == []
        assert warnings == []

    @pytest.mark.parametrize(
        "bundle_name",
        ["..", "../escape", "nested/../escape", "a/b", "/abs"],
        ids=["dotdot", "parent-traversal", "embedded-traversal", "separator", "absolute"],
    )
    def test_skips_and_warns_on_unsafe_bundle_name(self, bundle_name, monkeypatch):
        payload = {
            "skills": [
                {
                    "name": "skills/main.default.pii-handling",
                    "bundle_name": bundle_name,
                    "finalize_time": "2026-06-26T05:58:25Z",
                }
            ]
        }
        monkeypatch.setattr(sa, "_http_get_json", lambda url, token, timeout=30: (payload, None))
        warnings = []
        monkeypatch.setattr(sa, "print_warning", warnings.append)

        refs, reason = sa.list_schema_skills(WS, "token", "main", "default")

        assert reason is None
        assert refs == []
        assert len(warnings) == 1
        assert "unsafe bundle name" in warnings[0]

    def test_follows_pagination(self, monkeypatch):
        pages = [
            {
                "skills": [
                    {"name": "skills/main.default.a", "bundle_name": "a", "finalize_time": "t"}
                ],
                "next_page_token": "tok",
            },
            {
                "skills": [
                    {"name": "skills/main.default.b", "bundle_name": "b", "finalize_time": "t"}
                ]
            },
        ]
        captured_tokens = []

        def fake_get(url, token, timeout=30):
            captured_tokens.append("page_token=tok" in url)
            return pages.pop(0), None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        refs, reason = sa.list_schema_skills(WS, "token", "main", "default")

        assert reason is None
        assert refs == [ref("a"), ref("b")]
        assert captured_tokens == [False, True]

    def test_targets_uc_skills_api_for_the_schema(self, monkeypatch):
        captured = {}

        def fake_get(url, token, timeout=30):
            captured["url"] = url
            return {"skills": []}, None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        sa.list_schema_skills(WS, "token", "main", "default")

        assert "/api/2.1/unity-catalog/skills?" in captured["url"]
        assert "parent=schemas%2Fmain.default" in captured["url"]

    def test_http_failure_propagates_reason(self, monkeypatch):
        monkeypatch.setattr(
            sa, "_http_get_json", lambda url, token, timeout=30: (None, "HTTP 500 Server Error")
        )

        leaves, reason = sa.list_schema_skills(WS, "token", "main", "default")

        assert leaves == []
        assert reason == "HTTP 500 Server Error"


class TestListSkillFiles:
    def test_lists_under_the_skills_place(self, monkeypatch):
        captured = {}

        def fake_get(url, token, **kwargs):
            captured.update(url=url, **kwargs)
            return {"contents": []}, None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert captured["url"] == f"{WS}/api/2.0/fs/directories/Skills/main/default/triage"
        assert captured["max_retries"] == sa._FILES_API_MAX_RETRIES

    def test_flat_skill_returns_all_files(self, monkeypatch):
        skill = "/Skills/main/default/triage"
        monkeypatch.setattr(
            sa,
            "_http_get_json",
            lambda url, token, **kwargs: (
                {
                    "contents": [
                        {"path": f"{skill}/SKILL.md", "is_directory": False},
                        {"path": f"{skill}/README.md", "is_directory": False},
                        {"path": f"{skill}/config.yaml", "is_directory": False},
                    ]
                },
                None,
            ),
        )

        paths, reason = sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert reason is None
        assert set(paths) == {"SKILL.md", "README.md", "config.yaml"}

    def test_nested_tree_returns_all_files_across_levels(self, monkeypatch):
        skill = "/Skills/main/default/triage"
        listings = {
            "Skills/main/default/triage": {
                "contents": [
                    {"path": f"{skill}/SKILL.md", "is_directory": False},
                    {"path": f"{skill}/agents/", "is_directory": True},
                    {"path": f"{skill}/references/", "is_directory": True},
                ]
            },
            "Skills/main/default/triage/agents": {
                "contents": [
                    {"path": f"{skill}/agents/main.py", "is_directory": False},
                ]
            },
            "Skills/main/default/triage/references": {
                "contents": [
                    {"path": f"{skill}/references/primary.md", "is_directory": False},
                    {"path": f"{skill}/references/secondary.md", "is_directory": False},
                ]
            },
        }

        def fake_get(url, token, **kwargs):
            directory = url.split("/api/2.0/fs/directories/", 1)[1]
            return listings[directory], None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        paths, reason = sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert reason is None
        assert set(paths) == {
            "SKILL.md",
            "agents/main.py",
            "references/primary.md",
            "references/secondary.md",
        }

    def test_pagination_collects_files_from_all_pages(self, monkeypatch):
        skill = "/Skills/main/default/triage"
        pages = [
            {
                "contents": [{"path": f"{skill}/a.md", "is_directory": False}],
                "next_page_token": "tok",
            },
            {"contents": [{"path": f"{skill}/b.md", "is_directory": False}]},
        ]

        monkeypatch.setattr(sa, "_http_get_json", lambda url, token, **kwargs: (pages.pop(0), None))

        paths, reason = sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert reason is None
        assert set(paths) == {"a.md", "b.md"}

    def test_any_directory_failure_returns_empty_and_reason(self, monkeypatch):
        monkeypatch.setattr(
            sa, "_http_get_json", lambda url, token, **kwargs: (None, "HTTP 404 Not Found")
        )

        paths, reason = sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert paths == []
        assert reason == "HTTP 404 Not Found"

    def test_one_subdir_failure_aborts_whole_listing(self, monkeypatch):
        skill = "/Skills/main/default/triage"
        listings = {
            "Skills/main/default/triage": {
                "contents": [
                    {"path": f"{skill}/agents/", "is_directory": True},
                    {"path": f"{skill}/broken/", "is_directory": True},
                ]
            },
            "Skills/main/default/triage/agents": {
                "contents": [{"path": f"{skill}/agents/main.py", "is_directory": False}]
            },
        }

        def fake_get(url, token, **kwargs):
            directory = url.split("/api/2.0/fs/directories/", 1)[1]
            if directory not in listings:
                return None, "HTTP 500 Server Error"
            return listings[directory], None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        paths, reason = sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert paths == []
        assert reason == "HTTP 500 Server Error"

    def test_three_level_deep_tree_returns_all_files(self, monkeypatch):
        skill = "/Skills/main/default/triage"
        listings = {
            "Skills/main/default/triage": {
                "contents": [
                    {"path": f"{skill}/SKILL.md", "is_directory": False},
                    {"path": f"{skill}/agents/", "is_directory": True},
                ]
            },
            "Skills/main/default/triage/agents": {
                "contents": [
                    {"path": f"{skill}/agents/main.py", "is_directory": False},
                    {"path": f"{skill}/agents/tools/", "is_directory": True},
                ]
            },
            "Skills/main/default/triage/agents/tools": {
                "contents": [
                    {"path": f"{skill}/agents/tools/search.py", "is_directory": False},
                ]
            },
        }

        def fake_get(url, token, **kwargs):
            directory = url.split("/api/2.0/fs/directories/", 1)[1]
            return listings[directory], None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        paths, reason = sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert reason is None
        assert set(paths) == {"SKILL.md", "agents/main.py", "agents/tools/search.py"}

    def test_wave_three_failure_discards_partial_results(self, monkeypatch):
        skill = "/Skills/main/default/triage"
        listings = {
            "Skills/main/default/triage": {
                "contents": [
                    {"path": f"{skill}/a/", "is_directory": True},
                    {"path": f"{skill}/b/", "is_directory": True},
                ]
            },
            "Skills/main/default/triage/a": {
                "contents": [
                    {"path": f"{skill}/a/file.md", "is_directory": False},
                    {"path": f"{skill}/a/deep/", "is_directory": True},
                ]
            },
            "Skills/main/default/triage/b": {
                "contents": [
                    {"path": f"{skill}/b/file.md", "is_directory": False},
                    {"path": f"{skill}/b/deep/", "is_directory": True},
                ]
            },
            "Skills/main/default/triage/a/deep": {
                "contents": [{"path": f"{skill}/a/deep/ok.md", "is_directory": False}]
            },
        }

        def fake_get(url, token, **kwargs):
            directory = url.split("/api/2.0/fs/directories/", 1)[1]
            if directory not in listings:
                return None, "HTTP 500 Server Error"
            return listings[directory], None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        paths, reason = sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert paths == []
        assert reason == "HTTP 500 Server Error"

    def test_concurrency_cap_not_exceeded_during_listing(self, monkeypatch):
        skill = "/Skills/main/default/triage"
        subdirs = [f"dir_{i}" for i in range(40)]
        root_contents = [{"path": f"{skill}/{d}/", "is_directory": True} for d in subdirs]

        counter_lock = threading.Lock()
        state = {"in_flight": 0, "peak": 0}

        def fake_get(url, token, **kwargs):
            if "Skills/main/default/triage" in url and not any(f"/{d}" in url for d in subdirs):
                return {"contents": root_contents}, None
            with counter_lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            time.sleep(0.002)
            with counter_lock:
                state["in_flight"] -= 1
            return {"contents": []}, None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        paths, reason = sa.list_skill_files(WS, "token", "main", "default", "triage")

        assert reason is None
        assert paths == []
        assert state["peak"] <= sa._MAX_CONCURRENT_FILES_API_REQUESTS

    def test_queued_listings_are_cancelled_after_a_failure(self, monkeypatch):
        skill = "/Skills/main/default/triage"
        subdirs = ["fail"] + [f"dir_{i}" for i in range(6)]
        root_listing = {
            "contents": [{"path": f"{skill}/{d}/", "is_directory": True} for d in subdirs]
        }
        release = threading.Event()
        started = []

        def fake_get(url, token, **kwargs):
            directory = url.split("/api/2.0/fs/directories/", 1)[1]
            if directory == "Skills/main/default/triage":
                return root_listing, None
            if directory.endswith("/fail"):
                return None, "HTTP 429"
            started.append(directory)
            release.wait()
            return {"contents": []}, None

        pool = ThreadPoolExecutor(max_workers=2)
        monkeypatch.setattr(sa, "_files_api_pool", pool)
        monkeypatch.setattr(sa, "_http_get_json", fake_get)
        try:
            result = sa.list_skill_files(WS, "token", "main", "default", "triage")
        finally:
            release.set()
            pool.shutdown(wait=True)

        assert result == ([], "HTTP 429")
        assert len(started) <= 2


class TestFetchSkillFile:
    def test_returns_raw_bytes_from_files_api(self, monkeypatch):
        captured = {}

        def fake_get_bytes(url, token, **kwargs):
            captured.update(url=url, **kwargs)
            return b"# SKILL\n", None

        monkeypatch.setattr(sa, "_http_get_bytes", fake_get_bytes)

        body, reason = sa.fetch_skill_file(WS, "token", "main", "default", "triage", "SKILL.md")

        assert reason is None
        assert body == b"# SKILL\n"
        assert captured["url"] == f"{WS}/api/2.0/fs/files/Skills/main/default/triage/SKILL.md"
        assert captured["max_retries"] == sa._FILES_API_MAX_RETRIES

    def test_http_failure_propagates_reason(self, monkeypatch):
        monkeypatch.setattr(
            sa, "_http_get_bytes", lambda url, token, **kwargs: (None, "HTTP 404 Not Found")
        )

        body, reason = sa.fetch_skill_file(WS, "token", "main", "default", "triage", "gone.md")

        assert body is None
        assert reason == "HTTP 404 Not Found"


class TestFetchSkillBundle:
    def test_assembles_relpath_to_bytes_map(self, monkeypatch):
        contents = {"SKILL.md": b"# skill", "references/a.md": b"aaa"}
        monkeypatch.setattr(sa, "list_skill_files", lambda *a, **k: (list(contents), None))
        monkeypatch.setattr(
            sa, "fetch_skill_file", lambda ws, tok, c, s, leaf, rel: (contents[rel], None)
        )

        bundle, reason = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")

        assert reason is None
        assert bundle == contents

    def test_listing_failure_propagates_reason(self, monkeypatch):
        monkeypatch.setattr(sa, "list_skill_files", lambda *a, **k: ([], "HTTP 404 Not Found"))

        bundle, reason = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")

        assert bundle is None
        assert reason == "HTTP 404 Not Found"

    def test_file_failure_aborts_whole_bundle(self, monkeypatch):
        monkeypatch.setattr(
            sa, "list_skill_files", lambda *a, **k: (["SKILL.md", "broken.md"], None)
        )
        monkeypatch.setattr(
            sa,
            "fetch_skill_file",
            lambda ws, tok, c, s, leaf, rel: (
                (b"ok", None) if rel == "SKILL.md" else (None, "HTTP 500 Server Error")
            ),
        )

        bundle, reason = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")

        assert bundle is None
        assert reason == "HTTP 500 Server Error"

    def test_concurrent_bundle_assembles_correctly(self, monkeypatch):
        paths = [f"file_{i}.md" for i in range(10)]
        content_map = {p: f"content {i}".encode() for i, p in enumerate(paths)}
        monkeypatch.setattr(sa, "list_skill_files", lambda *a, **k: (paths, None))
        monkeypatch.setattr(
            sa, "fetch_skill_file", lambda ws, tok, c, s, leaf, rel: (content_map[rel], None)
        )

        bundle, reason = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")

        assert reason is None
        assert bundle == content_map

    def test_any_file_failure_returns_none_not_partial(self, monkeypatch):
        paths = ["a.md", "b.md", "c.md", "bad.md"]
        monkeypatch.setattr(sa, "list_skill_files", lambda *a, **k: (paths, None))
        monkeypatch.setattr(
            sa,
            "fetch_skill_file",
            lambda ws, tok, c, s, leaf, rel: (
                (None, "HTTP 500") if rel == "bad.md" else (b"ok", None)
            ),
        )

        bundle, reason = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")

        assert bundle is None
        assert reason == "HTTP 500"

    def test_listing_failure_does_not_call_fetch(self, monkeypatch):
        fetched = []
        monkeypatch.setattr(sa, "list_skill_files", lambda *a, **k: ([], "listing failed"))
        monkeypatch.setattr(
            sa, "fetch_skill_file", lambda *a, **k: fetched.append(a) or (b"x", None)
        )

        bundle, reason = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")

        assert bundle is None
        assert reason == "listing failed"
        assert fetched == []

    def test_concurrency_cap_never_exceeded(self, monkeypatch):
        paths = [f"file_{i}.md" for i in range(50)]
        monkeypatch.setattr(sa, "list_skill_files", lambda *a, **k: (paths, None))

        counter_lock = threading.Lock()
        state = {"in_flight": 0, "max_in_flight": 0}

        def fake_fetch(ws, tok, c, s, leaf, rel):
            with counter_lock:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            time.sleep(0.005)
            with counter_lock:
                state["in_flight"] -= 1
            return b"data", None

        monkeypatch.setattr(sa, "fetch_skill_file", fake_fetch)

        bundle, reason = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")

        assert reason is None
        assert len(bundle) == 50
        assert state["max_in_flight"] <= sa._MAX_CONCURRENT_FILES_API_REQUESTS

    def test_failure_returns_before_blocked_fetches_complete(self, monkeypatch):
        blocker = threading.Event()
        paths = ["fail.md", "slow_a.md", "slow_b.md"]
        monkeypatch.setattr(sa, "list_skill_files", lambda *a, **k: (paths, None))

        def fake_fetch(ws, tok, c, s, leaf, rel):
            if rel == "fail.md":
                return None, "HTTP 500"
            blocker.wait()
            return b"data", None

        monkeypatch.setattr(sa, "fetch_skill_file", fake_fetch)

        result = [None]

        def run():
            result[0] = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")

        t = threading.Thread(target=run)
        t.start()
        try:
            t.join(timeout=2.0)
            assert not t.is_alive(), (
                "fetch_skill_bundle did not return within 2s — fast-fail broken"
            )
            bundle, reason = result[0]
            assert bundle is None
            assert reason == "HTTP 500"
        finally:
            blocker.set()
            t.join()

    def test_queued_fetches_are_cancelled_after_a_failure(self, monkeypatch):
        paths = ["fail.md"] + [f"file_{i}.md" for i in range(6)]
        release = threading.Event()
        started = []

        def fake_fetch(ws, tok, c, s, leaf, rel):
            if rel == "fail.md":
                return None, "HTTP 429"
            started.append(rel)
            release.wait()
            return b"data", None

        pool = ThreadPoolExecutor(max_workers=2)
        monkeypatch.setattr(sa, "_files_api_pool", pool)
        monkeypatch.setattr(sa, "list_skill_files", lambda *a, **k: (paths, None))
        monkeypatch.setattr(sa, "fetch_skill_file", fake_fetch)
        try:
            result = sa.fetch_skill_bundle(WS, "token", "main", "default", "triage")
        finally:
            release.set()
            pool.shutdown(wait=True)

        assert result == (None, "HTTP 429")
        assert len(started) <= 2


class TestGetSkill:
    def test_returns_ref_with_location_parsed_from_fqn(self, monkeypatch):
        captured = {}

        def fake_get(url, token, timeout=30):
            captured["url"] = url
            return {
                "name": "skills/ml.prod.pii-handling",
                "bundle_name": "pii-handling",
                "finalize_time": "2026-06-26T05:58:25Z",
            }, None

        monkeypatch.setattr(sa, "_http_get_json", fake_get)

        result = sa.get_skill(WS, "token", "ml.prod.pii-handling")

        assert result == ref("pii-handling", catalog="ml", schema="prod")
        assert captured["url"] == f"{WS}/api/2.1/unity-catalog/skills/ml.prod.pii-handling"

    def test_not_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            sa, "_http_get_json", lambda url, token, timeout=30: (None, "HTTP 404 Not Found")
        )

        assert sa.get_skill(WS, "token", "main.default.gone") is None

    def test_unfinalized_skill_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            sa,
            "_http_get_json",
            lambda url, token, timeout=30: ({"name": "skills/main.default.draft"}, None),
        )

        assert sa.get_skill(WS, "token", "main.default.draft") is None


class TestSkillRefMetadata:
    def test_captures_uc_attribution_fields(self, monkeypatch):
        payload = {
            "skills": [
                {
                    "name": "skills/main.default.triage",
                    "bundle_name": "triage",
                    "finalize_time": "2026-06-26T05:58:25Z",
                    "id": "skill-uuid",
                    "metastore_id": "metastore-uuid",
                    "update_time": "2026-06-26T05:58:25Z",
                }
            ]
        }
        monkeypatch.setattr(sa, "_http_get_json", lambda url, token, timeout=30: (payload, None))

        (skill,), reason = sa.list_schema_skills(WS, "token", "main", "default")

        assert reason is None
        assert skill.metastore_id == "metastore-uuid"
        assert skill.skill_id == "skill-uuid"
        assert skill.uc_update_time == "2026-06-26T05:58:25Z"


def _walk_stub(schemas, reason=None):
    """A ``walk_catalog_schemas`` stub that probes each (catalog, schema) in order."""

    def walk(workspace, token, *, deadline, probe, collect, **kwargs):
        total = len(schemas)
        for done, (catalog, schema) in enumerate(schemas, start=1):
            collect(probe(catalog, schema), done, total)
        return reason

    return walk


class TestListAllSkills:
    def test_flattens_and_streams_across_schemas(self, monkeypatch):
        monkeypatch.setattr(
            sa, "walk_catalog_schemas", _walk_stub([("main", "default"), ("ml", "prod")])
        )
        by_schema = {
            "main.default": [ref("triage"), ref("pii")],
            "ml.prod": [ref("scoring", catalog="ml", schema="prod")],
        }
        monkeypatch.setattr(
            sa, "list_schema_skills", lambda ws, tok, c, s: (by_schema[f"{c}.{s}"], None)
        )
        streamed = []
        progress = []

        refs, reason = sa.list_all_skills(
            WS,
            "token",
            on_skills=streamed.append,
            on_progress=lambda done, total, found: progress.append((done, total, found)),
        )

        assert reason is None
        assert [r.fqn for r in refs] == [
            "main.default.pii",
            "main.default.triage",
            "ml.prod.scoring",
        ]
        assert [[r.fqn for r in batch] for batch in streamed] == [
            ["main.default.pii", "main.default.triage"],
            ["ml.prod.scoring"],
        ]
        assert progress == [(1, 2, 2), (2, 2, 3)]

    def test_dedupes_repeated_fqns(self, monkeypatch):
        monkeypatch.setattr(
            sa, "walk_catalog_schemas", _walk_stub([("main", "default"), ("main", "default")])
        )
        monkeypatch.setattr(sa, "list_schema_skills", lambda ws, tok, c, s: ([ref("triage")], None))
        streamed = []

        refs, reason = sa.list_all_skills(WS, "token", on_skills=streamed.append)

        assert [r.fqn for r in refs] == ["main.default.triage"]
        assert streamed == [[ref("triage")]]

    def test_walk_failure_returns_its_reason(self, monkeypatch):
        monkeypatch.setattr(
            sa, "walk_catalog_schemas", _walk_stub([], reason="no UC catalogs found")
        )

        assert sa.list_all_skills(WS, "token") == ([], "no UC catalogs found")

    def test_empty_walk_reports_no_skills(self, monkeypatch):
        monkeypatch.setattr(sa, "walk_catalog_schemas", _walk_stub([("main", "default")]))
        monkeypatch.setattr(sa, "list_schema_skills", lambda *a, **k: ([], None))

        assert sa.list_all_skills(WS, "token") == ([], "no skills found")

    def test_timeout_returns_partial_results_with_reason(self, monkeypatch):
        monkeypatch.setattr(sa, "walk_catalog_schemas", _walk_stub([("main", "default")]))
        monkeypatch.setattr(sa, "list_schema_skills", lambda ws, tok, c, s: ([ref("triage")], None))

        refs, reason = sa.list_all_skills(WS, "token", deadline_seconds=-1)

        assert [r.fqn for r in refs] == ["main.default.triage"]
        assert reason == sa._SKILLS_WALK_TIMEOUT_REASON
