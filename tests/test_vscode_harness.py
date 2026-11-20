"""Component checks for VS Code click failure evidence."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.integration.utils.vscode import CAPTURE_TIMEOUT_MS, CLICK_TIMEOUT_MS, VSCode


class TestClickEvidence(unittest.TestCase):
    def setUp(self):
        session = SimpleNamespace(
            home=Path("."), env={"XDG_CONFIG_HOME": "."}, artifacts=Path("."), record=Mock()
        )
        self.ui = VSCode(session, "picker")
        # The browser is the external boundary; the real click/evidence helper runs.
        self.ui.page = Mock()
        self.ui.page.evaluate.return_value = {"picker": None, "byokToast": False}
        self.ui.page.locator.return_value.evaluate_all.return_value = [
            {"text": "Use configured approval settings", "bounds": {"x": 1, "y": 2}}
        ]
        self.target = Mock()
        self.target.bounding_box.return_value = {"x": 1, "y": 2, "width": 10, "height": 20}
        self.error = RuntimeError("tooltip intercepted pointer events")

    def test_success_keeps_the_short_click_and_records_no_failure(self):
        self.ui._click("model picker: open", self.target)
        self.target.click.assert_called_once_with(timeout=CLICK_TIMEOUT_MS)
        self.ui.session.record.assert_not_called()
        self.ui.page.screenshot.assert_not_called()

    def test_failed_click_retains_evidence_and_original_error(self):
        self.target.click.side_effect = self.error
        with self.assertRaises(RuntimeError) as caught:
            self.ui._click("model picker: open", self.target)
        self.assertIs(caught.exception, self.error)
        name, evidence = self.ui.session.record.call_args.args
        self.assertEqual(name, "picker-click-failure-1.json")
        self.assertEqual(evidence["action"], "model picker: open")
        self.assertEqual(evidence["error"], str(self.error))
        self.assertEqual(evidence["targetBounds"], self.target.bounding_box.return_value)
        self.assertEqual(evidence["hovers"][0]["text"], "Use configured approval settings")
        self.assertEqual(evidence["screen"], self.ui.page.evaluate.return_value)
        self.assertGreaterEqual(evidence["elapsedSeconds"], 0)
        self.ui.page.screenshot.assert_called_once()
        self.assertEqual(self.ui.page.screenshot.call_args.kwargs["timeout"], CAPTURE_TIMEOUT_MS)
        self.target.bounding_box.assert_called_once_with(timeout=CAPTURE_TIMEOUT_MS)
        self.target.click.assert_called_once_with(timeout=CLICK_TIMEOUT_MS)

    def test_missing_target_keeps_core_failure_evidence(self):
        self.target.click.side_effect = self.error
        self.target.bounding_box.side_effect = TimeoutError("target disappeared")
        with self.assertRaises(RuntimeError) as caught:
            self.ui._click("model picker: open", self.target)
        self.assertIs(caught.exception, self.error)
        evidence = self.ui.session.record.call_args.args[1]
        self.assertEqual(evidence["error"], str(self.error))
        self.assertEqual(evidence["captureError"], "TimeoutError")
        self.assertEqual(evidence["hovers"][0]["text"], "Use configured approval settings")

    def test_capture_failure_preserves_click_error(self):
        self.target.click.side_effect = self.error
        self.ui.session.record.side_effect = OSError("artifact unavailable")
        with self.assertRaises(RuntimeError) as caught:
            self.ui._click("model picker: open", self.target)
        self.assertIs(caught.exception, self.error)
        self.assertIn("click capture failed: OSError", self.ui.actions[-1])
        self.target.click.assert_called_once_with(timeout=CLICK_TIMEOUT_MS)


if __name__ == "__main__":
    unittest.main()
