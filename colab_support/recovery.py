"""按历史清单恢复已有媒体；原录制清单和触发回执保持原值。"""
import copy
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .core import atomic_json, digest, verified_copy
from .merge import (MergeError, media_command, merge_rendered, probe, verify_decode,
                    video_encoding_args, video_frame_rate)
from .trigger import RUN_ID_RE, control_paths, validate_ack
from .upload_transaction import UploadError


class RecoveryError(RuntimeError):
    pass


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise RecoveryError('resume_manifest_unreadable') from None


def ordered_segments(manifest):
    values = manifest.get('segments')
    if not isinstance(values, dict) or not values:
        raise RecoveryError('resume_no_segments')
    ordered, groups = [], set()
    for key, segment in values.items():
        match = re.fullmatch(r'([A-Za-z0-9_-]+)-([1-9][0-9]*)', key)
        if not match or not isinstance(segment, dict):
            raise RecoveryError('resume_segment_invalid')
        if str(segment.get('group_id')) != match[1] or str(segment.get('segment_id')) != match[2]:
            raise RecoveryError('resume_segment_invalid')
        duration = segment.get('duration')
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise RecoveryError('resume_duration_invalid')
        groups.add(match[1])
        ordered.append((int(match[2]), key, segment))
    ordered.sort()
    if len(groups) != 1 or [i for i, _, _ in ordered] != list(range(1, len(ordered) + 1)):
        raise RecoveryError('resume_segment_gap')
    return [(key, segment) for _, key, segment in ordered]


