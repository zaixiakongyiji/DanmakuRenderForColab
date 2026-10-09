"""Notebook 信号隔离、监督器收尾和诊断隐私的回归检查。"""
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from colab_support.cli import supervise
from colab_support.notebook import run_recording
from colab_support.diagnostics import safe_diagnostic, ffmpeg_category
from colab_support.adapters import ManagedFFmpeg


class StopControlTests(unittest.TestCase):
    def supervise_with_interrupts(self, folder, interrupts, polls):
        args = SimpleNamespace(run_dir=folder, initial_wait=900, max_record=3600, drain_timeout=7200)
        process = MagicMock(pid=12345, returncode=0)
        process.poll.side_effect = polls
        with patch('colab_support.cli.sys.platform', 'linux'), \
             patch('colab_support.cli.subprocess.Popen', return_value=process), \
             patch('colab_support.cli.load_manifest', return_value={'status': 'success'}), \
             patch('colab_support.cli.time.sleep', side_effect=interrupts), \
             patch('colab_support.cli.os.killpg', create=True) as kill, \
             patch('colab_support.cli.signal.SIGKILL', 9, create=True), \
             patch('colab_support.cli.mark_failed') as failed:
            result = supervise(args)
        return result, kill, failed

    def test_existing_stop_plus_first_signal_still_drains(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / 'STOP').touch()
            result, kill, failed = self.supervise_with_interrupts(
                folder, [KeyboardInterrupt(), None], [None, None, 0, 0])
            self.assertEqual(result, 0)
            kill.assert_not_called()
            failed.assert_not_called()

    def test_first_signal_creates_only_stop(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            result, kill, failed = self.supervise_with_interrupts(
                folder, [KeyboardInterrupt(), None], [None, None, 0, 0])
            self.assertEqual(result, 0)
            self.assertTrue((folder / 'STOP').exists())
            self.assertFalse((folder / 'FORCE_STOP').exists())
            kill.assert_not_called()

    def test_two_direct_signals_force_stop(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            result, kill, failed = self.supervise_with_interrupts(
                folder, [KeyboardInterrupt(), KeyboardInterrupt()], [None, None, None, 2])
            self.assertEqual(result, 2)
            kill.assert_called_once_with(12345, 9)
            self.assertEqual(failed.call_args.args[1], 'forced_interrupt')

    def test_explicit_force_marker_terminates_without_signal(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / 'FORCE_STOP').touch()
            result, kill, failed = self.supervise_with_interrupts(folder, [], [None, 2])
            self.assertEqual(result, 2)
            kill.assert_called_once()
            self.assertEqual(failed.call_args.args[1], 'forced_interrupt')

    def test_notebook_first_interrupt_isolated_and_graceful(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            process = MagicMock(returncode=0)
            process.poll.side_effect = [None, 0, 0]
            with patch('colab_support.notebook.subprocess.Popen', return_value=process) as popen, \
                 patch('colab_support.notebook.time.sleep', side_effect=KeyboardInterrupt):
                result = run_recording(['python', 'colab_run.py'], folder, folder)
            self.assertEqual(result, 0)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertTrue((folder / 'STOP').exists())
            self.assertFalse((folder / 'FORCE_STOP').exists())
            process.send_signal.assert_not_called()
            process.kill.assert_not_called()

    def test_notebook_second_interrupt_uses_explicit_force(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            process = MagicMock(returncode=2)
            process.poll.side_effect = [None, None, 2, 2]
            with patch('colab_support.notebook.subprocess.Popen', return_value=process), \
                 patch('colab_support.notebook.time.sleep', side_effect=KeyboardInterrupt):
                result = run_recording(['python', 'colab_run.py'], folder, folder)
            self.assertEqual(result, 2)
            self.assertTrue((folder / 'STOP').exists())
            self.assertTrue((folder / 'FORCE_STOP').exists())
            process.send_signal.assert_not_called()

    def test_notebook_refuses_old_run_before_spawning(self):
        for name in ('manifest.json', 'supervisor.lock', 'STOP', 'FORCE_STOP'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                folder = Path(d)
                (folder / name).touch()
                with patch('colab_support.notebook.subprocess.Popen') as popen:
                    with self.assertRaisesRegex(ValueError, 'RUN_ID'):
                        run_recording(['python'], folder, folder)
                    popen.assert_not_called()


class DiagnosticTests(unittest.TestCase):
    def test_sensitive_input_cannot_escape_allowlist(self):
        payload = {'stage': 'probe', 'width': 1920, 'height': 1080,
                   'elapsed_seconds': 1.234567, 'url': 'https://example.invalid/?token=secret',
                   'cookie': 'SESSDATA=secret', 'message': 'private', 'error_type': 'secret',
                   'returncode': 'private', 'code': 'private'}
        result = safe_diagnostic(payload)
        self.assertEqual(result, {'stage': 'probe', 'width': 1920, 'height': 1080,
                                  'elapsed_seconds': 1.235, 'error_type': 'Error'})
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(safe_diagnostic({'stage': ['private']}), {'stage': 'unknown'})
        self.assertNotIn('elapsed_seconds', safe_diagnostic({'elapsed_seconds': float('nan')}))

    def test_failed_ffmpeg_reports_category_and_exit_without_raw_line(self):
        with tempfile.TemporaryDirectory() as d:
            diagnostics = []
            recording = ManagedFFmpeg(stream_url='https://example.invalid/?token=secret',
                                      output_dir=d, output_format='mp4', segment=1,
                                      ffmpeg='unused', diagnostic_callback=lambda stage, **details:
                                      diagnostics.append(safe_diagnostic(dict(details, stage=stage))))
            recording.ffmpeg_proc = MagicMock(returncode=1)
            recording.ffmpeg_proc.poll.return_value = 1
            recording.ffmpeg_monitor_proc = MagicMock()
            recording.ffmpeg_monitor_proc.is_alive.return_value = False
            recording.msg_queue = queue.Queue()
            recording.msg_queue.put('HTTP error 403 Forbidden https://example.invalid/?token=secret Cookie: private')
            recording.msg_queue.put('HTTP error 403 Forbidden duplicate')
            with patch.object(recording, 'start_ffmpeg'):
                with self.assertRaisesRegex(RuntimeError, 'ffmpeg_recording_failed'):
                    recording.start()
            self.assertEqual([x['stage'] for x in diagnostics],
                             ['recorder_start', 'ffmpeg_issue', 'recorder_exit'])
            self.assertEqual(diagnostics[1]['code'], 'http_forbidden')
            self.assertEqual(diagnostics[2]['returncode'], 1)
            self.assertNotIn('secret', json.dumps(diagnostics))
            self.assertNotIn('private', json.dumps(diagnostics))

    def test_stop_during_probe_never_starts_recording(self):
        from DMR.Downloader.stream_downloader import StreamDownloadTask
        task = object.__new__(StreamDownloadTask)
        task.external_stop_event = threading.Event()
        events = []
        task.kwargs = {'diagnostic_callback': lambda stage, **details: events.append(stage)}
        task.liveapi = MagicMock()
        task.liveapi.GetRoomInfo.return_value = {'title': 'offline'}
        task.liveapi.GetStreamerInfo.return_value = {'name': 'offline'}
        task.stream_option = {}
        task.taskname = 'test'
        def probe(*args):
            task.external_stop_event.set()
            return 1920, 1080
        with tempfile.TemporaryDirectory() as d:
            task.output_dir = d
            with patch('DMR.Downloader.stream_downloader.FFprobe.get_resolution', side_effect=probe), \
                 patch('DMR.Downloader.stream_downloader.ThreadPoolExecutor') as threads:
                task.start_once()
                threads.assert_not_called()
        self.assertEqual(events, ['room_info', 'stream_select', 'probe', 'probe_ready', 'stopping'])

    def test_manifest_retains_bounded_safe_diagnostics(self):
        from colab_support.core import Coordinator
        with tempfile.TemporaryDirectory() as d:
            c = Coordinator(Path(d), Path(d) / 'backup', MagicMock(), MagicMock())
            for i in range(40):
                c.handle({'source': 'downloader', 'event': 'diagnostic',
                          'data': {'stage': 'probe', 'attempt': i, 'url': 'private'}})
            self.assertEqual(c.manifest['phase'], 'probe')
            self.assertEqual(len(c.manifest['diagnostics']), 30)
            self.assertNotIn('private', json.dumps(c.manifest))


if __name__ == '__main__':
    unittest.main()
