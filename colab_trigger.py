"""本地检测 B站开播并触发 Colab Notebook。"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from colab_support.trigger import (
    DEFAULT_NOTEBOOK_URL, ColabLauncher, TriggerMonitor, probe_bilibili,
    validate_room_url,
)


def parser():
    p = argparse.ArgumentParser(description="检测 B站开播并打开 Colab 录制 Notebook。")
    p.add_argument("--url", required=True, help="B站直播间 URL")
    p.add_argument("--notebook-url", default=DEFAULT_NOTEBOOK_URL)
    p.add_argument("--state-file", type=Path, default=Path(".temp/colab-trigger-state.json"))
    p.add_argument("--drive-sync-root", type=Path,
                   help="Google Drive for desktop 的同步根目录；用于写入 DMRColab/control/runs/<run_id>/request.json")
    p.add_argument("--poll-seconds", type=float, default=60)
    p.add_argument("--live-confirmations", type=int, default=2)
    p.add_argument("--offline-confirmations", type=int, default=3)
    p.add_argument("--no-browser", action="store_true", help="只写控制文件，不打开浏览器")
    p.add_argument("--once", action="store_true", help="触发一次后退出")
    p.add_argument("--auto-run", action="store_true", help="连接专用 loopback CDP 浏览器并点击一次 Run all")
    p.add_argument("--cdp-url", help="专用浏览器 loopback CDP 地址")
    return p


def main():
    args = parser().parse_args()
    room_url = validate_room_url(args.url)
    if args.poll_seconds <= 0:
        raise ValueError("轮询间隔必须为正数")
    launcher = ColabLauncher(args.notebook_url, args.drive_sync_root, not args.no_browser,
                             auto_run=args.auto_run, cdp_url=args.cdp_url)
    from colab_support.locks import ProcessLock
    with ProcessLock(args.state_file.with_suffix('.lock')):
        return monitor_loop(args, room_url, launcher)


def monitor_loop(args, room_url, launcher):
    monitor = TriggerMonitor(room_url, launcher, args.state_file,
                             args.live_confirmations, args.offline_confirmations)
    print("本地 Colab 触发器已启动；查询失败不会判定为下播。", flush=True)
    while True:
        result = monitor.observe(probe_bilibili(room_url))
        print(json.dumps({"status": result.state, "triggered": result.triggered,
                          "run_id": result.run_id, "error": result.error}, ensure_ascii=False), flush=True)
        if args.once and result.triggered:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print("触发器参数错误：" + str(error), flush=True)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("已停止本地 Colab 触发器。", flush=True)
        raise SystemExit(0)
