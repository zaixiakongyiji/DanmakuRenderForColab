"""本地 Colab 触发器状态机测试。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from colab_support.trigger import ColabLauncher, TriggerMonitor, validate_room_url


class TriggerTests(unittest.TestCase):
    def test_url_validation(self):
        self.assertEqual(validate_room_url("https://live.bilibili.com/123/"), "https://live.bilibili.com/123/")
        for value in ("http://live.bilibili.com/1", "https://example.com/1", "https://live.bilibili.com/foo"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_room_url(value)

    def test_requires_two_live_observations_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as d:
            browser = Mock(return_value=True)
            launcher = ColabLauncher("https://colab.test/notebook", open_browser=True, browser=browser)
            monitor = TriggerMonitor("https://live.bilibili.com/1", launcher, Path(d) / "state.json",
                                     live_confirmations=2, offline_confirmations=2)
            self.assertEqual(monitor.observe(False).state, "offline")
            self.assertFalse(monitor.observe(True).triggered)
            result = monitor.observe(True)
            self.assertTrue(result.triggered)
            self.assertEqual(browser.call_count, 1)
            self.assertFalse(monitor.observe(True).triggered)
            self.assertEqual(browser.call_count, 1)
            saved = json.loads((Path(d) / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["triggered"])

    def test_probe_failure_does_not_end_active_run(self):
        with tempfile.TemporaryDirectory() as d:
            launcher = ColabLauncher(open_browser=False)
            monitor = TriggerMonitor("https://live.bilibili.com/1", launcher, Path(d) / "state.json",
                                     live_confirmations=1, offline_confirmations=2)
            self.assertTrue(monitor.observe(True).triggered)
            self.assertEqual(monitor.observe(None).state, "probe_unavailable")
            self.assertTrue(monitor.state.triggered)
            self.assertEqual(monitor.observe(False).state, "ending")
            self.assertEqual(monitor.observe(False).state, "ended")
            self.assertFalse(monitor.state.triggered)

    def test_drive_control_file_is_private_input(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            launcher = ColabLauncher(open_browser=False, drive_sync_root=root)
            monitor = TriggerMonitor("https://live.bilibili.com/1", launcher, root / "state.json",
                                     live_confirmations=1)
            result = monitor.observe(True)
            control = json.loads((root / "DMRColab/control/trigger.json").read_text(encoding="utf-8"))
            self.assertEqual(control["room_url"], "https://live.bilibili.com/1")
            self.assertEqual(control["run_id"], result.run_id)

    def test_launch_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as d:
            browser = Mock(return_value=False)
            launcher = ColabLauncher(open_browser=True, browser=browser)
            monitor = TriggerMonitor("https://live.bilibili.com/1", launcher, Path(d) / "state.json",
                                     live_confirmations=1)
            self.assertEqual(monitor.observe(True).state, "needs_attention")
            self.assertFalse(monitor.state.triggered)
            browser.return_value = True
            self.assertEqual(monitor.observe(True).state, "needs_attention")
            self.assertEqual(browser.call_count, 1)


if __name__ == "__main__":
    unittest.main()
