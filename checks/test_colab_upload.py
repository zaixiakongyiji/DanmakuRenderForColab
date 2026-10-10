"""投稿仅使用模拟账号和媒体，绝不连接真实服务。"""
import asyncio
import copy
import json
import queue
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from colab_support.core import Coordinator, digest
from colab_support.upload_config import load_upload_config, load_upload_cookie
from colab_support.upload_transaction import ColabBiliUploader, Journal, UploadError

BVID = 'BV1234567890'
CONFIG = {'enabled': True, 'account': 'test', 'expected_uid': 1, 'tid': 1, 'copyright': 2,
          'source': 'test', 'title': '{TITLE}', 'desc': 'test', 'tag': ['test'],
          'min_length': 120, 'line': 'AUTO', 'limit': 1, 'part_timeout': 1, 'drain_timeout': 1}


@dataclass
class Remote:
    bvid: str = BVID
    videos: list = field(default_factory=list)


class UploadConfigTests(unittest.TestCase):
    def test_disabled_missing_and_enabled_defaults(self):
        import yaml
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'upload.yml'
            self.assertFalse(load_upload_config(path)['enabled'])
            value = dict(CONFIG)
            del value['min_length']
            path.write_text(yaml.safe_dump(value))
            self.assertEqual(load_upload_config(path)['min_length'], 120)
            for key, bad in [('copyright', None), ('tid', None), ('source', ''), ('expected_uid', 2.5),
                             ('part_timeout', float('nan')), ('min_length', 1), ('limit', True), ('tag', [])]:
                path.write_text(yaml.safe_dump(dict(CONFIG, **{key: bad})))
                with self.subTest(key=key), self.assertRaises(ValueError):
                    load_upload_config(path)

    def test_cookie_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'cookie.json'
            path.write_text('private-secret')
            with self.assertRaises(ValueError) as caught:
                load_upload_cookie(path)
            self.assertNotIn('private-secret', str(caught.exception))
            path.write_text(json.dumps({'cookie_info': {'cookies': [
                {'name': 'SESSDATA', 'value': 'test'}, {'name': 'bili_jct', 'value': 'test'}]}}))
            self.assertEqual(load_upload_cookie(path)['SESSDATA'], 'test')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'video.mp4'
        self.path.write_bytes(b'synthetic')
        size, checksum = digest(self.path)
        self.segment = {'duration': 300, 'backup': {'rendered': {'size': size, 'sha256': checksum}}}
        self.api = MagicMock()
        self.api._session.get.return_value.json.return_value = {'code': 0, 'data': {'isLogin': True, 'mid': 1}}
        self.api.get_remote_data.return_value = None
        self.api.upload_media.side_effect = [{'filename': 'remote1'}, {'filename': 'remote2'}]
        self.uploader = ColabBiliUploader(None, CONFIG, api=self.api)
        self.uploader.metadata = Mock(return_value={'title': 'fixed title', 'videos': [], 'bvid': None})
        self.journal = Journal(self.root / 'local/upload.json', self.root / 'drive/upload.json', 'test', 1)
        def submit(payload, edit=False):
            self.assertEqual(self.journal.local.read_bytes(), self.journal.remote.read_bytes())
            persisted = json.loads(self.journal.remote.read_text())
            self.assertEqual(list(persisted['parts'].values())[-1]['status'], 'submitting')
            self.api.get_remote_data.return_value = Remote(videos=copy.deepcopy(payload['videos']))
            return {'code': 0, 'data': {'bvid': BVID, 'aid': 123}}
        self.api.submit_web.side_effect = submit

    def upload(self, key='s-1', index=1):
        return self.uploader.upload_part(self.path, self.segment, index, self.journal, key)

    def test_create_append_same_bvid_and_resume_dedup(self):
        self.assertEqual(self.upload()['status'], 'submitted')
        self.assertEqual(self.upload('s-2', 2)['status'], 'submitted')
        self.assertFalse(self.api.submit_web.call_args_list[0].kwargs['edit'])
        self.assertTrue(self.api.submit_web.call_args_list[1].kwargs['edit'])
        second = self.api.submit_web.call_args_list[1].args[0]
        self.assertEqual(second['bvid'], BVID)
        self.assertEqual([v['filename'] for v in second['videos']], ['remote1', 'remote2'])
        self.assertEqual(self.uploader.metadata.call_count, 1)
        self.assertEqual(self.upload()['status'], 'submitted')
        self.assertEqual(self.api.upload_media.call_count, 2)
        self.assertEqual(self.api.submit_web.call_count, 2)

    def test_persistence_failure_never_submits(self):
        with patch('colab_support.upload_transaction.verified_copy', side_effect=IOError('secret')):
            with self.assertRaises(IOError):
                self.upload()
        self.api.submit_web.assert_not_called()

    def test_timeout_after_remote_commit_reconciles(self):
        submit = self.api.submit_web.side_effect
        def timeout(payload, edit=False):
            submit(payload, edit)
            raise TimeoutError('secret')
        self.api.submit_web.side_effect = timeout
        self.uploader.discover = Mock(return_value=BVID)
        self.assertEqual(self.upload()['status'], 'submitted')
        self.assertEqual(self.api.submit_web.call_count, 1)
        self.assertNotIn('secret', self.journal.remote.read_text())

    def test_unresolved_timeout_never_resubmits(self):
        self.api.submit_web.side_effect = TimeoutError('private')
        self.uploader.discover = Mock(return_value=None)
        self.assertEqual(self.upload()['status'], 'unknown')
        self.assertEqual(self.upload()['status'], 'unknown')
        self.assertEqual(self.api.submit_web.call_count, 1)
        self.assertEqual(self.api.upload_media.call_count, 1)

    def test_query_failure_does_not_create_new_submission(self):
        self.upload()
        self.api.get_remote_data.return_value = None
        with self.assertRaisesRegex(UploadError, 'remote_query'):
            self.upload('s-2', 2)
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_locked_rejected_blocks_retry(self):
        self.api.submit_web.side_effect = None
        self.api.submit_web.return_value = {'code': 10010}
        self.assertEqual(self.upload()['status'], 'failed')
        self.assertEqual(self.upload()['status'], 'failed')
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_account_and_hash_checks_precede_transfer(self):
        self.api._session.get.return_value.json.return_value['data']['mid'] = 2
        with self.assertRaisesRegex(UploadError, 'identity_mismatch'):
            self.upload()
        self.api.upload_media.assert_not_called()
        self.api._session.get.return_value.json.return_value['data']['mid'] = 1
        self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(UploadError, 'checksum'):
            self.upload()
        self.api.upload_media.assert_not_called()

    def test_remote_order_mismatch_is_not_submitted(self):
        def wrong(payload, edit=False):
            self.api.get_remote_data.return_value = Remote(videos=[{'filename': 'unrelated'}])
            return {'code': 0, 'data': {'bvid': BVID}}
        self.api.submit_web.side_effect = wrong
        self.assertEqual(self.upload()['status'], 'unknown')

    def test_malformed_submit_response_remains_unknown(self):
        self.api.submit_web.side_effect = None
        self.api.submit_web.return_value = {'message': 'private response'}
        self.uploader.discover = Mock(return_value=None)
        self.assertEqual(self.upload()['status'], 'unknown')
        self.assertNotIn('private response', self.journal.remote.read_text())

    def test_known_part_remains_submitted_when_resume_query_fails(self):
        from colab_support.posting import execute_job
        self.upload()
        self.api.get_remote_data.return_value = None
        with patch('colab_support.posting.load_upload_config', return_value=CONFIG), \
             patch('colab_support.upload_transaction.ColabBiliUploader', return_value=self.uploader):
            result = execute_job({'config_file': 'unused', 'cookie': 'unused',
                'local': str(self.journal.local), 'remote': str(self.journal.remote), 'run_id': 'test',
                'path': str(self.path), 'metadata': self.segment, 'part_index': 1, 'key': 's-1'})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(json.loads(self.journal.remote.read_text())['parts']['s-1']['status'], 'submitted')
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_journal_damage_and_account_binding(self):
        self.journal.save()
        with self.assertRaisesRegex(UploadError, 'journal_invalid'):
            Journal(self.journal.local, self.journal.remote, 'test', 2)
        self.journal.local.write_text('invalid')
        with self.assertRaisesRegex(UploadError, 'journal_invalid'):
            Journal(self.journal.local, self.journal.remote, 'test', 1)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.post = Mock()
        self.c = Coordinator(self.root, self.root / 'drive', Mock(), Mock(),
                             post_submit=self.post, upload_config=CONFIG)

    def segment(self, n, duration=300, ready=True):
        path = self.root / 'rendered' / ('s-' + str(n) + '.mp4')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'x')
        self.c.manifest['segments']['s-' + str(n)] = {'segment_id': n, 'group_id': 's',
            'duration': duration, 'render': 'success', 'rendered_path': path.relative_to(self.root).as_posix(),
            'backup': {k: {'status': 'success'} for k in ('source', 'danmaku', 'rendered')} if ready else {},
            'upload': {'status': 'pending'}}

    def test_numeric_order_backup_gate_short_skip_and_dedup(self):
        self.segment(2)
        self.c._maybe_post()
        self.post.assert_not_called()
        self.segment(1, 100, ready=False)
        self.c._maybe_post()
        self.post.assert_not_called()
        self.segment(1, 100)
        self.c._maybe_post()
        self.assertEqual(self.post.call_args.args[1], 's-2')
        self.assertEqual(self.post.call_args.args[-1], 1)
        self.c._maybe_post()
        self.assertEqual(self.post.call_count, 1)
        rid = next(iter(self.c.pending_upload))
        event = {'source': 'uploader', 'event': 'end', 'request_id': rid, 'data': {'bvid': BVID}}
        self.c.handle(event)
        self.c.handle(event)
        self.assertEqual(self.post.call_count, 1)

    def test_failed_part_blocks_later_and_media_can_finish(self):
        self.segment(1)
        self.segment(2)
        self.c._maybe_post()
        rid = next(iter(self.c.pending_upload))
        self.c.handle({'source': 'uploader', 'event': 'unknown', 'request_id': rid, 'data': {'error': 'test'}})
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.c.manifest['segments']['s-2']['upload']['status'], 'failed')
        self.assertFalse(self.c.manifest['errors'])

    def test_final_gap_becomes_failure_without_waiting_forever(self):
        self.segment(2)
        self.c.producer_done = True
        self.assertTrue(self.c.posting_done())
        self.assertEqual(self.c.manifest['segments']['s-2']['upload']['status'], 'failed')
        self.post.assert_not_called()

    def test_failed_first_still_marks_short_tail_skipped(self):
        self.segment(1)
        self.segment(2, 100)
        self.c._maybe_post()
        rid = next(iter(self.c.pending_upload))
        self.c.handle({'source': 'uploader', 'event': 'error', 'request_id': rid, 'data': {'error': 'test'}})
        self.assertEqual(self.c.manifest['segments']['s-2']['upload']['status'], 'skipped_short')
        self.assertTrue(self.c.posting_done())

    def test_all_short_fails_posting_and_missing_backup_blocks(self):
        self.segment(1, 100)
        self.c.producer_done = True
        self.c._maybe_post()
        self.c.finish()
        self.assertEqual(self.c.manifest['upload']['status'], 'failed')
        self.assertEqual(self.c.manifest['status'], 'failed')
        self.post.assert_not_called()


