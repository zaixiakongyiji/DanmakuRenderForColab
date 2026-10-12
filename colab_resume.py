"""从私有 Drive 恢复历史运行的媒体处理和投稿。"""
import argparse
import json
import logging
from contextlib import ExitStack
from pathlib import Path

from colab_support.locks import ProcessLock
from colab_support.recovery import Recovery
from colab_support.trigger import RUN_ID_RE


def main(argv=None):
    parser = argparse.ArgumentParser(description='按 run_id 继续渲染、合并归档和投稿。')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--drive-root', type=Path, default=Path('/content/drive/MyDrive'))
    parser.add_argument('--work-root', type=Path, default=Path('/content/dmr-upload-resume'))
    parser.add_argument('--inspect-only', action='store_true')
    parser.add_argument('--no-upload', action='store_true')
    parser.add_argument('--reconcile-bvid')
    parser.add_argument('--append-run-id', action='append', default=[],
                        help='同场后续任务编号，可重复填写；--run-id 为已有稿件的主任务。')
    parser.add_argument('--target-bvid', help='跨任务追加的目标 BV 号，须与主任务投稿记录一致。')
    parser.add_argument('--encoder', choices=('h264_nvenc', 'libx264'), default='h264_nvenc')
    parser.add_argument('--media-timeout', type=float, default=14400)
    parser.add_argument('--min-free-gib', type=float, default=10)
    args = parser.parse_args(argv)
    if not RUN_ID_RE.fullmatch(args.run_id):
        raise ValueError('resume_run_id_invalid')
    logging.disable(logging.CRITICAL)
    work = (args.work_root / args.run_id).resolve()
    drive = args.drive_root.resolve()
    if work.is_relative_to(drive) or drive.is_relative_to(work):
        raise ValueError('resume_work_directory_invalid')
    # 与旧补传入口共享本机锁；不声明 Drive 跨运行时互斥。
    if args.append_run_id or args.target_bvid:
        from colab_support.session_append import SessionAppend
        runs = [args.run_id, *args.append_run_id]
        if (args.no_upload or args.reconcile_bvid or len(runs) < 2 or len(set(runs)) != len(runs) or
                any(not RUN_ID_RE.fullmatch(r) for r in runs)):
            raise ValueError('append_arguments_invalid')
        with ExitStack() as stack:
            for run_id in sorted(runs):
                stack.enter_context(ProcessLock(args.work_root / run_id / 'resume.lock'))
            session = SessionAppend(drive, args.work_root, args.run_id, args.append_run_id,
                args.target_bvid, encoder=args.encoder, min_free_gib=args.min_free_gib)
            if args.inspect_only:
                print(json.dumps(session.plan(), ensure_ascii=False, indent=2))
                return 0
            return session.execute(media_timeout=args.media_timeout)
    with ProcessLock(work / 'resume.lock'):
        recovery = Recovery(drive, work, args.run_id, encoder=args.encoder, min_free_gib=args.min_free_gib)
        if args.inspect_only:
            print(json.dumps(recovery.inspect(), ensure_ascii=False, indent=2))
            return 0
        result = recovery.execute(upload=not args.no_upload, reconcile_bvid=args.reconcile_bvid,
                                  media_timeout=args.media_timeout)
        print(json.dumps({key: recovery.state.get(key) for key in (
            'run_id', 'status', 'phase', 'original_status', 'original_errors', 'errors', 'merge', 'upload')},
            ensure_ascii=False, indent=2))
        return result


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        from colab_support.merge import MergeError
        from colab_support.recovery import RecoveryError
        from colab_support.upload_transaction import UploadError
        code = str(error) if isinstance(error, (RecoveryError, MergeError, UploadError)) else 'resume_failed'
        print('恢复未完成：' + code + '。请检查 resume_manifest.json（跨任务模式为 append_resume.json）和 upload.json。', flush=True)
        raise SystemExit(1)
