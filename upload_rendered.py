"""
手动上传已渲染的弹幕版视频（使用 B站网页 API）。
同一场直播的多个分段会合并到同一个B站视频的不同分P。
"""
import json
import hashlib
import math
import os
import re
import subprocess
import sys
import time
import logging
from datetime import datetime
from glob import glob
from pathlib import Path
from dataclasses import asdict

import yaml

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

logging.basicConfig(level=logging.INFO, format='[%(asctime)s][%(levelname)s]: %(message)s')
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from DMR.Uploader.biliwebapi import BiliWebApi
from colab_support.core import atomic_json, digest
from colab_support.locks import ProcessLock
from colab_support.upload_transaction import BVID, ColabBiliUploader, Journal, UploadError

# 配置
RENDER_DIR = './直播回放（弹幕版）'
SESSION_GAP_MINUTES = 120
TASK_CONFIG_PATH = 'configs/DMR-乡建茶舍.yml'
PROGRESS_FILE = '.temp/upload_progress.json'


def parse_filename(filename):
    basename = os.path.splitext(os.path.basename(filename))[0]
    m = re.search(r'(\d{4})年(\d{2})月(\d{2})日(\d{2})点(\d{2})分', basename)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)))
    return None


def group_by_session(files):
    dated = []
    for f in files:
        dt = parse_filename(f)
        if dt:
            dated.append((dt, f))
    dated.sort(key=lambda x: x[0])

    sessions = []
    current = []
    for i, (dt, f) in enumerate(dated):
        if i == 0:
            current.append((dt, f))
            continue
        gap = (dt - dated[i-1][0]).total_seconds() / 60
        if gap > SESSION_GAP_MINUTES:
            sessions.append(current)
            current = [(dt, f)]
        else:
            current.append((dt, f))
    if current:
        sessions.append(current)
    return sessions


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            progress = json.load(f)
        if not isinstance(progress, dict) or any(not isinstance(v, dict) for v in progress.values()):
            raise UploadError('upload_progress_invalid')
        return progress
    return {}


def save_progress(progress):
    atomic_json(PROGRESS_FILE, progress)


