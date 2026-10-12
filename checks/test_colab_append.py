"""同场多运行编号追加计划的边界检查。"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from colab_support.session_append import SessionAppend
from colab_support.core import atomic_json, digest
from colab_support.posting import execute_job
from colab_support.recovery import Recovery, RecoveryError
from colab_support.upload_transaction import ColabBiliUploader, Journal, UploadError
from checks.test_colab_upload import BVID, CONFIG, Remote

BASE_RUN = '20261001-210000-aaaaaaaa'
EXTRA_RUN = '20261001-220500-bbbbbbbb'


class FakeRecovery:
    def __init__(self, drive, work, run_id, **kwargs):
        self.run_id = run_id
        self.work = Path(work)
        self.backup = self.work / 'backup'
        self.original_hash = 'a' * 64
        is_base = run_id == BASE_RUN
        start = datetime(2026, 10, 1, 21, 0) if is_base else datetime(2026, 10, 1, 22, 5)
        self.state = {'run_id': run_id, 'settings': {'room_url': 'https://live.bilibili.com/100',
            'upload_min_length': 120}}
        self.segments = []
        for index in (1,):
            key = ('base' if is_base else 'extra') + '-1'
            segment = {'group_id': key.rsplit('-', 1)[0], 'segment_id': index,
                'duration': 300, 'ctime': start.isoformat(), 'render': 'success',
                'rendered_path': 'rendered/' + key + '.mp4',
                'backup': {'source': {'sha256': run_id + 's'}, 'danmaku': {'sha256': run_id + 'd'},
                           'rendered': {'status': 'success', 'sha256': run_id + 'r', 'size': 10}},
                'upload': {'status': 'failed'}}
            self.segments.append((key, segment))
            (self.work / segment['rendered_path']).parent.mkdir(parents=True, exist_ok=True)
            (self.work / segment['rendered_path']).write_bytes(b'0123456789')

    def inspect(self):
        return {}


class FakeJournal:
    def __init__(self, *args):
        self.data = {'version': 1, 'run_id': args[2], 'uid': 1, 'parts': {}}
        if args[2] == BASE_RUN:
            self.data['bvid'] = BVID
            self.data['parts']['base-1'] = {'status': 'submitted', 'part_index': 1,
                'filename': 'existing', 'sha256': BASE_RUN + 'r', 'size': 10,
                'expected_filenames': ['existing']}
        else:
            self.data['bvid'] = None

    def save(self):
        pass


class AppendPlanTests(unittest.TestCase):
    def test_same_room_and_ordered_new_segment_builds_plan(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('colab_support.session_append.Recovery', FakeRecovery), \
             patch('colab_support.session_append.Journal', FakeJournal), \
             patch('colab_support.session_append.load_upload_config', return_value={
                 'enabled': True, 'expected_uid': 1, 'min_length': 120}), \
             patch('colab_support.session_append.ColabBiliUploader') as uploader:
            uploader.return_value.check_identity.return_value = None
            uploader.return_value.remote.return_value = Mock(videos=[{'filename': 'existing'}])
            session = SessionAppend(Path(directory), Path(directory) / 'work',
                BASE_RUN, [EXTRA_RUN], BVID,
                min_free_gib=0)
            plan = session.plan()
            self.assertEqual(plan['existing_parts'], 1)
            self.assertEqual(plan['entries'][1]['action'], 'append')
            self.assertEqual(plan['entries'][1]['part_index'], 2)

    def test_different_room_stops_before_binding(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('colab_support.session_append.Recovery', FakeRecovery), \
             patch('colab_support.session_append.Journal', FakeJournal), \
             patch('colab_support.session_append.load_upload_config', return_value={
                 'enabled': True, 'expected_uid': 1, 'min_length': 120}), \
             patch('colab_support.session_append.ColabBiliUploader') as uploader:
            uploader.return_value.check_identity.return_value = None
            uploader.return_value.remote.return_value = Mock(videos=[{'filename': 'existing'}])
            session = SessionAppend(Path(directory), Path(directory) / 'work',
                BASE_RUN, [EXTRA_RUN], BVID,
                min_free_gib=0)
            session.recoveries[EXTRA_RUN].state['settings']['room_url'] = 'https://live.bilibili.com/1'
            with self.assertRaisesRegex(Exception, 'room_mismatch'):
                session.plan()


class AppendTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.drive, self.work = self.root / 'drive', self.root / 'work'
        self.manifests = {}
        for rid, start in ((BASE_RUN, datetime(2026, 10, 1, 21)),
                           (EXTRA_RUN, datetime(2026, 10, 1, 22, 5))):
            self.manifests[rid] = {'version': 2, 'run_id': rid, 'status': 'failed',
                'errors': ['recording_retries_exhausted'], 'settings': {
                    'room_url': 'https://live.bilibili.com/100', 'upload_min_length': 120},
                'segments': {}}
            self.add_segment(rid, 1, start)
        self.api = Mock()
        self.api._session.get.return_value.json.return_value = {
            'code': 0, 'data': {'isLogin': True, 'mid': 1}}
        self.api.get_remote_data.return_value = None
        self.api.upload_media.side_effect = [
            {'filename': 'remote' + str(i)} for i in range(1, 10)]
        self.uploader = ColabBiliUploader(None, CONFIG, api=self.api)
        self.uploader.metadata = Mock(return_value={'title': 'original', 'videos': [], 'bvid': None})
        def submit(payload, edit=False):
            self.api.get_remote_data.return_value = Remote(videos=copy.deepcopy(payload['videos']))
            return {'code': 0, 'data': {'bvid': BVID}}
        self.api.submit_web.side_effect = submit
        journal = self.journal(BASE_RUN)
        self.uploader.upload_part(self.backup(BASE_RUN) / 'rendered/s-1.mp4',
            self.manifests[BASE_RUN]['segments']['s-1'], 1, journal, 's-1')
        self.api.upload_media.reset_mock()
        self.api.submit_web.reset_mock()

        class Worker:
            def __init__(inner, events, cookie, config_file, config, work, backup):
                inner.events, inner.work, inner.backup = events, work, backup
                inner.cookie, inner.config_file = cookie, config_file
            def begin_drain(inner):
                pass
            def close(inner):
                pass
            def submit(inner, rid, key, path, metadata, index):
                result = execute_job({'cookie': str(inner.cookie), 'config_file': str(inner.config_file),
                    'local': str(inner.work / 'upload.json'), 'remote': str(inner.backup / 'upload.json'),
                    'run_id': inner.work.name, 'key': key, 'path': str(path), 'metadata': metadata,
                    'part_index': index, 'candidate': getattr(inner, 'candidate', None)})
                event = 'end' if result['status'] == 'submitted' else 'unknown'
                inner.events.put({'source': 'uploader', 'event': event, 'request_id': rid, 'data': result})

        for module in ('colab_support.session_append', 'colab_support.posting', 'colab_upload'):
            self.enterContext(patch(module + '.load_upload_config', return_value=CONFIG))
        self.enterContext(patch('colab_upload.load_upload_cookie', return_value={
            'SESSDATA': 'synthetic', 'bili_jct': 'synthetic'}))
        self.enterContext(patch('colab_support.uploader.prepare_identity'))
        self.enterContext(patch('colab_support.upload_transaction.ColabBiliUploader', return_value=self.uploader))
        self.enterContext(patch('colab_support.session_append.ColabBiliUploader', return_value=self.uploader))
        self.enterContext(patch('colab_upload.PostingWorker', Worker))
        self.enterContext(patch('colab_support.recovery.probe', return_value=(
            300, [{'codec_type': 'video', 'r_frame_rate': '25/1'}])))
        self.enterContext(patch('builtins.print'))

    def backup(self, rid):
        return self.drive / 'DMRColab/runs' / rid

    def journal(self, rid):
        return Journal(self.work / rid / 'upload.json', self.backup(rid) / 'upload.json', rid, 1)

    def save_manifest(self, rid):
        atomic_json(self.backup(rid) / 'manifest.json', self.manifests[rid])

    def add_segment(self, rid, index, start, duration=300):
        key = 's-' + str(index)
        segment = {'group_id': 's', 'segment_id': index, 'ctime': start.isoformat(),
                   'duration': duration, 'render': 'success', 'backup': {}, 'upload': {'status': 'failed'}}
        for kind, suffix in (('source', '.mp4'), ('danmaku', '.ass'), ('rendered', '.mp4')):
            path = self.backup(rid) / kind / (key + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((rid + kind + key).encode())
            size, checksum = digest(path)
            segment['backup'][kind] = {'status': 'success', 'size': size, 'sha256': checksum}
        self.manifests[rid]['segments'][key] = segment
        self.save_manifest(rid)
        return segment

    def session(self, bvid=BVID):
        return SessionAppend(self.drive, self.work, BASE_RUN, [EXTRA_RUN], bvid,
                             encoder='libx264', min_free_gib=0)

    def test_append_preserves_original_part_and_rerun_does_not_resubmit(self):
        originals = {r: (self.backup(r) / 'manifest.json').read_bytes() for r in self.manifests}
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual(self.api.upload_media.call_count, 1)
        self.assertEqual(self.api.submit_web.call_count, 1)
        call = self.api.submit_web.call_args
        self.assertTrue(call.kwargs['edit'])
        self.assertEqual(call.args[0]['bvid'], BVID)
        self.assertEqual([p['title'] for p in call.args[0]['videos']], ['P1', 'P2'])
        self.assertEqual([p['filename'] for p in call.args[0]['videos']], ['remote1', 'remote2'])
        self.assertEqual(self.uploader.metadata.call_count, 1)
        for rid in self.manifests:
            self.assertEqual((self.backup(rid) / 'manifest.json').read_bytes(), originals[rid])
        state = json.loads((self.backup(BASE_RUN) / 'append_resume.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'success')
        self.assertEqual(self.journal(EXTRA_RUN).data['append_target'], {'run_id': BASE_RUN, 'bvid': BVID})

    def test_timeout_after_commit_is_reconciled_without_another_post(self):
        submit = self.api.submit_web.side_effect
        def timeout(payload, edit=False):
            submit(payload, edit)
            raise TimeoutError()
        self.api.submit_web.side_effect = timeout
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_unknown_without_remote_commit_stops_and_never_resubmits(self):
        self.api.submit_web.side_effect = TimeoutError()
        self.assertEqual(self.session().execute(), 1)
        self.assertEqual(self.session().execute(), 1)
        self.assertEqual(self.api.submit_web.call_count, 1)
        self.assertEqual(self.api.upload_media.call_count, 1)
        key = EXTRA_RUN + '__s-1'
        self.assertEqual(self.journal(BASE_RUN).data['parts'][key]['status'], 'unknown')

    def test_unknown_later_visible_on_remote_is_only_reconciled(self):
        self.api.submit_web.side_effect = TimeoutError()
        self.assertEqual(self.session().execute(), 1)
        self.api.get_remote_data.return_value = Remote(videos=[
            {'filename': 'remote1', 'title': 'P1'}, {'filename': 'remote2', 'title': 'P2'}])
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_wrong_bvid_and_wrong_account_stop_before_binding(self):
        with self.assertRaisesRegex(RecoveryError, 'bvid_mismatch'):
            self.session('BV0987654321')
        self.api._session.get.return_value.json.return_value['data']['mid'] = 2
        with self.assertRaisesRegex(UploadError, 'identity_mismatch'):
            self.session().execute()
        self.assertFalse(self.journal(BASE_RUN).data.get('append_session'))
        self.api.submit_web.assert_not_called()

    def test_remote_parts_changed_stops_before_binding(self):
        self.api.get_remote_data.return_value = Remote(videos=[{'filename': 'unrelated'}])
        with self.assertRaisesRegex(RecoveryError, 'remote_parts_mismatch'):
            self.session().execute()
        self.assertFalse(self.journal(EXTRA_RUN).data.get('append_target'))
        self.api.submit_web.assert_not_called()

    def test_overlap_missing_time_and_earlier_new_part_are_rejected(self):
        segment = self.manifests[EXTRA_RUN]['segments']['s-1']
        for stamp, error in (('2026-10-01T21:03:00', 'recording_overlap'),
                             (None, 'recording_time_missing'),
                             ('2026-10-01T20:00:00', 'existing_part_order_changed')):
            with self.subTest(stamp=stamp):
                segment['ctime'] = stamp
                self.save_manifest(EXTRA_RUN)
                with self.assertRaisesRegex(RecoveryError, error):
                    self.session().execute()
        self.api.submit_web.assert_not_called()

    def test_exact_duplicate_is_skipped_and_gap_is_visible(self):
        plan = self.session().plan()
        self.assertEqual(plan['gaps'][0]['seconds'], 3600)
        base = self.manifests[BASE_RUN]['segments']['s-1']
        extra = self.manifests[EXTRA_RUN]['segments']['s-1']
        for kind, suffix in (('source', '.mp4'), ('danmaku', '.ass')):
            (self.backup(EXTRA_RUN) / kind / ('s-1' + suffix)).write_bytes(
                (self.backup(BASE_RUN) / kind / ('s-1' + suffix)).read_bytes())
            extra['backup'][kind] = copy.deepcopy(base['backup'][kind])
        extra['ctime'] = base['ctime']
        self.save_manifest(EXTRA_RUN)
        plan = self.session().plan()
        self.assertEqual(len(plan['entries']), 1)
        self.assertEqual(plan['duplicates'][0]['duplicate_of'], 's-1')
        self.assertEqual(self.session().execute(), 0)
        self.api.upload_media.assert_not_called()
        self.api.submit_web.assert_not_called()

    def test_existing_failed_parts_are_retried_in_order_before_new_run(self):
        for index in (2, 3):
            segment = self.add_segment(BASE_RUN, index, datetime(2026, 10, 1, 21) + timedelta(minutes=5 * (index - 1)))
            journal = self.journal(BASE_RUN)
            journal.data['parts']['s-' + str(index)] = dict(
                segment['backup']['rendered'], status='failed', part_index=index)
            journal.save()
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual([len(call.args[0]['videos']) for call in self.api.submit_web.call_args_list], [2, 3, 4])
        self.assertTrue(all(call.kwargs['edit'] for call in self.api.submit_web.call_args_list))

    def test_source_with_unresolved_submission_or_other_post_is_rejected(self):
        journal = self.journal(EXTRA_RUN)
        info = self.manifests[EXTRA_RUN]['segments']['s-1']['backup']['rendered']
        journal.data['parts']['s-1'] = dict(info, status='unknown', part_index=1,
            filename='elsewhere', expected_filenames=['elsewhere'])
        journal.save()
        with self.assertRaisesRegex(RecoveryError, 'source_submit_unresolved'):
            self.session().execute()
        journal.data.update(bvid='BV0987654321', parts={})
        journal.save()
        with self.assertRaisesRegex(RecoveryError, 'source_has_other_post'):
            self.session().execute()
        self.api.submit_web.assert_not_called()

    def test_bound_runs_cannot_be_uploaded_through_single_run_entry(self):
        from colab_upload import upload_manifest
        self.assertEqual(self.session().execute(), 0)
        for rid in (BASE_RUN, EXTRA_RUN):
            with self.subTest(run_id=rid), self.assertRaisesRegex(UploadError, 'use_append_session'):
                upload_manifest(self.drive, self.backup(rid), self.work / rid, self.manifests[rid])
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_short_tail_does_not_take_part_number_and_plan_remains_stable(self):
        self.add_segment(EXTRA_RUN, 2, datetime(2026, 10, 1, 22, 10), duration=30)
        self.assertEqual(self.session().plan()['entries'][-1]['action'], 'skipped_short')
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_cli_inspection_does_not_bind_or_create_a_claim(self):
        from colab_resume import main
        self.assertEqual(main(['--run-id', BASE_RUN, '--append-run-id', EXTRA_RUN,
            '--target-bvid', BVID, '--drive-root', str(self.drive), '--work-root', str(self.work),
            '--min-free-gib', '0', '--inspect-only']), 0)
        self.assertFalse(self.journal(BASE_RUN).data.get('append_session'))
        self.assertFalse((self.drive / 'DMRColab/control').exists())
        self.api.submit_web.assert_not_called()

    def test_missing_render_is_recovered_once_and_append_plan_survives_restart(self):
        from colab_support.core import verified_copy
        segment = self.manifests[EXTRA_RUN]['segments']['s-1']
        segment.update(render='error')
        segment['backup']['rendered'] = {'status': 'error'}
        self.save_manifest(EXTRA_RUN)
        def render(recovery, key, segment, deadline):
            target = recovery.work / 'rendered' / (key + '.mp4')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'recovered-render')
            relative = 'recovery/rendered/' + key + '.mp4'
            info = verified_copy(target, recovery.backup / relative)
            segment.update(render='success', rendered_path=target.relative_to(recovery.work).as_posix())
            segment['backup']['rendered'] = dict(info, status='success', recovery_path=relative)
            recovery.checkpoint()
        with patch.object(Recovery, 'render_missing', autospec=True, side_effect=render) as renderer:
            self.assertEqual(self.session().execute(), 0)
            self.assertEqual(self.session().execute(), 0)
        self.assertEqual(renderer.call_count, 1)
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_media_interruption_is_saved_and_resumes_without_new_binding(self):
        with patch.object(Recovery, 'restore_media', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.session().execute()
        binding = copy.deepcopy(self.journal(BASE_RUN).data['append_session'])
        state = json.loads((self.backup(BASE_RUN) / 'append_resume.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(self.session().execute(), 0)
        self.assertEqual(self.journal(BASE_RUN).data['append_session'], binding)
        self.assertEqual(self.api.submit_web.call_count, 1)

    def test_bound_plan_cannot_drop_a_run(self):
        self.assertEqual(self.session().execute(), 0)
        journal = self.journal(BASE_RUN)
        journal.data['append_session']['runs'].append('20261001-230000-cccccccc')
        journal.save()
        with self.assertRaisesRegex(RecoveryError, 'session_plan_changed'):
            self.session().execute()
        self.assertEqual(self.api.submit_web.call_count, 1)


class AppendNotebookTests(unittest.TestCase):
    def test_old_source_stops_before_dependency_install_and_current_source_builds_command(self):
        root = Path(__file__).resolve().parents[1]
        cells = json.loads((root / 'notebooks/colab_resume.ipynb').read_text(encoding='utf-8'))['cells']
        source = next(''.join(c['source']) for c in cells if c['cell_type'] == 'code' and '③ 下载' in ''.join(c['source']))
        params = dict(run_id=BASE_RUN, append_ids=[EXTRA_RUN], target_bvid=BVID,
                      upload=True, bvid='', encoder='libx264', timeout=14400, free=10)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'project'
            (project / 'colab_support').mkdir(parents=True)
            script = project / 'colab_resume.py'
            script.write_text("parser.add_argument('--run-id')\n", encoding='utf-8')
            with patch('tempfile.mkdtemp', return_value=directory), \
                 patch('subprocess.run') as runner, \
                 patch('subprocess.check_output', return_value='old-commit'), patch('builtins.print'):
                context = dict(RESUME_PHASE='form', RESUME_PARAMETERS=params, DRIVE_ROOT=Path(directory), Path=Path)
                with self.assertRaisesRegex(RuntimeError, '尚不支持跨任务追加'):
                    exec(source, context)
                self.assertEqual(context['RESUME_PHASE'], 'invalid')
                self.assertIsNone(context['RESUME_COMMAND'])
                self.assertEqual(runner.call_count, 1)
                script.write_bytes((root / 'colab_resume.py').read_bytes())
                (project / 'colab_support/session_append.py').write_text('', encoding='utf-8')
                runner.reset_mock()
                context['RESUME_PHASE'] = 'form'
                exec(source, context)
                self.assertEqual(context['RESUME_PHASE'], 'dependencies')
                self.assertIn('--append-run-id', context['RESUME_COMMAND'])
                self.assertIn('--target-bvid', context['RESUME_COMMAND'])

    def test_failed_inspection_prints_diagnostic_and_never_marks_inspected(self):
        root = Path(__file__).resolve().parents[1]
        cells = json.loads((root / 'notebooks/colab_resume.ipynb').read_text(encoding='utf-8'))['cells']
        source = next(''.join(c['source']) for c in cells if c['cell_type'] == 'code' and '④ 校验' in ''.join(c['source']))
        import subprocess
        for status, diagnostic in ((2, 'unrecognized arguments: --append-run-id'), (1, 'append_room_mismatch'), (0, 'plan')):
            with self.subTest(status=status), patch('subprocess.run', return_value=Mock(
                    returncode=status, stdout=diagnostic, stderr='')) as runner, patch('builtins.print') as printer:
                context = dict(RESUME_PHASE='dependencies', RESUME_COMMAND=('python', 'resume.py'),
                               PROJECT=root, subprocess=subprocess)
                if status:
                    with self.assertRaisesRegex(RuntimeError, '尚未开始恢复与投稿'):
                        exec(source, context)
                    self.assertEqual(context['RESUME_PHASE'], 'invalid')
                else:
                    exec(source, context)
                    self.assertEqual(context['RESUME_PHASE'], 'inspected')
                printer.assert_any_call(diagnostic, end='')
                self.assertIn('--inspect-only', runner.call_args.args[0])

    def test_append_form_validates_all_runs_and_incompatible_modes(self):
        path = Path(__file__).resolve().parents[1] / 'notebooks/colab_resume.ipynb'
        cells = [c for c in json.loads(path.read_text(encoding='utf-8'))['cells'] if c['cell_type'] == 'code']
        form = ''.join(cells[1]['source']).replace("RUN_ID = ''", "RUN_ID = '" + BASE_RUN + "'")
        form = form.replace("APPEND_RUN_IDS = ''", "APPEND_RUN_IDS = '" + EXTRA_RUN + "'")
        form = form.replace("TARGET_BVID = ''", "TARGET_BVID = '" + BVID + "'")
        with tempfile.TemporaryDirectory() as directory:
            drive = Path(directory)
            for rid in (BASE_RUN, EXTRA_RUN):
                path = drive / 'DMRColab/runs' / rid / 'manifest.json'
                path.parent.mkdir(parents=True)
                path.write_text('{}', encoding='utf-8')
            context = {'RESUME_PHASE': 'drive', 'DRIVE_ROOT': drive}
            exec(form, context)
            self.assertEqual(context['RESUME_PARAMETERS']['append_ids'], [EXTRA_RUN])
            for invalid in (form.replace('UPLOAD = True', 'UPLOAD = False'),
                            form.replace("RECONCILE_BVID = ''", "RECONCILE_BVID = '" + BVID + "'"),
                            form.replace(EXTRA_RUN, BASE_RUN)):
                with self.assertRaises(ValueError):
                    exec(invalid, {'RESUME_PHASE': 'drive', 'DRIVE_ROOT': drive})
            self.assertIn("'append_resume.json' if RESUME_PARAMETERS['append_ids']", ''.join(cells[-1]['source']))


if __name__ == '__main__':
    unittest.main()
