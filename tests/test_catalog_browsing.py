"""Component coverage for metadata discovery and the shared MCP/Skills browser."""

import asyncio
import contextlib
import threading

import pytest
import questionary
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ucode import databricks, mcp, skills_api, skills_download
from ucode.ui.interactive_picker import (
    CatalogChoice,
    StreamingInquirerControl,
    _detail_tokens,
    picker_details,
    picker_style,
    scrolling_checkbox,
)


def test_mcp_listing_retains_metadata_without_extra_requests(monkeypatch):
    calls = []
    pages = iter(
        [
            {
                "mcp_services": [
                    {
                        "name": "mcp-services/system.ai.search",
                        "comment": "Search docs",
                        "created_by": "creator@example.com",
                        "create_time": "2026-09-01T00:00:00Z",
                    },
                    {"name": "mcp-services/other.schema.hidden", "comment": "Wrong parent"},
                    {
                        "name": "mcp-services/system.ai.inactive",
                        "config": {"connection": {"status": "FAILED"}},
                    },
                ],
                "next_page_token": "next",
            },
            {"mcp_services": [{"name": "mcp-services/system.ai.empty"}]},
        ]
    )

    def get(url, token, timeout):
        calls.append(url)
        return next(pages), None

    monkeypatch.setattr(databricks, "_http_get_json", get)
    metadata = {}
    names, reason = databricks.list_mcp_services("https://example.com", "token", metadata=metadata)
    assert reason is None
    assert names == ["system.ai.empty", "system.ai.search"]
    assert set(metadata) == set(names)
    assert metadata["system.ai.search"]["comment"] == "Search docs"
    assert metadata["system.ai.search"]["created_by"] == "creator@example.com"
    assert len(calls) == 2
    assert all("view=BASIC" in url for url in calls)
    assert "page_token=next" in calls[1]


def test_streamed_mcp_metadata_is_ready_before_rows_are_emitted(monkeypatch):
    metadata = {}
    emitted = []

    def walk(workspace, token, *, probe, collect, **kwargs):
        for index, schema in enumerate(["one", "two"]):
            collect(probe("cat", schema), index + 1, 2)
        return None

    def listing(workspace, token, parent, *, metadata):
        name = parent + ".tool"
        metadata[name] = {"comment": "Tool in " + parent}
        return [name], None

    def on_services(names):
        emitted.extend((name, metadata[name]["comment"]) for name in names)

    monkeypatch.setattr(databricks, "walk_catalog_schemas", walk)
    monkeypatch.setattr(databricks, "list_mcp_services", listing)
    names, reason = databricks.list_all_mcp_services(
        "https://example.com",
        "token",
        metadata=metadata,
        on_services=on_services,
    )
    assert reason is None
    assert names == ["cat.one.tool", "cat.two.tool"]
    assert emitted == [("cat.one.tool", "Tool in cat.one"), ("cat.two.tool", "Tool in cat.two")]


def test_skill_metadata_survives_parsing_and_choice_construction(tmp_path):
    ref = skills_api._skill_ref(
        {
            "name": "skills/cat.sch.securable",
            "bundle_name": "bundle",
            "description": "Analyze tickets",
            "finalize_time": "2026-09-01T00:00:00Z",
            "created_by": "creator@example.com",
            "create_time": "2026-08-01T00:00:00Z",
            "update_time": "2026-09-01T00:00:00Z",
        }
    )
    assert ref is not None
    choice = skills_download._skill_download_choice(ref, [tmp_path])
    assert choice.value == "cat.sch.securable"
    assert "Full name: cat.sch.securable" in choice.description
    assert "Bundle: bundle" in choice.description
    assert "Description: Analyze tickets" in choice.description
    assert "Created by: creator@example.com" in choice.description
    assert "Created: 2026-08-01" in choice.description
    assert "Updated: 2026-09-01" in choice.description


def test_initial_and_streamed_mcp_choices_have_identical_details(monkeypatch):
    details = {"comment": "Read docs", "created_by": "creator@example.com"}
    initial = mcp.build_mcp_picker_choices(
        [],
        [],
        [],
        [],
        ["cat.sch.docs"],
        service_metadata={"cat.sch.docs": details},
    )[0]

    def discover(workspace, profile, *, on_services, metadata, **kwargs):
        metadata["cat.sch.docs"] = details
        on_services(["cat.sch.docs"])

    monkeypatch.setattr(mcp, "discover_all_mcp_service_names", discover)
    streamed = []
    mcp._mcp_services_background_loader("https://example.com", None, set(), True)(
        streamed.extend,
        threading.Event(),
    )
    assert streamed[0].description == initial.description
    assert streamed[0].value == initial.value
    assert "Created by: creator@example.com" in initial.description
    managed = mcp.build_mcp_picker_choices(
        [],
        [],
        [],
        [],
        ["cat.sch.docs"],
        managed_names={"cat-sch-docs"},
        service_metadata={"cat.sch.docs": details},
    )[0]
    assert managed.disabled == "Managed by workspace"
    assert managed.status == "✓ Managed"


