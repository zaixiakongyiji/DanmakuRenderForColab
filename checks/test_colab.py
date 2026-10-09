import json
import queue
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from colab_support.cli import prepare_cookie
from colab_support.core import BackupWorker, Coordinator, LiveWindow, verified_copy


class CapturingBackup:
    def __init__(self):
        self.jobs = []

    def submit(self, *args, **kwargs):
        self.jobs.append((args, kwargs))


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'run'
        self.root.mkdir()
        self.backup = CapturingBackup()
        self.renders = []
        self.c = Coordinator(self.root, Path(self.temp.name) / 'drive', self.backup,
                             lambda *args: self.renders.append(args))
        (self.root / 'source.mp4').write_bytes(b'video')
        (self.root / 'source.ass').write_text('[Events]\nDialogue: hello', encoding='utf-8')
        self.video = {'group_id': 'session', 'segment_id': 1, 'path': str(self.root / 'source.mp4'),
                      'dm_file_id': str(self.root / 'source.ass'), 'duration': 300}

    def segment(self):
        self.c.handle({'source': 'downloader', 'event': 'livesegment', 'data': self.video})

    def ack_all(self, failed_kind=None):
        while self.c.pending_backup:
            rid, (key, kind) = next(iter(self.c.pending_backup.items()))
            self.c.handle({'source': 'backup', 'event': 'error' if kind == failed_kind else 'end',
                           'request_id': rid, 'data': {'size': 5, 'sha256': 'fake', 'attempts': 1}})

    def producer_done(self, reason='live_end'):
        self.c.handle({'source': 'downloader', 'event': 'producer_done', 'data': {'reason': reason}})

    def render_done(self, error=False):
        rid, video, path = self.renders[-1]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'rendered')
        self.c.handle({'source': 'render', 'event': 'start', 'request_id': rid})
        self.c.handle({'source': 'render', 'event': 'error' if error else 'end', 'request_id': rid,
                       'data': {'output': {'path': str(path)}}})

    def test_full_flow_duplicate_and_late_render(self):
        self.segment()
        self.segment()
        self.assertEqual(len(self.renders), 1)
        self.producer_done()
        self.ack_all()
        self.assertFalse(self.c.drained())
        self.render_done()
        self.ack_all()
        self.assertTrue(self.c.drained())
        self.c.finish()
        self.ack_all()
        self.assertEqual(self.c.manifest['status'], 'success')
        segment = self.c.manifest['segments']['session-1']
        self.assertEqual(set(segment['backup']), {'source', 'danmaku', 'rendered'})
        self.assertEqual(segment['danmaku_dialogues'], 1)

    def test_empty_queue_is_not_producer_completion(self):
        self.assertFalse(self.c.drained())
        self.c.handle({'source': 'downloader', 'event': 'liveend', 'data': None})
        self.assertFalse(self.c.drained())
        with self.assertRaises(RuntimeError):
            self.c.finish()

    def test_final_segment_before_barrier(self):
        self.segment()
        self.render_done()
        self.ack_all()
        self.assertFalse(self.c.drained())
        self.video = dict(self.video, segment_id=2)
        self.segment()
        self.producer_done('requested')
        self.render_done()
        self.ack_all()
        self.c.finish()
        self.assertEqual(len(self.c.manifest['segments']), 2)

    def test_render_failure_is_terminal_and_keeps_source_backups(self):
        self.segment()
        self.render_done(error=True)
        self.producer_done()
        self.ack_all()
        self.c.finish()
        self.assertEqual(self.c.manifest['status'], 'failed')
        self.assertTrue((self.root / 'source.mp4').exists())
        self.assertNotIn('rendered', self.c.manifest['segments']['session-1']['backup'])

    def test_backup_failure_is_not_success(self):
        self.segment()
        self.render_done()
        self.producer_done()
        self.ack_all(failed_kind='source')
        self.c.finish()
        self.assertEqual(self.c.manifest['status'], 'failed')
        self.assertEqual(self.c.manifest['segments']['session-1']['backup']['source']['status'], 'error')

    def test_disk_low_reports_failure(self):
        self.producer_done('disk_low')
        self.ack_all()
        self.c.finish()
        self.assertIn('disk_low', self.c.manifest['errors'])
        self.assertEqual(self.c.manifest['status'], 'failed')

    def test_metadata_failure_does_not_recursively_checkpoint(self):
        self.c.checkpoint()
        self.ack_all(failed_kind='checkpoint')
        self.assertFalse(self.c.pending_backup)
        self.assertEqual(len(self.backup.jobs), 1)


