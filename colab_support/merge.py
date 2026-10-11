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
from fractions import Fraction
from pathlib import Path


class MergeError(RuntimeError):
    def __init__(self, code, *, differences=None):
        super().__init__(code)
        self.differences = differences or []


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
                  'sample_rate', 'channels', 'channel_layout', 'extradata_hash', 'r_frame_rate')
        signature = [{key: s.get(key) for key in fields} for s in streams]
        if any(not s.get('codec_name') or not s.get('time_base') for s in streams):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise MergeError('merge_invalid_media') from None
    return duration, signature


def video_frame_rate(signature):
    try:
        video = next(s for s in signature if s.get('codec_type') == 'video')
        rate = Fraction(video['r_frame_rate'])
        if not 1 <= rate <= 120:
            raise ValueError()
        return str(rate)
    except (StopIteration, KeyError, TypeError, ValueError, ZeroDivisionError):
        raise MergeError('merge_invalid_frame_rate') from None


def video_encoding_args(rate, encoder):
    options = ['-b:v', '15M'] if encoder == 'h264_nvenc' else ['-crf', '18', '-preset', 'fast']
    return options + ['-r', rate, '-fps_mode', 'cfr', '-pix_fmt', 'yuv420p',
                      '-profile:v', 'main', '-video_track_timescale', '90000']


def stream_differences(inputs, signatures):
    reference = signatures[0]
    differences = []
    for (key, _), current in zip(inputs[1:], signatures[1:]):
        if len(current) != len(reference):
            differences.append({'segment': key, 'fields': ['stream_count']})
            continue
        for index, (expected, actual) in enumerate(zip(reference, current)):
            if not isinstance(expected, dict) or not isinstance(actual, dict):
                if expected != actual:
                    differences.append({'segment': key, 'stream_index': index,
                                        'fields': ['signature']})
                continue
            fields = sorted(field for field in expected.keys() | actual.keys()
                            if expected.get(field) != actual.get(field))
            if fields:
                differences.append({'segment': key, 'stream_index': index, 'fields': fields})
    return differences


def normalize_inputs(inputs, signatures, durations, folder, deadline, ffmpeg, ffprobe, encoder,
                     min_free_gib):
    reference = signatures[0]
    if not all(isinstance(stream, dict) for signature in signatures for stream in signature):
        raise MergeError('merge_incompatible_streams', differences=stream_differences(inputs, signatures))
    # 只处理同规格 H.264/AAC 的时间及编码头差异，不猜测分辨率或声道变化的处理方式。
    stable_fields = ('codec_type', 'codec_name', 'width', 'height', 'pix_fmt',
                     'sample_aspect_ratio', 'sample_rate', 'channels', 'channel_layout')
    valid = (sum(s.get('codec_type') == 'video' for s in reference) == 1 and
             sum(s.get('codec_type') == 'audio' for s in reference) <= 1 and
             all(s.get('codec_name') == {'video': 'h264', 'audio': 'aac'}.get(s.get('codec_type'))
                 for s in reference))
    if not valid or any(len(current) != len(reference) or any(
            any(a.get(field) != b.get(field) for field in stable_fields)
            for a, b in zip(reference, current)) for current in signatures[1:]):
        raise MergeError('merge_incompatible_streams', differences=stream_differences(inputs, signatures))
    rate = video_frame_rate(reference)
    # 标准化临时文件和最终归档同时存在，按输出码率预留两份空间。
    estimated_size = max(sum(path.stat().st_size for _, path in inputs),
                         math.ceil(sum(durations) * 16_000_000 / 8))
    if shutil.disk_usage(folder).free < math.ceil(estimated_size * 2.2) + int(min_free_gib * 1024**3):
        raise MergeError('merge_disk_low')
    normalized, signature = [], None
    for index, ((key, path), duration) in enumerate(zip(inputs, durations)):
        target = folder / f'{index}.mp4'
        command = [ffmpeg, '-nostdin', '-n', '-hide_banner', '-v', 'error', '-i', str(path),
                   '-map', '0:v:0', '-map', '0:a:0?', '-c:v', encoder,
                   *video_encoding_args(rate, encoder), '-c:a', 'aac', '-b:a', '192k',
                   '-movflags', '+faststart', str(target)]
        media_command(command, deadline)
        actual, current = probe(target, ffprobe, deadline)
        if abs(actual - duration) > max(0.25, 2 / float(Fraction(rate))):
            raise MergeError('merge_normalization_duration_mismatch')
        if signature is not None and current != signature:
            raise MergeError('merge_normalization_stream_mismatch')
        signature = current
        normalized.append((key, target))
    return normalized, signature


def probe_hwaccel_cuda(ffmpeg, target, deadline):
    """探测 FFmpeg 与当前环境是否支持 CUDA 硬件解码加速。"""
    try:
        media_command([ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
                       '-hwaccel', 'cuda', '-i', str(target),
                       '-frames:v', '1', '-f', 'null', '-'], deadline)
        return True
    except MergeError as error:
        if str(error) == 'merge_timeout':
            raise
        return False