def test_details_preserve_fields_and_normalize_dates():
    details = picker_details(
        "cat.sch.tool",
        "Long description. " * 100,
        created_by="creator\n@example.com",
        create_time="2026-09-01T03:00:00+03:00",
        update_time="invalid",
    )
    rendered = "".join(token for _, token in _detail_tokens(details))
    assert "…\nCreated by: creator @example.com" in rendered
    assert "Created: 2026-09-01" in rendered
    assert "Updated: Not available" in rendered
    missing = picker_details("cat.sch.tool", {"unexpected": "value"}, created_by=123)
    assert "Description: No description provided" in missing
    assert "Created by: Not available" in missing


@pytest.mark.parametrize(
    "keys, expected",
    [
        (" \x1b[B \r", ["first", "second"]),
        ("second \r", ["second"]),
        ("\x01\r", ["first", "second"]),
        ("\r", []),
        ("\x03", None),
    ],
)
def test_browser_keyboard_preserves_selection_and_cancellation(keys, expected):
    async def run():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            question = scrolling_checkbox(
                "Browse",
                [
                    CatalogChoice(
                        name,
                        value=name,
                        name=name,
                        location="cat.sch",
                        status="✗ Not installed",
                        description=picker_details(name, name),
                    )
                    for name in ["first", "second"]
                ],
                "space select, enter save",
                picker_style(),
                show_description=True,
                table=True,
            )
            task = asyncio.create_task(question.ask_async())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            result = await asyncio.wait_for(task, 2)
            assert result == expected

    asyncio.run(run())


def test_empty_browser_streams_choices_with_details_and_cancels_loader():
    cancel = []

    def loader(append, cancel_event):
        cancel.append(cancel_event)
        append([questionary.Choice("streamed", value="value", description="Details: streamed")])

    async def run():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            question = scrolling_checkbox(
                "Browse",
                [],
                "space select",
                picker_style(),
                show_description=True,
                table=True,
                background_loader=loader,
            )
            task = asyncio.create_task(question.ask_async())
            control = next(
                c
                for c in question.application.layout.find_all_controls()
                if isinstance(c, StreamingInquirerControl)
            )
            async with asyncio.timeout(2):
                while not control.choices:
                    await asyncio.sleep(0.01)
            assert control.choices[0].description == "Details: streamed"
            pipe.send_text(" \r")
            assert await asyncio.wait_for(task, 2) == ["value"]
            assert cancel[0].is_set()

    asyncio.run(run())


@pytest.mark.parametrize("columns, rows", [(80, 24), (40, 12)])
def test_rendered_browser_scrolls_details_in_small_terminals(columns, rows):
    """Render the real application and assert terminal contents and saved values."""
    from io import StringIO

    import pyte
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.vt100 import Vt100_Output

    async def run():
        stdout = StringIO()
        output = Vt100_Output(
            stdout,
            lambda: Size(rows=rows, columns=columns),
            term="xterm-256color",
            enable_cpr=False,
        )
        screen = pyte.Screen(columns, rows)
        stream = pyte.Stream(screen)
        offset = 0

        def rendered():
            nonlocal offset
            raw = stdout.getvalue()
            stream.feed(raw[offset:])
            offset = len(raw)
            return "\n".join(screen.display)

        async def eventually_shows(*expected):
            # Windows delivers pipe input via an executor thread; poll rather than sleep.
            text = rendered()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(5):
                    while not all(item in text for item in expected):
                        await asyncio.sleep(0.02)
                        text = rendered()
            for item in expected:
                assert item in text

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            choices = [
                CatalogChoice(
                    f"cat.sch.tool{index}",
                    value=str(index),
                    name=f"tool{index}",
                    location="cat.sch",
                    status="✗ Not installed",
                    description=picker_details(
                        f"cat.sch.tool{index}",
                        "Read documents. " * 40,
                        created_by="team@example.com",
                        update_time="2026-10-15T12:30:00Z",
                    ),
                )
                for index in range(20)
            ]
            question = scrolling_checkbox(
                "Browse",
                choices,
                "space select, enter save",
                picker_style(),
                show_description=True,
                table=True,
            )
            task = asyncio.create_task(question.ask_async())
            try:
                await eventually_shows("Select", "UC location", "tool0", "[ ]", "✗")
                pipe.send_text("\x1b[6~")  # One Page Down visibly moves the detail pane
                await eventually_shows("Updated:", "2026-10-15")
                pipe.send_text("\x1b[B")  # Next choice resets details to the top
                await eventually_shows("Full name: cat.sch.tool1")
                pipe.send_text(" \r")
                assert await asyncio.wait_for(task, 2) == ["1"]
            finally:
                if not task.done():
                    pipe.send_text("\x03")
                    await asyncio.wait_for(task, 2)

    asyncio.run(run())


