"""本地触发只测试模拟浏览器和私有临时目录。"""
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from colab_support.drive_authorization import format_browser_exception
from colab_support.trigger import (ColabLauncher, TriggerMonitor, TRIGGER_SCHEMA_VERSION,
    check_playwright_loopback, claim_request, control_paths, validate_room_url,
    validate_trigger_request, write_ack)
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

    def test_click_authorize_drive_dialog(self):
        page = MagicMock()
        drive_btn = MagicMock()
        drive_btn.count.return_value = 1
        drive_btn.first.is_visible.return_value = True
        none_btn = MagicMock()
        none_btn.count.return_value = 0

        def role(kind, **kwargs):
            if '连接到 Google 云端硬盘' in str(kwargs.get('name')):
                return drive_btn
            return none_btn

        page.get_by_role.side_effect = role
        self.assertTrue(ColabLauncher._click_authorize_drive(page))
        drive_btn.first.click.assert_called_once()

        drive_btn.count.return_value = 0
        self.assertFalse(ColabLauncher._click_authorize_drive(page))

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

    def test_room_url_switch_gracefully_resets_or_conflicts(self):
        from colab_support.trigger import TriggerState
        state_file = self.root / 'switch_state.json'
        old_state = TriggerState(room_url='https://live.bilibili.com/1', status='offline')
        old_state.save(state_file)
        new_state = TriggerState.load(state_file, 'https://live.bilibili.com/2')
        self.assertEqual(new_state.room_url, 'https://live.bilibili.com/2')
        self.assertEqual(new_state.status, 'unknown')
        self.assertIsNone(new_state.last_error)

        active_state = TriggerState(room_url='https://live.bilibili.com/1', status='running',
                                    triggered=True, run_id='20261010-000000-00000000',
                                    triggered_at='2026-10-10T00:00:00+00:00')
        active_state.save(state_file)
        conflict_state = TriggerState.load(state_file, 'https://live.bilibili.com/2')
        self.assertEqual(conflict_state.status, 'needs_attention')
        self.assertEqual(conflict_state.last_error, 'active_run_conflict')
        self.assertTrue(conflict_state.triggered)
        self.assertEqual(conflict_state.run_id, '20261010-000000-00000000')

    def test_task_switch_preserves_uncertain_run_state(self):
        from colab_monitor import reset_trigger_state_if_inactive
        state_file = self.root / 'switch_state.json'
        state_file.write_text(json.dumps({
            'triggered': False, 'status': 'needs_attention',
            'run_id': '20261010-000000-00000000', 'last_error': 'launch_state_unknown'
        }), encoding='utf-8')
        self.assertFalse(reset_trigger_state_if_inactive('old.yml', 'new.yml', state_file))
        self.assertTrue(state_file.exists())

        state_file.write_text(json.dumps({'triggered': False, 'status': 'ended'}), encoding='utf-8')
        self.assertTrue(reset_trigger_state_if_inactive('old.yml', 'new.yml', state_file))
        self.assertFalse(state_file.exists())

        state_file.write_text('[]', encoding='utf-8')
        self.assertFalse(reset_trigger_state_if_inactive('old.yml', 'new.yml', state_file))
        self.assertTrue(state_file.exists())

    def test_cli_json_flag_and_logging_format(self):
        import logging
        import io
        from contextlib import redirect_stdout
        from colab_trigger import (
            parser as trigger_parser,
            format_trigger_message,
            monitor_loop,
            setup_logger,
            resolve_target_info,
        )
        import colab_trigger
        from colab_support.trigger import TriggerResult

        args = trigger_parser().parse_args(['--url', 'https://live.bilibili.com/12345', '--json'])
        self.assertTrue(args.json)

        args_default = trigger_parser().parse_args(['--url', 'https://live.bilibili.com/12345'])
        self.assertFalse(args_default.json)

        logger = setup_logger()
        self.assertEqual(logger.level, logging.INFO)
        self.assertTrue(len(logger.handlers) >= 1)

        target, room_id = resolve_target_info('https://live.bilibili.com/12345')
        self.assertEqual(room_id, '12345')
        self.assertTrue(len(target) > 0)

        # 检查各状态消息格式化
        level, msg = format_trigger_message(target, TriggerResult('offline'))
        self.assertEqual(level, logging.INFO)
        self.assertIn('直播未开播', msg)
        self.assertIn(target, msg)

        level, msg = format_trigger_message(target, TriggerResult('opened', triggered=True, run_id='run-123'))
        self.assertEqual(level, logging.INFO)
        self.assertIn('已发送 Colab 运行请求，等待认领', msg)
        self.assertIn('run-123', msg)

        level, msg = format_trigger_message(target, TriggerResult('opened', run_id='run-123'))
        self.assertIn('等待认领', msg)
        self.assertIn('未重复点击', msg)
        self.assertNotIn('正在打开', msg)

        level, msg = format_trigger_message(target, TriggerResult('running', run_id='run-123'))
        self.assertEqual(level, logging.INFO)
        self.assertIn('Colab 正常运行中', msg)

        level, msg = format_trigger_message(target, TriggerResult('needs_attention', run_id='run-123', error='claim_timeout'))
        self.assertEqual(level, logging.WARNING)
        self.assertIn('需人工关注', msg)
        self.assertIn('Colab 认领请求超时', msg)

        level, msg = format_trigger_message(target, TriggerResult('failed', run_id='run-123', error='oom'))
        self.assertEqual(level, logging.ERROR)
        self.assertIn('Colab 任务执行失败', msg)

        args = trigger_parser().parse_args([
            '--url', 'https://live.bilibili.com/12345', '--json', '--once',
        ])
        fake_monitor = Mock()
        fake_monitor.return_value.observe.return_value = TriggerResult(
            'opened', triggered=True, run_id='run-123')
        output = io.StringIO()
        with redirect_stdout(output), \
                patch.object(colab_trigger, 'TriggerMonitor', fake_monitor), \
                patch.object(colab_trigger, 'probe_bilibili', return_value=True), \
                patch.object(colab_trigger, 'resolve_target_info', return_value=('房间 12345', '12345')), \
                patch.object(colab_trigger, 'setup_logger', return_value=Mock()):
            self.assertEqual(monitor_loop(args, args.url, Mock()), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), {
            'status': 'opened', 'triggered': True, 'run_id': 'run-123', 'error': None,
        })

    def test_monitor_loop_deduplication_and_verbose(self):
        from unittest.mock import Mock, patch
        from colab_support.trigger import TriggerResult
        import colab_trigger
        from colab_trigger import parser as trigger_parser, monitor_loop

        # 默认模式：相同状态日志去重
        args_default = trigger_parser().parse_args([
            '--url', 'https://live.bilibili.com/12345', '--once',
        ])
        fake_monitor = Mock()
        fake_monitor.return_value.observe.side_effect = [
            TriggerResult('offline'),
            TriggerResult('offline'),
            TriggerResult('opened', triggered=True, run_id='run-123'),
        ]
        mock_logger = Mock()
        with patch.object(colab_trigger, 'TriggerMonitor', fake_monitor), \
                patch.object(colab_trigger, 'probe_bilibili', return_value=True), \
                patch.object(colab_trigger, 'resolve_target_info', return_value=('舍茶建乡', '12345')), \
                patch.object(colab_trigger, 'setup_logger', return_value=mock_logger), \
                patch('time.sleep', return_value=None):
            self.assertEqual(monitor_loop(args_default, args_default.url, Mock()), 0)

        # 连续两次 offline 只应记录 1 次，最后 opened (triggered) 记录 1 次，共 2 次 log
        self.assertEqual(mock_logger.log.call_count, 2)
        logged_msgs = [call.args[1] for call in mock_logger.log.call_args_list]
        self.assertIn('直播未开播', logged_msgs[0])
        self.assertIn('已发送 Colab 运行请求，等待认领', logged_msgs[1])

        # verbose 模式：不进行去重，每轮都输出
        args_verbose = trigger_parser().parse_args([
            '--url', 'https://live.bilibili.com/12345', '--once', '--verbose',
        ])
        fake_monitor_verbose = Mock()
        fake_monitor_verbose.return_value.observe.side_effect = [
            TriggerResult('offline'),
            TriggerResult('offline'),
            TriggerResult('opened', triggered=True, run_id='run-123'),
        ]
        mock_logger_verbose = Mock()
        with patch.object(colab_trigger, 'TriggerMonitor', fake_monitor_verbose), \
                patch.object(colab_trigger, 'probe_bilibili', return_value=True), \
                patch.object(colab_trigger, 'resolve_target_info', return_value=('舍茶建乡', '12345')), \
                patch.object(colab_trigger, 'setup_logger', return_value=mock_logger_verbose), \
                patch('time.sleep', return_value=None):
            self.assertEqual(monitor_loop(args_verbose, args_verbose.url, Mock()), 0)

        # verbose 模式下每轮都输出，共 3 次 log
        self.assertEqual(mock_logger_verbose.log.call_count, 3)

    def test_auto_run_restarts_closed_browser_reuses_blank_and_retries_click(self):
        import sys
        import colab_monitor
        from colab_trigger import parser as trigger_parser

        value = {
            'task_config': 'test.yml', 'drive_sync_root': str(self.root),
            'notebook_url': 'https://colab.test/notebook', 'cdp_url': 'http://127.0.0.1:9222',
            'browser': 'C:/Chrome/chrome.exe',
        }
        with patch.object(colab_monitor, 'task_room', return_value='https://live.bilibili.com/1'):
            cmd = colab_monitor.trigger_command(value)
        self.assertIn('--browser', cmd)
        self.assertIn('C:/Chrome/chrome.exe', cmd)
        parsed = trigger_parser().parse_args(cmd[3:])
        self.assertEqual(parsed.browser, 'C:/Chrome/chrome.exe')

        launcher = ColabLauncher(
            'https://colab.test/github/owner/repo/blob/codex%2Fcolab-poc/notebooks/colab_record.ipynb',
            self.root, auto_run=True, cdp_url='http://127.0.0.1:9222',
            browser_executable='C:/Chrome/chrome.exe',
        )
        blank = MagicMock(url='about:blank')
        context = MagicMock(pages=[blank])
        browser = MagicMock(contexts=[context])
        manager = MagicMock()
        manager.__enter__.return_value.chromium.connect_over_cdp.return_value = browser
        api = MagicMock(sync_playwright=MagicMock(return_value=manager))

        with patch.dict(sys.modules, {'playwright.sync_api': api}), \
                patch('colab_support.trigger.cdp_ready', side_effect=[False, True]) as ready, \
                patch('colab_support.trigger.os.name', 'nt'), \
                patch('colab_support.trigger.subprocess.Popen') as popen, \
                patch.object(launcher, '_click_run_all', side_effect=[False, True]) as click_run, \
                patch.object(launcher, 'try_authorize_drive', return_value=True) as auth:
            self.assertTrue(launcher._auto_run_browser())

        popen.assert_called_once()
        self.assertEqual(ready.call_count, 2)
        context.new_page.assert_not_called()
        blank.goto.assert_called_once_with(launcher.notebook_url, wait_until='domcontentloaded')
        self.assertEqual(click_run.call_count, 2)
        blank.wait_for_timeout.assert_called_once_with(250)
        auth.assert_called_once()

        with patch.dict(sys.modules, {'playwright.sync_api': api}), \
                patch('colab_support.trigger.cdp_ready', return_value=False), \
                patch('colab_support.trigger.os.name', 'nt'), \
                patch('colab_support.trigger.subprocess.Popen', side_effect=OSError('missing browser')):
            self.assertFalse(launcher._auto_run_browser())
            self.assertFalse(launcher.restore_notebook(launcher.notebook_url))
            self.assertIsNone(launcher.runtime_connection_state(reopen_missing=True))

        blank.url = 'about:blank'
        with patch.dict(sys.modules, {'playwright.sync_api': api}), \
                patch('colab_support.trigger.cdp_ready', return_value=True), \
                patch.object(launcher, '_click_run_all', return_value=False) as click_fail:
            self.assertFalse(launcher._auto_run_browser())
        self.assertEqual(click_fail.call_count, 20)

    def test_github_warning_dialog_and_multiple_buttons_filtered_by_visibility(self):
        from colab_support.drive_authorization import DRIVE_BUTTON, DriveAuthorization, click_drive_dialog
        for label in ('仍然运行', '仍要執行', '照常執行', '仍然執行', 'Run anyway'):
            self.assertIsNotNone(DRIVE_BUTTON.search(label), label)

        hidden = MagicMock()
        hidden.is_visible.return_value = False
        hidden.is_enabled.return_value = True
        visible = MagicMock()
        visible.is_visible.return_value = True
        visible.is_enabled.return_value = True

        locator = MagicMock()
        locator.count.return_value = 2
        locator.nth.side_effect = lambda idx: [hidden, visible][idx]
        page = MagicMock()
        page.get_by_role.return_value = locator
        self.assertTrue(click_drive_dialog(page))
        hidden.click.assert_not_called()
        visible.click.assert_called_once_with(timeout=3000)

        hidden.is_visible.return_value = True
        visible.click.reset_mock()
        self.assertFalse(click_drive_dialog(page))
        visible.click.assert_not_called()

        actor = DriveAuthorization('https://colab.research.google.com/github/o/r/blob/codex%2Fcolab-poc/n.ipynb')
        page.url = 'https://colab.research.google.com/github/o/r/blob/codex/colab-poc/n.ipynb#scrollTo=1'
        page.is_closed.return_value = False
        self.assertTrue(actor._remember(page))

    def test_conflict_and_corrupted_errors_not_overwritten_by_claim_timeout(self):
        for err in ('active_run_conflict', 'ack_invalid', 'trigger_state_invalid'):
            with self.subTest(err=err):
                self.start()
                self.monitor.state.status = 'needs_attention'
                self.monitor.state.last_error = err
                self.monitor.state.save(self.monitor.state_file)
                self.clock += 901
                result = self.monitor.observe(True)
                self.assertEqual(result.error, err)
                with self.assertRaisesRegex(ValueError, '只有认领超时'):
                    self.monitor.recover_expired()

    def test_probe_unavailable_error_is_cleared_when_probe_recovers(self):
        res_err = self.monitor.observe(None)
        self.assertEqual(res_err.state, 'probe_unavailable')
        self.assertEqual(res_err.error, 'probe_unavailable')

        res_off = self.monitor.observe(False)
        self.assertEqual(res_off.state, 'offline')
        self.assertIsNone(res_off.error)
        self.assertIsNone(self.monitor.state.last_error)

        self.monitor.observe(None)
        self.assertEqual(self.monitor.state.last_error, 'probe_unavailable')
        res_open = self.monitor.observe(True)
        self.assertEqual(res_open.state, 'opened')
        self.assertTrue(res_open.triggered)
        self.assertIsNone(res_open.error)
        self.assertIsNone(self.monitor.state.last_error)

    def test_loopback_interception_check_and_sanitized_browser_error(self):
        import sys
        import colab_monitor
        import colab_trigger

        check_playwright_loopback()
        with patch('colab_support.trigger.socket.socketpair',
                   side_effect=ConnectionError('Unexpected peer connection')):
            with self.assertRaisesRegex(ValueError, 'Unexpected peer connection.*Proxifier'):
                check_playwright_loopback()
            with patch('colab_monitor.importlib.util.find_spec', return_value=object()), \
                    self.assertRaisesRegex(ValueError, '127\\.0\\.0\\.1'):
                colab_monitor.main([])
            with patch.object(sys, 'argv', [
                'colab_trigger.py', '--url', 'https://live.bilibili.com/1',
                '--drive-sync-root', str(self.root), '--auto-run', '--cdp-url', 'http://127.0.0.1:9222',
            ]), self.assertRaisesRegex(ValueError, 'Unexpected peer connection'):
                colab_trigger.main()

        raw = RuntimeError(
            r'goto failed at https://colab.research.google.com/drive/secret-id from C:\Users\alice\drive'
        )
        formatted = format_browser_exception(raw)
        self.assertIn('<url>', formatted)
        self.assertIn('<path>', formatted)
        self.assertNotIn('secret-id', formatted)
        self.assertNotIn('alice', formatted)

        wrapped = RuntimeError('playwright failed')
        wrapped.__cause__ = ConnectionError('Unexpected peer connection')
        self.assertIn('Unexpected peer connection', format_browser_exception(wrapped))
        self.assertIn('Handle Direct Connections', format_browser_exception(wrapped))

    def test_launch_state_unknown_preserves_root_error_past_claim_timeout(self):
        import io
        import logging
        import sys
        from contextlib import redirect_stdout
        import colab_trigger

        launcher = ColabLauncher('https://colab.test/notebook', self.root,
                                 auto_run=True, cdp_url='http://127.0.0.1:9222')
        monitor = TriggerMonitor('https://live.bilibili.com/1', launcher,
                                 self.root / 'auto_state.json', live_confirmations=1,
                                 offline_confirmations=2, now=lambda: self.clock)
        manager = MagicMock()
        manager.__enter__.side_effect = ConnectionError('Unexpected peer connection')
        api = MagicMock(sync_playwright=MagicMock(return_value=manager))
        with patch.dict(sys.modules, {'playwright.sync_api': api}):
            res = monitor.observe(True)

        self.assertEqual(res.state, 'needs_attention')
        self.assertEqual(res.error, 'launch_state_unknown')
        self.assertIn('Unexpected peer connection', launcher.last_browser_error or '')

        self.clock += 901
        res_after = monitor.observe(True)
        self.assertEqual(res_after.error, 'launch_state_unknown')
        self.assertEqual(monitor.state.last_error, 'launch_state_unknown')

        level, msg = colab_trigger.format_trigger_message(
            '舍茶建乡', res_after, detail=launcher.last_browser_error,
        )
        self.assertEqual(level, logging.WARNING)
        self.assertIn('Colab 启动状态未知', msg)
        self.assertIn('--recover-expired', msg)
        self.assertIn('Unexpected peer connection', msg)

        args = colab_trigger.parser().parse_args([
            '--url', 'https://live.bilibili.com/1', '--json', '--once', '--auto-run',
        ])
        fake_monitor = Mock()
        fake_monitor.return_value.state.ack_status = None
        fake_monitor.return_value.resume_pending.return_value = None
        fake_monitor.return_value.observe.return_value = res_after
        output = io.StringIO()
        with redirect_stdout(output), \
                patch.object(colab_trigger, 'TriggerMonitor', fake_monitor), \
                patch.object(colab_trigger, 'probe_bilibili', return_value=True), \
                patch.object(colab_trigger, 'resolve_target_info', return_value=('舍茶建乡', '1')), \
                patch.object(colab_trigger, 'setup_logger', return_value=Mock()):
            self.assertEqual(colab_trigger.monitor_loop(args, args.url, launcher), 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload['error'], 'launch_state_unknown')
        self.assertIn('Unexpected peer connection', payload['detail'])

    def test_resume_pending_idle_close_failure_logs_distinct_warning(self):
        import io
        import sys
        from contextlib import redirect_stdout
        import colab_trigger

        launcher = ColabLauncher('https://colab.test/notebook', self.root,
                                 auto_run=True, cdp_url='http://127.0.0.1:9222')
        monitor = TriggerMonitor('https://live.bilibili.com/1', launcher,
                                 self.root / 'idle_state.json', live_confirmations=1,
                                 offline_confirmations=2, now=lambda: self.clock)
        manager = MagicMock()
        manager.__enter__.side_effect = ConnectionError('Unexpected peer connection')
        api = MagicMock(sync_playwright=MagicMock(return_value=manager))
        with patch.dict(sys.modules, {'playwright.sync_api': api}), \
                patch('colab_support.trigger.cdp_ready', return_value=True):
            self.assertFalse(monitor.resume_pending())
        self.assertIn('Unexpected peer connection', launcher.last_browser_error or '')

        mock_logger = Mock()
        args = colab_trigger.parser().parse_args([
            '--url', 'https://live.bilibili.com/1', '--once',
        ])
        fake_monitor = Mock(state=monitor.state)
        fake_monitor.resume_pending.return_value = False
        fake_monitor.observe.return_value = colab_trigger.TriggerResult('opened', triggered=True, run_id='run-1')
        with patch.object(colab_trigger, 'TriggerMonitor', return_value=fake_monitor), \
                patch.object(colab_trigger, 'probe_bilibili', return_value=True), \
                patch.object(colab_trigger, 'resolve_target_info', return_value=('舍茶建乡', '1')), \
                patch.object(colab_trigger, 'setup_logger', return_value=mock_logger):
            self.assertEqual(colab_trigger.monitor_loop(args, args.url, launcher), 0)
        warning_text = mock_logger.warning.call_args.args[0]
        self.assertIn('遗留 Notebook 页面', warning_text)
        self.assertNotIn('原 Notebook 页面未能显示', warning_text)
        self.assertIn('Unexpected peer connection', warning_text)

        args_json = colab_trigger.parser().parse_args([
            '--url', 'https://live.bilibili.com/1', '--once', '--json',
        ])
        output = io.StringIO()
        with redirect_stdout(output), \
                patch.object(colab_trigger, 'TriggerMonitor', return_value=fake_monitor), \
                patch.object(colab_trigger, 'probe_bilibili', return_value=True), \
                patch.object(colab_trigger, 'resolve_target_info', return_value=('舍茶建乡', '1')), \
                patch.object(colab_trigger, 'setup_logger', return_value=Mock()):
            self.assertEqual(colab_trigger.monitor_loop(args_json, args_json.url, launcher), 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0]['event'], 'idle_notebook_close_failed')
        self.assertIn('Unexpected peer connection', events[0]['detail'])


if __name__ == '__main__':
    unittest.main()
