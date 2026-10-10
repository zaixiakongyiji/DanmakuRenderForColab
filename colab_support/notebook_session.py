"""Notebook 参数、预检和运行的单次会话令牌。"""
import math
from dataclasses import dataclass
from pathlib import Path

from .trigger import claim_request, control_paths, make_run_id, read_active_request, validate_room_url


@dataclass
class NotebookSession:
    project: Path
    drive_root: Path
    run_dir: Path
    backup_dir: Path
    run_id: str
    command: tuple
    phase: str = 'parameters'

    def preflight(self, checked):
        if self.phase != 'parameters':
            raise RuntimeError('notebook_parameter_order_invalid')
        self.phase = 'invalid'
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
        Path(drive_root) / 'DMRColab/runs' / run_id, run_id, tuple(command))
