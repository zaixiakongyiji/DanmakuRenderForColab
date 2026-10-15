"""过期请求恢复仅使用临时目录，不操作真实浏览器或活动监控。"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

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
                self.assert_blocked('仍连接' if connection else '无法确认')

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
            notebook_url='https://colab.test/old-notebook', reopen_missing=True)

    def test_legacy_state_uses_explicit_recovery_with_current_notebook(self):
        self.monitor.state.notebook_url = None
        self.monitor.state.save(self.monitor.state_file)
        self.monitor.recover_expired()
        self.launcher.runtime_connection_state.assert_called_once_with(
            notebook_url=None, reopen_missing=True)

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

    def test_restart_restores_pending_original_page_without_relaunch(self):
        self.clock -= 850
        self.monitor.state.status, self.monitor.state.last_error = 'opened', None
        self.monitor.state.save(self.monitor.state_file)
        self.launcher.notebook_url = 'https://colab.test/new-notebook'
        self.launcher.restore_notebook = Mock(return_value=True)
        restarted = TriggerMonitor(self.monitor.room_url, self.launcher,
            self.monitor.state_file, now=lambda: self.clock)
        request_before = self.request.read_bytes()
        restarted.resume_pending()
        self.launcher.restore_notebook.assert_called_once_with('https://colab.test/old-notebook')
        self.launcher._auto_run_browser.assert_called_once()
        self.assertEqual(restarted.state.run_id, self.run_id)
        self.assertEqual(self.request.read_bytes(), request_before)
        self.assertEqual(restarted.state.status, 'opened')

    def test_resume_skips_expired_terminal_and_uncertain_requests(self):
        self.launcher.restore_notebook = Mock(return_value=True)
        self.monitor.resume_pending()
        self.clock -= 850
        self.monitor.state.status, self.monitor.state.last_error = 'opened', None
        write_ack(self.ack, self.run_id, 'success')
        self.monitor.resume_pending()
        self.ack.unlink()
        self.monitor.state.ack_status = None
        self.monitor.state.status, self.monitor.state.last_error = 'needs_attention', 'launch_state_unknown'
        self.monitor.resume_pending()
        self.launcher.restore_notebook.assert_not_called()

    def test_failed_page_restore_preserves_request_and_reports_problem(self):
        self.clock -= 850
        self.monitor.state.status, self.monitor.state.last_error = 'opened', None
        self.launcher.restore_notebook = Mock(return_value=False)
        self.monitor.resume_pending()
        self.assertEqual(self.monitor.state.last_error, 'pending_notebook_unavailable')
        self.assertEqual(self.monitor.state.run_id, self.run_id)
        self.launcher._auto_run_browser.assert_called_once()

    def test_restart_restores_all_active_states_after_claim_deadline(self):
        self.launcher.notebook_url = 'https://colab.test/new-notebook'
        self.launcher.restore_notebook = Mock(return_value=True)
        request_before = self.request.read_bytes()
        for status in ('accepted', 'running', 'draining'):
            with self.subTest(status=status):
                write_ack(self.ack, self.run_id, status)
                value = json.loads(self.ack.read_text(encoding='utf-8'))
                value['updated_at'] = datetime.fromtimestamp(self.clock, timezone.utc).isoformat()
                self.ack.write_text(json.dumps(value), encoding='utf-8')
                self.monitor.observe(None)
                restarted = TriggerMonitor(self.monitor.room_url, self.launcher,
                    self.monitor.state_file, now=lambda: self.clock)
                self.assertTrue(restarted.resume_pending())
                self.assertEqual(restarted.state.status, status)
                self.assertEqual(restarted.state.ack_status, status)
                self.assertEqual(restarted.state.run_id, self.run_id)
                self.launcher.restore_notebook.assert_called_with('https://colab.test/old-notebook')
        self.assertEqual(self.request.read_bytes(), request_before)
        self.launcher._auto_run_browser.assert_called_once()

    def test_new_running_ack_restores_page_even_when_saved_state_is_opened(self):
        self.monitor.state.status, self.monitor.state.last_error = 'opened', None
        write_ack(self.ack, self.run_id, 'running')
        self.launcher.restore_notebook = Mock(return_value=False)
        self.assertFalse(self.monitor.resume_pending())
        self.launcher.restore_notebook.assert_called_once_with('https://colab.test/old-notebook')
        self.assertEqual(self.monitor.state.ack_status, 'running')
        self.assertEqual(self.monitor.state.last_error, 'runtime_heartbeat_stale')
        self.launcher._auto_run_browser.assert_called_once()


class BrowserRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.launcher = ColabLauncher('https://colab.test/original', Path('.'),
                                     auto_run=True, cdp_url='http://127.0.0.1:9222')
        self.page = Mock(url='https://colab.test/original#scrollTo=cell')
        self.context = Mock(pages=[Mock(url='about:blank')])
        self.context.new_page.return_value = self.page
        self.browser = Mock(contexts=[self.context])
        manager = MagicMock()
        self.pw = manager.__enter__.return_value
        self.pw.chromium.connect_over_cdp.return_value = self.browser
        self.api = Mock(sync_playwright=Mock(return_value=manager))
        self.patch_api = patch.dict(sys.modules, {'playwright.sync_api': self.api})
        self.patch_api.start()
        self.addCleanup(self.patch_api.stop)

    def test_missing_tab_is_reopened_without_running_or_connecting(self):
        with patch.object(self.launcher, '_connection_state', return_value=False):
            self.assertIs(self.launcher.runtime_connection_state(reopen_missing=True), False)
        self.context.new_page.assert_called_once()
        self.page.goto.assert_called_once_with(self.launcher.notebook_url,
            wait_until='domcontentloaded', timeout=30000)
        self.page.get_by_role.assert_not_called()

    def test_regular_probe_does_not_reopen_missing_tab(self):
        self.assertIsNone(self.launcher.runtime_connection_state())
        self.context.new_page.assert_not_called()

    def test_reopened_page_waits_for_ui_and_preserves_connected_state(self):
        for connected in (True, False):
            with self.subTest(connected=connected), patch.object(
                    self.launcher, '_connection_state', side_effect=[None, connected]):
                self.assertIs(self.launcher.runtime_connection_state(reopen_missing=True), connected)
        self.assertEqual(self.page.wait_for_timeout.call_count, 2)

    def test_existing_original_tab_is_reused_after_settings_change(self):
        self.context.pages = [self.page]
        with patch.object(self.launcher, '_connection_state', return_value=False):
            self.assertIs(self.launcher.runtime_connection_state(
                notebook_url=self.page.url, reopen_missing=True), False)
        self.context.new_page.assert_not_called()
        self.page.goto.assert_not_called()

    def test_duplicate_connected_tab_prevents_false_disconnected_result(self):
        self.context.pages = [self.page, Mock(url=self.launcher.notebook_url)]
        with patch.object(self.launcher, '_connection_state', side_effect=[False, True]):
            self.assertIs(self.launcher.runtime_connection_state(reopen_missing=True), True)
        self.context.new_page.assert_not_called()

    def test_unrecognized_ui_and_navigation_errors_remain_unknown(self):
        with patch.object(self.launcher, '_connection_state', return_value=None), \
             patch('colab_support.trigger.time.monotonic', side_effect=[0, 16]):
            self.assertIsNone(self.launcher.runtime_connection_state(reopen_missing=True))
        self.page.goto.side_effect = RuntimeError('navigation failed')
        self.assertIsNone(self.launcher.runtime_connection_state(reopen_missing=True))

    def test_pending_restore_navigates_blank_tab_without_run_all(self):
        blank = self.context.pages[0]
        blank.url = 'about:blank'
        blank.goto.side_effect = lambda *args, **kwargs: setattr(blank, 'url', args[0])
        self.assertTrue(self.launcher.restore_notebook(self.launcher.notebook_url))
        blank.goto.assert_called_once_with(self.launcher.notebook_url,
            wait_until='domcontentloaded', timeout=30000)
        blank.get_by_role.return_value.first.click.assert_not_called()
        blank.bring_to_front.assert_called_once()
        self.context.new_page.assert_not_called()

    def test_pending_restore_reuses_original_and_preserves_other_tabs(self):
        self.context.pages = [self.page]
        self.assertTrue(self.launcher.restore_notebook(self.launcher.notebook_url))
        self.page.goto.assert_not_called()
        self.page.bring_to_front.assert_called_once()
        self.context.new_page.assert_not_called()
        self.context.pages = [Mock(url='https://other.test')]
        self.assertTrue(self.launcher.restore_notebook(self.launcher.notebook_url))
        self.context.pages[0].goto.assert_not_called()
        self.context.new_page.assert_called_once()

    def test_restore_requires_rendered_notebook_before_reporting_success(self):
        self.context.pages = [self.page]
        self.page.get_by_role.return_value.first.wait_for.side_effect = RuntimeError('not rendered')
        self.assertFalse(self.launcher.restore_notebook(self.launcher.notebook_url))
        self.page.bring_to_front.assert_not_called()

    def test_pending_restore_navigation_failure_is_reported(self):
        self.context.pages[0].goto.side_effect = RuntimeError('navigation failed')
        self.assertFalse(self.launcher.restore_notebook(self.launcher.notebook_url))


if __name__ == '__main__':
    unittest.main()
