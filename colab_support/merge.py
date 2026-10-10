"""按清单合并完整成品；所有媒体和子进程工作均受同一收尾截止时间约束。"""
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class MergeError(RuntimeError):
    pass


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise MergeError('merge_timeout')
    return seconds


def media_command(command, deadline, *, capture=False, cwd=None):
    # 不回显命令、源路径或第三方原始异常；失败细节使用固定类别。
    with tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                    stderr=errors, timeout=remaining(deadline), check=False)
        except subprocess.TimeoutExpired:
            raise MergeError('merge_timeout') from None
        except OSError:
            raise MergeError('merge_tool_unavailable') from None
        if result.returncode:
            raise MergeError('merge_media_command_failed')
        # FFmpeg 非零退出才视为失败；stderr 不写入清单，避免泄露路径和源流信息。
        return result.stdout


def probe(path, ffprobe, deadline):
    raw = media_command([ffprobe, '-v', 'error', '-show_format', '-show_streams',
                         '-show_data_hash', 'sha256', '-of', 'json', str(path)],
                        deadline, capture=True)
    try:
        info = json.loads(raw)
        duration = float(info['format']['duration'])
        streams = info['streams']
        if not math.isfinite(duration) or duration <= 0 or not streams:
            raise ValueError()
        if not any(s.get('codec_type') == 'video' for s in streams):
            raise ValueError()
        if any(s.get('codec_type') not in ('video', 'audio') for s in streams):
            raise ValueError()
        fields = ('codec_type', 'codec_name', 'codec_tag_string', 'profile', 'level',
                  'width', 'height', 'pix_fmt', 'sample_aspect_ratio', 'time_base',
                  'sample_rate', 'channels', 'channel_layout', 'extradata_hash')
        signature = [{key: s.get(key) for key in fields} for s in streams]
        if any(not s.get('codec_name') or not s.get('time_base') for s in streams):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise MergeError('merge_invalid_media') from None
    return duration, signature


def select_inputs(run_dir, manifest):
    segments = manifest['segments']
    if not segments:
        raise MergeError('merge_no_segments')
    ordered = []
    groups = set()
    root = Path(run_dir).resolve()
    for key, segment in segments.items():
        match = re.fullmatch(r'([A-Za-z0-9_-]+)-([1-9][0-9]*)', key)
        if not match or segment.get('render') != 'success':
            raise MergeError('merge_incomplete_segments')
        groups.add(match[1])
        relative = segment.get('rendered_path')
        if not relative or Path(relative).is_absolute():
            raise MergeError('merge_missing_render_path')
        path = (root / relative).resolve()
        if not path.is_relative_to(root / 'rendered') or not path.is_file() or path.stat().st_size == 0:
            raise MergeError('merge_missing_rendered_file')
        ordered.append((int(match[2]), key, path))
    ordered.sort()
    if len(groups) != 1 or [item[0] for item in ordered] != list(range(1, len(ordered) + 1)):
        raise MergeError('merge_segment_gap')
    if len({item[2] for item in ordered}) != len(ordered):
        raise MergeError('merge_duplicate_file')
    return [(key, path) for _, key, path in ordered]


