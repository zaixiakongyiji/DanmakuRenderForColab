"""本地触发只测试模拟浏览器和私有临时目录。"""
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock

from colab_support.trigger import (ColabLauncher, TriggerMonitor, TRIGGER_SCHEMA_VERSION,
    claim_request, control_paths, validate_room_url, validate_trigger_request, write_ack)
from colab_support.locks import ProcessLock
from colab_support.notebook_session import prepare_session


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = datetime.now(timezone.utc).timestamp()
        self.browser = Mock(return_value=True)
        self.launcher = ColabLauncher('https://colab.test/notebook', self.root, browser=self.browser)
        self.monitor = TriggerMonitor('https://live.bilibili.com/1', self.launcher,
            self.root / 'state.json', live_confirmations=1, offline_confirmations=2, now=lambda: self.clock)

    def start(self):
        return self.monitor.observe(True)

    def ack(self, status):
        path = control_paths(self.root, self.monitor.state.run_id)[2]
        write_ack(path, self.monitor.state.run_id, status)
        value = json.loads(path.read_text())
        value['updated_at'] = datetime.fromtimestamp(self.clock, timezone.utc).isoformat()
        path.write_text(json.dumps(value))

    def test_url_and_real_cli_parameters(self):
        from colab_trigger import parser as trigger_parser
        from colab_support.cli import parser as cloud_parser
        args = trigger_parser().parse_args(['--url', 'https://live.bilibili.com/1',
            '--auto-run', '--cdp-url', 'http://127.0.0.1:9222', '--drive-sync-root', str(self.root)])
        self.assertTrue(args.auto_run)
        cloud = cloud_parser().parse_args(['--url', args.url, '--run-dir', 'test'])
        self.assertEqual(cloud.segment, 3600)
        for value in ('http://live.bilibili.com/1', 'https://example.com/1', 'https://live.bilibili.com/1?secret'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_room_url(value)
        with self.assertRaises(ValueError):
            ColabLauncher(auto_run=True, drive_sync_root=self.root, cdp_url='http://example.com:9222')
        with self.assertRaises(ValueError):
            ColabLauncher(auto_run=True, cdp_url='http://127.0.0.1:9222')

    def test_dedup_and_run_scoped_requests(self):
        result = self.start()
        self.assertTrue(result.triggered)
        self.monitor.observe(True)
        self.assertEqual(self.browser.call_count, 1)
        request, _, _ = control_paths(self.root, result.run_id)
        payload = json.loads(request.read_text())
        self.assertEqual(payload['run_id'], result.run_id)
        self.assertEqual(payload['schema_version'], TRIGGER_SCHEMA_VERSION)
        self.assertEqual(claim_request(self.root, payload), control_paths(self.root, result.run_id)[2])
        with self.assertRaises(ValueError):
            claim_request(self.root, payload)

    def test_probe_failure_never_rearms_active_run(self):
        self.start()
        self.monitor.observe(None)
        for _ in range(4):
            self.monitor.observe(False)
        self.assertTrue(self.monitor.state.triggered)
        self.assertEqual(self.browser.call_count, 1)

    def test_terminal_and_continuous_offline_rearm(self):
        first = self.start().run_id
        self.ack('success')
        self.monitor.observe(False)
        self.monitor.observe(None)
        self.monitor.observe(False)
        self.assertEqual(self.monitor.state.run_id, first)
        self.monitor.observe(False)
        self.assertIsNone(self.monitor.state.run_id)
        second = self.start().run_id
        self.assertNotEqual(first, second)
        self.assertEqual(self.browser.call_count, 2)

    def test_claim_timeout_and_stale_heartbeat_need_attention(self):
        self.start()
        self.clock += 901
        self.assertEqual(self.monitor.observe(True).error, 'claim_timeout')
        self.assertEqual(self.browser.call_count, 1)
        self.ack('running')
        self.clock += 301
        self.assertEqual(self.monitor.observe(False).error, 'runtime_heartbeat_stale')
        self.assertTrue(self.monitor.state.triggered)

    def test_launch_uncertainty_and_corrupted_state_fail_closed(self):
        self.browser.return_value = False
        self.assertEqual(self.start().state, 'needs_attention')
        self.monitor.observe(True)
        self.assertEqual(self.browser.call_count, 1)
        (self.root / 'state.json').write_text('invalid')
        other = TriggerMonitor('https://live.bilibili.com/1', self.launcher, self.root / 'state.json', live_confirmations=1)
        self.assertEqual(other.observe(True).state, 'needs_attention')
        self.assertEqual(self.browser.call_count, 1)

    def test_expired_request_and_path_traversal_rejected(self):
        result = self.start()
        payload = json.loads(control_paths(self.root, result.run_id)[0].read_text())
        with self.assertRaisesRegex(ValueError, 'expired'):
            validate_trigger_request(payload, datetime.now(timezone.utc) + timedelta(hours=1))
        payload['run_id'] = '../other'
        with self.assertRaises(ValueError):
            validate_trigger_request(payload)
        with self.assertRaises(ValueError):
            control_paths(self.root, '../other')

    def test_single_process_lock(self):
        with ProcessLock(self.root / 'process.lock'):
            with self.assertRaises(RuntimeError):
                with ProcessLock(self.root / 'process.lock'):
                    pass
        with ProcessLock(self.root / 'process.lock'):
            pass

    def test_browser_busy_dialog_and_exact_single_click(self):
        page = MagicMock()
        alerts = MagicMock()
        alerts.all_text_contents.return_value = []
        button = MagicMock()
        button.count.return_value = 1
        button.is_enabled.return_value = button.is_visible.return_value = True
        absent = MagicMock()
        absent.count.return_value = 0
        def role(kind, **kwargs):
            if kind in ('alert', 'status'):
                return alerts
            if kind == 'dialog' or '停止' in str(kwargs.get('name')) or 'Sign in' in str(kwargs.get('name')):
                return absent
            if 'Connected' in str(kwargs.get('name')):
                return button
            return button
        page.get_by_role.side_effect = role
        self.assertTrue(ColabLauncher._click_run_all(page))
        button.click.assert_called_once()
        alerts.all_text_contents.return_value = ['正在执行']
        self.assertFalse(ColabLauncher._click_run_all(page))
        self.assertEqual(button.click.call_count, 1)

    def test_notebook_new_ids_and_failed_preflight_invalidates(self):
        args = (self.root, self.root / 'drive', self.root / 'cookie', 'https://live.bilibili.com/1', 'python')
        first = prepare_session(*args, work_root=self.root / 'work')
        second = prepare_session(*args, work_root=self.root / 'work')
        self.assertNotEqual(first.run_id, second.run_id)
        runner = Mock()
        with self.assertRaises(RuntimeError):
            first.run(runner)
        with self.assertRaises(RuntimeError):
            first.preflight(Mock(side_effect=RuntimeError('preflight failed')))
        with self.assertRaises(RuntimeError):
            first.run(runner)
        runner.assert_not_called()
        second.preflight(Mock())
        runner.return_value = 0
        self.assertEqual(second.run(runner), 0)
        with self.assertRaises(RuntimeError):
            second.run(runner)

    def test_notebook_sources_parse_without_saved_output(self):
        notebook = json.loads((Path(__file__).resolve().parents[1] / 'notebooks/colab_record.ipynb').read_text(encoding='utf-8'))
        for cell in notebook['cells']:
            if cell['cell_type'] == 'code':
                compile(''.join(cell['source']), cell['id'], 'exec')
                self.assertFalse(cell['outputs'])
                self.assertIsNone(cell['execution_count'])


if __name__ == '__main__':
    unittest.main()
