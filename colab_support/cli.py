"""Colab 入口与环境检查。重型 DMR 依赖只在 worker 内导入。"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .core import atomic_json

def write_control_ack(path, run_id, status, error=None):
    if not path or not run_id:
        return
    value = {'schema_version': 1, 'run_id': run_id, 'status': status,
             'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    if error:
        value['error'] = error
    atomic_json(path, value)


ROOT = Path(__file__).resolve().parents[1]


def prepare_cookie(source, private_dir):
    try:
        payload = json.loads(Path(source).read_text(encoding='utf-8-sig'))
        cookies = payload['cookie_info']['cookies']
        if not isinstance(cookies, list) or not cookies:
            raise ValueError()
        normalized = []
        for cookie in cookies:
            if not isinstance(cookie.get('name'), str) or not cookie['name']:
                raise ValueError()
            if not isinstance(cookie.get('value'), str):
                raise ValueError()
            normalized.append({'name': cookie['name'], 'value': cookie['value']})
        if not any(c['name'] == 'SESSDATA' and c['value'] for c in normalized):
            raise ValueError()
    except Exception:
        raise ValueError('观看 Cookie 文件缺失或无效，需要包含 cookie_info.cookies 和 SESSDATA。') from None
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = private_dir / 'watch.json'
    destination.touch(mode=0o600, exist_ok=True)
    os.chmod(destination, 0o600)
    destination.write_text(json.dumps({'cookie_info': {'cookies': normalized}}), encoding='utf-8')
    return destination


def run_checked(command, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=timeout)
    if result.returncode:
        # 此函数仅运行无账号数据的环境探针，绝不用于打印直播 FFmpeg 命令。
        raise RuntimeError('环境检查失败：' + result.stderr[-4000:])
    return result.stdout


def preflight(args):
    if sys.platform != 'linux':
        raise RuntimeError('实际录制入口需要 Linux/Colab；离线状态机测试可在 Windows 运行。')
    import shutil
    for name in ('ffmpeg', 'ffprobe', 'fc-match', 'nvidia-smi'):
        if not shutil.which(name):
            raise RuntimeError('缺少工具：' + name)
    if not args.drive_root.is_dir():
        raise RuntimeError('Drive 目录不存在，请先运行 drive.mount 并确认 MyDrive。')
    prepare_cookie(args.cookie, args.run_dir / 'private')
    font = run_checked(['fc-match', '-f', '%{family}', 'Noto Sans CJK SC'])
    if 'Noto Sans CJK SC' not in font:
        raise RuntimeError('未找到 Noto Sans CJK SC，拒绝静默字体替换。')
    gpu = run_checked(['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'])
    folder = args.run_dir / 'preflight'
    folder.mkdir(parents=True, exist_ok=True)
    source, subtitle, output = folder / 'source.mp4', folder / 'sample.ass', folder / 'rendered.mp4'
    subtitle.write_text('''[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,32,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,中文弹幕预检 Colab
''', encoding='utf-8')
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                 'color=c=blue:s=640x360:r=25', '-t', '2', '-c:v', 'mpeg4', str(source)])
    # 与正式路径一样使用 CPU 字幕滤镜和 NVENC 编码，不能用纯转码代替预检。
    run_checked(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-vf',
                 "subtitles=filename='" + str(subtitle) + "'", '-c:v', 'h264_nvenc',
                 '-b:v', '15M', str(output)])
    duration = float(run_checked(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                  '-of', 'default=nw=1:nk=1', str(output)]).strip())
    if duration < 1.8 or output.stat().st_size == 0:
        raise RuntimeError('预检成品无效')
    atomic_json(folder / 'result.json', {'gpu': gpu.strip(), 'font': font, 'duration': duration})
    print('环境预检通过；请在 Notebook 播放预检成品确认中文字形。', flush=True)


def parser():
    p = argparse.ArgumentParser(description='单场 Colab 录制、弹幕渲染和 Drive 备份，不投稿。')
    p.add_argument('--url', required=True, help='本次录制的标准 B 站直播间 URL，无默认直播间。')
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--drive-root', type=Path, default=Path('/content/drive/MyDrive'))
    p.add_argument('--cookie', type=Path)
    p.add_argument('--segment', type=int, default=300)
    p.add_argument('--initial-wait', type=float, default=900)
    p.add_argument('--offline-grace', type=float, default=180)
    p.add_argument('--max-record', type=float, default=43200)
    p.add_argument('--drain-timeout', type=float, default=7200)
    p.add_argument('--min-free-gib', type=float, default=10)
    p.add_argument('--preflight-only', action='store_true')
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--run-id', help=argparse.SUPPRESS)
    p.add_argument('--ack-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--auto-run', action='store_true', help='使用专用 CDP 浏览器自动点击一次 Run all')
    p.add_argument('--cdp-url', help='专用浏览器 loopback CDP 地址')
    return p


def load_manifest(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def mark_failed(run_dir, code):
    path = Path(run_dir) / 'manifest.json'
    manifest = load_manifest(path)
    manifest['status'] = 'failed'
    manifest.setdefault('errors', []).append(code)
    atomic_json(path, manifest)


def supervise(args):
    if sys.platform != 'linux':
        raise RuntimeError('实际运行需要 Linux/Colab。')
    if (args.run_dir / 'manifest.json').exists():
        raise ValueError('此运行目录已有结果，请使用新的运行编号。')
    lock = args.run_dir / 'supervisor.lock'
    with lock.open('x', encoding='utf-8') as f:
        f.write(str(os.getpid()))
    process = None
    interrupt_count = 0
    started = time.monotonic()
    draining_since = None
    stop_path = args.run_dir / 'STOP'
    try:
        command = [sys.executable, str(ROOT / 'colab_run.py'), *sys.argv[1:], '--worker']
        with (args.run_dir / 'console.log').open('a', encoding='utf-8') as console:
            process = subprocess.Popen(command, cwd=args.run_dir, stdout=console,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        while process.poll() is None:
            try:
                manifest = load_manifest(args.run_dir / 'manifest.json')
                if stop_path.exists() or manifest.get('draining_since'):
                    if draining_since is None:
                        draining_since = time.monotonic()
                hard_limit = args.initial_wait + args.max_record + args.drain_timeout + 300
                if manifest.get('status') == 'draining':
                    write_control_ack(args.ack_file, args.run_id, 'draining')
                timed_out = time.monotonic() - started > hard_limit
                timed_out |= draining_since is not None and time.monotonic() - draining_since > args.drain_timeout
                force_requested = interrupt_count >= 2 or (args.run_dir / 'FORCE_STOP').exists()
                if timed_out or force_requested:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=15)
                    mark_failed(args.run_dir, 'forced_interrupt' if force_requested else 'drain_timeout')
                    # 超时后不再同步可能已挂起的 Drive；本地状态保留为失败。
                    return 2
                time.sleep(0.5)
            except KeyboardInterrupt:
                # STOP 可由 Notebook 或外部控制创建，不代表监督器已收到过中断。
                interrupt_count += 1
                stop_path.touch()
                print('监督器收到停止信号：正常收尾' if interrupt_count == 1 else '监督器收到再次停止信号：强制结束', flush=True)
        manifest = load_manifest(args.run_dir / 'manifest.json')
        if process.returncode != 0 or manifest.get('status') != 'success':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if manifest.get('status') != 'failed':
                mark_failed(args.run_dir, 'worker_failed')
            return 1
        return 0
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=15)
            mark_failed(args.run_dir, 'supervisor_aborted')
        lock.unlink(missing_ok=True)


def main():
    args = parser().parse_args()
    args.run_dir = args.run_dir.resolve()
    args.drive_root = args.drive_root.resolve()
    args.cookie = (args.cookie or args.drive_root / 'DMRColab/credentials/bilibili.json').resolve()
    parsed = urlparse(args.url)
    if parsed.scheme != 'https' or parsed.netloc != 'live.bilibili.com' or not parsed.path.strip('/').isdigit():
        raise ValueError('首版仅支持标准 B 站直播间 URL。')
    if any(value <= 0 for value in (args.segment, args.initial_wait, args.offline_grace,
                                    args.max_record, args.drain_timeout, args.min_free_gib)):
        raise ValueError('时间、分段和空间限制必须为正数。')
    if args.drive_root == args.run_dir or args.run_dir.is_relative_to(args.drive_root):
        raise ValueError('工作目录必须位于云端临时磁盘，不能放在 Drive 中。')
    args.run_dir.mkdir(parents=True, exist_ok=True)
    if args.preflight_only:
        try:
            preflight(args)
        except Exception as error:
            write_control_ack(args.ack_file, args.run_id, 'failed', type(error).__name__)
            print(str(error))
            return 1
        return 0
    write_control_ack(args.ack_file, args.run_id, 'running')
    if args.worker:
        from .runtime import run
        result = run(args)
        write_control_ack(args.ack_file, args.run_id, 'success' if result == 0 else 'failed', None if result == 0 else 'worker_failed')
        return result
    result = supervise(args)
    manifest = load_manifest(args.run_dir / 'manifest.json')
    write_control_ack(args.ack_file, args.run_id, 'success' if result == 0 and manifest.get('status') == 'success' else 'failed', None if result == 0 else 'run_failed')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return result
