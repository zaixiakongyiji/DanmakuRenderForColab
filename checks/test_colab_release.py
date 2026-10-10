"""按需启动和释放只操作模拟页面、合成文件与注入的释放函数。"""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from colab_support.core import atomic_json, verified_copy
from colab_support.locks import ProcessLock
from colab_support.notebook_session import NotebookSession, prepare_session
from colab_support.runtime_release import finish_session
from colab_support.trigger import ColabLauncher, TriggerMonitor, control_paths, write_ack


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_id = '20261010-000000-abcdef12'
        work = self.root / 'work' / self.run_id
        work.mkdir(parents=True)
        self.session = NotebookSession(self.root, self.root / 'drive', work,
            self.root / 'drive/DMRColab/runs' / self.run_id, self.run_id, (),
            phase='finished', automatic=True)
        self.release = Mock()
        self.manifest = {'version': 2, 'run_id': self.run_id, 'status': 'success',
                         'errors': [], 'segments': {}, 'upload': {'status': 'disabled'}}
        self.save()
        self.output = io.StringIO()
        capture = contextlib.redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def save(self):
        atomic_json(self.session.run_dir / 'manifest.json', self.manifest)

    def finish(self, code=0, **kwargs):
        return finish_session(self.session, code, release_runtime=self.release, **kwargs)

    def ack(self):
        return json.loads(control_paths(self.session.drive_root, self.run_id)[2].read_text(encoding='utf-8'))

    def test_success_persists_manifest_and_ack_before_release(self):
        def release():
            local = json.loads((self.session.run_dir / 'manifest.json').read_text(encoding='utf-8'))
            cloud = json.loads((self.session.backup_dir / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(local, cloud)
            self.assertEqual(self.ack()['status'], 'success')
            self.assertEqual(self.ack()['runtime_release']['status'], 'requested')
        self.release.side_effect = release
        result = self.finish()
        self.assertEqual(result['status'], 'success')
        self.release.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, 'already_attempted'):
            self.finish()
        self.release.assert_called_once()

    def test_success_verifies_real_segment_and_merge_backup(self):
        source = self.session.run_dir / 'source/segment.mp4'
        source.parent.mkdir()
        source.write_bytes(b'synthetic source')
        ass = source.with_suffix('.ass')
        ass.write_text('[Events]', encoding='utf-8')
        rendered = self.session.run_dir / 'rendered/part-1.mp4'
        rendered.parent.mkdir()
        rendered.write_bytes(b'synthetic render')
        segment = {'source': source.name, 'danmaku': ass.name,
                   'rendered_path': 'rendered/part-1.mp4', 'backup': {}}
        for kind, path in [('source', source), ('danmaku', ass), ('rendered', rendered)]:
            check = verified_copy(path, self.session.backup_dir / kind / ('part-1' + path.suffix))
            segment['backup'][kind] = dict(check, status='success')
        self.manifest['segments']['part-1'] = segment
        merged = self.session.run_dir / 'merged/complete.mp4'
        merged.parent.mkdir()
        merged.write_bytes(b'synthetic merge')
        check = verified_copy(merged, self.session.backup_dir / 'merged/complete.mp4')
        self.manifest['merge'] = {'status': 'success', 'path': 'merged/complete.mp4',
                                  'backup': dict(check, status='success')}
        self.save()
        self.finish()
        self.release.assert_called_once()

    def test_missing_success_backup_keeps_runtime(self):
        self.manifest['segments']['part-1'] = {'source': 'segment.mp4', 'backup': {}}
        self.save()
        result = self.finish()
        self.assertEqual(result['runtime_release']['status'], 'unknown')
        self.release.assert_not_called()
        self.assertTrue((self.session.run_dir / 'manifest.json').exists())

    def test_failed_run_preserves_unbacked_media_but_excludes_credentials(self):
        self.manifest['status'] = 'failed'
        self.save()
        source = self.session.run_dir / 'source/last.mp4'
        source.parent.mkdir()
        source.write_bytes(b'last segment')
        private = self.session.run_dir / 'private/cookie.json'
        private.parent.mkdir()
        private.write_text('secret credential', encoding='utf-8')
        (self.session.run_dir / 'console.log').write_text('secret url', encoding='utf-8')
        result = self.finish(1)
        self.release.assert_called_once()
        self.assertEqual(self.ack()['status'], 'failed')
        self.assertEqual((self.session.backup_dir / 'recovery/source/last.mp4').read_bytes(), b'last segment')
        self.assertEqual(len(result['recovery_files']), 1)
        self.assertFalse((self.session.backup_dir / 'private').exists())
        self.assertFalse((self.session.backup_dir / 'console.log').exists())
        self.assertNotIn('secret', self.output.getvalue())

    def test_manual_mode_never_releases(self):
        self.session.automatic = False
        self.finish()
        self.release.assert_not_called()
        self.assertFalse(control_paths(self.session.drive_root, self.run_id)[2].exists())

    def test_preflight_failure_without_manifest_is_persisted_and_released(self):
        (self.session.run_dir / 'manifest.json').unlink()
        self.session.phase = 'invalid'
        result = self.finish(1, failure='notebook_preflight_failed')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('notebook_preflight_failed', result['errors'])
        self.assertEqual(self.ack()['status'], 'failed')
        self.release.assert_called_once()

    def test_unassign_failure_is_unknown_and_never_retried(self):
        self.release.side_effect = RuntimeError('private raw error')
        result = self.finish()
        self.assertEqual(result['runtime_release']['status'], 'unknown')
        self.assertEqual(self.ack()['runtime_release']['status'], 'unknown')
        self.assertNotIn('private raw error', self.output.getvalue())
        with self.assertRaises(RuntimeError):
            self.finish()
        self.release.assert_called_once()

    def test_drive_copy_failure_never_releases(self):
        with patch('colab_support.runtime_release.verified_copy', side_effect=OSError('private path')) as copy:
            result = self.finish()
        self.assertEqual(result['runtime_release']['status'], 'unknown')
        self.assertEqual(copy.call_count, 6)  # 最终同步与失败证据同步各最多三次。
        self.release.assert_not_called()

    def test_active_supervisor_or_lock_never_releases(self):
        with ProcessLock(self.session.run_dir.parent / 'colab-runtime.lock'):
            self.finish()
        self.release.assert_not_called()

    def test_supervisor_marker_never_releases(self):
        (self.session.run_dir / 'supervisor.lock').touch()
        self.finish()
        self.release.assert_not_called()

    def test_preflight_gpu_is_saved_before_release(self):
        atomic_json(self.session.run_dir / 'preflight/result.json',
                    {'gpu': 'NVIDIA T4, test driver', 'font': 'Noto Sans CJK SC', 'duration': 2,
                     'cookie': 'secret'})
        result = self.finish()
        self.assertIn('T4', result['environment']['gpu'])
        self.assertNotIn('cookie', result['environment'])

    def test_notebook_failure_and_run_cells_obey_lifecycle(self):
        nb = json.loads((Path(__file__).resolve().parents[1] / 'notebooks/colab_record.ipynb').read_text(encoding='utf-8'))
        cells = {c['id']: ''.join(c['source']) for c in nb['cells']}
        self.session.preflight = Mock(side_effect=RuntimeError('preflight'))
        with patch('colab_support.runtime_release.finish_session') as finish:
            with self.assertRaises(RuntimeError):
                exec(cells['colab-08'], {'SESSION': self.session, 'checked': Mock()})
            finish.assert_called_once_with(self.session, 1, failure='notebook_preflight_failed')
        self.session.phase = 'release_requested'
        with patch('colab_support.notebook.run_recording') as runner:
            with self.assertRaises(RuntimeError):
                exec(cells['colab-10'], {'SESSION': self.session})
            runner.assert_not_called()
        self.session.phase = 'preflight'
        self.session.run = Mock(return_value=0)
        with patch('colab_support.runtime_release.finish_session') as finish:
            exec(cells['colab-10'], {'SESSION': self.session})
            finish.assert_called_once_with(self.session, 0)
        self.session.run.side_effect = KeyboardInterrupt()
        with patch('colab_support.runtime_release.finish_session') as finish:
            with self.assertRaises(KeyboardInterrupt):
                exec(cells['colab-10'], {'SESSION': self.session})
            finish.assert_called_once_with(self.session, 1, failure='notebook_run_interrupted')
        exec(cells['colab-12'], {'SESSION': self.session})  # 自动模式不访问旧结果或 CU 变量。


class BrowserReleaseTests(unittest.TestCase):
    def page(self, runtime=None, status=(), dialog=False):
        page = MagicMock()
        button = MagicMock()
        button.count.return_value = 1
        button.is_visible.return_value = button.is_enabled.return_value = True
        absent = MagicMock()
        absent.count.return_value = 0
        messages = MagicMock()
        messages.all_text_contents.return_value = list(status)
        def role(kind, name=None):
            if kind in ('alert', 'status'):
                return messages
            if kind == 'dialog':
                return button if dialog else absent
            pattern = name.pattern if name else ''
            if 'Run all' in pattern:
                return button
            if 'Connected' in pattern:
                return button if runtime is True else absent
            if 'hosted runtime' in pattern:
                return button if runtime is False else absent
            return absent
        page.get_by_role.side_effect = role
        return page, button

    def test_disconnected_page_clicks_run_all_without_runtime_gate(self):
        page, button = self.page(runtime=False)
        self.assertTrue(ColabLauncher._click_run_all(page))
        button.click.assert_called_once()
        self.assertFalse(ColabLauncher._connection_state(page))

    def test_unknown_page_busy_dialog_and_login_fail_closed(self):
        for options in ({'status': ('Connecting',)}, {'status': ('Running',)}, {'dialog': True}):
            page, button = self.page(**options)
            self.assertFalse(ColabLauncher._click_run_all(page))
            button.click.assert_not_called()
        page, button = self.page()
        button.count.return_value = 0
        self.assertFalse(ColabLauncher._click_run_all(page))
        self.assertIsNone(ColabLauncher._connection_state(page))

    def test_terminal_connected_or_unknown_runtime_blocks_next_launch(self):
        for connected in (True, None, False):
            with self.subTest(connected=connected), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                clock = [datetime.now(timezone.utc).timestamp()]
                launcher = ColabLauncher('https://colab.test/notebook', root, auto_run=True,
                                         cdp_url='http://127.0.0.1:9222')
                launcher._auto_run_browser = Mock(return_value=True)
                launcher.runtime_connection_state = Mock(return_value=connected)
                monitor = TriggerMonitor('https://live.bilibili.com/1', launcher, root / 'state.json',
                                         1, 2, now=lambda: clock[0])
                monitor.observe(True)
                ack = control_paths(root, monitor.state.run_id)[2]
                write_ack(ack, monitor.state.run_id, 'success')
                value = json.loads(ack.read_text())
                value['runtime_release'] = {'status': 'requested'}
                atomic_json(ack, value)
                self.assertEqual(monitor.observe(False).error, 'runtime_release_pending')
                clock[0] += 61
                result = monitor.observe(False)
                if connected is False:
                    self.assertEqual(result.state, 'success')
                    monitor.observe(False)
                    self.assertIsNone(monitor.state.run_id)
                else:
                    self.assertEqual(result.error, 'runtime_release_unconfirmed')
                    for _ in range(4):
                        monitor.observe(False)
                    self.assertIsNotNone(monitor.state.run_id)
                launcher._auto_run_browser.assert_called_once()

    def test_unknown_release_and_legacy_ack_never_rearm(self):
        for release in ({'status': 'unknown'}, {}):
            with self.subTest(release=release), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                clock = [datetime.now(timezone.utc).timestamp()]
                launcher = ColabLauncher('https://colab.test/notebook', root, auto_run=True,
                                         cdp_url='http://127.0.0.1:9222')
                launcher._auto_run_browser = Mock(return_value=True)
                launcher.runtime_connection_state = Mock(return_value=False)
                monitor = TriggerMonitor('https://live.bilibili.com/1', launcher, root / 'state.json',
                                         1, 2, now=lambda: clock[0])
                monitor.observe(True)
                path = control_paths(root, monitor.state.run_id)[2]
                write_ack(path, monitor.state.run_id, 'failed')
                ack = json.loads(path.read_text())
                ack['runtime_release'] = release
                atomic_json(path, ack)
                clock[0] += 61
                self.assertEqual(monitor.observe(False).error, 'runtime_release_unknown')
                self.assertIsNotNone(monitor.state.run_id)


class LauncherSetupTests(unittest.TestCase):
    def test_monitor_starts_browser_blank_until_live_without_prepare(self):
        import colab_monitor
        value = {'cdp_url': 'http://127.0.0.1:9222', 'browser': 'test-browser',
                 'notebook_url': 'https://colab.test/notebook'}
        with patch('colab_monitor.cdp_ready', side_effect=[False, True]), \
             patch('colab_monitor.os.name', 'nt'), \
             patch('colab_monitor.subprocess.Popen') as popen, \
             patch('builtins.input') as prompt:
            colab_monitor.ensure_browser(value)
        self.assertEqual(popen.call_args.args[0][-1], 'about:blank')
        prompt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
