"""Notebook 前台控制：正常停止与强制停止使用不同标记文件。"""
import subprocess
import time
from pathlib import Path

from .cli import load_manifest


def run_recording(command, project, run_dir):
    run_dir = Path(run_dir)
    if any((run_dir / name).exists() for name in
           ('manifest.json', 'supervisor.lock', 'STOP', 'FORCE_STOP')):
        raise ValueError('此运行目录已有任务或结果。请重新执行参数单元格生成新的 RUN_ID，再开始录制。')
    # 隔离 Notebook 的进程组，避免一次界面中断同时送到监督器。
    process = subprocess.Popen(command, cwd=project, start_new_session=True)
    stop_requested = False
    force_deadline = None
    last_status = None
    try:
        while process.poll() is None:
            try:
                if force_deadline is not None and time.monotonic() >= force_deadline:
                    raise RuntimeError('已请求强制结束，但监督器尚未退出；请保留运行时检查进程和本地清单。')
                state = load_manifest(run_dir / 'manifest.json')
                if state:
                    brief = (state.get('status'), state.get('phase'), len(state.get('segments', {})),
                             tuple(state.get('errors', [])), tuple(state.get('warnings', [])))
                    if brief != last_status:
                        print('状态 / 阶段 / 分段数 / 错误 / 提醒:', brief, flush=True)
                        last_status = brief
                time.sleep(1)
            except KeyboardInterrupt:
                run_dir.mkdir(parents=True, exist_ok=True)
                if not stop_requested:
                    stop_requested = True
                    (run_dir / 'STOP').touch()
                    print('已请求正常收尾；启动探测可能尚未结束，请等待尾段、渲染和备份。再次停止才会请求强制结束。', flush=True)
                else:
                    (run_dir / 'FORCE_STOP').touch()
                    if force_deadline is None:
                        force_deadline = time.monotonic() + 45
                    print('已明确请求强制结束；本次不会标记为成功。', flush=True)
    finally:
        if process.poll() is None:
            # 界面出现其他异常时仍先请求收尾，不按等待数秒自动升级为强杀。
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / 'STOP').touch()
    return process.returncode