class LifecycleTests(unittest.TestCase):
    def test_unknown_status_resets_continuous_offline_window(self):
        w = LiveWindow(900, 180, 43200)
        self.assertIsNone(w.observe(True, 0))
        self.assertIsNone(w.observe(False, 10))
        self.assertIsNone(w.observe(None, 185))
        self.assertIsNone(w.observe(False, 200))
        self.assertIsNone(w.observe(False, 379))
        self.assertEqual(w.observe(False, 380), 'live_end')

    def test_initial_wait_and_recording_limit(self):
        w = LiveWindow(900, 180, 3600)
        self.assertIsNone(w.observe(None, 899))
        self.assertEqual(w.observe(False, 900), 'initial_timeout')
        w = LiveWindow(900, 180, 3600)
        w.observe(True, 100)
        self.assertEqual(w.observe(None, 3700), 'record_limit')


class BackupAndCookieTests(unittest.TestCase):
    def test_verified_copy_and_three_attempt_limit(self):
        with tempfile.TemporaryDirectory() as d:
            source, dest = Path(d) / 'a', Path(d) / 'drive/b'
            source.write_bytes(b'original')
            result = verified_copy(source, dest)
            self.assertEqual(dest.read_bytes(), b'original')
            self.assertEqual(result['size'], 8)
            events = queue.Queue()
            attempts = []
            def failing(*args):
                attempts.append(1)
                raise OSError('simulated')
            worker = BackupWorker(events, copy=failing, retry_wait=0)
            worker.submit('job', source, dest)
            message = events.get(timeout=3)
            worker.close()
            self.assertEqual(len(attempts), 3)
            self.assertEqual(message['event'], 'error')
            self.assertTrue(source.exists())

    def test_retry_recovers_and_corrupt_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            source, dest = Path(d) / 'a', Path(d) / 'b'
            source.write_bytes(b'original')
            with patch('colab_support.core.shutil.copyfile', side_effect=lambda a, b: Path(b).write_bytes(b'bad')):
                with self.assertRaises(IOError):
                    verified_copy(source, dest)
            self.assertFalse(dest.exists())
            count = []
            def transient(a, b):
                count.append(1)
                if len(count) < 3:
                    raise OSError('temporary')
                return verified_copy(a, b)
            q = queue.Queue()
            worker = BackupWorker(q, copy=transient, retry_wait=0)
            worker.submit('job', source, dest)
            event = q.get(timeout=3)
            worker.close()
            self.assertEqual(event['event'], 'end')
            self.assertEqual(event['data']['attempts'], 3)

    def test_cookie_schema_and_no_refresh_token_copy(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'local.json'
            source.write_text(json.dumps({'cookie_info': {'cookies': [{'name': 'SESSDATA', 'value': 'secret'}]},
                                          'token_info': {'refresh_token': 'do-not-copy'}}))
            result = prepare_cookie(source, Path(d) / 'private')
            self.assertNotIn('token_info', json.loads(result.read_text()))
            source.write_text('{"SESSDATA": "invalid-shape-secret"}')
            with self.assertRaises(ValueError) as raised:
                prepare_cookie(source, Path(d) / 'private')
            self.assertNotIn('invalid-shape-secret', str(raised.exception))


class MergeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), '需要 FFmpeg/ffprobe')
    def test_stream_copy_merge_verifies_duration_and_decode(self):
        import shutil
        from colab_support.merge import merge_rendered
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'run'
            rendered = root / 'rendered'
            rendered.mkdir(parents=True)
            for index in (1, 2):
                target = rendered / f'session-{index}.mp4'
                subprocess.run([shutil.which('ffmpeg'), '-y', '-v', 'error', '-f', 'lavfi',
                                '-i', 'testsrc=size=160x120:rate=10',
                                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                                '-t', '1', '-c:a', 'aac',
                                '-c:v', 'libx264', '-g', '10', '-pix_fmt', 'yuv420p',
                                '-movflags', '+faststart', str(target)], check=True)
            manifest = {'segments': {
                'session-1': {'render': 'success', 'rendered_path': 'rendered/session-1.mp4'},
                'session-2': {'render': 'success', 'rendered_path': 'rendered/session-2.mp4'},
            }}
            result = merge_rendered(root, manifest, time.monotonic() + 60, min_free_gib=0)
            self.assertEqual(result['status'], 'success')
            self.assertEqual(result['segment_count'], 2)
            self.assertTrue((root / 'merged/complete.mp4').is_file())
            self.assertTrue(result['decode_verified'])

    def test_merge_rejects_gap_and_incomplete_segment(self):
        from colab_support.merge import MergeError, select_inputs
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'run'
            (root / 'rendered').mkdir(parents=True)
            (root / 'rendered/room-1.mp4').write_bytes(b'x')
            with self.assertRaisesRegex(MergeError, 'merge_incomplete_segments'):
                select_inputs(root, {'segments': {'room-1': {'render': 'error', 'rendered_path': 'rendered/room-1.mp4'}}})
            with self.assertRaisesRegex(MergeError, 'merge_segment_gap'):
                select_inputs(root, {'segments': {
                    'room-1': {'render': 'success', 'rendered_path': 'rendered/room-1.mp4'},
                    'room-3': {'render': 'success', 'rendered_path': 'rendered/room-1.mp4'},
                }})


    def test_merge_disk_and_stream_guards_preserve_segments(self):
        from types import SimpleNamespace
        from colab_support.merge import MergeError, merge_rendered
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'rendered').mkdir()
            segments = {}
            for i in (1, 2):
                relative = f'rendered/session-{i}.mp4'
                (root / relative).write_bytes(b'original')
                segments[f'session-{i}'] = {'render': 'success', 'rendered_path': relative}
            manifest = {'segments': segments}
            with patch('colab_support.merge.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                with self.assertRaisesRegex(MergeError, 'merge_disk_low'):
                    merge_rendered(root, manifest, time.monotonic() + 10, min_free_gib=0)
            with patch('colab_support.merge.probe', side_effect=[(1, ['avc']), (1, ['hevc'])]):
                with self.assertRaisesRegex(MergeError, 'merge_incompatible_streams'):
                    merge_rendered(root, manifest, time.monotonic() + 10, min_free_gib=0)
            self.assertFalse((root / 'merged/complete.mp4').exists())
            self.assertEqual((root / 'rendered/session-1.mp4').read_bytes(), b'original')

    def test_merge_and_backup_failures_cannot_finish_successfully(self):
        from colab_support.merge import MergeError, finalize_merge
        for mode in ('merge_error', 'backup_error', 'upstream_error'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                root, drive = Path(d) / 'run', Path(d) / 'drive'
                events = queue.Queue()
                def copy(source, destination):
                    raise OSError('simulated drive failure')
                worker = BackupWorker(events, copy=copy, retry_wait=0)
                try:
                    c = Coordinator(root, drive, worker, lambda *a: None, require_merge=True)
                    c.producer_done = True
                    c.manifest['segments']['session-1'] = {'render': 'success'}
                    calls = []
                    def merge(*args, **kwargs):
                        calls.append(1)
                        if mode == 'merge_error':
                            raise MergeError('merge_duration_mismatch')
                        output = root / 'merged/complete.mp4'
                        output.parent.mkdir(parents=True)
                        output.write_bytes(b'merged')
                        return {'status': 'success', 'path': 'merged/complete.mp4'}
                    if mode == 'upstream_error':
                        c.error('render_failed:session-1')
                    finalize_merge(c, events, time.monotonic() + 5, merge=merge)
                    c.finish()
                    while c.pending_backup:
                        c.handle(events.get(timeout=5))
                    self.assertEqual(c.manifest['status'], 'failed')
                    self.assertEqual(json.loads((drive / 'manifest.json').read_text(encoding='utf-8')), c.manifest)
                    if mode == 'backup_error':
                        self.assertEqual(c.manifest['merge']['backup']['attempts'], 3)
                        self.assertEqual(c.manifest['merge']['backup']['status'], 'error')
                        self.assertTrue((root / 'merged/complete.mp4').exists())
                    elif mode == 'upstream_error':
                        self.assertEqual(calls, [])
                        self.assertEqual(c.manifest['merge']['status'], 'skipped')
                    else:
                        self.assertEqual(c.manifest['merge']['status'], 'error')
                finally:
                    worker.close()

    def test_merge_backup_timeout_is_bounded(self):
        from colab_support.merge import MergeError, finalize_merge
        with tempfile.TemporaryDirectory() as d:
            c = Coordinator(Path(d), Path(d) / 'drive', CapturingBackup(), lambda *a: None,
                            require_merge=True)
            c.producer_done = True
            with self.assertRaisesRegex(MergeError, 'merge_timeout'):
                finalize_merge(c, queue.Queue(), time.monotonic() + 0.02,
                               merge=lambda *a, **kw: {'status': 'success', 'path': 'merged/complete.mp4'})
            self.assertIn('drain_timeout', c.manifest['errors'])
            self.assertEqual(c.manifest['merge']['backup']['status'], 'error')
            self.assertNotEqual(c.manifest['status'], 'success')


if __name__ == '__main__':
    unittest.main()
