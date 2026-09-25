import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from playwright.sync_api import Error as BrowserError

from study_assistant import launch_browser_context


class BrowserLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.playwright = Mock()
        self.playwright.chromium.launch_persistent_context = Mock()

    def test_auto_uses_chrome_when_available(self):
        context = object()
        self.playwright.chromium.launch_persistent_context.return_value = context

        result, channel = launch_browser_context(
            self.playwright, {"browser_channel": "auto"}, Path(self.temp.name)
        )

        self.assertIs(result, context)
        self.assertEqual(channel, "chrome")
        self.assertEqual(
            self.playwright.chromium.launch_persistent_context.call_args.kwargs["channel"],
            "chrome",
        )

    def test_auto_falls_back_to_edge_when_chrome_fails(self):
        context = object()
        launcher = self.playwright.chromium.launch_persistent_context
        launcher.side_effect = [BrowserError("Chrome executable not found"), context]

        result, channel = launch_browser_context(
            self.playwright, {"browser_channel": "auto"}, Path(self.temp.name)
        )

        self.assertIs(result, context)
        self.assertEqual(channel, "msedge")
        self.assertEqual(launcher.call_count, 2)
        self.assertEqual(launcher.call_args.kwargs["channel"], "msedge")

    def test_explicit_chrome_also_falls_back_to_edge(self):
        context = object()
        launcher = self.playwright.chromium.launch_persistent_context
        launcher.side_effect = [BrowserError("Chrome executable not found"), context]

        result, channel = launch_browser_context(
            self.playwright, {"browser_channel": "chrome"}, Path(self.temp.name)
        )

        self.assertIs(result, context)
        self.assertEqual(channel, "msedge")
        self.assertEqual(launcher.call_count, 2)

    def test_explicit_edge_does_not_try_chrome(self):
        context = object()
        launcher = self.playwright.chromium.launch_persistent_context
        launcher.return_value = context

        result, channel = launch_browser_context(
            self.playwright, {"browser_channel": "msedge"}, Path(self.temp.name)
        )

        self.assertIs(result, context)
        self.assertEqual(channel, "msedge")
        self.assertEqual(launcher.call_count, 1)
        self.assertEqual(launcher.call_args.kwargs["channel"], "msedge")

    def test_auto_reports_when_both_browsers_fail(self):
        launcher = self.playwright.chromium.launch_persistent_context
        launcher.side_effect = [BrowserError("Chrome missing"), BrowserError("Edge missing")]

        with self.assertRaisesRegex(BrowserError, "chrome.*msedge"):
            launch_browser_context(
                self.playwright, {"browser_channel": "auto"}, Path(self.temp.name)
            )


if __name__ == "__main__":
    unittest.main()
