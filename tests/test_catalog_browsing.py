"""Component coverage for metadata discovery and the shared MCP/Skills browser."""

import asyncio
import threading

import pytest
import questionary
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ucode import databricks, mcp, skills_api, skills_download
from ucode.ui.interactive_picker import (
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
    assert "Location: cat.sch.securable" in choice.description
    assert "Bundle: bundle" in choice.description
    assert "Description: Analyze tickets" in choice.description
    assert "Created by: creator@example.com" in choice.description
    assert "Created: 2026-08-01 00:00 UTC" in choice.description
    assert "Updated: 2026-09-01 00:00 UTC" in choice.description


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
    assert not mcp.build_mcp_picker_choices(
        [],
        [],
        [],
        [],
        ["cat.sch.docs"],
        managed_names={"cat-sch-docs"},
        service_metadata={"cat.sch.docs": details},
    )


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
    assert "Created: 2026-09-01 00:00 UTC" in rendered
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
                    questionary.Choice(name, value=name, description=picker_details(name, name))
                    for name in ["first", "second"]
                ],
                "space select, enter save",
                picker_style(),
                show_description=True,
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

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            choices = [
                questionary.Choice(
                    f"cat.sch.tool{index}",
                    value=str(index),
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
            )
            task = asyncio.create_task(question.ask_async())
            try:
                await asyncio.sleep(0.1)
                assert "cat.sch.tool0" in rendered()
                pipe.send_text("\x1b[6~\x1b[6~")  # Page Down to audit fields
                await asyncio.sleep(0.1)
                text = rendered()
                assert "Updated:" in text
                assert "2026-10-15 12:30 UTC" in text
                pipe.send_text("\x1b[B")  # Next choice resets details to the top
                await asyncio.sleep(0.1)
                assert "Location: cat.sch.tool1" in rendered()
                pipe.send_text(" \r")
                assert await asyncio.wait_for(task, 2) == ["1"]
            finally:
                if not task.done():
                    pipe.send_text("\x03")
                    await asyncio.wait_for(task, 2)

    asyncio.run(run())
