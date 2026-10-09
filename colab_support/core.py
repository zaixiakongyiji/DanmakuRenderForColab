"""不依赖 DMR 的状态机与可校验备份，供离线测试。"""
import hashlib
import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from .diagnostics import safe_diagnostic


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def digest(path):
    result = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            size += len(block)
            result.update(block)
    return size, result.hexdigest()


def verified_copy(source, destination):
    """只证明挂载目录读回一致，不代表 Drive 服务端持久性。"""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.partial')
    expected = digest(source)
    shutil.copyfile(source, temporary)
    if digest(temporary) != expected or digest(source) != expected:
        raise IOError('backup_checksum_mismatch')
    os.replace(temporary, destination)
    return {'size': expected[0], 'sha256': expected[1]}


class BackupWorker:
    def __init__(self, events, copy=verified_copy, retry_wait=1):
        self.events = events
        self.copy = copy
        self.retry_wait = retry_wait
        self.jobs = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, job_id, source, destination, content=None):
        self.jobs.put((job_id, source, Path(destination), content))

    def close(self):
        self.jobs.put(None)
        self.thread.join()

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            job_id, source, destination, content = job
            for attempt in range(1, 4):
                try:
                    if content is None:
                        result = self.copy(source, destination)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        temporary = destination.with_name(destination.name + '.partial')
                        temporary.write_bytes(content)
                        if temporary.read_bytes() != content:
                            raise IOError('checkpoint_checksum_mismatch')
                        os.replace(temporary, destination)
                        result = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                    self.events.put({'source': 'backup', 'event': 'end', 'request_id': job_id,
                                     'data': dict(result, attempts=attempt)})
                    break
                except Exception as error:
                    if attempt == 3:
                        self.events.put({'source': 'backup', 'event': 'error', 'request_id': job_id,
                                         'data': {'error': type(error).__name__, 'attempts': attempt}})
                    else:
                        time.sleep(self.retry_wait * attempt)


class LiveWindow:
    """None 表示查询失败，会打断连续下播判断。"""
    def __init__(self, initial_wait=900, offline_grace=180, max_record=43200, now=0):
        self.initial_wait = initial_wait
        self.offline_grace = offline_grace
        self.max_record = max_record
        self.created = now
        self.live_since = None
        self.offline_since = None

    def observe(self, status, now):
        if self.live_since is None and status is True:
            self.live_since = now
        if self.live_since is None:
            return 'initial_timeout' if now - self.created >= self.initial_wait else None
        if now - self.live_since >= self.max_record:
            return 'record_limit'
        if status is False:
            if self.offline_since is None:
                self.offline_since = now
            if now - self.offline_since >= self.offline_grace:
                return 'live_end'
        else:
            self.offline_since = None
        return None


