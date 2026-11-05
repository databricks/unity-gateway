"""Unit checks for the integration harness's virtual terminal screen."""

import pyte

from tests.integration.utils.terminal import TerminalScreen


def _screen() -> TerminalScreen:
    return TerminalScreen(10, 2, lambda data: None)


def test_display_renders_text_like_pyte():
    screen = _screen()
    pyte.Stream(screen).feed("hi 世界")

    assert screen.display[0] == "hi 世界" + " " * 3
    assert screen.display == pyte.Screen.display.fget(screen)


def test_display_treats_an_empty_cell_as_a_space():
    screen = _screen()
    pyte.Stream(screen).feed("ab")
    screen.buffer[0][1] = screen.buffer[0][1]._replace(data="")

    assert screen.display[0] == "a" + " " * 9
