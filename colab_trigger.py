"""本地检测 B站开播并触发 Colab Notebook。"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import logging
import sys
from urllib.parse import urlparse

from colab_support.trigger import (
    DEFAULT_NOTEBOOK_URL, ColabLauncher, TriggerMonitor, TriggerResult,
    probe_bilibili, validate_room_url,
)

FRIENDLY_ERRORS = {
    "claim_timeout": "Colab 认领请求超时；核实旧任务未运行后停止监控，执行 start_colab_monitor.cmd --recover-expired 恢复",
    "runtime_heartbeat_stale": "Colab 运行心跳超时",
    "runtime_release_unconfirmed": "Colab 运行时释放未确认",
    "runtime_release_pending": "等待 Colab 释放运行时",
    "runtime_release_unknown": "Colab 运行时释放状态未知",
    "launch_state_unknown": "Colab 启动状态未知",
    "ack_invalid": "回执文件无效或损坏",
    "probe_unavailable": "网络探测不可用",
    "trigger_state_invalid": "触发器状态文件损坏",
    "google_account_ambiguous": "Google 页面有多个账号，请指定 --google-account 或手动选择",
    "google_account_mismatch": "Google 页面没有配置的账号，请检查专用浏览器登录",
    "google_account_unconfirmed": "无法确认授权页面账号，请手动核对",
    "google_login_required": "Google 需要登录，请在专用浏览器完成登录",
    "google_auth_challenge": "Google 需要验证码或二次验证，请在专用浏览器完成",
    "google_auth_page_unrecognized": "Google 授权页面无法识别，请手动检查",
    "authorization_browser_unavailable": "授权监听无法连接专用浏览器，下一轮重试",
    "authorization_stop_timeout": "授权监听尚未退出，请检查专用浏览器",
}


def setup_logger() -> logging.Logger:
    """初始化对齐原项目规范的控制台 Logger。"""
    trigger_logger = logging.getLogger("ColabTrigger")
    trigger_logger.setLevel(logging.INFO)
    trigger_logger.propagate = False
    if not trigger_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s]: %(message)s"))
        trigger_logger.addHandler(handler)
    return trigger_logger


def resolve_target_info(room_url: str) -> tuple[str, str]:
    """解析目标直播间信息，返回 (主播名或房间标识, 房间号)。"""
    room_id = urlparse(room_url).path.strip('/')
    try:
        from DMR.LiveAPI import LiveAPI
        api = LiveAPI(room_url)
        info = api.GetStreamerInfo()
        if info and getattr(info, 'name', None):
            return str(info.name), room_id
    except Exception:
        pass
    return f"房间 {room_id}", room_id


def format_trigger_message(target_name: str, result: TriggerResult) -> tuple[int, str]:
    """将 TriggerResult 转换为直观的人类可读日志文本与对应日志级别。"""
    run_info = f" (run_id: {result.run_id})" if result.run_id else ""
    error_desc = FRIENDLY_ERRORS.get(result.error, result.error) if result.error else ""
    error_info = f"，原因: {error_desc}" if error_desc else ""

    if result.triggered:
        return logging.INFO, f"[{target_name}] 检测到直播开始！已发送 Colab 运行请求，等待认领{run_info}"

    state = result.state
    if state == "offline":
        return logging.INFO, f"[{target_name}] 直播未开播，正在监听中..."
    if state == "live":
        return logging.INFO, f"[{target_name}] 检测到开播信号，正在确认状态..."
    if state in ("dispatching", "opened"):
        return logging.INFO, f"[{target_name}] 正在打开 Colab Notebook 并发出录制请求{run_info}"
    if state == "accepted":
        return logging.INFO, f"[{target_name}] Colab 已认领录制任务{run_info}"
    if state == "running":
        return logging.INFO, f"[{target_name}] 录制中，Colab 正常运行中{run_info}"
    if state == "draining":
        return logging.INFO, f"[{target_name}] 直播已结束，Colab 正在收尾与备份{run_info}"
    if state == "success":
        return logging.INFO, f"[{target_name}] Colab 录制与备份任务完成，正在等待下播确认{run_info}"
    if state == "failed":
        return logging.ERROR, f"[{target_name}] Colab 任务执行失败{run_info}{error_info}"
    if state == "ended":
        return logging.INFO, f"[{target_name}] 直播已结束，重置监听状态"
    if state == "probe_unavailable":
        return logging.WARNING, f"[{target_name}] 直播状态查询失败，保持当前状态继续重试"
    if state == "needs_attention":
        return logging.WARNING, f"[{target_name}] 需人工关注{run_info}{error_info}"
    return logging.INFO, f"[{target_name}] 状态: {state}{run_info}{error_info}"


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
    p.add_argument("--once", action="store_true", help="触发一次后退出；自动点击模式等待认领后退出")
    p.add_argument("--auto-run", action="store_true", help="连接专用 loopback CDP 浏览器并点击一次 Run all")
    p.add_argument("--cdp-url", help="专用浏览器 loopback CDP 地址")
    p.add_argument("--google-account", default='', help="Drive 授权所用 Google 账号邮箱；留空仅自动选择唯一账号")
    p.add_argument("--recover-expired", action="store_true", help="核实旧任务未运行后，归档未认领的过期请求状态并恢复监控")
    p.add_argument("--json", action="store_true", help="使用原始 JSON 格式输出状态日志（用于自动化脚本解析）")
    p.add_argument("--verbose", action="store_true", help="每次轮询均输出日志（即使状态未变化）")
    return p


def main():
    args = parser().parse_args()
    room_url = validate_room_url(args.url)
    if args.poll_seconds <= 0:
        raise ValueError("轮询间隔必须为正数")
    launcher = ColabLauncher(args.notebook_url, args.drive_sync_root, not args.no_browser,
                             auto_run=args.auto_run, cdp_url=args.cdp_url, google_account=args.google_account)
    from colab_support.locks import ProcessLock
    try:
        with ProcessLock(args.state_file.with_suffix('.lock')):
            return monitor_loop(args, room_url, launcher)
    finally:
        launcher.close()


def monitor_loop(args, room_url, launcher):
    monitor = TriggerMonitor(room_url, launcher, args.state_file,
                             args.live_confirmations, args.offline_confirmations)
    logger = setup_logger()
    if getattr(args, 'recover_expired', False):
        archive = monitor.recover_expired()
        if args.json:
            print(json.dumps({'status': 'recovered', 'archive': str(archive)}, ensure_ascii=False), flush=True)
        else:
            logger.info('已归档未认领的过期请求状态：%s；重新确认开播状态。', archive)
    target_name, room_id = resolve_target_info(room_url)

    if not args.json:
        logger.info(f"开始监控直播间：{room_url} (目标: {target_name})")
        logger.info(f"轮询间隔: {args.poll_seconds:.0f}s，开播判定: 连续{args.live_confirmations}次，下播判定: 连续{args.offline_confirmations}次")
        logger.info("本地 Colab 触发器已启动；查询失败不会判定为下播。")

    last_log_signature = None

    while True:
        result = monitor.observe(probe_bilibili(room_url))
        if args.json:
            print(json.dumps({"status": result.state, "triggered": result.triggered,
                              "run_id": result.run_id, "error": result.error}, ensure_ascii=False), flush=True)
        else:
            level, msg = format_trigger_message(target_name, result)
            signature = (result.state, result.triggered, result.run_id, result.error)
            verbose = getattr(args, 'verbose', False)
            if verbose or signature != last_log_signature:
                logger.log(level, msg)
                last_log_signature = signature

        if args.once:
            if result.triggered and not args.auto_run:
                return 0
            if args.auto_run:
                if monitor.state.ack_status is not None:
                    return 1 if monitor.state.ack_status == 'failed' else 0
                if result.error in {'claim_timeout', 'launch_state_unknown', 'active_run_conflict',
                                    'ack_invalid', 'trigger_state_invalid'}:
                    return 1
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    _logger = setup_logger()
    try:
        raise SystemExit(main())
    except ValueError as error:
        _logger.error(f"触发器参数错误：{error}")
        raise SystemExit(2)
    except KeyboardInterrupt:
        _logger.info("已停止本地 Colab 触发器。")
        raise SystemExit(0)