class PostingProcessTests(unittest.TestCase):
    def test_timeout_during_submit_is_unknown_and_process_is_killed(self):
        from colab_support.posting import PostingWorker
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'run'
            root.mkdir()
            journal = Journal(root / 'upload.json', root / 'drive/upload.json', 'run', 1)
            journal.data['parts']['s-1'] = {'status': 'submitting', 'part_index': 1,
                'size': 1, 'sha256': 'a' * 64, 'filename': 'remote1', 'expected_filenames': ['remote1']}
            journal.save()
            process = MagicMock()
            process.poll.side_effect = [None, 0]
            config = dict(CONFIG, part_timeout=0.001)
            events = queue.Queue()
            with patch('colab_support.posting.subprocess.Popen', return_value=process),                  patch('colab_support.posting.time.monotonic', side_effect=[0, 2]):
                worker = PostingWorker(events, root / 'cookie', root / 'config', config, root, root / 'drive')
                worker.submit('job', 's-1', root / 'video', {}, 1)
                message = events.get(timeout=3)
                while message['event'] == 'progress':
                    message = events.get(timeout=3)
                worker.close()
            self.assertEqual(message['event'], 'unknown')
            process.kill.assert_called_once()
            self.assertEqual(json.loads(journal.remote.read_text())['parts']['s-1']['status'], 'unknown')

    def test_new_job_after_drain_deadline_never_spawns(self):
        from colab_support.posting import PostingWorker
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            events = queue.Queue()
            with patch('colab_support.posting.subprocess.Popen') as popen:
                worker = PostingWorker(events, root / 'cookie', root / 'config', CONFIG, root, root / 'drive')
                worker.deadline = 0
                worker.submit('job', 's-1', root / 'video', {}, 1)
                event = events.get(timeout=3)
                worker.close()
                popen.assert_not_called()
            self.assertEqual(event['data']['error'], 'upload_drain_timeout')

    def test_resume_checksum_rejects_changed_drive_media(self):
        from colab_upload import restore_segments
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            backup = root / 'drive'
            segment = {'segment_id': 1, 'group_id': 's', 'backup': {}}
            for kind, suffix in (('source', '.mp4'), ('danmaku', '.ass'), ('rendered', '.mp4')):
                path = backup / kind / ('s-1' + suffix)
                path.parent.mkdir(parents=True)
                path.write_bytes(b'test')
                size, checksum = digest(path)
                segment['backup'][kind] = {'status': 'success', 'size': size, 'sha256': checksum}
            manifest = {'segments': {'s-1': segment}}
            restore_segments(backup, root / 'work', manifest)
            (backup / 'rendered/s-1.mp4').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'backup_invalid'):
                restore_segments(backup, root / 'work', manifest)


