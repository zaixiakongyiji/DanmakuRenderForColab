"""多次失败任务的分段追加到同一稿件，沿用主任务的投稿事务。"""
import copy
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

from .core import atomic_json, verified_copy
from .recovery import Recovery, RecoveryError, read_json
from .trigger import RUN_ID_RE, validate_room_url
from .upload_config import load_upload_config
from .upload_transaction import BVID, SHANGHAI, ColabBiliUploader, Journal, UploadError


def recording_window(segment):
    try:
        start = datetime.fromisoformat(segment['ctime'])
        start = start.replace(tzinfo=SHANGHAI) if start.tzinfo is None else start.astimezone(SHANGHAI)
        duration = segment['duration']
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        return start, start + timedelta(seconds=duration)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RecoveryError('append_recording_time_missing') from None


class SessionAppend:
    def __init__(self, drive, work_root, base_run, extra_runs, target_bvid, *,
                 encoder='h264_nvenc', min_free_gib=10, uploader=None):
        runs = [base_run, *extra_runs]
        if (len(runs) < 2 or len(set(runs)) != len(runs) or
                any(not isinstance(r, str) or not RUN_ID_RE.fullmatch(r) for r in runs)):
            raise RecoveryError('append_run_ids_invalid')
        if not isinstance(target_bvid, str) or not BVID.fullmatch(target_bvid):
            raise RecoveryError('append_target_bvid_invalid')
        self.drive, self.work_root = Path(drive).resolve(), Path(work_root).resolve()
        self.base_run, self.target_bvid = base_run, target_bvid
        self.recoveries = {r: Recovery(self.drive, self.work_root / r, r,
            encoder=encoder, min_free_gib=min_free_gib) for r in runs}
        self.base = self.recoveries[base_run]
        self.config = load_upload_config(self.drive / 'DMRColab/config/upload.yml')
        if not self.config.get('enabled'):
            raise UploadError('upload_disabled')
        self.journals = {r: Journal(rec.work / 'upload.json', rec.backup / 'upload.json',
            r, self.config['expected_uid']) for r, rec in self.recoveries.items()}
        self.journal = self.journals[base_run]
        if self.journal.data.get('append_target'):
            raise RecoveryError('append_base_is_source')
        if self.journal.data.get('bvid') != target_bvid:
            raise RecoveryError('append_base_bvid_mismatch')
        self.uploader = uploader or ColabBiliUploader(
            self.drive / 'DMRColab/credentials/bilibili_upload.json', self.config)
        self.state_path = self.base.work / 'append_resume.json'
        self.remote_state = self.base.backup / 'append_resume.json'

    def plan(self):
        rooms, entries, duplicates, minimums = set(), [], [], {}
        for run_id, recovery in self.recoveries.items():
            recovery.inspect()
            try:
                rooms.add(validate_room_url(recovery.state['settings']['room_url']))
            except (KeyError, ValueError):
                raise RecoveryError('append_room_missing') from None
            minimum = recovery.state['settings'].get('upload_min_length', self.config['min_length'])
            if type(minimum) not in (int, float) or not math.isfinite(minimum) or minimum < 120:
                raise RecoveryError('append_min_length_invalid')
            minimums[run_id] = minimum
            journal = self.journals[run_id].data
            marker = journal.get('append_target')
            if run_id != self.base_run:
                if marker and marker != {'run_id': self.base_run, 'bvid': self.target_bvid}:
                    raise RecoveryError('append_source_bound_elsewhere')
                if journal.get('append_session') or journal.get('bvid'):
                    raise RecoveryError('append_source_has_other_post')
                if any(p['status'] in {'submitted', 'submitting', 'unknown'} or
                       p.get('submission_rejected') or p.get('operation_id')
                       for p in journal['parts'].values()):
                    raise RecoveryError('append_source_submit_unresolved')
            for key, segment in recovery.segments:
                start, end = recording_window(segment)
                identity = tuple(segment['backup'][kind]['sha256'] for kind in ('source', 'danmaku'))
                item = {'key': key if run_id == self.base_run else run_id + '__' + key,
                    'source_run_id': run_id, 'source_key': key, 'start': start.isoformat(),
                    'end': end.isoformat(), 'identity': list(identity)}
                entries.append(item)
        if len(rooms) != 1:
            raise RecoveryError('append_room_mismatch')
        # 优先保留主任务的副本，之后按录制时间检查顺序及重叠。
        unique, seen = [], {}
        for item in entries:
            identity = tuple(item['identity'])
            if identity in seen:
                if item['source_run_id'] == self.base_run:
                    raise RecoveryError('append_base_duplicate_segments')
                duplicates.append(dict(item, duplicate_of=seen[identity]['key']))
            else:
                unique.append(item)
                seen[identity] = item
        unique.sort(key=lambda item: (item['start'], item['key']))
        previous, gaps, index = None, [], 0
        for item in unique:
            start, end = datetime.fromisoformat(item['start']), datetime.fromisoformat(item['end'])
            if previous:
                previous_end = datetime.fromisoformat(previous['end'])
                if start < previous_end:
                    raise RecoveryError('append_recording_overlap:' + previous['key'] + ':' + item['key'])
                if start > previous_end:
                    gaps.append({'after': previous['key'], 'before': item['key'],
                                 'seconds': (start - previous_end).total_seconds()})
            prior = self.journal.data['parts'].get(item['key'])
            segment = dict(self.recoveries[item['source_run_id']].segments)[item['source_key']]
            if not prior and segment['duration'] < minimums[item['source_run_id']]:
                item.update(action='skipped_short', part_index=None)
            else:
                index += 1
                item.update(action='append', part_index=index)
            previous = item
        mapping = {item['key']: item for item in unique}
        if set(self.journal.data['parts']) - set(mapping):
            raise RecoveryError('append_journal_segment_unknown')
        confirmed = sorted((p for p in self.journal.data['parts'].values() if p['status'] == 'submitted'),
                           key=lambda p: p['part_index'])
        if not confirmed or [p['part_index'] for p in confirmed] != list(range(1, len(confirmed) + 1)):
            raise RecoveryError('append_base_prefix_invalid')
        for key, part in self.journal.data['parts'].items():
            item = mapping[key]
            if part['part_index'] != item['part_index']:
                raise RecoveryError('append_existing_part_order_changed')
            rec = self.recoveries[item['source_run_id']]
            segment = dict(rec.segments)[item['source_key']]
            info = segment['backup'].get('rendered', {})
            if info.get('sha256') != part['sha256'] or info.get('size') != part['size']:
                raise RecoveryError('append_existing_content_changed')
        binding = {'base_run_id': self.base_run, 'target_bvid': self.target_bvid,
            'runs': sorted(self.recoveries), 'original_sha256': {r: rec.original_hash
                for r, rec in self.recoveries.items()}, 'entries': unique, 'duplicates': duplicates}
        existing = self.journal.data.get('append_session')
        if existing and existing != binding:
            raise RecoveryError('append_session_plan_changed')
        self.uploader.check_identity()
        remote = self.uploader.remote(self.target_bvid)
        names = [p['filename'] for p in confirmed]
        expected = [v['filename'] for v in remote.videos]
        unresolved = [p for p in self.journal.data['parts'].values() if p['status'] in {'unknown', 'submitting'}]
        if len(unresolved) > 1:
            raise RecoveryError('append_multiple_unresolved_parts')
        possibilities = [names]
        if unresolved:
            part = unresolved[0]
            if part['part_index'] != len(confirmed) + 1:
                raise RecoveryError('append_unresolved_order_invalid')
            possibilities.append(part['expected_filenames'])
        if expected not in possibilities:
            raise RecoveryError('append_remote_parts_mismatch')
        self.binding = binding
        return dict(binding, existing_parts=len(confirmed), gaps=gaps,
                    warning='时间区间来自录制清单，缺失片段不会补录；部分重叠不自动裁剪。')

    def checkpoint(self, status, **fields):
        value = dict(run_id=self.base_run, bvid=self.target_bvid, status=status,
                     updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **fields)
        atomic_json(self.state_path, value)
        verified_copy(self.state_path, self.remote_state)

    def bind_sources(self):
        self.journal.data['append_session'] = self.binding
        self.journal.save()
        for run_id, journal in self.journals.items():
            if run_id != self.base_run:
                journal.data['append_target'] = {'run_id': self.base_run, 'bvid': self.target_bvid}
                journal.save()

    def execute(self, *, media_timeout=14400):
        from colab_upload import upload_manifest
        if type(media_timeout) not in (int, float) or not math.isfinite(media_timeout) or media_timeout <= 0:
            raise RecoveryError('append_timeout_invalid')
        plan = self.plan()
        self.bind_sources()
        self.checkpoint('running', phase='restoring', plan=plan)
        try:
            deadline = time.monotonic() + media_timeout
            for recovery in self.recoveries.values():
                recovery.restore_media(deadline)
            manifest = {'run_id': self.base_run, 'settings': {'upload_min_length': 120}, 'segments': {}}
            for item in self.binding['entries']:
                recovery = self.recoveries[item['source_run_id']]
                segment = copy.deepcopy(dict(recovery.segments)[item['source_key']])
                if item['action'] == 'skipped_short':
                    continue
                path = recovery.work / segment['rendered_path']
                # 追加 worker 使用主任务目录，不拷贝第二份录像，路径只供子进程读取。
                segment.update(rendered_path=str(path), segment_id=item['part_index'])
                manifest['segments'][item['key']] = segment
            self.checkpoint('running', phase='uploading', plan=plan)
            result = upload_manifest(self.drive, self.base.backup, self.base.work, manifest,
                                     append_session=True)
            summary = read_json(self.base.work / 'upload_resume.json')
            self.checkpoint('success' if result == 0 else 'failed', phase='finished', plan=plan, upload=summary)
            return result
        except BaseException as error:
            code = str(error) if isinstance(error, (RecoveryError, UploadError)) else 'append_step_failed'
            self.checkpoint('failed', phase='interrupted', plan=plan, error=code)
            raise
