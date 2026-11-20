"""Component checks for VS Code click readiness."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.integration.utils.vscode import CLICK_TIMEOUT_MS, VSCode


class TestHoverReadiness(unittest.TestCase):
    def setUp(self):
        session = SimpleNamespace(home=Path("."), env={"XDG_CONFIG_HOME": "."})
        self.ui = VSCode(session, "hover")
        # The browser and installed application are external boundaries in these checks.
        self.ui.page = Mock()
        self.ui.code = SimpleNamespace(poll=lambda: None)
        self.target = Mock()

    def test_click_waits_for_hover_to_close(self):
        hover = {"visible": True}
        observations = iter((1, 0))

        def count():
            hover["visible"] = bool(next(observations))
            return int(hover["visible"])

        self.ui.page.locator.return_value.count.side_effect = count
        self.target.click.side_effect = lambda **kwargs: self.assertFalse(hover["visible"])
        self.ui._click("model picker: open", self.target)
        self.target.click.assert_called_once_with(timeout=CLICK_TIMEOUT_MS)
        self.ui.page.mouse.move.assert_called_once_with(0, 0)

    def test_click_without_hover(self):
        self.ui.page.locator.return_value.count.return_value = 0
        self.ui._click("model picker: open", self.target)
        self.target.click.assert_called_once_with(timeout=CLICK_TIMEOUT_MS)

    def test_persistent_hover_fails_without_clicking(self):
        self.ui.page.locator.return_value.count.return_value = 1
        with patch("tests.integration.utils.vscode.STEP_TIMEOUT_S", 0.01):
            with self.assertRaisesRegex(AssertionError, "hover tooltip to close"):
                self.ui._click("model picker: open", self.target)
        self.target.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
