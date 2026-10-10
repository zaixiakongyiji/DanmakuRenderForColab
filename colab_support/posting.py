"""串行投稿进程监督；网络卡住不阻塞录制，超时后不重发提交。"""
import argparse
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .core import atomic_json
from .locks import ProcessLock
from .upload_config import load_upload_config

ROOT = Path(__file__).resolve().parents[1]


class PostingWorker:
    def __init__(self, events, cookie, config_file, config, run_dir, backup_dir):
        self.events, self.cookie, self.config_file = events, Path(cookie), Path(config_file)
        self.config, self.run_dir, self.backup_dir = config, Path(run_dir), Path(backup_dir)
        self.jobs = queue.Queue()
        self.closed = threading.Event()
        self.deadline = None
        self.process = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, request_id, key, path, metadata, part_index):
        self.jobs.put((request_id, key, path, metadata, part_index))

    def begin_drain(self):
        if self.deadline is None:
            self.deadline = time.monotonic() + self.config['drain_timeout']

    def close(self):
        self.closed.set()
        self.jobs.put(None)
        self.thread.join(timeout=5)

    def _run(self):
        while not self.closed.is_set():
            job = self.jobs.get()
            if job is None:
                return
            rid, key, path, metadata, index = job
            try:
                folder = self.run_dir / 'private'
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                job_path, result_path = folder / (rid + '.json'), folder / (rid + '.result.json')
                self._execute_job(rid, key, path, metadata, index, job_path, result_path)
            except Exception:
                self.events.put({'source': 'uploader', 'event': 'unknown', 'request_id': rid,
                                 'data': {'status': 'unknown', 'error': 'upload_worker_failed'}})

    def _execute_job(self, rid, key, path, metadata, index, job_path, result_path):
        atomic_json(job_path, {'cookie': str(self.cookie), 'config_file': str(self.config_file),
            'run_id': self.run_dir.name, 'local': str(self.run_dir / 'upload.json'),
            'remote': str(self.backup_dir / 'upload.json'), 'key': key, 'path': str(path),
            'metadata': metadata, 'part_index': index, 'result': str(result_path),
            'candidate': getattr(self, 'candidate', None)})
        try:
            if self.closed.is_set() or (self.deadline is not None and time.monotonic() >= self.deadline):
                self.events.put({'source': 'uploader', 'event': 'error', 'request_id': rid,
                                 'data': {'status': 'failed', 'error': 'upload_drain_timeout'}})
                return
            # 子进程继承录制 worker 的进程组，外层强制结束可一并回收。
            self.process = subprocess.Popen([sys.executable, '-m', 'colab_support.posting',
                '--job', str(job_path)], cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            end = time.monotonic() + self.config['part_timeout']
            timed_out = False
            last_progress = None
            progress_at = 0
            while self.process.poll() is None:
                now = time.monotonic()
                if now - progress_at >= 1:
                    progress_at = now
                    try:
                        state = json.loads((self.run_dir / 'upload.json').read_text(encoding='utf-8'))
                        part = state['parts'][key]
                        if part['status'] in {'uploading', 'uploaded', 'submitting'} and part['status'] != last_progress:
                            last_progress = part['status']
                            safe = {k: part[k] for k in ('status', 'filename', 'part_index', 'sha256', 'size') if k in part}
                            self.events.put({'source': 'uploader', 'event': 'progress', 'request_id': rid, 'data': safe})
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                if self.closed.is_set() or now >= end or (self.deadline is not None and now >= self.deadline):
                    timed_out = True
                    self.process.kill()
                    self.process.wait(timeout=10)
                    break
                self.closed.wait(0.2)
            if timed_out:
                result = self._timeout_result(key)
            elif result_path.is_file():
                result = json.loads(result_path.read_text(encoding='utf-8'))
            else:
                result = self._timeout_result(key, 'upload_process_failed')
        except Exception:
            result = self._timeout_result(key, 'upload_process_failed')
        finally:
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            self.process = None
        status = result.get('status')
        event = 'end' if status == 'submitted' else 'unknown' if status == 'unknown' else 'error'
        self.events.put({'source': 'uploader', 'event': event, 'request_id': rid, 'data': result})

    def _timeout_result(self, key, code='upload_timeout'):
        try:
            from .upload_transaction import Journal
            journal = Journal(self.run_dir / 'upload.json', self.backup_dir / 'upload.json',
                              self.run_dir.name, self.config['expected_uid'])
            part = journal.data['parts'].get(key)
            if part:
                if part['status'] == 'submitted':
                    return journal.result(key)
                status = 'unknown' if part['status'] in ('submitting', 'unknown') else 'failed'
                part.update(status=status, error=code)
                journal.save()
                return journal.result(key)
        except Exception:
            # 持久化结果未知时不能声称可以安全重传。
            return {'status': 'unknown', 'error': 'upload_journal_unavailable'}
        return {'status': 'failed', 'error': code}


def execute_job(job):
    from .upload_transaction import ColabBiliUploader, Journal, UploadError
    config = load_upload_config(job['config_file'])
    if not config.get('enabled'):
        return {'status': 'failed', 'error': 'upload_disabled'}
    journal = Journal(job['local'], job['remote'], job['run_id'], config['expected_uid'])
    try:
        return ColabBiliUploader(job['cookie'], config).upload_part(
            Path(job['path']), job['metadata'], job['part_index'], journal, job['key'], job.get('candidate'))
    except Exception as error:
        code = str(error) if isinstance(error, UploadError) else 'upload_operation_failed'
        part = journal.data['parts'].get(job['key'])
        if part and part['status'] == 'submitted':
            return {'status': 'failed', 'error': code}
        if part:
            part.update(status='unknown' if part['status'] in ('submitting', 'unknown') else 'failed', error=code)
            try:
                journal.save()
                return journal.result(job['key'])
            except Exception:
                return {'status': 'unknown', 'error': 'upload_journal_persistence_failed'}
        return {'status': 'failed', 'error': code}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    job = json.loads(args.job.read_text(encoding='utf-8'))
    with ProcessLock(Path(job['local']).with_suffix('.lock')):
        try:
            result = execute_job(job)
        except Exception:
            result = {'status': 'unknown', 'error': 'upload_journal_unavailable'}
        atomic_json(job['result'], result)
    return 0 if result.get('status') == 'submitted' else 1


if __name__ == '__main__':
    raise SystemExit(main())
