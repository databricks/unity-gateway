"""Streaming multi-select picker built on questionary / prompt_toolkit.

Presentation-only primitives shared by the product pickers (MCP services, skills). They operate on
a generic `questionary.Choice` list and an optional `background_loader`, with no MCP- or
Databricks-specific logic, so any caller can supply its own choices and discovery walk.
"""

from __future__ import annotations

import string
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC
from typing import Any

import questionary
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.filters import Condition, IsDone
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.utils import get_cwidth
from questionary.prompts.common import InquirerControl
from questionary.question import Question
from questionary.styles import merge_styles_default

from ucode.time_utils import parse_update_time

PICKER_VISIBLE_ROWS = 10

# Cap the highlighted-row description preview so a long description stays within the
# footer instead of dominating the screen.
_DESCRIPTION_PREVIEW_CHARS = 240
# Left margin for the description footer, applied to wrapped lines too (see get_line_prefix).
_DESCRIPTION_INDENT = "  "


def _description_preview(description: str) -> str:
    """``description`` truncated to the footer budget, with an ellipsis when clipped."""
    if len(description) <= _DESCRIPTION_PREVIEW_CHARS:
        return description
    return description[: _DESCRIPTION_PREVIEW_CHARS - 1].rstrip() + "…"


def _description_footer_tokens(description: str) -> list[tuple[str, str]]:
    """Footer tokens for the highlighted row's description.

    A caller emphasizes a leading label by formatting the description as ``"label: text"``:
    the ``label:`` renders bold and the rest as the truncated preview. A description with no
    ``": "`` renders entirely as the preview."""
    description = " ".join(description.split())
    label, sep, body = description.partition(": ")
    if not sep:
        return [("class:instruction", _description_preview(description))]
    return [("bold", f"{label}{sep}"), ("class:instruction", _description_preview(body))]


def picker_details(
    name: str,
    description: object,
    *,
    created_by: object = None,
    create_time: object = None,
    update_time: object = None,
    bundle_name: str | None = None,
) -> str:
    """Build plain-text detail lines from list metadata; never fetch bundles or credentials.

    Creator is labeled as such: UC does not expose the bundle's author. Missing fields
    remain explicit rather than implying that the owner is the author.
    """

    def text(value: object, fallback: str = "Not available") -> str:
        return " ".join(value.split()) if isinstance(value, str) and value.strip() else fallback

    def date(value: object) -> str:
        parsed = parse_update_time(value) if isinstance(value, str) else None
        return parsed.astimezone(UTC).strftime("%Y-%m-%d") if parsed else "Not available"

    lines = [f"Full name: {text(name)}"]
    if bundle_name:
        lines.append(f"Bundle: {text(bundle_name)}")
    lines.extend(
        [
            f"Description: {text(description, 'No description provided')}",
            f"Created by: {text(created_by)}",
            f"Created: {date(create_time)}",
            f"Updated: {date(update_time)}",
        ]
    )
    return "\n".join(lines)