class Recovery:
    def __init__(self, drive, work, run_id, *, encoder='h264_nvenc', min_free_gib=10):
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise RecoveryError('resume_run_id_invalid')
        if encoder not in {'h264_nvenc', 'libx264'}:
            raise RecoveryError('resume_encoder_invalid')
        if type(min_free_gib) not in (int, float) or not math.isfinite(min_free_gib) or min_free_gib < 0:
            raise RecoveryError('resume_disk_limit_invalid')
        self.drive, self.work = Path(drive).resolve(), Path(work).resolve()
        if self.work.name != run_id or self.work.is_relative_to(self.drive) or self.drive.is_relative_to(self.work):
            raise RecoveryError('resume_work_directory_invalid')
        self.backup = self.drive / 'DMRColab/runs' / run_id
        original_path = self.backup / 'manifest.json'
        original = read_json(original_path)
        if original.get('run_id') != run_id or original.get('status') not in {'failed', 'success'}:
            raise RecoveryError('resume_run_not_terminal')
        ack = control_paths(self.drive, run_id)[2]
        if ack.exists():
            value = read_json(ack)
            try:
                validate_ack(value, run_id)
            except ValueError:
                raise RecoveryError('resume_ack_invalid') from None
            if value['status'] not in {'success', 'failed'}:
                raise RecoveryError('resume_run_still_active')
        self.original_hash = digest(original_path)[1]
        remote_state, local_state = self.backup / 'resume_manifest.json', self.work / 'resume_manifest.json'
        states = [read_json(p) for p in (local_state, remote_state) if p.exists()]
        if len(states) == 2 and states[0] != states[1]:
            raise RecoveryError('resume_checkpoint_diverged')
        if states:
            self.state = states[0]
            if (self.state.get('version') != 1 or self.state.get('run_id') != run_id or
                    self.state.get('original_sha256') != self.original_hash):
                raise RecoveryError('resume_checkpoint_invalid')
        else:
            self.state = {'version': 1, 'run_id': run_id, 'original_sha256': self.original_hash,
                'original_status': original['status'], 'original_errors': original.get('errors', []),
                'status': 'pending', 'phase': 'inspection', 'errors': [],
                'segments': copy.deepcopy(original['segments']),
                'settings': copy.deepcopy(original.get('settings', {})),
                'merge': copy.deepcopy(original.get('merge', {}))}
        self.segments = ordered_segments(self.state)
        if set(self.state['segments']) != set(original['segments']):
            raise RecoveryError('resume_checkpoint_segments_changed')
        self.encoder, self.min_free_gib = encoder, min_free_gib

    def checkpoint(self):
        self.state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        atomic_json(self.work / 'resume_manifest.json', self.state)
        verified_copy(self.work / 'resume_manifest.json', self.backup / 'resume_manifest.json')

    def checked_backup(self, relative, info):
        relative = Path(relative)
        path = (self.backup / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(self.backup.resolve()):
            raise RecoveryError('resume_backup_path_invalid')
        if info.get('status') != 'success' or not path.is_file():
            raise RecoveryError('resume_backup_missing')
        if digest(path) != (info.get('size'), info.get('sha256')):
            raise RecoveryError('resume_backup_checksum_mismatch')
        return path

    def inspect(self):
        plan = []
        journals = [read_json(p) for p in (self.work / 'upload.json', self.backup / 'upload.json') if p.exists()]
        for journal in journals:
            if journal.get('run_id') != self.state['run_id'] or not isinstance(journal.get('parts'), dict):
                raise RecoveryError('resume_upload_journal_invalid')
        if len(journals) == 2 and journals[0] != journals[1]:
            raise RecoveryError('resume_upload_journal_diverged')
        for key, segment in self.segments:
            previous = segment.get('upload', {})
            recovered = self.state.get('upload', {}).get('parts', {}).get(key, {})
            evidence = (previous, recovered)
            if any(p.get('status') in {'uploading', 'uploaded', 'submitting', 'submitted', 'unknown'} or
                   p.get('filename') or p.get('bvid') for p in evidence):
                if not journals or key not in journals[0]['parts']:
                    raise RecoveryError('resume_upload_journal_missing')
            backups = segment.get('backup', {})
            for kind, suffix in (('source', '.mp4'), ('danmaku', '.ass')):
                self.checked_backup(f'{kind}/{key}{suffix}', backups.get(kind, {}))
            info = backups.get('rendered', {})
            if info.get('status') == 'success':
                self.checked_backup(info.get('recovery_path', f'rendered/{key}.mp4'), info)
                action = 'reuse_rendered'
            else:
                action = 'render_from_source'
                # 已开始的投稿必须保持原文件身份，不能用重渲染覆盖。
                for journal in journals:
                    if key in journal['parts']:
                        raise RecoveryError('resume_render_has_upload_transaction')
            plan.append({'segment': key, 'action': action})
        merge = self.state.get('merge', {})
        if merge.get('status') == 'success' and merge.get('backup', {}).get('status') == 'success':
            self.checked_backup(merge['backup'].get('recovery_path', merge.get('path', '')),
                                merge['backup'])
            merge_action = 'reuse_merged'
        else:
            merge_action = 'merge_rendered'
        return {'run_id': self.state['run_id'], 'original_status': self.state['original_status'],
                'original_errors': self.state['original_errors'], 'segments': plan, 'merge': merge_action}

    def require_space(self, size):
        self.work.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.work).free < size + self.min_free_gib * 1024**3:
            raise RecoveryError('resume_disk_low')

    def render_missing(self, key, segment, deadline):
        backups = segment['backup']
        estimated = max(backups['source']['size'], math.ceil(segment['duration'] * 16_000_000 / 8))
        self.require_space(backups['source']['size'] + estimated * 2 + backups['danmaku']['size'])
        print('正在从原片与 ASS 恢复弹幕成品：' + key, flush=True)
        try:
            font = subprocess.run(['fc-match', '-f', '%{family}', 'Noto Sans CJK SC'],
                                  capture_output=True, text=True, timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            raise RecoveryError('resume_font_unavailable') from None
        if 'Noto Sans CJK SC' not in font:
            raise RecoveryError('resume_font_unavailable')
        with tempfile.TemporaryDirectory(prefix='render-', dir=self.work) as temporary:
            folder = Path(temporary)
            source, subtitle, target = folder / 'source.mp4', folder / 'danmaku.ass', folder / 'rendered.mp4'
            verified_copy(self.checked_backup(f'source/{key}.mp4', backups['source']), source)
            verified_copy(self.checked_backup(f'danmaku/{key}.ass', backups['danmaku']), subtitle)
            duration, signature = probe(source, 'ffprobe', deadline)
            rate = self.state['settings'].get('render_frame_rate') or video_frame_rate(signature)
            self.state['settings']['render_frame_rate'] = rate
            media_command(['ffmpeg', '-nostdin', '-n', '-v', 'error',
                '-fflags', '+discardcorrupt+genpts', '-i', source.name,
                '-vf', "subtitles=filename='danmaku.ass'", '-map', '0:v:0', '-map', '0:a:0?',
                '-c:v', self.encoder, *video_encoding_args(rate, self.encoder),
                '-c:a', 'copy', '-movflags', '+faststart', target.name], deadline, cwd=folder)
            actual, _ = probe(target, 'ffprobe', deadline)
            if abs(duration - actual) > 1:
                raise RecoveryError('resume_render_duration_mismatch')
            verify_decode('ffmpeg', target, actual, [actual], deadline)
            local = self.work / 'rendered' / f'{key}.mp4'
            verified_copy(target, local)
        relative = f'recovery/rendered/{key}.mp4'
        info = verified_copy(local, self.backup / relative)
        segment.update(render='success', rendered_path=local.relative_to(self.work).as_posix())
        backups['rendered'] = dict(info, status='success', recovery_path=relative)
        self.checkpoint()

    def restore_media(self, deadline):
        for key, segment in self.segments:
            self.state['phase'] = 'render:' + key
            self.checkpoint()
            info = segment['backup'].get('rendered', {})
            if info.get('status') == 'success':
                path = self.checked_backup(info.get('recovery_path', f'rendered/{key}.mp4'), info)
                local = self.work / 'rendered' / f'{key}.mp4'
                if not local.exists() or digest(local) != (info['size'], info['sha256']):
                    self.require_space(path.stat().st_size)
                    verified_copy(path, local)
                segment.update(render='success', rendered_path=local.relative_to(self.work).as_posix())
                if not self.state['settings'].get('render_frame_rate'):
                    _, signature = probe(local, 'ffprobe', deadline)
                    self.state['settings']['render_frame_rate'] = video_frame_rate(signature)
            else:
                self.render_missing(key, segment, deadline)
            print(json.dumps({'segment': key, 'render': 'ready'}, ensure_ascii=False), flush=True)
        self.checkpoint()

    def merge(self, deadline):
        current = self.state.get('merge', {})
        if current.get('status') == 'success' and current.get('backup', {}).get('status') == 'success':
            self.checked_backup(current['backup'].get('recovery_path', current['path']), current['backup'])
            print('复用已校验的合并归档。', flush=True)
            return
        self.state['phase'] = 'merging'
        self.checkpoint()
        print('正在合并归档；参数不一致时先规范化，原分段保持原值。', flush=True)
        # 每次使用新临时目录，中断残留不能冒充已校验的合并结果。
        with tempfile.TemporaryDirectory(prefix='merge-', dir=self.work) as temporary:
            folder = Path(temporary)
            (folder / 'rendered').mkdir()
            # 同盘硬链接不占第二份媒体空间，且满足 merge 的目录约束。
            for key, segment in self.segments:
                target = folder / segment['rendered_path']
                os.link(self.work / segment['rendered_path'], target)
            manifest = copy.deepcopy(self.state)
            manifest['settings']['encoder'] = self.encoder
            result = merge_rendered(folder, manifest, deadline, min_free_gib=self.min_free_gib)
            relative = 'recovery/merged/complete.mp4'
            info = verified_copy(folder / result['path'], self.backup / relative)
            result['backup'] = dict(info, status='success', recovery_path=relative)
            self.state['merge'] = result
            self.checkpoint()

    def execute(self, *, upload=True, reconcile_bvid=None, media_timeout=14400):
        if type(media_timeout) not in (int, float) or not math.isfinite(media_timeout) or media_timeout <= 0:
            raise RecoveryError('resume_timeout_invalid')
        self.inspect()
        self.state.update(status='running', errors=[], phase='restoring')
        self.checkpoint()
        try:
            deadline = time.monotonic() + media_timeout
            self.restore_media(deadline)
            # 投稿与归档各自收尾，合并失败也可补传已校验的分 P。
            try:
                self.merge(deadline)
            except (MergeError, RecoveryError) as error:
                code = str(error)
                self.state['merge'] = {'status': 'error', 'error': code}
                self.state['errors'].append(code)
                self.checkpoint()
            if upload:
                from colab_upload import upload_manifest
                self.state['phase'] = 'uploading'
                self.checkpoint()
                code = upload_manifest(self.drive, self.backup, self.work, self.state, reconcile_bvid)
                self.state['upload'] = read_json(self.work / 'upload_resume.json')
                if code:
                    self.state['errors'].append('resume_upload_incomplete')
            else:
                self.state['upload'] = {'status': 'not_requested'}
            self.state.update(status='failed' if self.state['errors'] else 'success', phase='finished')
        except BaseException as error:
            code = str(error) if isinstance(error, (RecoveryError, MergeError, UploadError)) else 'resume_step_failed'
            if self.state['phase'] == 'uploading':
                self.state['upload'] = {'status': 'unknown', 'error': code}
            self.state.update(status='failed', phase='interrupted')
            self.state['errors'].append(code)
            raise
        finally:
            self.checkpoint()
        return 0 if self.state['status'] == 'success' else 1
