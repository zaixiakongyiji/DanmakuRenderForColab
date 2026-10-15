"""R9–R14 回归：使用临时媒体、模拟投稿和模拟浏览器，不访问真实账号。"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import render_only
import test_colab_upload as transactions
from DMR.Render.ffmpeg import RawFFmpegRender
from DMR.Task.liveevents import LiveEvents
from DMR.utils import PipeMessage, VideoInfo
from colab_support.trigger import ColabLauncher, TriggerMonitor, atomic_json, control_paths, write_ack
from colab_support.upload_transaction import UploadError


class ReviewRegressions(unittest.TestCase):
    def test_R9_release_checks_original_notebook_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock = [datetime.now(timezone.utc).timestamp()]
            old, new = 'https://colab.test/old', 'https://colab.test/new'
            launcher = ColabLauncher(old, root, auto_run=True, cdp_url='http://127.0.0.1:9222')
            launcher._auto_run_browser = Mock(return_value=True)
            monitor = TriggerMonitor('https://live.bilibili.com/1', launcher, root / 'state.json',
                                     1, 2, now=lambda: clock[0])
            run_id = monitor.observe(True).run_id
            ack = control_paths(root, run_id)[2]
            write_ack(ack, run_id, 'success')
            value = json.loads(ack.read_text())
            value['runtime_release'] = {'status': 'requested'}
            atomic_json(ack, value)
            clock[0] += 61
            launcher.notebook_url = new
            launcher.runtime_connection_state = Mock(side_effect=lambda notebook_url=None: notebook_url == old)
            monitor = TriggerMonitor(monitor.room_url, launcher, monitor.state_file, 1, 2, now=lambda: clock[0])
            for _ in range(3):
                self.assertEqual(monitor.observe(False).error, 'runtime_release_unconfirmed')
            self.assertFalse(monitor.observe(True).triggered)
            launcher.runtime_connection_state.assert_called_with(notebook_url=old)
            launcher._auto_run_browser.assert_called_once()
            launcher.runtime_connection_state.return_value = False
            launcher.runtime_connection_state.side_effect = None
            monitor.observe(False)
            monitor.observe(False)
            self.assertIsNone(monitor.state.run_id)

    def test_R10_external_part_added_during_transfer_is_preserved(self):
        case = transactions.TransactionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.upload()
        def transfer(*args, **kwargs):
            case.api.get_remote_data.return_value = transactions.Remote(
                videos=[{'filename': 'remote1'}, {'filename': 'external'}])
            return {'filename': 'remote2'}
        case.api.upload_media.side_effect = transfer
        with self.assertRaisesRegex(UploadError, 'upload_remote_parts_mismatch'):
            case.upload('s-2', 2)
        self.assertEqual(case.api.submit_web.call_count, 1)
        self.assertEqual(case.api.get_remote_data.return_value.videos[-1]['filename'], 'external')
        self.assertEqual(case.journal.data['parts']['s-2']['status'], 'uploaded')

    def clean_events(self, status='uploading', wait=None, upload_args=None):
        config = {'common_event_args': {'auto_clean': True},
                  'upload_args': upload_args or {},
                  'clean_args': {'dm_video': [{'method': 'delete', 'delay': 0, 'w_srcfile': True}]}}
        events = LiveEvents('test', config)
        events.state_dict = {'g': [{
            'src_video': {'status': status, 'file': VideoInfo(path='source.mp4', duration=300),
                          'wait': ['source-upload'] if wait is None else wait},
            'src_video_pre': {'status': None, 'file': None, 'wait': []},
            'dm_video': {'status': 'uploaded', 'file': VideoInfo(path='rendered.mp4'), 'wait': []},
        }]}
        return events

    def test_R11_clean_waits_for_source_upload_then_resumes(self):
        events = self.clean_events()
        self.assertEqual(events._check_for_clean(), [])
        self.assertEqual(events.state_dict['g'][0]['src_video']['status'], 'uploading')
        messages = events.onUploadEnd(PipeMessage('uploader', 'replay/test', 'end', request_id='source-upload'))
        self.assertEqual([f.path for f in messages[0].data['files']], ['rendered.mp4', 'source.mp4'])

    def test_R11_clean_retains_source_until_planned_upload(self):
        events = self.clean_events('ready', [], {'src_video': [{'min_length': 120}]})
        self.assertEqual(events._check_for_clean(), [])
        events.config['upload_args']['src_video'][0]['min_length'] = 600
        self.assertEqual(len(events._check_for_clean()), 1)

    def test_R11_clean_waits_for_other_render(self):
        events = self.clean_events('rendering', ['render-request'])
        self.assertEqual(events._check_for_clean(), [])

    def test_R12_debug_exit_code_controls_success(self):
        for code in (0, 7):
            with self.subTest(code=code), patch('DMR.Render.ffmpeg.subprocess.Popen') as popen:
                popen.return_value.returncode = code
                status, _ = RawFFmpegRender(debug=True).call_ffmpeg(['unused'])
                self.assertEqual(status, code == 0)
                popen.return_value.wait.assert_called_once()

    def test_R13_render_only_retries_invalid_and_truncated_outputs(self):
        for output_duration, source_duration, skip in (
                (10, 3600, False), (-1, 3600, False), (float('nan'), 3600, False),
                (3600, -1, False), (3600, 3600, True)):
            with self.subTest(output=output_duration, source=source_duration), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                source = root / 'source.mp4'
                source.write_bytes(b'source')
                source.with_suffix('.ass').write_text('[Events]\n')
                out = root / 'out'
                out.mkdir()
                (out / 'source（弹幕版）.mp4').write_bytes(b'partial')
                config = SimpleNamespace(global_config={'render_args': {'dmrender': {'format': 'mp4'}}})
                with patch.object(sys, 'argv', ['render_only.py', '--input_dir', str(root), '--output_dir', str(out)]), \
                        patch.object(render_only, 'Config', return_value=config), \
                        patch('builtins.input', side_effect=['', '']), \
                        patch.object(render_only.FFprobe, 'get_duration', side_effect=[output_duration, source_duration]), \
                        patch('DMR.Render.dmrender.DmRender') as renderer, redirect_stdout(io.StringIO()):
                    renderer.return_value.render_one.return_value = (True, '')
                    render_only.main()
                self.assertEqual(renderer.call_count, 0 if skip else 1)

    def test_R14_all_short_segments_do_not_abort_other_upload_targets(self):
        events = LiveEvents('test', {'common_event_args': {'auto_upload': True},
            'upload_args': {'src_video': [
                {'realtime': False, 'min_length': 120, 'engine': 'mock'},
                {'realtime': False, 'min_length': 10, 'engine': 'mock'}]}})
        video = VideoInfo(path='short.mp4', group_id='g', duration=30)
        events.onLiveSegment(PipeMessage('downloader', 'replay/test', 'livesegment', data=video))
        messages = events.onLiveEnd(PipeMessage('downloader', 'replay/test', 'liveend', data='g'))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].data['args']['min_length'], 10)


if __name__ == '__main__':
    unittest.main()