def _detail_tokens(description: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for index, line in enumerate(description.splitlines()):
        if index:
            tokens.append(("", "\n"))
        tokens.extend(_description_footer_tokens(line))
    return tokens


class _Back:
    """Sentinel type: a wizard step returns the `_BACK` instance when the user
    presses Left (←) to go back. Distinct from None (cancel) and [] (empty)."""


# Singleton instance used everywhere; compare with `is _BACK`.
_BACK = _Back()


def picker_style() -> questionary.Style:
    return questionary.Style(
        [
            ("pointer", "fg:cyan bold"),
            ("highlighted", "noinherit"),
            ("selected", "noinherit"),
            ("answer", "fg:cyan"),
        ]
    )


class CatalogChoice(questionary.Choice):
    """A searchable choice whose table cells are independent of its selection state."""

    def __init__(
        self,
        title: str,
        value: str,
        *,
        name: str,
        location: str,
        status: str,
        checked: bool = False,
        disabled: str | None = None,
        description: str | None = None,
    ):
        super().__init__(
            title, value=value, checked=checked, disabled=disabled, description=description
        )
        self.name = name
        self.location = location
        self.status = status


def _table_cell(value: str, width: int) -> str:
    value = " ".join(value.split())
    if get_cwidth(value) > width:
        clipped = ""
        for char in value:
            if get_cwidth(clipped + char) > width - 1:
                break
            clipped += char
        value = clipped + "…"
    return value + " " * max(0, width - get_cwidth(value))


def _table_columns(width: int) -> tuple[int, int, int]:
    # Keep the table compact on wide terminals. Stretching short values across the full
    # viewport makes related cells harder to scan and leaves the status far from the name.
    width = min(width, 96)
    status = 16 if width >= 60 else 6
    remaining = max(2, width - 9 - 4 - status)
    return remaining // 2, remaining - remaining // 2, status


def _table_cells(name: str, location: str, status: str, width: int) -> str:
    name_width, location_width, status_width = _table_columns(width)
    if width < 60 and status.startswith(("✓", "✗")):
        status = status[0]
    return "  ".join(
        [
            _table_cell(name, name_width),
            _table_cell(location, location_width),
            _table_cell(status, status_width),
        ]
    )


class StreamingInquirerControl(InquirerControl):
    """`InquirerControl` that tolerates an empty or all-disabled choice list.

    Stock `InquirerControl.__init__` ends with ``if not self.is_selection_valid(): raise`` and
    `is_selection_valid` dereferences `pointed_at`, which `_init_choices` leaves unset when no row
    is selectable — so constructing it with an empty (or every-row-disabled) list raises, and
    navigation later hits the same unset cursor. Our picker intentionally opens on an empty list
    and fills it in via the background loader, and `ug mcp add` can legitimately show only
    already-configured (disabled) rows. Default the cursor and treat "nothing selectable" as valid
    so construction, rendering, and navigation don't crash."""

    def __init__(self, choices, *, table: bool = False, **kwargs):
        self.table = table
        super().__init__(choices, **kwargs)

    def is_selection_valid(self) -> bool:
        if getattr(self, "pointed_at", None) is None:
            self.pointed_at = 0
        selectable = any(
            not isinstance(c, questionary.Separator) and not c.disabled for c in self.choices
        )
        if not selectable:
            # Empty, or every row a separator/disabled: nothing to validate, and nothing for the
            # navigation skip-loop to land on — report valid so we neither raise nor spin.
            return True
        if self.pointed_at >= len(self.choices):
            return False
        if self.table and isinstance(self.get_pointed_at(), CatalogChoice):
            # Configured/managed rows stay browsable for their details; Space and
            # select-all still exclude their disabled choices.
            return True
        return super().is_selection_valid()

    def _get_choice_tokens(self):
        # Stock rendering unconditionally reads `filtered_choices[pointed_at]`, which raises on an
        # empty list. Render nothing when there are no rows (the picker is still streaming them in).
        if not self.filtered_choices:
            return []
        if not self.table:
            return super()._get_choice_tokens()
        tokens = []
        width = get_app().output.get_size().columns
        for index, choice in enumerate(self.filtered_choices):
            if index:
                tokens.append(("", "\n"))
            pointed = index == self.pointed_at
            tokens.append(("class:pointer", "› " if pointed else "  "))
            if pointed:
                tokens.append(("[SetCursorPosition]", ""))
            selected = choice.value in self.selected_options
            checkbox = "[-]" if choice.disabled else "[x]" if selected else "[ ]"
            tokens.append(("class:text", checkbox + "    "))
            if isinstance(choice, CatalogChoice):
                name, location, status = choice.name, choice.location, choice.status
            else:
                # Legacy MCP registrations have no UC location, but remain removable.
                name = str(choice.title)
                location = "—"
                status = "✓ Configured" if choice.checked else "✗ Not configured"
            style = (
                "class:disabled"
                if choice.disabled
                else "class:highlighted"
                if pointed
                else "class:text"
            )
            tokens.append((style, _table_cells(name, location, status, width)))
        return tokens


def merge_new_choices(
    existing: list[questionary.Choice | questionary.Separator],
    new_choices: list[questionary.Choice],
) -> tuple[list[questionary.Choice | questionary.Separator], int]:
    """Merge streamed choices by value and return the rows plus the number newly discovered.

    A rich catalog row replaces a plain initial row with the same value. This preserves the
    selection (which is value-based) while adding the streamed location and detail metadata.
    """
    incoming = {choice.value: choice for choice in new_choices}
    merged: list[questionary.Choice | questionary.Separator] = []
    seen: set[Any] = set()
    added = 0
    for choice in existing:
        if isinstance(choice, questionary.Choice) and choice.value in incoming:
            replacement = incoming[choice.value]
            if isinstance(replacement, CatalogChoice) and not isinstance(choice, CatalogChoice):
                merged.append(replacement)
            else:
                merged.append(choice)
            seen.add(choice.value)
        else:
            merged.append(choice)
    for choice in new_choices:
        if choice.value not in seen:
            merged.append(choice)
            seen.add(choice.value)
            added += 1
    return merged, added


def scrolling_checkbox(
    message: str,
    choices: list[questionary.Choice | questionary.Separator],
    instruction: str,
    style: questionary.Style,
    allow_back: bool = False,
    background_loader: Callable[
        [Callable[[list[questionary.Choice]], None], threading.Event], str | None
    ]
    | None = None,
    loading_noun: str = "MCP services",
    show_description: bool = False,
    table: bool = False,
) -> Question:
    """Multi-select checkbox picker.

    ``background_loader``, if given, streams more choices in after the picker is already
    on screen: it's run on a daemon thread and handed an ``append(choices)`` callback that
    adds rows (deduped by value) and repaints, so the picker opens instantly on whatever
    ``choices`` are ready and fills in the rest without blocking. A footer shows a live
    "Loading more {loading_noun}, this might take a few seconds..." count while it runs.

    ``show_description`` adds a footer previewing the highlighted row's ``Choice.description``.
    Newline-separated metadata retains its labels; Page Up/Down scroll the detail pane.
    It's a separate window rather than questionary's inline ``show_description`` because the
    choices window is sized to the row count, so an inline line would be clipped."""
    merged_style = merge_styles_default(
        [
            questionary.Style([("bottom-toolbar", "noreverse")]),
            style,
        ]
    )
    # Empty-tolerant control: the picker can open with zero selectable rows (streaming in via the
    # background loader, or an `mcp add` where everything is already configured) — see the subclass.
    control = StreamingInquirerControl(
        choices,
        pointer="›",
        show_description=False,
        table=table,
    )
    # Live loading state for the background-loader footer (see below).
    loading: dict[str, Any] = {"active": background_loader is not None, "found": 0, "message": None}

    def get_prompt_tokens() -> list[tuple[str, str]]:
        tokens = [("class:qmark", ""), ("class:question", f" {message} ")]
        if control.is_answered:
            selected_count = len(control.selected_options)
            answer = "done" if selected_count == 0 else f"done ({selected_count} selections)"
            tokens.append(("class:answer", answer))
        else:
            tokens.append(("class:instruction", instruction))
        return tokens

    def get_selected_values() -> list[Any]:
        return [choice.value for choice in control.get_selected_values()]

    def perform_validation() -> bool:
        control.error_message = None
        return True

    def detail_height() -> int:
        return min(8, max(1, app.output.get_size().rows // 3))

    def visible_choice_rows() -> int:
        # Reserve the prompt and optional loading/search/scroll footers. Re-evaluate
        # on resize so the detail pane cannot push the choices out of a short terminal.
        reserved = 5 + int(table) + (detail_height() + 1 if show_description else 0)
        return min(PICKER_VISIBLE_ROWS, max(1, app.output.get_size().rows - reserved))

    @Condition
    def has_more_choices() -> bool:
        # Live so the scroll hint appears as background-loaded rows stream in.
        return len(control.choices) > visible_choice_rows()

    @Condition
    def show_loading_footer() -> bool:
        return bool(loading["active"]) or loading["message"] is not None

    def loading_tokens() -> list[tuple[str, str]]:
        if loading["active"]:
            return [
                (
                    "class:instruction",
                    f"  ⏳ Loading more {loading_noun}, this might take a few seconds..."
                    f" ({loading['found']} found)",
                )
            ]
        return [("class:instruction", f"  {loading['message']}")]

    @Condition
    def has_search_string() -> bool:
        return control.get_search_string_tokens() is not None

    def pointed_description() -> str | None:
        if not (show_description and control.filtered_choices):
            return None
        try:
            pointed = control.get_pointed_at()
        except IndexError:
            return None
        description = getattr(pointed, "description", None)
        return description if isinstance(description, str) and description else None

    detail_line = {"offset": 0}

    def description_tokens() -> list[tuple[str, str]]:
        description = pointed_description()
        if description is None:
            return []
        return _detail_tokens("\n".join(description.splitlines()[detail_line["offset"] :]))

    @Condition
    def has_description() -> bool:
        return pointed_description() is not None

    validation_prompt: PromptSession = PromptSession(bottom_toolbar=lambda: control.error_message)
    # Render the prompt as a fixed 1-row window rather than a PromptSession
    # container: the latter expands to fill the terminal height, which in a tall
    # window pushes the choices list to the very bottom (a large blank gap).
    layout = Layout(
        HSplit(
            [
                Window(
                    height=Dimension.exact(1),
                    content=FormattedTextControl(get_prompt_tokens),
                ),
                ConditionalContainer(
                    Window(
                        height=Dimension.exact(1),
                        content=FormattedTextControl(
                            lambda: [
                                (
                                    "bold",
                                    "  Select "
                                    + _table_cells(
                                        "Name",
                                        "UC location",
                                        "Status",
                                        app.output.get_size().columns,
                                    ),
                                )
                            ]
                        ),
                    ),
                    filter=Condition(lambda: table) & ~IsDone(),
                ),
                ConditionalContainer(
                    # Height tracks the live choice count (capped at the visible max) so the
                    # window grows as background-loaded rows stream in, with no blank gap when
                    # only a few choices are present.
                    Window(
                        control,
                        height=lambda: Dimension.exact(
                            min(visible_choice_rows(), max(1, len(control.choices)))
                        ),
                    ),
                    filter=~IsDone(),
                ),
                ConditionalContainer(
                    Window(
                        height=Dimension.exact(1),
                        content=FormattedTextControl(
                            lambda: [("class:instruction", "  ↑/↓ scroll for more")]
                        ),
                    ),
                    filter=has_more_choices & ~IsDone(),
                ),
                ConditionalContainer(
                    Window(
                        height=Dimension.exact(1),
                        content=FormattedTextControl(loading_tokens),
                    ),
                    filter=show_loading_footer & ~IsDone(),
                ),
                ConditionalContainer(
                    Window(
                        height=Dimension.exact(2),
                        content=FormattedTextControl(control.get_search_string_tokens),
                    ),
                    filter=has_search_string & ~IsDone(),
                ),
                ConditionalContainer(
                    validation_prompt.layout.container,
                    filter=Condition(lambda: control.error_message is not None),
                ),
                # Pinned at the bottom, one blank line below the other footers, so the
                # highlighted row's description reads as a separate detail pane.
                ConditionalContainer(
                    HSplit(
                        [
                            Window(height=Dimension.exact(1)),
                            Window(
                                height=lambda: Dimension(
                                    min=1, preferred=detail_height(), max=detail_height()
                                ),
                                content=FormattedTextControl(
                                    description_tokens,
                                    show_cursor=False,
                                ),
                                wrap_lines=True,
                                get_line_prefix=lambda line, wrap: _DESCRIPTION_INDENT,
                            ),
                        ]
                    ),
                    filter=has_description & ~IsDone(),
                ),
            ]
        )
    )

    bindings = KeyBindings()
    cancel_event = threading.Event()

    def _exit(event: Any, **kwargs: Any) -> None:
        cancel_event.set()
        event.app.exit(**kwargs)

    @bindings.add(Keys.ControlQ, eager=True)
    @bindings.add(Keys.ControlC, eager=True)
    def _(event: Any) -> None:
        _exit(event, exception=KeyboardInterrupt, style="class:aborting")

    @bindings.add(" ", eager=True)
    def _(_event: Any) -> None:
        if control.choice_count == 0:
            return  # nothing to toggle (e.g. picker still streaming, or all rows filtered out)
        pointed = control.get_pointed_at()
        if isinstance(pointed, questionary.Separator) or pointed.disabled:
            return  # separators and already-configured (disabled) rows aren't toggleable
        pointed_choice = pointed.value
        if pointed_choice in control.selected_options:
            control.selected_options.remove(pointed_choice)
        else:
            control.selected_options.append(pointed_choice)
        perform_validation()

    @bindings.add(Keys.ControlA, eager=True)
    def _(_event: Any) -> None:
        # Toggle-all: select every selectable choice, or clear the selection if
        # everything is already selected. `a` alone is reserved for type-to-filter.
        selectable = [
            choice.value
            for choice in control.choices
            if not isinstance(choice, questionary.Separator) and not choice.disabled
        ]
        if all(value in control.selected_options for value in selectable):
            control.selected_options = []
        else:
            control.selected_options = list(selectable)
        perform_validation()

    def move_cursor_down(event: Any) -> None:
        detail_line["offset"] = 0
        if control.choice_count == 0:
            return
        control.select_next()
        # Bound the skip-past-disabled scan so an all-disabled list can't spin forever.
        tries = 0
        while not control.is_selection_valid() and tries < control.choice_count:
            control.select_next()
            tries += 1

    def move_cursor_up(event: Any) -> None:
        detail_line["offset"] = 0
        if control.choice_count == 0:
            return
        control.select_previous()
        tries = 0
        while not control.is_selection_valid() and tries < control.choice_count:
            control.select_previous()
            tries += 1

    def search_filter(event: Any) -> None:
        detail_line["offset"] = 0
        control.add_search_character(event.key_sequence[0].key)

    for character in string.printable:
        if character in string.whitespace:
            continue
        bindings.add(character, eager=True)(search_filter)
    bindings.add(Keys.Backspace, eager=True)(search_filter)

    bindings.add(Keys.Down, eager=True)(move_cursor_down)
    bindings.add(Keys.Up, eager=True)(move_cursor_up)
    bindings.add(Keys.ControlN, eager=True)(move_cursor_down)
    bindings.add(Keys.ControlP, eager=True)(move_cursor_up)

    if show_description:

        @bindings.add(Keys.PageDown, eager=True)
        def _(_event: Any) -> None:
            description = pointed_description() or ""
            detail_line["offset"] = min(
                max(0, len(description.splitlines()) - 1), detail_line["offset"] + 3
            )

        @bindings.add(Keys.PageUp, eager=True)
        def _(_event: Any) -> None:
            detail_line["offset"] = max(0, detail_line["offset"] - 3)

    @bindings.add(Keys.ControlM, eager=True)
    def _(event: Any) -> None:
        control.submission_attempted = True
        if perform_validation():
            control.is_answered = True
            _exit(event, result=get_selected_values())

    if allow_back:

        @bindings.add(Keys.Left, eager=True)
        def _(event: Any) -> None:
            # Wizard back-navigation: exit this step with the _BACK sentinel so
            # the caller re-shows the previous step. Left arrow is otherwise
            # unused in this multi-select (cursor moves with up/down).
            _exit(event, result=_BACK)

    @bindings.add(Keys.Any)
    def _(_event: Any) -> None:
        """Ignore other text input."""

    app: Application = Application(
        layout=layout,
        key_bindings=bindings,
        style=merged_style,
    )

    if background_loader is not None:
        started = False

        def run_on_loop(fn: Callable[[], None]) -> None:
            # Background updates MUST run on the picker's event-loop thread: mutating the control
            # off-thread drops rows (prompt_toolkit's invalidate() no-ops until the app is running)
            # and disturbs live input (toggling/removal). Wait briefly for the app to start, then
            # hand `fn` to the loop; bail once the picker has closed or if it never starts in time.
            nonlocal started
            deadline = time.monotonic() + 15.0
            while not (app.is_running and app.loop is not None):
                if started or time.monotonic() > deadline:
                    return
                time.sleep(0.02)
            started = True
            with suppress(Exception):
                app.loop.call_soon_threadsafe(fn)

        def append(new_choices: list[questionary.Choice]) -> None:
            def apply() -> None:
                # On the UI thread: rebind choices (atomic; render reads the list live) and repaint.
                # Selections track by value, so appended rows never disturb checkboxes/scroll/filter.
                merged, added = merge_new_choices(control.choices, new_choices)
                if merged != control.choices:
                    control.choices = merged
                    loading["found"] += added
                    app.invalidate()

            run_on_loop(apply)

        def worker() -> None:
            message: str | None = None
            try:
                message = background_loader(append, cancel_event)
            except Exception:
                # Discovery is best-effort; a failed background walk just stops streaming.
                pass
            finally:

                def finish() -> None:
                    loading["active"] = False
                    loading["message"] = message
                    app.invalidate()

                run_on_loop(finish)

        threading.Thread(target=worker, name="picker-loader", daemon=True).start()

    return Question(app)
