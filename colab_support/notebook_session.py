"""Notebook 参数、预检和运行的单次会话令牌。"""
import math
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .trigger import (claim_request, control_paths, make_run_id, read_active_request,
                      validate_ack, validate_room_url, write_ack)


class PreparationHeartbeat:
    def __init__(self, session, phase, interval=30):
        self.session, self.phase, self.interval = session, phase, interval
        self.stop_event = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name='colab-preparation-heartbeat', daemon=True)

    def update(self):
        path = control_paths(self.session.drive_root, self.session.run_id)[2]
        ack = json.loads(path.read_text(encoding='utf-8'))
        validate_ack(ack, self.session.run_id)
        if ack['status'] != 'accepted' or self.stop_event.is_set():
            return False
        write_ack(path, self.session.run_id, 'accepted', phase=self.phase)
        return True

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                if not self.update():
                    return
            except Exception:
                self.error = 'preparation_heartbeat_failed'
                return

    def start(self):
        if not self.update():
            raise RuntimeError('notebook_preparation_ack_invalid')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('preparation_heartbeat_stop_failed')
        if self.error:
            raise RuntimeError(self.error)


@dataclass
class NotebookSession:
    project: Path
    drive_root: Path
    run_dir: Path
    backup_dir: Path
    run_id: str
    command: tuple
    phase: str = 'parameters'
    automatic: bool = False
    _heartbeat: object = field(default=None, init=False, repr=False, compare=False)

    @property
    def preparation_active(self):
        return self._heartbeat is not None and self._heartbeat.thread.is_alive()

    @contextmanager
    def preparation(self, phase):
        heartbeat = PreparationHeartbeat(self, phase) if self.automatic else None
        self._heartbeat = heartbeat
        try:
            if heartbeat:
                heartbeat.start()
            try:
                yield
            finally:
                if heartbeat and heartbeat.thread.ident is not None:
                    heartbeat.stop()
        except BaseException:
            self.phase = 'invalid'
            if self.automatic and not self.preparation_active:
                try:
                    write_ack(control_paths(self.drive_root, self.run_id)[2], self.run_id,
                              'failed', 'notebook_' + phase + '_failed', phase=phase)
                except OSError:
                    print('准备阶段失败回执无法保存，请检查 Drive；保留运行时。', flush=True)
            raise

    def install_dependencies(self, checked):
        if self.phase != 'parameters':
            raise RuntimeError('notebook_parameter_order_invalid')
        self.phase = 'invalid'
        with self.preparation('dependencies'):
            checked(['apt-get', 'update', '-qq'])
            checked(['apt-get', 'install', '-y', 'ffmpeg', 'fontconfig', 'fonts-noto-cjk'])
            checked([self.command[0], '-m', 'pip', 'install', '-r',
                     str(self.project / 'colab_support/requirements.txt')])
            checked(['fc-cache', '-f'])
        self.phase = 'parameters'

    def preflight(self, checked):
        if self.phase != 'parameters':
            raise RuntimeError('notebook_parameter_order_invalid')
        self.phase = 'invalid'
        with self.preparation('preflight'):
            checked(list(self.command) + ['--preflight-only'], cwd=self.project)
        self.phase = 'preflight'

    def run(self, runner):
        if self.phase != 'preflight':
            raise RuntimeError('notebook_preflight_required')
        self.phase = 'running'
        result = runner(list(self.command), self.project, self.run_dir)
        self.phase = 'finished'
        return result


def prepare_session(project, drive_root, cookie, room_url, executable, *, segment=3600,
                    initial_wait=900, offline_grace=180, max_record=43200,
                    drain_timeout=7200, min_free_gib=10, work_root=Path('/content/dmr-runs')):
    from .locks import ProcessLock
    # 在同一运行时仍有录制进程时，参数重跑也不能创建第二场。
    with ProcessLock(Path(work_root) / 'colab-runtime.lock'):
        pass
    trigger = None if room_url.strip() else read_active_request(drive_root)
    if not room_url.strip() and trigger is None:
        raise ValueError('trigger_request_missing')
    if trigger:
        room_url, run_id = trigger['room_url'], trigger['run_id']
    else:
        room_url, run_id = validate_room_url(room_url), make_run_id()
    for value in (segment, initial_wait, offline_grace, max_record, drain_timeout, min_free_gib):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('notebook_limits_invalid')
    room_url = validate_room_url(room_url)
    run_dir = Path(work_root) / run_id
    if any((run_dir / name).exists() for name in ('manifest.json', 'supervisor.lock', 'STOP', 'FORCE_STOP')):
        raise ValueError('notebook_run_exists')
    if trigger:
        ack = claim_request(drive_root, trigger)
    else:
        ack = control_paths(drive_root, run_id)[2]
    command = [executable, str(Path(project) / 'colab_run.py'), '--url', room_url,
               '--run-dir', str(run_dir), '--drive-root', str(drive_root), '--cookie', str(cookie),
               '--run-id', run_id, '--ack-file', str(ack), '--segment', str(segment),
               '--initial-wait', str(initial_wait), '--offline-grace', str(offline_grace),
               '--max-record', str(max_record), '--drain-timeout', str(drain_timeout),
               '--min-free-gib', str(min_free_gib)]
    return NotebookSession(Path(project), Path(drive_root), run_dir,
        Path(drive_root) / 'DMRColab/runs' / run_id, run_id, tuple(command), automatic=trigger is not None)
