"""诊断只输出固定类别和数值，不保存原始日志、URL、请求头或账号信息。"""
import math

STAGES = frozenset((
    'unknown', 'awaiting_live', 'room_info', 'stream_select', 'probe', 'probe_ready',
    'probe_fallback', 'danmaku_ready', 'recorder_start', 'recorder_exit',
    'ffmpeg_issue', 'retry', 'stopping', 'producer_done',
))
ERROR_TYPES = frozenset((
    'RuntimeError', 'ValueError', 'OSError', 'TimeoutError', 'TimeoutExpired',
    'ConnectionError', 'FileNotFoundError', 'PermissionError', 'CalledProcessError',
))
CATEGORIES = {
    'http_unauthorized': ('401 unauthorized', 'http error 401'),
    'http_forbidden': ('403 forbidden', 'http error 403'),
    'http_not_found': ('404 not found', 'http error 404'),
    'network_timeout': ('timed out', 'connection timeout'),
    'network_unreachable': ('connection refused', 'network is unreachable', 'failed to resolve'),
    'tls_error': ('tls error', 'certificate verify failed'),
    'invalid_data': ('invalid data found', 'invalid nal unit', 'error parsing'),
    'decoder_missing': ('decoder not found', 'unknown decoder', 'unsupported codec'),
    'muxer_error': ('could not write header', 'error writing trailer', 'not supported in container'),
    'io_error': ('input/output error', 'no space left on device', 'permission denied'),
}


def ffmpeg_category(line):
    lowered = line.lower()
    return next((code for code, words in CATEGORIES.items()
                 if any(word in lowered for word in words)), None)


def safe_diagnostic(data):
    stage = data.get('stage')
    result = {'stage': stage if isinstance(stage, str) and stage in STAGES else 'unknown'}
    code = data.get('code')
    if isinstance(code, str) and code in CATEGORIES:
        result['code'] = code
    error_type = data.get('error_type')
    if error_type is not None:
        result['error_type'] = error_type if isinstance(error_type, str) and error_type in ERROR_TYPES else 'Error'
    for key in ('returncode', 'width', 'height', 'attempt'):
        value = data.get(key)
        if type(value) is int:
            result[key] = value
    elapsed = data.get('elapsed_seconds')
    if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
        result['elapsed_seconds'] = round(elapsed, 3)
    return result