def merge_rendered(run_dir, manifest, deadline, *, min_free_gib=10,
                   ffmpeg='ffmpeg', ffprobe='ffprobe'):
    started = time.monotonic()
    inputs = select_inputs(run_dir, manifest)
    folder = Path(run_dir) / 'merged'
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / 'complete.mp4'
    partial = folder / 'complete.partial.mp4'
    if output.exists() or partial.exists():
        raise MergeError('merge_output_exists')
    total_size = sum(path.stat().st_size for _, path in inputs)
    # 留出输出文件、封装开销及正常运行的最低余量；不删除任何已有分段。
    required = math.ceil(total_size * 1.1) + int(min_free_gib * 1024**3)
    if shutil.disk_usage(folder).free < required:
        raise MergeError('merge_disk_low')
    durations, signature = [], None
    for _, path in inputs:
        duration, current = probe(path, ffprobe, deadline)
        if signature is not None and current != signature:
            raise MergeError('merge_incompatible_streams')
        signature = current
        durations.append(duration)
    # 使用相对路径和标准 ffconcat 转义，支持空格及单引号，不拷贝第二套输入。
    def quote(path):
        value = Path(os.path.relpath(path, folder)).as_posix()
        if '\n' in value or '\r' in value:
            raise MergeError('merge_invalid_path')
        return "'" + value.replace("'", "'\\''") + "'"
    playlist = folder / 'concat.txt'
    playlist.write_text('ffconcat version 1.0\n' + ''.join(
        'file ' + quote(path) + '\n' for _, path in inputs), encoding='utf-8')
    media_command([ffmpeg, '-nostdin', '-n', '-hide_banner', '-v', 'warning',
                   '-f', 'concat', '-safe', '0', '-i', playlist.name,
                   '-map', '0', '-c', 'copy', '-movflags', '+faststart', partial.name],
                  deadline, cwd=folder)
    actual, merged_signature = probe(partial, ffprobe, deadline)
    expected = sum(durations)
    tolerance = max(1.0, 0.1 * len(inputs))
    if abs(actual - expected) > tolerance:
        raise MergeError('merge_duration_mismatch')
    if merged_signature != signature:
        raise MergeError('merge_output_stream_mismatch')
    # 实际解码全片验证可读性；不等同于人工确认音画同步、弹幕同步。
    media_command([ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-xerror',
                   '-i', str(partial), '-map', '0:v', '-map', '0:a?', '-f', 'null', '-'], deadline)
    remaining(deadline)
    partial.replace(output)
    return {'status': 'success', 'mode': 'stream_copy',
            'path': output.relative_to(Path(run_dir)).as_posix(),
            'segments': [key for key, _ in inputs], 'segment_count': len(inputs),
            'expected_duration_seconds': expected, 'duration_seconds': actual,
            'duration_tolerance_seconds': tolerance, 'size': output.stat().st_size,
            'decode_verified': True, 'elapsed_seconds': time.monotonic() - started}


def finalize_merge(coordinator, events, deadline, *, min_free_gib=10,
                   ffmpeg='ffmpeg', ffprobe='ffprobe', merge=merge_rendered):
    """在 producer 屏障和所有分段任务完成之后执行，备份沿用同一重试队列。"""
    if not coordinator.media_drained():
        raise RuntimeError('tasks_not_drained')
    state = coordinator.manifest['merge']
    if state['status'] != 'pending':
        raise RuntimeError('merge_already_finalized')
    if coordinator.manifest['errors']:
        state.update(status='skipped', reason='upstream_failed', backup={'status': 'skipped'})
        coordinator.checkpoint()
    else:
        state['status'] = 'running'
        coordinator.manifest['phase'] = 'merging'
        coordinator.checkpoint()
        try:
            result = merge(coordinator.run_dir, coordinator.manifest, deadline,
                           min_free_gib=min_free_gib, ffmpeg=ffmpeg, ffprobe=ffprobe)
        except Exception as error:
            code = str(error) if isinstance(error, MergeError) else 'merge_failed'
            state.update(status='error', error=code, backup={'status': 'skipped'})
            coordinator.error(code)
            coordinator.checkpoint()
        else:
            state.update(result)
            state['backup'] = {'status': 'pending'}
            coordinator.manifest['phase'] = 'merged_backup'
            coordinator.pending_backup['merged-output'] = (None, 'merged')
            coordinator.backup.submit('merged-output', coordinator.run_dir / state['path'],
                                      coordinator.backup_dir / state['path'])
            coordinator.checkpoint()
    while coordinator.pending_backup:
        try:
            coordinator.handle(events.get(timeout=remaining(deadline)))
        except (queue.Empty, MergeError):
            if state['backup']['status'] == 'pending':
                state['backup'] = {'status': 'error', 'error': 'merge_backup_timeout'}
            coordinator.error('drain_timeout')
            raise MergeError('merge_timeout') from None