class UpstreamTests(unittest.TestCase):
    def test_readonly_path_never_calls_login_and_desktop_default_does(self):
        from DMR.Uploader.biliwebapi import BiliWebApi
        with tempfile.TemporaryDirectory() as d:
            cookie = Path(d) / 'cookie.json'
            info = {'cookie_info': {'cookies': [{'name': 'bili_jct', 'value': 'test'}]}}
            cookie.write_text(json.dumps(info))
            with patch.object(BiliWebApi, 'login_by_biliuprs', return_value=info) as login:
                api = BiliWebApi(cookies=str(cookie), readonly_cookies=True)
                login.assert_not_called()
                self.assertIsNone(api.refresh_token)
                self.assertEqual(api.chunk_attempts, 3)
                BiliWebApi(cookies=str(cookie))
                login.assert_called_once()

    def test_templates_use_shanghai_time(self):
        from DMR.Uploader.biliwebapi import BiliWebApi
        api = object.__new__(BiliWebApi)
        config = dict(CONFIG, title='{STREAMER.NAME} {CTIME.YEAR}-{CTIME.MONTH:02d}-{CTIME.DAY:02d} {CTIME.HOUR:02d}:{CTIME.MINUTE:02d}')
        uploader = ColabBiliUploader(None, config, api=api)
        data = uploader.metadata({'duration': 300, 'ctime': '2026-10-09T20:30:00+00:00',
            'streamer': {'name': 'test'}, 'group_id': 's', 'segment_id': 1}, Path('test.mp4'))
        self.assertEqual(data['title'], 'test 2026-10-10 04:30')
        self.assertNotIn('extra_kwargs', data)

    def test_failed_chunk_never_merges_and_tail_size_is_exact(self):
        from DMR.Uploader.biliwebapi import BiliWebApi
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'video'
            path.write_bytes(b'abcde')
            api = object.__new__(BiliWebApi)
            api._session = MagicMock()
            api.limit = 1
            api.chunk_attempts = 3
            api._session.post.return_value.json.return_value = {'upload_id': 'test', 'OK': 1}
            ret = {'chunk_size': 3, 'auth': 'private', 'endpoint': '//test.invalid',
                   'biz_id': 1, 'upos_uri': 'upos://test/video.mp4'}
            api.upload_chunk_thread = Mock(side_effect=[{'partNumber': 1, 'eTag': 'etag'}, None])
            self.assertIsNone(asyncio.run(api.upos_stream(path, 'video.mp4', 5, ret)))
            self.assertEqual(api._session.post.call_count, 1)
            tail = api.upload_chunk_thread.call_args_list[1].args[3]
            self.assertEqual((tail['size'], tail['end'], tail['total']), (2, 5, 5))
            self.assertEqual(api.upload_chunk_thread.call_args_list[1].args[-1], 3)


if __name__ == '__main__':
    unittest.main()
