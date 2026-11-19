"""子进程日志读取，避免长任务将整份日志加载到内存。"""


LOG_TAIL_BYTES = 64 * 1024


def iter_process_log(stream):
    """分块扫描所有日志，超长单行也不会导致无界分配。"""
    stream.seek(0)
    overlap = b''
    for line in iter(lambda: stream.readline(LOG_TAIL_BYTES), b''):
        # 超长行分块时保留少量重叠，避免成功标记或 URL 刚好被块边界截断。
        data = overlap + line
        yield data.decode('utf-8', errors='ignore').strip()
        overlap = data[-64:] if not line.endswith(b'\n') else b''


def read_log_tail(stream, limit=LOG_TAIL_BYTES):
    stream.seek(0, 2)
    end = stream.tell()
    stream.seek(max(0, end - limit))
    return stream.read(limit).decode('utf-8', errors='ignore')
