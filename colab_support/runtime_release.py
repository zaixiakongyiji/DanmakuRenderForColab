"""Notebook 最终清单、回执和运行时释放；不把请求释放当作已释放。"""
import json
from pathlib import Path
from datetime import datetime, timezone

from .core import atomic_json, digest, verified_copy
from .locks import ProcessLock
from .trigger import TRIGGER_SCHEMA_VERSION, control_paths


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _load(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('manifest_invalid')
    return value


def summarize(session):
    result = _load(session.run_dir / 'manifest.json')
    print('运行编号:', session.run_id, '状态:', result.get('status'))
    print('计数:', result.get('counts', {}))
    print('录制备份:', result.get('recording_backup'), '合并:', result.get('merge', {}).get('status'),
          '投稿:', result.get('upload', {}).get('status'))
    print('失败项:', result.get('errors', []), '投稿失败项:', result.get('upload_errors', []))
    print('Drive 备份目录:', session.backup_dir, flush=True)
    return result


def _copy(source, target):
    for attempt in range(3):
        try:
            return verified_copy(source, target)
        except (OSError, ValueError):
            if attempt == 2:
                raise


def _colab_release():
    from google.colab import runtime
    runtime.unassign()


def _persist(session, result, release):
    result['runtime_release'] = release
    local = session.run_dir / 'manifest.json'
    atomic_json(local, result)
    _copy(local, session.backup_dir / 'manifest.json')
    cloud = _load(session.backup_dir / 'manifest.json')
    if cloud != result:
        raise IOError('final_manifest_mismatch')
    status = result.get('status')
    if status not in {'success', 'failed'}:
        raise ValueError('final_status_invalid')
    ack = {'schema_version': TRIGGER_SCHEMA_VERSION, 'run_id': session.run_id,
           'status': status, 'updated_at': _timestamp(), 'runtime_release': release}
    if status == 'failed':
        ack['error'] = 'run_failed'
    if release['status'] == 'unknown':
        ack['error'] = release['reason']
    path = control_paths(session.drive_root, session.run_id)[2]
    atomic_json(path, ack)
    if _load(path) != ack:
        raise IOError('final_ack_mismatch')


def finish_session(session, returncode, *, failure=None, release_runtime=None):
    """先持久化结果再释放；保存失败或仍有进程时保留运行时和本地文件。"""
    if session.phase in {'release_requested', 'release_unknown'}:
        raise RuntimeError('runtime_release_already_attempted')
    local = session.run_dir / 'manifest.json'
    result = _load(local) if local.exists() else {
        'version': 2, 'run_id': session.run_id, 'status': 'failed', 'segments': {}, 'errors': []}
    if failure or returncode != 0 or result.get('status') != 'success':
        result['status'] = 'failed'
        # 错误由调用方使用固定类别，不保存异常正文和命令行。
        result.setdefault('errors', [])
        code = failure or 'notebook_run_failed'
        if code not in result['errors']:
            result['errors'].append(code)
    atomic_json(local, result)
    preflight = session.run_dir / 'preflight/result.json'
    if preflight.is_file():
        probe = _load(preflight)
        result['environment'] = {key: probe[key] for key in ('gpu', 'font', 'duration') if key in probe}
        atomic_json(local, result)
    summarize(session)
    if not session.automatic:
        return result
    session.phase = 'release_unknown'
    release = {'status': 'unknown', 'event': 'runtime_release_requested',
               'updated_at': _timestamp(), 'reason': 'finalization_incomplete'}
    try:
        if (session.run_dir / 'supervisor.lock').exists():
            raise RuntimeError('recording_still_active')
        with ProcessLock(session.run_dir.parent / 'colab-runtime.lock'):
            # 失败场次补存已关闭的媒体，避免释放后丢失唯一副本；不复制凭据和原始 console。
            recovery = []
            backed_up = {}
            for key, segment in result.get('segments', {}).items():
                for kind in ('source', 'danmaku', 'rendered'):
                    name = segment.get('rendered_path') if kind == 'rendered' else segment.get(kind)
                    if name:
                        source = session.run_dir / name if kind == 'rendered' else session.run_dir / 'source' / name
                        target = session.backup_dir / kind / (key + source.suffix)
                        backed_up[source] = (target, segment.get('backup', {}).get(kind, {}))
            merge = result.get('merge', {})
            if merge.get('path'):
                backed_up[session.run_dir / merge['path']] = (session.backup_dir / merge['path'], merge.get('backup', {}))
            for folder in ('source', 'danmaku', 'rendered', 'merged'):
                base = session.run_dir / folder
                if not base.exists():
                    continue
                for source in sorted(base.rglob('*')):
                    if not source.is_file() or source.is_symlink():
                        continue
                    relative = source.relative_to(session.run_dir)
                    target = session.backup_dir / 'recovery' / relative
                    if result['status'] != 'success':
                        existing = backed_up.get(source)
                        if existing:
                            saved, expected = existing
                            if (expected.get('status') == 'success' and expected.get('sha256')
                                    and saved.is_file() and digest(saved) == digest(source)
                                    and digest(source) == (expected.get('size'), expected.get('sha256'))):
                                continue
                        check = _copy(source, target)
                        recovery.append({'path': str(relative), 'backup_path': str(target.relative_to(session.backup_dir)), **check})
            if recovery:
                result['recovery_files'] = recovery
            if result['status'] == 'success':
                # 分段已由备份线程做 SHA-256 读回；释放前核对存在与大小，避免再下载整场视频。
                for key, segment in result.get('segments', {}).items():
                    for kind in ('source', 'danmaku', 'rendered'):
                        expected = segment.get('backup', {}).get(kind, {})
                        name = segment.get('rendered_path') if kind == 'rendered' else segment.get(kind)
                        if not name:
                            raise IOError('media_backup_unverified')
                        target = session.backup_dir / kind / (key + Path(name).suffix)
                        if (expected.get('status') != 'success' or not expected.get('sha256')
                                or not target.is_file() or target.stat().st_size != expected.get('size')):
                            raise IOError('media_backup_unverified')
                merge = result.get('merge', {})
                if merge.get('status') == 'success':
                    expected = merge.get('backup', {})
                    target = session.backup_dir / merge['path']
                    if (expected.get('status') != 'success' or not expected.get('sha256')
                            or not target.is_file() or target.stat().st_size != expected.get('size')):
                        raise IOError('merged_backup_unverified')
            release = {'status': 'requested', 'event': 'runtime_release_requested', 'updated_at': _timestamp()}
            _persist(session, result, release)
        session.phase = 'release_requested'
        print('结果和回执已保存；正在请求释放 Colab 运行时。requested 不代表已确认释放。', flush=True)
        (release_runtime or _colab_release)()
        return result
    except BaseException:
        release = {'status': 'unknown', 'event': 'runtime_release_requested',
                   'updated_at': _timestamp(), 'reason': 'runtime_release_unknown'}
        result['runtime_release'] = release
        session.phase = 'release_unknown'
        # 若 Drive 已不可用，至少保留临时磁盘中的失败证据；不再次调用释放。
        atomic_json(local, result)
        try:
            _persist(session, result, release)
        except Exception:
            pass
        print('运行时释放未确认；保留运行时供检查。若 Drive 不可写，本地监控将通过超时报告 needs_attention。', flush=True)
        return result