def get_video_duration(filepath):
    try:
        proc = subprocess.run(
            ['./tools/ffprobe.exe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
            capture_output=True, text=True, timeout=30, check=True
        )
        duration = float(proc.stdout.strip())
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        return duration
    except (OSError, subprocess.SubprocessError, ValueError):
        raise UploadError('video_probe_failed') from None


def session_key(session):
    return session[0][0].strftime('%Y%m%d%H%M')


def get_upload_config():
    with open(TASK_CONFIG_PATH, 'r', encoding='utf-8') as f:
        task_cfg = yaml.safe_load(f)

    dm_configs = task_cfg.get('upload_args', {}).get('dm_video')
    if not dm_configs:
        logger.error('配置中没有 dm_video 上传设置')
        sys.exit(1)
    if isinstance(dm_configs, list):
        task_upload = dm_configs[0]
    else:
        task_upload = dm_configs
    cfg = {}
    global_path = Path('configs/global.yml')
    if global_path.exists():
        with global_path.open(encoding='utf-8') as f:
            cfg.update(yaml.safe_load(f).get('upload_args', {}).get('bilibili', {}))
    cfg.update(task_upload)

    # 从任务配置中读取直播信息
    dl_cfg = task_cfg.get('download_args', {})
    if 'live' in dl_cfg and isinstance(dl_cfg['live'], dict):
        live_cfg = dl_cfg['live']
    else:
        live_cfg = dl_cfg
    cfg['_streamer_url'] = live_cfg.get('url', '')

    # 从配置或文件名解析主播名称
    cfg['_streamer_name'] = live_cfg.get('streamer_name', '')
    return cfg


def guess_streamer_name(files):
    """从文件名中解析主播名称，例如 '舍茶建乡-2026年05月01日20点42分（弹幕版）.mp4' → '舍茶建乡'"""
    for f in files:
        basename = os.path.splitext(os.path.basename(f))[0]
        m = re.match(r'^(.+?)-\d{4}年\d{2}月\d{2}日', basename)
        if m:
            return m.group(1)
    return ''


def file_key(path):
    name = os.path.normcase(os.path.basename(path))
    return hashlib.sha256(name.encode('utf-8')).hexdigest()


class LocalJournal(Journal):
    """本地场次日志复用投稿事务结果格式，整个进度文件原子写入。"""
    def __init__(self, progress, key, uid):
        self.progress = progress
        self.data = progress.setdefault(key, {'version': 1, 'run_id': key, 'uid': uid,
                                              'parts': {}, 'files': {}, 'bvid': None, 'aid': None})
        if (self.data.get('version') != 1 or self.data.get('run_id') != key
                or self.data.get('uid') != uid or not isinstance(self.data.get('parts'), dict)
                or not isinstance(self.data.get('files'), dict)):
            raise UploadError('upload_progress_invalid')
        if self.data.get('bvid') and not BVID.fullmatch(str(self.data['bvid'])):
            raise UploadError('upload_progress_invalid')
        parts = self.data['parts']
        for part in parts.values():
            if (not isinstance(part, dict)
                    or part.get('status') not in {'pending', 'uploading', 'uploaded', 'submitting', 'submitted', 'failed', 'unknown'}
                    or type(part.get('part_index')) is not int or part['part_index'] <= 0
                    or type(part.get('size')) is not int or part['size'] <= 0
                    or not re.fullmatch(r'[0-9a-f]{64}', str(part.get('sha256', '')))):
                raise UploadError('upload_progress_invalid')
            if part['status'] in {'uploaded', 'submitting', 'submitted', 'unknown'} and not part.get('filename'):
                raise UploadError('upload_progress_invalid')
            if part['status'] in {'submitting', 'submitted', 'unknown'} and not part.get('expected_filenames'):
                raise UploadError('upload_progress_invalid')
        if sorted(p['part_index'] for p in parts.values()) != list(range(1, len(parts) + 1)):
            raise UploadError('upload_progress_invalid')

    def save(self):
        self.data['uploaded'] = sum(p['status'] == 'submitted' for p in self.data['parts'].values())
        save_progress(self.progress)


def prepare_journal(progress, session, uploader):
    key = session_key(session)
    keys = {file_key(path) for _, path in session}
    matches = [k for k, value in progress.items() if keys.intersection(value.get('parts', {}))]
    if len(matches) > 1 or (matches and key in progress and matches[0] != key):
        raise UploadError('upload_session_ambiguous')
    if matches:
        key = matches[0]
    old = progress.get(key)
    if old is not None and 'parts' not in old:
        count = old.get('uploaded', 0)
        if type(count) is not int or count < 0 or (count and not old.get('bvid')):
            raise UploadError('upload_legacy_progress_ambiguous')
        data = {'version': 1, 'run_id': key, 'uid': uploader.config['expected_uid'],
                'parts': {}, 'files': {}, 'bvid': old.get('bvid'), 'aid': None}
        if data['bvid']:
            remote = uploader.remote(data['bvid'])
            if count > len(remote.videos):
                raise UploadError('upload_legacy_progress_ambiguous')
            # 旧计数不作为跳过依据，逐 P 用原文件名标题唯一匹配。
            for index, part in enumerate(remote.videos, 1):
                candidates = [path for _, path in session
                              if part.get('title') in (Path(path).stem[:80], Path(path).name[:80])]
                if len(candidates) != 1:
                    raise UploadError('upload_legacy_progress_ambiguous')
                path = candidates[0]
                identity = file_key(path)
                if identity in data['parts']:
                    raise UploadError('upload_legacy_progress_ambiguous')
                size, checksum = digest(path)
                data['files'][identity] = os.path.basename(path)
                data['parts'][identity] = {'status': 'submitted', 'part_index': index,
                    'size': size, 'sha256': checksum, 'filename': part['filename'],
                    'cid': part.get('cid'), 'expected_filenames': [p['filename'] for p in remote.videos[:index]]}
            data['metadata'] = asdict(remote)
            data['metadata'].pop('extra_kwargs', None)
        progress[key] = data
    return LocalJournal(progress, key, uploader.config['expected_uid'])


def create_uploader(cfg):
    account = cfg.get('account', 'bilibili')
    cookies_path = cfg.get('cookies') or f'.login_info/{account}.json'
    api = BiliWebApi(cookies=cookies_path, account=account,
                     limit=cfg.get('limit', 3), readonly_cookies=True)
    try:
        response = api._session.get('https://api.bilibili.com/x/web-interface/nav', timeout=10).json()
        identity = response['data']
        if response['code'] != 0 or not identity.get('isLogin') or int(identity['mid']) <= 0:
            raise ValueError()
        uid = int(identity['mid'])
    except Exception:
        raise UploadError('upload_auth_unavailable') from None
    config = dict(cfg)
    config['expected_uid'] = int(cfg.get('expected_uid', uid))
    config['line'] = cfg.get('line') or 'AUTO'
    uploader = ColabBiliUploader(None, config, api=api)
    if uid != config['expected_uid']:
        raise UploadError('upload_identity_mismatch')
    return uploader


def upload_session(session, journal, uploader, cfg):
    state = journal.data
    state['done'] = False
    journal.save()
    min_length = cfg.get('min_length', 30)
    if type(min_length) not in (int, float) or not math.isfinite(min_length) or min_length < 0:
        raise UploadError('upload_min_length_invalid')
    ordered = sorted(state['parts'].values(), key=lambda p: p['part_index'])
    if state['bvid'] and all(p['status'] == 'submitted' for p in ordered):
        remote = uploader.remote(state['bvid'])
        if [p['filename'] for p in remote.videos] != [p['filename'] for p in ordered]:
            raise UploadError('upload_remote_parts_mismatch')

    for position, (_, path) in enumerate(session):
        identity = file_key(path)
        current = state['parts'].get(identity)
        # 已提交文件仍核对内容和远端，但不再用 FFprobe 重建上传序号。
        duration = 0 if current and current['status'] == 'submitted' else get_video_duration(path)
        if not current and duration < min_length:
            logger.info('跳过短视频 %s (%.0fs < %.0fs)', os.path.basename(path), duration, min_length)
            continue
        if not current:
            submitted_dates = [parse_filename(state['files'].get(k, '')) for k, p in state['parts'].items()
                               if p['status'] == 'submitted']
            if any(stamp and parse_filename(path) < stamp for stamp in submitted_dates):
                raise UploadError('upload_part_order_invalid')
        index = current['part_index'] if current else len(state['parts']) + 1
        state['files'][identity] = os.path.basename(path)
        expected = current if current else dict(zip(('size', 'sha256'), digest(path)))
        segment = {'duration': duration, 'ctime': session[0][0].isoformat(),
                   'title': cfg.get('_recording_title', '天黑请喝茶'),
                   'streamer': {'name': cfg['_streamer_name'], 'url': cfg.get('_streamer_url', '')},
                   'group_id': state['run_id'], 'segment_id': index,
                   'backup': {'rendered': {'size': expected['size'], 'sha256': expected['sha256']}}}
        logger.info('处理 P%s: %s', index, os.path.basename(path))
        result = uploader.upload_part(path, segment, index, journal, identity, part_title=Path(path).stem[:80])
        if result['status'] != 'submitted':
            raise UploadError(result.get('error', 'upload_failed'))
        if not current and position < len(session) - 1:
            time.sleep(30)
    if any(p['status'] != 'submitted' for p in state['parts'].values()):
        raise UploadError('upload_previous_part_blocked')
    state['done'] = True
    journal.save()


def run_uploads():
    cfg = get_upload_config()
    account = cfg.get('account', 'bilibili')
    logger.info(f'上传账号: {account}')
    all_files = sorted(glob(os.path.join(RENDER_DIR, '*.mp4')))
    if not all_files:
        logger.error(f'在 {RENDER_DIR} 中没有找到 mp4 文件')
        return 1
    if any(parse_filename(path) is None for path in all_files):
        raise UploadError('upload_filename_invalid')
    if not cfg.get('_streamer_name'):
        cfg['_streamer_name'] = guess_streamer_name(all_files) or '未知主播'
    sessions = group_by_session(all_files)
    logger.info(f'共找到 {len(all_files)} 个视频，分为 {len(sessions)} 个场次')
    progress = load_progress()
    uploader = create_uploader(cfg)
    failed = False
    for i, session in enumerate(sessions):
        journal = None
        try:
            journal = prepare_journal(progress, session, uploader)
            upload_session(session, journal, uploader, cfg)
            logger.info('场次 %s 处理完成，BVID=%s', i + 1, journal.data['bvid'])
        except Exception as error:
            failed = True
            if journal is not None:
                journal.data['done'] = False
                journal.save()
            logger.error('场次 %s 未完成: %s', i + 1, error)
    if failed:
        logger.error('存在未完成的上传场次')
        return 1
    logger.info('全部场次处理完成')
    return 0


def main():
    try:
        with ProcessLock(Path(PROGRESS_FILE).with_suffix('.lock')):
            return run_uploads()
    except KeyboardInterrupt:
        logger.warning('上传已中断，提交结果将在下次运行时核对')
        return 130
    except Exception as error:
        logger.error('上传未完成: %s', error)
        return 1


if __name__ == '__main__':
    sys.exit(main())