def _install_browser_fixture(tmp_path, *, metastore_id="meta", workspace="https://example.com"):
    from ucode.skills_state import SkillInstall, record_downloads

    ref = skills_api.SkillRef("cat", "sch", "triage", "triage", metastore_id=metastore_id)
    roots = skills_download.skill_dir_roots(str(tmp_path))
    skills_download.write_skill(roots, ref, {"SKILL.md": b"skill"})
    record_downloads(
        [
            SkillInstall(
                fqn=ref.fqn,
                bundle_name=ref.bundle_name,
                workspace=workspace,
                scope="user",
                base=str(tmp_path),
                dirs=tuple(str(root / ref.bundle_name) for root in roots),
                metastore_id=metastore_id,
            )
        ]
    )
    return ref, roots


def test_installed_marker_requires_exact_source_at_target_path(tmp_path):
    from dataclasses import replace

    ref, roots = _install_browser_fixture(tmp_path)
    choice = skills_download._skill_download_choice(ref, roots)
    assert choice.status == "✓ Installed"
    assert choice.checked is False  # installed does not mean selected to download
    assert choice.disabled is None  # updates are still allowed
    same_bundle_other_schema = replace(ref, schema="other")
    other = skills_download._skill_download_choice(same_bundle_other_schema, roots)
    assert other.status == "✗ Not installed"
    assert "Local bundle: Already on disk" in other.description
    assert (
        skills_download._skill_download_choice(
            replace(ref, metastore_id="different"),
            roots,
        ).status
        == "✗ Not installed"
    )
    # The same source installed elsewhere must not mark this target installed.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other_roots = skills_download.skill_dir_roots(str(elsewhere))
    skills_download.write_skill(other_roots, ref, {"SKILL.md": b"unattributed"})
    assert skills_download._skill_download_choice(ref, other_roots).status == "✗ Not installed"


def test_installed_marker_requires_files_in_every_target_root(tmp_path):
    ref, roots = _install_browser_fixture(tmp_path)
    (roots[1] / ref.bundle_name / "SKILL.md").unlink()
    choice = skills_download._skill_download_choice(ref, roots)
    assert choice.status == "✗ Not installed"
    assert "installation is incomplete" in choice.description


def test_workspace_only_attribution_is_not_shared_across_workspaces(tmp_path):
    ref, roots = _install_browser_fixture(tmp_path, metastore_id=None)
    assert (
        skills_download._skill_download_choice(
            ref,
            roots,
            workspace="https://example.com/",
        ).status
        == "✓ Installed"
    )
    assert (
        skills_download._skill_download_choice(
            ref,
            roots,
            workspace="https://other.example.com",
        ).status
        == "✗ Not installed"
    )
    assert skills_download._skill_download_choice(ref, roots).status == "✗ Not installed"


def test_skill_loader_reads_attribution_once_for_all_streamed_batches(monkeypatch, tmp_path):
    ref, roots = _install_browser_fixture(tmp_path)
    calls = []
    real_list = skills_download.list_downloaded
    monkeypatch.setattr(skills_download, "list_downloaded", lambda: calls.append(1) or real_list())

    def list_skills(workspace, token, *, on_skills, **kwargs):
        on_skills([ref])
        on_skills([skills_api.SkillRef("cat", "other", "triage", "triage", metastore_id="meta")])
        return [], None

    monkeypatch.setattr(skills_download, "list_all_skills", list_skills)
    choices = []
    skills_download._skills_download_background_loader("https://example.com", "token", roots)(
        choices.extend,
        threading.Event(),
    )
    assert calls == [1]
    assert [choice.status for choice in choices] == ["✓ Installed", "✗ Not installed"]


@pytest.mark.parametrize(
    "keys, expected",
    [
        ("managed \r", []),
        ("existing \r", []),
        ("\x01\r", ["add:mcp-service:cat.sch.new"]),
    ],
)
def test_read_only_mcp_rows_are_browsable_but_never_selected(keys, expected):
    choices = mcp.build_mcp_picker_choices(
        [],
        [],
        [],
        [{"name": "cat-sch-existing", "url": "https://example.com"}],
        ["cat.sch.managed", "cat.sch.existing", "cat.sch.new"],
        additive=True,
        managed_names={"cat-sch-managed"},
    )

    async def run():
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            question = scrolling_checkbox(
                "MCP",
                choices,
                "Select",
                picker_style(),
                table=True,
                show_description=True,
            )
            task = asyncio.create_task(question.ask_async())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, 2) == expected
            assert [choice.status for choice in choices] == [
                "✓ Managed",
                "✓ Configured",
                "✗ Not configured",
            ]

    asyncio.run(run())
