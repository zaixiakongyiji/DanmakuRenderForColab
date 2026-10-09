import json
import queue
import tempfile
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


if __name__ == '__main__':
    unittest.main()
