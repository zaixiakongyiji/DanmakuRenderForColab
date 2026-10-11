"""过期请求恢复仅使用临时目录，不操作真实浏览器或活动监控。"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from colab_support.trigger import ColabLauncher, TriggerMonitor, control_paths, write_ack


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = datetime.now(timezone.utc).timestamp()
        self.launcher = ColabLauncher('https://colab.test/old-notebook', self.root,
                                     auto_run=True, cdp_url='http://127.0.0.1:9222')
        self.launcher._auto_run_browser = Mock(return_value=True)
        self.launcher.runtime_connection_state = Mock(return_value=False)
        self.monitor = TriggerMonitor('https://live.bilibili.com/1', self.launcher,
                                      self.root / 'state.json', 2, now=lambda: self.clock)
        self.monitor.observe(True)
        self.run_id = self.monitor.observe(True).run_id
        self.request, self.claim, self.ack = control_paths(self.root, self.run_id)
        self.clock += 901
        self.assertEqual(self.monitor.observe(True).error, 'claim_timeout')

    def assert_blocked(self, message):
        before = self.monitor.state_file.read_bytes()
        with self.assertRaisesRegex(ValueError, message):
            self.monitor.recover_expired()
        self.assertEqual(self.monitor.state_file.read_bytes(), before)
        self.assertEqual(self.monitor.state.run_id, self.run_id)
        self.assertFalse(list(self.root.glob('*.bak.json')))

    def test_expired_unclaimed_idle_request_is_archived_and_reconfirmed(self):
        request_before = self.request.read_bytes()
        old_state = json.loads(self.monitor.state_file.read_text(encoding='utf-8'))
        archive = self.monitor.recover_expired()
        self.assertEqual(json.loads(archive.read_text(encoding='utf-8')), old_state)
        self.assertEqual(self.request.read_bytes(), request_before)
        self.assertIsNone(self.monitor.state.run_id)
        self.assertFalse(self.monitor.observe(True).triggered)
        result = self.monitor.observe(True)
        self.assertTrue(result.triggered)
        self.assertNotEqual(result.run_id, self.run_id)
        self.assertEqual(self.launcher._auto_run_browser.call_count, 2)

    def test_claim_and_any_ack_block_recovery(self):
        for path in (self.claim, self.ack):
            with self.subTest(path=path.name):
                path.write_text('{}', encoding='utf-8')
                self.assert_blocked('已有认领或回执')
                path.unlink()

    def test_restart_can_recover_before_timeout_label_was_saved(self):
        self.monitor.state.status = 'opened'
        self.monitor.state.last_error = None
        self.monitor.state.save(self.monitor.state_file)
        archive = self.monitor.recover_expired()
        self.assertTrue(archive.is_file())
        self.assertIsNone(self.monitor.state.run_id)

    def test_late_claim_during_browser_check_blocks_recovery(self):
        def check(**kwargs):
            self.claim.write_text('{}', encoding='utf-8')
            return False
        self.launcher.runtime_connection_state.side_effect = check
        self.assert_blocked('已有认领或回执')

    def test_running_state_blocks_recovery(self):
        write_ack(self.ack, self.run_id, 'running')
        self.monitor.observe(True)
        self.assert_blocked('只有认领超时')
        self.launcher.runtime_connection_state.assert_not_called()

    def test_connected_or_unknown_runtime_blocks_recovery(self):
        for connection in (True, None):
            with self.subTest(connection=connection):
                self.launcher.runtime_connection_state.return_value = connection
                self.assert_blocked('仍连接或无法确认')

    def test_invalid_missing_and_unexpired_requests_block_recovery(self):
        original = self.request.read_text(encoding='utf-8')
        self.request.write_text('[]', encoding='utf-8')
        self.assert_blocked('无法核实旧请求已过期')
        self.request.unlink()
        self.assert_blocked('无法核实旧请求已过期')
        self.request.write_text(original, encoding='utf-8')
        self.clock -= 901
        self.assert_blocked('无法核实旧请求已过期')

    def test_recovery_checks_original_notebook_after_settings_change(self):
        self.launcher.notebook_url = 'https://colab.test/new-notebook'
        self.monitor.recover_expired()
        self.launcher.runtime_connection_state.assert_called_once_with(
            notebook_url='https://colab.test/old-notebook')

    def test_legacy_state_uses_explicit_recovery_with_current_notebook(self):
        self.monitor.state.notebook_url = None
        self.monitor.state.save(self.monitor.state_file)
        self.monitor.recover_expired()
        self.launcher.runtime_connection_state.assert_called_once_with()

    def test_failed_archive_keeps_old_state(self):
        archive = self.root / ('state.expired-' + self.run_id + '.bak.json')
        archive.write_text('existing evidence', encoding='utf-8')
        before = self.monitor.state_file.read_bytes()
        with self.assertRaisesRegex(ValueError, '无法归档'):
            self.monitor.recover_expired()
        self.assertEqual(self.monitor.state_file.read_bytes(), before)
        self.assertEqual(archive.read_text(encoding='utf-8'), 'existing evidence')

    def test_recovery_flag_reaches_trigger_command(self):
        import colab_monitor
        value = {'task_config': 'test.yml', 'drive_sync_root': str(self.root),
                 'notebook_url': self.launcher.notebook_url, 'cdp_url': self.launcher.cdp_url}
        with patch.object(colab_monitor, 'task_room', return_value=self.monitor.room_url):
            self.assertNotIn('--recover-expired', colab_monitor.trigger_command(value))
            self.assertIn('--recover-expired', colab_monitor.trigger_command(value, True))


if __name__ == '__main__':
    unittest.main()
