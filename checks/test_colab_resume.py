"""历史任务恢复：仅使用临时目录、合成媒体和模拟投稿。"""
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from colab_support.core import atomic_json, digest
from colab_support.merge import MergeError
from colab_support.recovery import Recovery, RecoveryError
from colab_support.trigger import control_paths, write_ack
from colab_support.upload_transaction import ColabBiliUploader, Journal, UploadError
from checks.test_colab_upload import BVID, CONFIG, Remote

RUN_ID = '20261011-224438-5faf4204'
ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which('ffmpeg') or str(ROOT / 'tools/ffmpeg.exe')
FFPROBE = shutil.which('ffprobe') or str(ROOT / 'tools/ffprobe.exe')


class RecoveryFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.drive = self.root / 'drive'
        self.work = self.root / 'work' / RUN_ID
        self.backup = self.drive / 'DMRColab/runs' / RUN_ID
        self.manifest = {'version': 2, 'run_id': RUN_ID, 'status': 'failed',
                         'errors': ['merge_incompatible_streams'], 'segments': {},
                         'settings': {'upload_min_length': 120, 'encoder': 'libx264'}}
        for index in (1, 2):
            key = f's-{index}'
            segment = {'group_id': 's', 'segment_id': index, 'duration': 300,
                       'render': 'success', 'backup': {}, 'upload': {'status': 'failed'}}
            for kind, suffix in (('source', '.mp4'), ('danmaku', '.ass'), ('rendered', '.mp4')):
                path = self.backup / kind / (key + suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((kind + key).encode())
                segment['backup'][kind] = self.info(path)
            self.manifest['segments'][key] = segment
        self.save_original()

    @staticmethod
    def info(path):
        size, checksum = digest(path)
        return {'status': 'success', 'size': size, 'sha256': checksum}

    def save_original(self):
        atomic_json(self.backup / 'manifest.json', self.manifest)

    def recovery(self):
        return Recovery(self.drive, self.work, RUN_ID, encoder='libx264', min_free_gib=0)


class RecoveryTests(RecoveryFixture, unittest.TestCase):
    @staticmethod
    def fake_probe(*args):
        return 300, [{'codec_type': 'video', 'r_frame_rate': '25/1'}]

    @staticmethod
    def fake_merge(folder, manifest, deadline, **kwargs):
        target = folder / 'merged/complete.mp4'
        target.parent.mkdir()
        target.write_bytes(b'joined')
        return {'status': 'success', 'path': 'merged/complete.mp4', 'decode_verified': True}

    def test_inspection_has_no_metadata_mutations(self):
        before = (self.backup / 'manifest.json').read_bytes()
        plan = self.recovery().inspect()
        self.assertEqual([p['action'] for p in plan['segments']], ['reuse_rendered'] * 2)
        self.assertEqual(plan['merge'], 'merge_rendered')
        self.assertEqual((self.backup / 'manifest.json').read_bytes(), before)
        self.assertFalse((self.backup / 'resume_manifest.json').exists())

    def test_terminal_run_and_control_ack_are_required(self):
        self.manifest['status'] = 'running'
        self.save_original()
        with self.assertRaisesRegex(RecoveryError, 'not_terminal'):
            self.recovery()
        self.manifest['status'] = 'failed'
        self.save_original()
        write_ack(control_paths(self.drive, RUN_ID)[2], RUN_ID, 'running')
        with self.assertRaisesRegex(RecoveryError, 'still_active'):
            self.recovery()

    def test_checksum_mismatch_and_gap_stop_before_processing(self):
        (self.backup / 'rendered/s-1.mp4').write_bytes(b'corrupt')
        with self.assertRaisesRegex(RecoveryError, 'checksum'):
            self.recovery().inspect()
        del self.manifest['segments']['s-1']
        self.save_original()
        with self.assertRaisesRegex(RecoveryError, 'segment_gap'):
            self.recovery()

    def test_missing_render_requires_originals_and_no_upload_intent(self):
        self.manifest['segments']['s-1']['backup']['rendered'] = {'status': 'error'}
        self.save_original()
        self.assertEqual(self.recovery().inspect()['segments'][0]['action'], 'render_from_source')
        atomic_json(self.backup / 'upload.json', {'run_id': RUN_ID, 'parts': {'s-1': {'status': 'unknown'}}})
        with self.assertRaisesRegex(RecoveryError, 'render_has_upload_transaction'):
            self.recovery().inspect()

    def test_missing_journal_cannot_create_duplicate_post(self):
        self.manifest['segments']['s-1']['upload'] = {'status': 'submitted', 'bvid': BVID}
        self.save_original()
        with self.assertRaisesRegex(RecoveryError, 'journal_missing'):
            self.recovery().inspect()

    def test_checkpoint_and_original_changes_are_not_silently_overwritten(self):
        recovery = self.recovery()
        recovery.checkpoint()
        value = copy.deepcopy(recovery.state)
        value['phase'] = 'different'
        atomic_json(self.work / 'resume_manifest.json', value)
        with self.assertRaisesRegex(RecoveryError, 'checkpoint_diverged'):
            self.recovery()
        atomic_json(self.work / 'resume_manifest.json', recovery.state)
        self.manifest['errors'].append('recording_gap')
        self.save_original()
        with self.assertRaisesRegex(RecoveryError, 'checkpoint_invalid'):
            self.recovery()

    def test_backup_paths_cannot_escape_run_directory(self):
        recovery = self.recovery()
        with self.assertRaisesRegex(RecoveryError, 'backup_path_invalid'):
            recovery.checked_backup('../manifest.json', {})

    def test_reentry_reuses_merge_and_preserves_history(self):
        before = (self.backup / 'manifest.json').read_bytes()
        ack = control_paths(self.drive, RUN_ID)[2]
        write_ack(ack, RUN_ID, 'failed')
        ack_before = ack.read_bytes()
        with patch('colab_support.recovery.probe', side_effect=self.fake_probe), \
             patch('colab_support.recovery.merge_rendered', side_effect=self.fake_merge) as merger:
            first = self.recovery()
            self.assertEqual(first.execute(upload=False), 0)
            second = self.recovery()
            self.assertEqual(second.execute(upload=False), 0)
        self.assertEqual(merger.call_count, 1)
        self.assertEqual(second.state['upload']['status'], 'not_requested')
        self.assertEqual(second.state['original_errors'], ['merge_incompatible_streams'])
        self.assertEqual((self.backup / 'manifest.json').read_bytes(), before)
        self.assertEqual(ack.read_bytes(), ack_before)
        self.assertTrue((self.backup / 'recovery/merged/complete.mp4').is_file())

    def test_interrupted_media_step_is_retryable_without_repeating_finished_step(self):
        with patch('colab_support.recovery.probe', side_effect=self.fake_probe), \
             patch('colab_support.recovery.merge_rendered', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.recovery().execute(upload=False)
        state = json.loads((self.backup / 'resume_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'failed')
        with patch('colab_support.recovery.probe', side_effect=self.fake_probe), \
             patch('colab_support.recovery.merge_rendered', side_effect=self.fake_merge), \
             patch.object(Recovery, 'render_missing') as renderer:
            self.assertEqual(self.recovery().execute(upload=False), 0)
        renderer.assert_not_called()

    def test_merge_failure_does_not_block_valid_part_upload(self):
        def upload(drive, backup, work, manifest, candidate):
            atomic_json(work / 'upload_resume.json', {'status': 'success', 'parts': {}})
            return 0
        with patch('colab_support.recovery.probe', side_effect=self.fake_probe), \
             patch('colab_support.recovery.merge_rendered', side_effect=MergeError('merge_incompatible_streams')), \
             patch('colab_upload.upload_manifest', side_effect=upload) as uploader:
            recovery = self.recovery()
            self.assertEqual(recovery.execute(), 1)
        uploader.assert_called_once()
        self.assertEqual(recovery.state['upload']['status'], 'success')
        self.assertEqual(recovery.state['merge']['status'], 'error')

    def test_existing_merge_checksum_is_required(self):
        path = self.backup / 'merged/complete.mp4'
        path.parent.mkdir()
        path.write_bytes(b'archive')
        self.manifest['merge'] = {'status': 'success', 'path': 'merged/complete.mp4', 'backup': self.info(path)}
        self.save_original()
        self.assertEqual(self.recovery().inspect()['merge'], 'reuse_merged')
        path.unlink()
        with self.assertRaisesRegex(RecoveryError, 'backup_missing'):
            self.recovery().inspect()

    def test_upload_exception_is_saved_without_reusing_old_success(self):
        recovery = self.recovery()
        recovery.state['upload'] = {'status': 'success', 'parts': {}}
        with patch('colab_support.recovery.probe', side_effect=self.fake_probe), \
             patch('colab_support.recovery.merge_rendered', side_effect=self.fake_merge), \
             patch('colab_upload.upload_manifest', side_effect=UploadError('upload_disabled')):
            with self.assertRaisesRegex(UploadError, 'upload_disabled'):
                recovery.execute()
        state = json.loads((self.backup / 'resume_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['upload'], {'status': 'unknown', 'error': 'upload_disabled'})
        self.assertIn('upload_disabled', state['errors'])

    def test_cli_inspects_same_run_without_claim_or_recording(self):
        from colab_resume import main
        with patch('builtins.print'):
            result = main(['--run-id', RUN_ID, '--drive-root', str(self.drive),
                           '--work-root', str(self.work.parent), '--inspect-only'])
        self.assertEqual(result, 0)
        self.assertFalse((self.backup / 'resume_manifest.json').exists())
        self.assertFalse(control_paths(self.drive, RUN_ID)[1].exists())

    def uploader_context(self, unresolved=False):
        from colab_support.posting import execute_job
        api = Mock()
        api._session.get.return_value.json.return_value = {'code': 0, 'data': {'isLogin': True, 'mid': 1}}
        api.get_remote_data.return_value = None
        api.upload_media.side_effect = [{'filename': 'remote1'}, {'filename': 'remote2'}]
        uploader = ColabBiliUploader(None, CONFIG, api=api)
        uploader.metadata = Mock(return_value={'title': 'fixed', 'videos': [], 'bvid': None})
        uploader.discover = Mock(return_value=None)
        def submit(payload, edit=False):
            if unresolved:
                raise TimeoutError()
            api.get_remote_data.return_value = Remote(videos=copy.deepcopy(payload['videos']))
            return {'code': 0, 'data': {'bvid': BVID}}
        api.submit_web.side_effect = submit
        class Worker:
            def __init__(inner, events, cookie, config_file, config, work, backup):
                inner.events, inner.work, inner.backup = events, work, backup
                inner.cookie, inner.config_file = cookie, config_file
            def begin_drain(inner):
                pass
            def close(inner):
                pass
            def submit(inner, rid, key, path, metadata, index):
                value = execute_job({'cookie': str(inner.cookie), 'config_file': str(inner.config_file),
                    'local': str(inner.work / 'upload.json'), 'remote': str(inner.backup / 'upload.json'),
                    'run_id': RUN_ID, 'key': key, 'path': str(path), 'metadata': metadata,
                    'part_index': index, 'candidate': getattr(inner, 'candidate', None)})
                inner.events.put({'source': 'uploader', 'event': 'progress', 'request_id': rid,
                                  'data': {'status': 'uploading'}})
                event = 'end' if value['status'] == 'submitted' else 'unknown'
                inner.events.put({'source': 'uploader', 'event': event, 'request_id': rid, 'data': value})
        cookies = {'SESSDATA': 'synthetic', 'bili_jct': 'synthetic'}
        self.enterContext(patch('colab_upload.load_upload_config', return_value=CONFIG))
        self.enterContext(patch('colab_support.posting.load_upload_config', return_value=CONFIG))
        self.enterContext(patch('colab_upload.load_upload_cookie', return_value=cookies))
        self.enterContext(patch('colab_support.uploader.prepare_identity'))
        self.enterContext(patch('colab_support.upload_transaction.ColabBiliUploader', return_value=uploader))
        self.enterContext(patch('colab_upload.PostingWorker', Worker))
        self.enterContext(patch('colab_support.recovery.probe', side_effect=self.fake_probe))
        self.enterContext(patch('colab_support.recovery.merge_rendered', side_effect=self.fake_merge))
        return api

    def test_two_parts_recover_to_same_post_and_second_run_deduplicates(self):
        api = self.uploader_context()
        self.assertEqual(self.recovery().execute(), 0)
        self.assertEqual(self.recovery().execute(), 0)
        self.assertEqual(api.upload_media.call_count, 2)
        self.assertEqual(api.submit_web.call_count, 2)
        journal = Journal(self.work / 'upload.json', self.backup / 'upload.json', RUN_ID, 1)
        self.assertEqual(journal.data['bvid'], BVID)
        self.assertEqual([p['status'] for p in journal.data['parts'].values()], ['submitted'] * 2)

    def test_unknown_is_reconciled_without_resubmission_or_later_parts(self):
        api = self.uploader_context(unresolved=True)
        self.assertEqual(self.recovery().execute(), 1)
        self.assertEqual(self.recovery().execute(), 1)
        self.assertEqual(api.upload_media.call_count, 1)
        self.assertEqual(api.submit_web.call_count, 1)
        summary = json.loads((self.work / 'upload_resume.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['parts']['s-1']['status'], 'unknown')
        self.assertNotIn('s-2', summary['parts'])


@unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(), '需要 FFmpeg/ffprobe')
class RecoveryMediaTests(RecoveryFixture, unittest.TestCase):
    def test_real_mismatched_frame_rates_merge_without_rerendering_segments(self):
        for index, rate, timescale in ((1, '25', 12800), (2, '299/12', 19136)):
            key = f's-{index}'
            target = self.backup / 'rendered' / f'{key}.mp4'
            subprocess.run([FFMPEG, '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                f'testsrc=size=160x120:rate={rate}', '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-profile:v', 'main', '-video_track_timescale', str(timescale), str(target)],
                capture_output=True, check=True, timeout=20)
            self.manifest['segments'][key]['backup']['rendered'] = self.info(target)
        self.save_original()
        old = [digest(self.backup / 'rendered' / f's-{index}.mp4') for index in (1, 2)]
        with patch.object(Recovery, 'render_missing') as renderer:
            recovery = self.recovery()
            self.assertEqual(recovery.execute(upload=False, media_timeout=60), 0)
        renderer.assert_not_called()
        self.assertEqual(recovery.state['merge']['mode'], 'normalized_stream_copy')
        self.assertEqual(old, [digest(self.backup / 'rendered' / f's-{index}.mp4') for index in (1, 2)])

    def test_real_missing_render_is_burned_backed_up_and_reused(self):
        del self.manifest['segments']['s-2']
        segment = self.manifest['segments']['s-1']
        source = self.backup / 'source/s-1.mp4'
        subprocess.run([FFMPEG, '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc=size=160x120:rate=25', '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            str(source)], capture_output=True, check=True, timeout=20)
        subtitle = self.backup / 'danmaku/s-1.ass'
        subtitle.write_text('''[Script Info]
ScriptType: v4.00+
PlayResX: 160
PlayResY: 120
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,16,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,Recovery test
''', encoding='utf-8')
        segment['backup'].update(source=self.info(source), danmaku=self.info(subtitle),
                                 rendered={'status': 'error'})
        segment['render'] = 'error'
        self.save_original()
        original_run = subprocess.run
        def run(command, **kwargs):
            if command[0] == 'fc-match':
                return Mock(stdout='Noto Sans CJK SC')
            return original_run(command, **kwargs)
        with patch('colab_support.recovery.subprocess.run', side_effect=run):
            first = self.recovery()
            self.assertEqual(first.execute(upload=False, media_timeout=60), 0)
        target = self.backup / 'recovery/rendered/s-1.mp4'
        self.assertTrue(target.is_file())
        self.assertNotEqual(digest(target), digest(source))
        second = self.recovery()
        self.assertEqual(second.inspect()['segments'][0]['action'], 'reuse_rendered')
        with patch.object(Recovery, 'render_missing') as renderer:
            self.assertEqual(second.execute(upload=False, media_timeout=60), 0)
        renderer.assert_not_called()


class RecoveryNotebookTests(unittest.TestCase):
    def test_notebook_code_compiles_and_guards_execution_order(self):
        value = json.loads((ROOT / 'notebooks/colab_resume.ipynb').read_text(encoding='utf-8'))
        cells = [c for c in value['cells'] if c['cell_type'] == 'code']
        for cell in cells:
            compile(''.join(cell['source']), 'recovery_notebook', 'exec')
            self.assertIsNone(cell['execution_count'])
            self.assertFalse(cell['outputs'])
        self.assertIn("drive.mount('/content/drive')", ''.join(cells[0]['source']))
        self.assertIn("#@title ② 选择历史任务", ''.join(cells[1]['source']))
        for cell in cells[2:]:
            with self.assertRaises(RuntimeError):
                exec(''.join(cell['source']), {})
        form = ''.join(cells[1]['source']).replace("RUN_ID = ''", "RUN_ID = '" + RUN_ID + "'")
        with tempfile.TemporaryDirectory() as directory:
            drive = Path(directory)
            manifest = drive / 'DMRColab/runs' / RUN_ID / 'manifest.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{}', encoding='utf-8')
            context = {'RESUME_PHASE': 'drive', 'DRIVE_ROOT': drive}
            exec(form, context)
            self.assertEqual(context['RESUME_PHASE'], 'form')
            self.assertEqual(context['RESUME_PARAMETERS']['run_id'], RUN_ID)
            context['RESUME_COMMAND'] = ('stale',)
            context['RESUME_PHASE'] = 'drive'
            exec(form, context)
            self.assertEqual(context['RESUME_PHASE'], 'form')
            self.assertIsNone(context['RESUME_COMMAND'])
            self.assertEqual(context['RESUME_PARAMETERS']['run_id'], RUN_ID)


if __name__ == '__main__':
    unittest.main()
