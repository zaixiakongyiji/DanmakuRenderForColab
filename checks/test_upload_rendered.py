"""手动投稿回归测试，仅使用临时媒体和模拟 API。"""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

import upload_rendered as upload
from DMR.Uploader.biliwebapi import BiliWebApi
from colab_support.upload_transaction import ColabBiliUploader, UploadError

BVID = 'BV1234567890'
CONFIG = {'account': 'test', 'expected_uid': 1, 'line': 'AUTO', 'min_length': 30,
          '_streamer_name': 'test', '_streamer_url': 'https://example.invalid/1',
          'title': '{STREAMER.NAME} {CTIME.MONTH} {ctime.day:02d}',
          'desc': '{TITLE}', 'tag': '{STREAMER.NAME}', 'source': '{STREAMER.URL}'}


class FakeApi:
    def __init__(self):
        self.remote = None
        self.metadata = None
        self.query_failed = False
        self.discover_enabled = True
        self.submit_code = 0
        self._session = Mock()
        self._session.get.side_effect = self.get
        self.upload_media = Mock(side_effect=self.transfer)
        self.submit_web = Mock(side_effect=self.submit)

    def get(self, url, **kwargs):
        if url.endswith('/nav'):
            value = {'code': 0, 'data': {'isLogin': True, 'mid': 1}}
        else:
            entries = [{'Archive': {'bvid': BVID}}] if self.remote and self.discover_enabled else []
            value = {'code': 0, 'data': {'arc_audits': entries}}
        return SimpleNamespace(json=lambda: value)

    def videoinfo_to_videos(self, video, config):
        self.metadata = BiliWebApi.videoinfo_to_videos(self, video, config)
        return self.metadata

    def get_remote_data(self, bvid):
        if self.query_failed or not self.remote or self.remote.bvid != bvid:
            return None
        return copy.deepcopy(self.remote)

    def transfer(self, path, **kwargs):
        return {'filename': 'remote' + str(self.upload_media.call_count)}

    def submit(self, payload, edit=False):
        if self.submit_code:
            return {'code': self.submit_code}
        self.remote = copy.deepcopy(self.remote if edit else self.metadata)
        self.remote.bvid = BVID
        self.remote.aid = 1
        self.remote.videos = copy.deepcopy(payload['videos'])
        return {'code': 0, 'data': {'bvid': BVID, 'aid': 1}}


class RenderedUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.progress_file = self.root / 'progress.json'
        self.files = []
        self.first = self.add_file(20)
        self.second = self.add_file(21)
        self.cfg = dict(CONFIG)
        self.api = FakeApi()
        self.uploader = ColabBiliUploader(None, self.cfg, api=self.api)
        self.probe = Mock(return_value=300)
        for patcher in (
            patch.object(upload, 'PROGRESS_FILE', str(self.progress_file)),
            patch.object(upload, 'get_upload_config', side_effect=lambda: dict(self.cfg)),
            patch.object(upload, 'glob', side_effect=lambda _: list(self.files)),
            patch.object(upload, 'create_uploader', return_value=self.uploader),
            patch.object(upload, 'get_video_duration', self.probe),
            patch.object(upload.time, 'sleep'),
            patch.object(upload.logger, 'info'),
            patch.object(upload.logger, 'error'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_file(self, hour):
        path = self.root / f'test-2026年10月10日{hour:02d}点00分（弹幕版）.mp4'
        path.write_bytes(('synthetic-' + str(hour)).encode())
        self.files.append(str(path))
        return str(path)

    def state(self):
        return next(iter(upload.load_progress().values()))

    def test_R1_remote_query_failure_never_creates_another_archive(self):
        self.files = [self.first]
        self.assertEqual(upload.main(), 0)
        self.files.append(self.second)
        self.api.query_failed = True
        self.assertEqual(upload.main(), 1)
        self.assertEqual(self.api.submit_web.call_count, 1)
        self.assertEqual(self.api.upload_media.call_count, 1)
        self.assertEqual(self.state()['bvid'], BVID)

    def test_R2_publication_settings_reach_submission(self):
        self.cfg.update(is_only_self=1, dtime=14400, no_disturbance=1, no_reprint=1)
        self.files = [self.first]
        self.assertEqual(upload.main(), 0)
        payload = self.api.submit_web.call_args.args[0]
        self.assertEqual(payload['is_only_self'], 1)
        self.assertEqual(payload['no_disturbance'], 1)
        self.assertEqual(payload['no_reprint'], 1)
        self.assertGreater(payload['dtime'], 0)

    def test_R3_new_part_reopens_completed_session(self):
        self.files = [self.first]
        self.assertEqual(upload.main(), 0)
        self.assertTrue(self.state()['done'])
        self.files.append(self.second)
        self.assertEqual(upload.main(), 0)
        self.assertEqual(self.api.upload_media.call_count, 2)
        self.assertEqual(self.api.submit_web.call_count, 2)
        self.assertTrue(self.api.submit_web.call_args.kwargs['edit'])
        self.assertEqual(self.state()['uploaded'], 2)
        self.assertEqual(self.api.remote.videos[1]['title'], Path(self.second).stem)

    def test_R4_previously_uploaded_file_does_not_change_resume_position(self):
        self.files = [self.first]
        self.assertEqual(upload.main(), 0)
        self.files.append(self.second)
        self.probe.side_effect = lambda path: 0 if path == self.first else 300
        self.assertEqual(upload.main(), 0)
        self.assertEqual(self.api.upload_media.call_count, 2)
        self.assertEqual(self.state()['parts'][upload.file_key(self.second)]['part_index'], 2)

    def test_removed_first_file_keeps_original_session_and_archive(self):
        self.assertEqual(upload.main(), 0)
        third = self.add_file(22)
        self.files.remove(self.first)
        Path(self.first).unlink()
        self.assertEqual(upload.main(), 0)
        self.assertEqual(len(upload.load_progress()), 1)
        self.assertEqual(self.api.submit_web.call_count, 3)
        self.assertEqual(self.state()['parts'][upload.file_key(third)]['part_index'], 3)

    def test_R5_standard_templates_receive_complete_metadata(self):
        self.files = [self.first]
        self.assertEqual(upload.main(), 0)
        payload = self.api.submit_web.call_args.args[0]
        self.assertEqual(payload['title'], 'test 10 10')
        self.assertEqual(payload['tag'], 'test')
        self.assertEqual(payload['source'], CONFIG['_streamer_url'])
        self.assertEqual(payload['desc'], '天黑请喝茶')

    def test_R6_post_commit_checkpoint_failure_reconciles_without_reupload(self):
        self.files = [self.first]
        save = upload.save_progress
        def fail_after_commit(progress):
            if any(state.get('bvid') for state in progress.values()):
                raise OSError('simulated checkpoint failure')
            save(progress)
        with patch.object(upload, 'save_progress', side_effect=fail_after_commit):
            self.assertEqual(upload.main(), 1)
        self.assertEqual(self.state()['parts'][upload.file_key(self.first)]['status'], 'submitting')
        self.assertEqual(upload.main(), 0)
        self.assertEqual(self.api.submit_web.call_count, 1)
        self.assertEqual(self.api.upload_media.call_count, 1)
        self.assertEqual(self.state()['bvid'], BVID)

    def test_submit_intent_is_saved_before_post(self):
        submit = self.api.submit_web.side_effect
        def checked(payload, edit=False):
            part = list(self.state()['parts'].values())[-1]
            self.assertEqual(part['status'], 'submitting')
            self.assertEqual(part['expected_filenames'], [p['filename'] for p in payload['videos']])
            return submit(payload, edit)
        self.api.submit_web.side_effect = checked
        self.assertEqual(upload.main(), 0)

    def test_intent_persistence_failure_never_posts(self):
        save = upload.save_progress
        def fail_intent(progress):
            if any(p['status'] == 'submitting' for s in progress.values() for p in s['parts'].values()):
                raise OSError('simulated persistence failure')
            save(progress)
        with patch.object(upload, 'save_progress', side_effect=fail_intent):
            self.assertEqual(upload.main(), 1)
        self.api.submit_web.assert_not_called()
        self.assertEqual(upload.main(), 0)
        self.assertEqual(self.api.upload_media.call_count, 2)

    def test_unresolved_submission_never_posts_twice(self):
        self.files = [self.first]
        self.api.discover_enabled = False
        self.api.submit_web.side_effect = TimeoutError('simulated lost response')
        self.assertEqual(upload.main(), 1)
        self.assertEqual(upload.main(), 1)
        self.assertEqual(self.api.submit_web.call_count, 1)
        self.assertEqual(self.api.upload_media.call_count, 1)

    def test_R7_probe_failure_is_not_a_short_video(self):
        self.probe.side_effect = lambda path: 300 if path == self.first else (_ for _ in ()).throw(UploadError('video_probe_failed'))
        self.assertEqual(upload.main(), 1)
        self.assertFalse(self.state()['done'])
        self.assertEqual(self.state()['uploaded'], 1)
        self.probe.side_effect = None
        self.assertEqual(upload.main(), 0)
        self.assertEqual(self.api.submit_web.call_count, 2)

    def test_R8_rejected_submission_returns_failure_without_completion_log(self):
        self.api.submit_code = 10010
        self.assertEqual(upload.main(), 1)
        self.assertFalse(self.state()['done'])
        self.assertNotIn('全部场次处理完成', [call.args[0] for call in upload.logger.info.call_args_list])
        self.assertEqual(upload.main(), 1)
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_legacy_completed_progress_migrates_by_remote_titles(self):
        self.files = [self.first]
        self.assertEqual(upload.main(), 0)
        key = next(iter(upload.load_progress()))
        upload.save_progress({key: {'bvid': BVID, 'uploaded': 1, 'done': True}})
        self.files.append(self.second)
        self.assertEqual(upload.main(), 0)
        self.assertEqual(self.api.upload_media.call_count, 2)
        self.assertEqual(self.state()['uploaded'], 2)

    def test_ambiguous_legacy_progress_is_preserved_without_posting(self):
        self.assertEqual(upload.main(), 0)
        key = next(iter(upload.load_progress()))
        old = {key: {'bvid': BVID, 'uploaded': 2, 'done': True}}
        upload.save_progress(old)
        self.api.remote.videos[0]['title'] = 'unmatched'
        self.assertEqual(upload.main(), 1)
        self.assertEqual(upload.load_progress(), old)
        self.assertEqual(self.api.submit_web.call_count, 2)

    def test_changed_content_is_not_silently_skipped(self):
        self.assertEqual(upload.main(), 0)
        Path(self.first).write_bytes(b'changed content')
        self.assertEqual(upload.main(), 1)
        self.assertEqual(self.api.submit_web.call_count, 2)

    def test_empty_scan_returns_failure_without_api_initialization(self):
        self.files = []
        self.assertEqual(upload.main(), 1)
        upload.create_uploader.assert_not_called()


class ProgressAndProbeTests(unittest.TestCase):
    def test_atomic_save_preserves_previous_progress_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as d, patch.object(upload, 'PROGRESS_FILE', str(Path(d) / 'progress.json')):
            old = {'session': {'uploaded': 1}}
            upload.save_progress(old)
            with patch('colab_support.core.os.replace', side_effect=OSError('simulated disk failure')):
                with self.assertRaises(OSError):
                    upload.save_progress({'session': {'uploaded': 2}})
            self.assertEqual(upload.load_progress(), old)

    def test_invalid_duration_and_subprocess_failures_raise(self):
        for output in ('', 'nan', 'inf', '0', '-1', 'not a duration'):
            with self.subTest(output=output), patch.object(upload.subprocess, 'run', return_value=SimpleNamespace(stdout=output)):
                with self.assertRaisesRegex(UploadError, 'video_probe_failed'):
                    upload.get_video_duration('test.mp4')
        for error in (OSError('missing'), subprocess.TimeoutExpired('ffprobe', 30), subprocess.CalledProcessError(1, 'ffprobe')):
            with self.subTest(error=type(error)), patch.object(upload.subprocess, 'run', side_effect=error):
                with self.assertRaisesRegex(UploadError, 'video_probe_failed'):
                    upload.get_video_duration('test.mp4')
        with patch.object(upload.subprocess, 'run', return_value=SimpleNamespace(stdout='45.5')) as run:
            self.assertEqual(upload.get_video_duration('test.mp4'), 45.5)
            self.assertTrue(run.call_args.kwargs['check'])

    def test_global_publication_defaults_merge_with_task_config(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = root / 'task.yml'
            task.write_text(yaml.safe_dump({'upload_args': {'dm_video': {'title': 'task', 'is_only_self': 1}}, 'download_args': {'url': 'test'}}))
            global_config = root / 'global.yml'
            global_config.write_text(yaml.safe_dump({'upload_args': {'bilibili': {'dtime': 14400, 'is_only_self': 0}}}))
            original_path = upload.Path
            with patch.object(upload, 'TASK_CONFIG_PATH', str(task)), patch.object(upload, 'Path', side_effect=lambda p: global_config if p == 'configs/global.yml' else original_path(p)):
                cfg = upload.get_upload_config()
            self.assertEqual((cfg['is_only_self'], cfg['dtime'], cfg['title']), (1, 14400, 'task'))


if __name__ == '__main__':
    unittest.main()
