"""从 Drive 校验恢复单场成品并补传，unknown 只核对远端。"""
import argparse
import json
import logging
import queue
from pathlib import Path

from colab_support.core import atomic_json, digest, verified_copy
from colab_support.locks import ProcessLock
from colab_support.posting import PostingWorker
from colab_support.trigger import RUN_ID_RE
from colab_support.upload_config import load_upload_config, load_upload_cookie


def restore_segments(backup, work, manifest):
    segments = sorted(manifest.get('segments', {}).items(), key=lambda item: int(item[1]['segment_id']))
    if not segments or [int(s['segment_id']) for _, s in segments] != list(range(1, len(segments) + 1)):
        raise ValueError('resume_segment_gap')
    if len({s['group_id'] for _, s in segments}) != 1:
        raise ValueError('resume_multiple_groups')
    for key, segment in segments:
        if not isinstance(key, str) or not all(c.isalnum() or c in '-_' for c in key):
            raise ValueError('resume_key_invalid')
        for kind, suffix in (('source', '.mp4'), ('danmaku', '.ass'), ('rendered', '.mp4')):
            info = segment.get('backup', {}).get(kind, {})
            path = Path(backup) / kind / (key + suffix)
            if info.get('status') != 'success' or digest(path) != (info.get('size'), info.get('sha256')):
                raise ValueError('resume_backup_invalid')
        target = Path(work) / 'rendered' / (key + '.mp4')
        verified_copy(Path(backup) / 'rendered' / (key + '.mp4'), target)
        segment['rendered_path'] = target.relative_to(work).as_posix()
    return segments


def parser():
    value = argparse.ArgumentParser(description='按 run_id 从私有 Drive 补传已备份的分 P。')
    value.add_argument('--run-id', required=True)
    value.add_argument('--drive-root', type=Path, default=Path('/content/drive/MyDrive'))
    value.add_argument('--work-root', type=Path, default=Path('/content/dmr-upload-resume'))
    value.add_argument('--reconcile-bvid', help='创建响应丢失时，由人工指定需核对的 BVID；仍验证远端文件序列。')
    return value


def main():
    args = parser().parse_args()
    if not RUN_ID_RE.fullmatch(args.run_id):
        raise ValueError('resume_run_id_invalid')
    logging.disable(logging.CRITICAL)
    drive = args.drive_root.resolve()
    work = (args.work_root / args.run_id).resolve()
    if work.is_relative_to(drive) or drive.is_relative_to(work):
        raise ValueError('resume_work_directory_invalid')
    backup = drive / 'DMRColab/runs' / args.run_id
    config_path = drive / 'DMRColab/config/upload.yml'
    cookie = drive / 'DMRColab/credentials/bilibili_upload.json'
    config = load_upload_config(config_path)
    if not config.get('enabled'):
        raise ValueError('upload_disabled')
    load_upload_cookie(cookie)
    from colab_support.uploader import prepare_identity
    prepare_identity(cookie, config)
    with ProcessLock(work / 'resume.lock'):
        manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('run_id') != args.run_id or manifest.get('status') not in {'failed', 'success'}:
            raise ValueError('resume_run_not_terminal')
        segments = restore_segments(backup, work, manifest)
        from colab_support.upload_transaction import Journal
        journal = Journal(work / 'upload.json', backup / 'upload.json', args.run_id, config['expected_uid'])
        journal.save()
        if args.reconcile_bvid:
            from colab_support.upload_transaction import BVID
            if not BVID.fullmatch(args.reconcile_bvid):
                raise ValueError('resume_bvid_invalid')
            # 私有配置中的候选编号仅供当前恢复进程核对，不进入普通录制配置。
        events = queue.Queue()
        posting = PostingWorker(events, cookie, config_path, config, work, backup)
        posting.candidate = args.reconcile_bvid
        posting.begin_drain()
        summary = {'run_id': args.run_id, 'status': 'failed', 'parts': {}}
        index = 0
        minimum = manifest.get('settings', {}).get('upload_min_length', config['min_length'])
        try:
            for key, segment in segments:
                prior = journal.data['parts'].get(key)
                if not prior and segment['duration'] < minimum:
                    summary['parts'][key] = {'status': 'skipped_short'}
                    continue
                index += 1
                posting.submit(key, key, work / segment['rendered_path'], segment, index)
                result = events.get(timeout=config['part_timeout'] + 30)
                summary['parts'][key] = result['data']
                if result['data'].get('status') != 'submitted':
                    break
            if index and len(summary['parts']) == len(segments) and all(
                    p['status'] in {'submitted', 'skipped_short'} for p in summary['parts'].values()):
                summary['status'] = 'success'
        finally:
            posting.close()
            atomic_json(work / 'upload_resume.json', summary)
            verified_copy(work / 'upload_resume.json', backup / 'upload_resume.json')
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary['status'] == 'success' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('补传未完成，请检查私有配置、备份校验和 upload.json 状态。')
        raise SystemExit(1)
