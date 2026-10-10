"""双击入口使用的本地设置向导；私有设置不进入 Git。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from colab_support.locks import ProcessLock
from colab_support.trigger import atomic_json, validate_cdp, validate_room_url

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / '.temp/colab-monitor-settings.json'


def prompt(label, default=''):
    value = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip().strip('"')
    return value or default


def task_room(path):
    import yaml
    try:
        value = yaml.safe_load(Path(path).read_text(encoding='utf-8-sig'))
        return validate_room_url(value['download_args']['url'])
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        raise ValueError('任务配置无法读取有效直播间：请检查 download_args.url。') from None


def validate_settings(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('本地启动设置无效，请用 --setup 重新配置；不会删除监控状态。')
    for key in ('task_config', 'drive_sync_root', 'notebook_url', 'browser', 'cdp_url'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError('本地启动设置缺少字段：' + key)
    task_room(value['task_config'])
    drive = Path(value['drive_sync_root'])
    if not drive.is_absolute() or not drive.is_dir():
        raise ValueError('请填写同一账号 My Drive 的实际同步目录。')
    try:
        (drive / 'DMRColab').mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ValueError('无法在 Drive 同步目录创建 DMRColab，请检查路径和权限。') from None
    notebook = urlparse(value['notebook_url'])
    if (notebook.scheme != 'https' or notebook.netloc != 'colab.research.google.com'
            or not notebook.path.startswith('/drive/') or not notebook.path[7:].strip('/')):
        raise ValueError('请填写已保存私有副本的 Colab 地址：https://colab.research.google.com/drive/...')
    validate_cdp(value['cdp_url'])
    if not Path(value['browser']).is_file():
        raise ValueError('Chrome/Edge 程序路径不存在，请用 --setup 重新配置。')
    return value


def find_browser():
    for folder, suffix in (
        ('PROGRAMFILES', 'Google/Chrome/Application/chrome.exe'),
        ('LOCALAPPDATA', 'Google/Chrome/Application/chrome.exe'),
        ('PROGRAMFILES(X86)', 'Microsoft/Edge/Application/msedge.exe'),
        ('PROGRAMFILES', 'Microsoft/Edge/Application/msedge.exe'),
    ):
        if os.environ.get(folder):
            path = Path(os.environ[folder]) / suffix
            if path.is_file():
                return str(path)
    return ''


def setup(old=None):
    old = old or {}
    print('首次设置：选择本地任务、Drive 同步目录和私有 Notebook。不会读取或保存 Cookie。')
    tasks = sorted((ROOT / 'configs').glob('DMR-*.yml'))
    for index, task in enumerate(tasks, 1):
        print(f'  {index}. {task.name}')
    selected = prompt('任务配置编号或完整路径', old.get('task_config', '1' if len(tasks) == 1 else ''))
    if selected.isdigit():
        index = int(selected) - 1
        if not 0 <= index < len(tasks):
            raise ValueError('任务编号无效。')
        task = tasks[index]
    else:
        task = Path(selected)
        if not selected:
            raise ValueError('请选择一个任务配置。')
    value = {
        'schema_version': 1,
        'task_config': str(task.resolve()),
        'drive_sync_root': prompt('My Drive 同步根目录（例如 G:\你的云端硬盘）', old.get('drive_sync_root', '')),
        'notebook_url': prompt('私有 Colab Notebook 地址', old.get('notebook_url', '')),
        'browser': prompt('Chrome/Edge 程序路径', old.get('browser', find_browser())),
        'cdp_url': prompt('专用浏览器 CDP 地址', old.get('cdp_url', 'http://127.0.0.1:9222')),
    }
    validate_settings(value)
    atomic_json(SETTINGS, value)
    print('设置已保存到 .temp/colab-monitor-settings.json。')
    return value


def load_settings(reconfigure=False):
    value = None
    if SETTINGS.exists():
        try:
            value = json.loads(SETTINGS.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            if not reconfigure:
                raise ValueError('启动设置损坏，请用 --setup 重新配置；不会自动重置或重新点击。') from None
    return setup(value if isinstance(value, dict) else None) if reconfigure or value is None else validate_settings(value)


def cdp_ready(address):
    try:
        with urlopen(address.rstrip('/') + '/json/version', timeout=2) as stream:
            value = json.load(stream)
        endpoint = urlparse(value.get('webSocketDebuggerUrl', ''))
        return (isinstance(value.get('Browser'), str) and endpoint.scheme == 'ws'
                and endpoint.hostname in {'127.0.0.1', 'localhost', '::1'}
                and endpoint.port == urlparse(address).port)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def ensure_browser(value, prepare=False):
    if cdp_ready(value['cdp_url']):
        return
    if os.name != 'nt':
        raise ValueError('双击浏览器启动仅适用于 Windows；其他平台请先准备已有 CDP 浏览器。')
    port = urlparse(value['cdp_url']).port
    profile = ROOT / '.temp/colab-browser-profile'
    # 独立 profile 保留首次登录结果，避免控制日常浏览器。
    subprocess.Popen([value['browser'], '--remote-debugging-address=127.0.0.1',
                      f'--remote-debugging-port={port}', f'--user-data-dir={profile}',
                      value['notebook_url'] if prepare else 'about:blank'], cwd=ROOT)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if cdp_ready(value['cdp_url']):
            if prepare:
                print('首次准备：登录 Colab、授权 Drive 和保存参数可使用 CPU；正式 Notebook 保存为 GPU 类型，监控期间无需连接。')
                print('选择“读取本地触发请求”并保存副本；准备完毕可断开 CPU，开播后才点击全部运行申请 GPU。')
                input('页面准备好后按 Enter 开始监控：')
            return
        time.sleep(0.5)
    raise ValueError('浏览器 CDP 未就绪，请检查专用浏览器。未启动监控或发出录制请求。')


def trigger_command(value):
    return [sys.executable, '-u', str(ROOT / 'colab_trigger.py'),
            '--url', task_room(value['task_config']),
            '--drive-sync-root', value['drive_sync_root'],
            '--notebook-url', value['notebook_url'], '--auto-run',
            '--cdp-url', value['cdp_url']]


def main(argv=None):
    parser = argparse.ArgumentParser(description='一键启动本地 Colab 开播监控。')
    parser.add_argument('--setup', action='store_true', help='重新设置启动参数，保留监控状态')
    args = parser.parse_args(argv)
    if importlib.util.find_spec('playwright') is None:
        raise ValueError('缺少 Playwright。请先执行：python -m pip install -r colab_support/monitor_requirements.txt')
    with ProcessLock(ROOT / '.temp/colab-monitor-launcher.lock'):
        first_setup = args.setup or not SETTINGS.exists()
        value = load_settings(args.setup)
        ensure_browser(value, prepare=first_setup)
        print('监控目标：', task_room(value['task_config']))
        print('默认每 60 秒检查一次，连续两次开播后自动点击一次全部运行。Ctrl+C 停止本地监控。')
        print('本入口只读取任务的直播间；录制与投稿设置使用 Notebook 表单和私有 Drive 配置。')
        return subprocess.call(trigger_command(value), cwd=ROOT)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('已停止本地启动入口。')
        raise SystemExit(0)
    except (ValueError, OSError, RuntimeError):
        # 锁和 IO 错误可能包含私有路径，避免输出原始异常。
        if sys.exc_info()[0] is ValueError:
            print(str(sys.exc_info()[1]))
        else:
            print('启动失败：请检查依赖、路径权限和是否已有监控实例；保留原状态文件。')
        raise SystemExit(2)