class Coordinator:
    def __init__(self, run_dir, backup_dir, backup, render_submit, now=time.monotonic):
        self.run_dir = Path(run_dir)
        self.backup_dir = Path(backup_dir)
        self.backup = backup
        self.render_submit = render_submit
        self.now = now
        self.started = now()
        self.producer_done = False
        self.pending_render = {}
        self.pending_backup = {}
        self.sequence = 0
        self.manifest = {'version': 1, 'run_id': self.run_dir.name, 'status': 'running',
                         'segments': {}, 'errors': [], 'warnings': [], 'stop_reason': None,
                         'backup_verification': 'mounted_directory_readback_sha256',
                         'backup_dir': str(self.backup_dir)}

    def error(self, code):
        if code not in self.manifest['errors']:
            self.manifest['errors'].append(code)

    def _backup(self, key, kind, path):
        job_id = uuid.uuid4().hex
        destination = self.backup_dir / kind / (key + Path(path).suffix)
        self.pending_backup[job_id] = (key, kind)
        self.manifest['segments'][key]['backup'][kind] = {'status': 'pending'}
        self.backup.submit(job_id, str(path), destination)

    def checkpoint(self):
        atomic_json(self.run_dir / 'manifest.json', self.manifest)
        self.sequence += 1
        job_id = 'checkpoint-' + str(self.sequence)
        self.pending_backup[job_id] = (None, 'checkpoint')
        content = json.dumps(self.manifest, ensure_ascii=False, indent=2).encode('utf-8')
        self.backup.submit(job_id, None, self.backup_dir / 'manifest.json', content=content)

    def handle(self, message):
        source, event = message['source'], message['event']
        data, request_id = message.get('data'), message.get('request_id')
        if source == 'downloader':
            if event == 'producer_done':
                self.producer_done = True
                self.manifest['stop_reason'] = data['reason']
                self.manifest['recording_elapsed_seconds'] = data.get('recording_seconds', 0)
                if data['reason'] not in ('live_end', 'record_limit', 'requested'):
                    self.error(data['reason'])
            elif event == 'livesegment':
                key = f"{data['group_id']}-{data['segment_id']}"
                if not all(c.isalnum() or c in '-_' for c in key):
                    raise ValueError('invalid_segment_key')
                if key in self.manifest['segments']:
                    return
                video, ass = Path(data['path']), Path(data['dm_file_id'])
                for path in (video, ass):
                    if not path.resolve().is_relative_to(self.run_dir.resolve()) or not path.is_file():
                        raise ValueError('invalid_segment_file')
                self.manifest['segments'][key] = {
                    'source': video.name, 'danmaku': ass.name, 'duration': data['duration'],
                    'render': 'pending', 'backup': {}, 'render_queued_at': self.now(),
                    'danmaku_dialogues': sum(line.startswith('Dialogue:') for line in ass.read_text(encoding='utf-8-sig').splitlines())}
                self._backup(key, 'source', video)
                self._backup(key, 'danmaku', ass)
                rid = uuid.uuid4().hex
                self.pending_render[rid] = key
                try:
                    self.render_submit(rid, data, self.run_dir / 'rendered' / (key + '.mp4'))
                except Exception:
                    self.pending_render.pop(rid)
                    self.manifest['segments'][key]['render'] = 'error'
                    self.error('render_submit:' + key)
            elif event == 'diagnostic':
                diagnostic = safe_diagnostic(data)
                self.manifest['phase'] = diagnostic['stage']
                history = self.manifest.setdefault('diagnostics', [])
                history.append(diagnostic)
                del history[:-30]
            elif event == 'quality':
                self.manifest['quality'] = data
                if data['quality'] < 10000:
                    warning = '观看登录可能失效或源流画质降低：当前 quality < 10000，请检查实际画质。'
                    if warning not in self.manifest['warnings']:
                        self.manifest['warnings'].append(warning)
            elif event == 'liveerror':
                self.manifest['warnings'].append('录制重连：' + str(data))
            else:
                return
        elif source == 'render' and event == 'start':
            key = self.pending_render.get(request_id)
            if key is None:
                return
            segment = self.manifest['segments'][key]
            segment['render'] = 'rendering'
            segment['render_started_at'] = self.now()
        elif source == 'render' and event in ('end', 'error'):
            key = self.pending_render.pop(request_id, None)
            if key is None:
                return
            segment = self.manifest['segments'][key]
            queued = segment.pop('render_queued_at')
            started = segment.pop('render_started_at', queued)
            segment['render_elapsed_seconds'] = self.now() - started
            segment['render_wait_seconds'] = started - queued
            if event == 'end':
                output = Path(data['output']['path'])
                if not output.is_file() or output.stat().st_size == 0:
                    segment['render'] = 'error'
                    self.error('render_output_missing:' + key)
                else:
                    segment['render'] = 'success'
                    self._backup(key, 'rendered', output)
            else:
                segment['render'] = 'error'
                self.error('render_failed:' + key)
        elif source == 'backup' and event in ('end', 'error'):
            item = self.pending_backup.pop(request_id, None)
            if item is None:
                return
            key, kind = item
            if event == 'error':
                self.error('backup_failed:' + (key or 'metadata') + ':' + kind)
            if key is None:
                # 确认检查点不能再次生成检查点，避免无限任务链。
                atomic_json(self.run_dir / 'manifest.json', self.manifest)
                return
            self.manifest['segments'][key]['backup'][kind] = dict(
                data, status='success' if event == 'end' else 'error')
        else:
            return
        self.checkpoint()

    def drained(self):
        return self.producer_done and not self.pending_render and not self.pending_backup

    def finish(self):
        if not self.drained():
            raise RuntimeError('tasks_not_drained')
        if not self.manifest['segments']:
            self.error('no_segments')
        self.manifest['elapsed_seconds'] = self.now() - self.started
        self.manifest['status'] = 'failed' if self.manifest['errors'] else 'success'
        self.checkpoint()