def compute_check_windows(actual_duration, segment_durations, window_seconds=30.0):
    """计算需要重点校验的时间窗口 [start, length]，合并重叠区间。"""
    if actual_duration <= window_seconds * 2:
        return [(0.0, actual_duration)]
    half = window_seconds / 2.0
    points = [0.0]
    cumulative = 0.0
    for dur in segment_durations[:-1]:
        cumulative += dur
        points.append(cumulative)
    points.append(actual_duration)

    raw_windows = [(0.0, min(window_seconds, actual_duration))]
    for boundary in points[1:-1]:
        start = max(0.0, boundary - half)
        end = min(actual_duration, boundary + half)
        raw_windows.append((start, end - start))
    tail_start = max(0.0, actual_duration - window_seconds)
    raw_windows.append((tail_start, actual_duration - tail_start))

    intervals = [[s, s + length] for s, length in raw_windows]
    intervals.sort()

    merged = []
    for cur in intervals:
        if not merged:
            merged.append(cur)
        else:
            prev = merged[-1]
            if cur[0] <= prev[1] + 1.0:
                prev[1] = max(prev[1], cur[1])
            else:
                merged.append(cur)
    return [(round(s, 3), round(e - s, 3)) for s, e in merged]


def verify_decode(ffmpeg, partial, actual_duration, segment_durations, deadline):
    """支持 CUDA 硬件加速探测与长视频接缝抽样校验的健全性解码。"""
    use_cuda = probe_hwaccel_cuda(ffmpeg, partial, deadline)
    if use_cuda:
        # CUDA 硬解可用：直接高速全片硬解校验
        media_command([ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-xerror',
                       '-hwaccel', 'cuda', '-i', str(partial),
                       '-map', '0:v', '-map', '0:a?', '-f', 'null', '-'], deadline)
        return 'cuda_full'

    # CPU 软解降级
    if actual_duration <= 120.0:
        # 短视频直接全片软解，多线程拉满
        media_command([ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-xerror',
                       '-i', str(partial), '-map', '0:v', '-map', '0:a?',
                       '-threads', '0', '-f', 'null', '-'], deadline)
        return 'cpu_full'

    # 长视频：智能头尾与接缝处抽样校验，避免单线程硬解耗时数小时
    windows = compute_check_windows(actual_duration, segment_durations, window_seconds=30.0)
    for start, length in windows:
        cmd = [ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-xerror']
        if start > 0:
            cmd.extend(['-ss', str(start)])
        cmd.extend(['-i', str(partial), '-t', str(length),
                    '-map', '0:v', '-map', '0:a?', '-threads', '0', '-f', 'null', '-'])
        media_command(cmd, deadline)
    return 'cpu_windows'


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
    durations, signatures = [], []
    for _, path in inputs:
        duration, current = probe(path, ffprobe, deadline)
        signatures.append(current)
        durations.append(duration)
    differences = stream_differences(inputs, signatures)
    encoder = 'h264_nvenc' if manifest.get('settings', {}).get('encoder') == 'h264_nvenc' else 'libx264'
    with tempfile.TemporaryDirectory(prefix='normalized-', dir=folder) as temporary:
        signature = signatures[0]
        if differences:
            inputs, signature = normalize_inputs(inputs, signatures, durations, Path(temporary),
                deadline, ffmpeg, ffprobe, encoder, min_free_gib)
        result = concatenate(inputs, signature, durations, folder, partial, output, deadline,
                             ffmpeg, ffprobe)
    result.update(mode='normalized_stream_copy' if differences else 'stream_copy',
                  stream_differences=differences, elapsed_seconds=time.monotonic() - started)
    if differences:
        result['normalization_encoder'] = encoder
    return result


def concatenate(inputs, signature, durations, folder, partial, output, deadline, ffmpeg, ffprobe):
    # 使用相对路径和标准 ffconcat 转义，支持空格及单引号，不拷贝第二套输入。
    def quote(path):
        value = Path(os.path.relpath(path, folder)).as_posix()
        if '\n' in value or '\r' in value:
            raise MergeError('merge_invalid_path')
        return "'" + value.replace("'", "'\\''") + "'"
    playlist = folder / 'concat.txt'
    playlist.write_text('ffconcat version 1.0\n' + ''.join(
        'file ' + quote(path) + '\n' for _, path in inputs), encoding='utf-8')
    command = [ffmpeg, '-nostdin', '-n', '-hide_banner', '-v', 'warning',
                   '-f', 'concat', '-safe', '0', '-i', playlist.name,
                   '-map', '0', '-c', 'copy', '-movflags', '+faststart']
    video = next(s for s in signature if s['codec_type'] == 'video')
    timescale = Fraction(video['time_base'])
    if timescale.numerator == 1:
        command.extend(['-video_track_timescale', str(timescale.denominator)])
    media_command(command + [partial.name], deadline, cwd=folder)
    actual, merged_signature = probe(partial, ffprobe, deadline)
    expected = sum(durations)
    tolerance = max(1.0, 0.1 * len(inputs))
    if abs(actual - expected) > tolerance:
        raise MergeError('merge_duration_mismatch')
    if merged_signature != signature:
        raise MergeError('merge_output_stream_mismatch')
    # 验证可读性（支持 CUDA 硬件加速或接缝窗口抽样校验）；不等同于人工确认音画同步、弹幕同步。
    decode_mode = verify_decode(ffmpeg, partial, actual, durations, deadline)
    remaining(deadline)
    partial.replace(output)
    return {'status': 'success', 'mode': 'stream_copy',
            'path': output.relative_to(folder.parent).as_posix(),
            'segments': [key for key, _ in inputs], 'segment_count': len(inputs),
            'expected_duration_seconds': expected, 'duration_seconds': actual,
            'duration_tolerance_seconds': tolerance, 'size': output.stat().st_size,
            'decode_verified': True, 'decode_mode': decode_mode}


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
            if isinstance(error, MergeError) and error.differences:
                state['stream_differences'] = error.differences
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
