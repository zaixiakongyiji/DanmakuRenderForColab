"""媒体传输和稿件提交事务；模糊提交只核对，绝不自动重发。"""
import copy
import json
import re
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import atomic_json, digest, verified_copy
from .upload_config import load_upload_cookie

SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
BVID = re.compile(r'^BV[0-9A-Za-z]{10}$')


class UploadError(RuntimeError):
    pass


class Journal:
    def __init__(self, local, remote, run_id, uid):
        self.local, self.remote = Path(local), Path(remote)
        copies = []
        for path in (self.local, self.remote):
            if path.exists():
                try:
                    value = json.loads(path.read_text(encoding='utf-8'))
                    if value['version'] != 1 or value['run_id'] != run_id or value['uid'] != uid or not isinstance(value['parts'], dict):
                        raise ValueError()
                    if value.get('bvid') and not BVID.fullmatch(value['bvid']):
                        raise ValueError()
                    indices = []
                    for key, part in value['parts'].items():
                        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', key):
                            raise ValueError()
                        if part.get('status') not in {'pending', 'uploading', 'uploaded', 'submitting', 'submitted', 'failed', 'unknown'}:
                            raise ValueError()
                        if type(part.get('part_index')) is not int or part['part_index'] <= 0:
                            raise ValueError()
                        indices.append(part['part_index'])
                        if not isinstance(part.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', part['sha256']):
                            raise ValueError()
                        if type(part.get('size')) is not int or part['size'] <= 0:
                            raise ValueError()
                        if part.get('filename') and not re.fullmatch(r'[A-Za-z0-9_-]+', part['filename']):
                            raise ValueError()
                        if part['status'] in {'submitting', 'submitted', 'unknown'} and not part.get('expected_filenames'):
                            raise ValueError()
                    if len(indices) != len(set(indices)):
                        raise ValueError()
                    copies.append(value)
                except Exception:
                    raise UploadError('upload_journal_invalid') from None
        # 本地比 Drive 新可能意味着上次持久化中断。不能据此重复提交。
        if len(copies) == 2 and copies[0] != copies[1]:
            raise UploadError('upload_journal_diverged')
        self.data = copies[0] if copies else {'version': 1, 'run_id': run_id, 'uid': uid,
                                              'parts': {}, 'bvid': None, 'aid': None}

    def save(self):
        atomic_json(self.local, self.data)
        verified_copy(self.local, self.remote)
        if self.local.read_bytes() != self.remote.read_bytes():
            raise UploadError('upload_journal_persistence_failed')

    def result(self, key):
        part = self.data['parts'][key]
        fields = ('status', 'error', 'part_index', 'filename', 'cid', 'elapsed_seconds', 'sha256', 'size')
        return dict({k: part[k] for k in fields if k in part}, bvid=self.data['bvid'], aid=self.data['aid'])


class ColabBiliUploader:
    def __init__(self, cookie_file, config, api=None):
        self.config = config
        if api is None:
            load_upload_cookie(cookie_file)
            from DMR.Uploader.biliwebapi import BiliWebApi
            api = BiliWebApi(cookies=str(cookie_file), account=config['account'],
                             limit=config['limit'], sort_videos=False, readonly_cookies=True)
        self.api = api

    def check_identity(self):
        try:
            result = self.api._session.get('https://api.bilibili.com/x/web-interface/nav', timeout=10).json()
            data = result['data']
            if result['code'] != 0 or not data.get('isLogin'):
                raise ValueError()
            if int(data['mid']) != self.config['expected_uid']:
                raise UploadError('upload_identity_mismatch')
        except UploadError:
            raise
        except Exception:
            raise UploadError('upload_auth_unavailable') from None

    def metadata(self, segment, path):
        from DMR.utils import StreamerInfo, VideoInfo
        stamp = segment.get('ctime')
        stamp = datetime.fromisoformat(stamp) if stamp else datetime.now(SHANGHAI)
        stamp = stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp.astimezone(SHANGHAI)
        video = VideoInfo(path=str(path), duration=segment['duration'], title=segment.get('title', ''),
            streamer=StreamerInfo(**segment.get('streamer', {})), ctime=stamp,
            group_id=segment['group_id'], segment_id=segment['segment_id'], dtype='dm_video')
        data = self.api.videoinfo_to_videos(video, self.config)
        if self.config.get('cover') and not data.cover:
            raise UploadError('upload_cover_failed')
        value = asdict(data)
        value.pop('extra_kwargs', None)
        return value

    def remote(self, bvid):
        value = self.api.get_remote_data(bvid)
        if value is None or str(value.bvid) != bvid:
            raise UploadError('upload_remote_query_failed')
        return value

    def discover(self, filenames):
        # 仅用于找回创建响应丢失的稿件。有限查询失败或未找到都保留 unknown。
        found = []
        for page in range(1, 6):
            try:
                result = self.api._session.get('https://member.bilibili.com/x/web/archives',
                    params={'status': 'is_pubing,pubed,not_pubed', 'pn': page, 'ps': 50}, timeout=10).json()
                if result.get('code') != 0:
                    return None
                entries = result['data']['arc_audits']
                for entry in entries:
                    bvid = entry.get('Archive', entry.get('archive', {})).get('bvid')
                    if not bvid or not BVID.fullmatch(bvid):
                        continue
                    remote = self.api.get_remote_data(bvid)
                    if remote and [v['filename'] for v in remote.videos] == filenames:
                        found.append(bvid)
                if len(entries) < 50:
                    break
            except Exception:
                return None
        return found[0] if len(set(found)) == 1 else None

    def reconcile(self, journal, key, candidate=None):
        part = journal.data['parts'][key]
        filenames = part['expected_filenames']
        bvid = journal.data['bvid'] or candidate or self.discover(filenames)
        if not bvid or not BVID.fullmatch(bvid):
            return False
        remote = self.remote(bvid)
        if [v['filename'] for v in remote.videos] != filenames:
            return False
        journal.data['bvid'] = bvid
        aid = getattr(remote, 'aid', None)
        if isinstance(aid, int) and aid > 0:
            journal.data['aid'] = aid
        part.update(status='submitted', cid=remote.videos[-1].get('cid'))
        part.pop('error', None)
        journal.save()
        return True

    def upload_part(self, path, segment, index, journal, key, candidate=None, *, part_title=None):
        started = time.monotonic()
        self.check_identity()
        size, checksum = digest(path)
        expected = segment['backup']['rendered']
        if (size, checksum) != (expected['size'], expected['sha256']):
            raise UploadError('upload_checksum_mismatch')
        parts = journal.data['parts']
        current = parts.get(key)
        if current and (current['sha256'] != checksum or current['part_index'] != index):
            raise UploadError('upload_content_changed')
        if current and current.get('submission_rejected'):
            return journal.result(key)
        if current and current['status'] in ('submitting', 'unknown'):
            if not self.reconcile(journal, key, candidate):
                current.update(status='unknown', error='upload_submit_unresolved')
                journal.save()
            return journal.result(key)
        if current and current['status'] == 'submitted':
            remote = self.remote(journal.data['bvid'])
            ordered = sorted(parts.values(), key=lambda p: p['part_index'])
            submitted_names = [p['filename'] for p in ordered if p['status'] == 'submitted']
            possible_names = [p['filename'] for p in ordered if p.get('filename')]
            remote_names = [v['filename'] for v in remote.videos]
            if remote_names not in (submitted_names, possible_names):
                raise UploadError('upload_remote_parts_mismatch')
            journal.save()
            return journal.result(key)
        confirmed = sorted((p for k, p in parts.items() if k != key and p['status'] == 'submitted'),
                           key=lambda p: p['part_index'])
        if [p['part_index'] for p in confirmed] != list(range(1, index)):
            raise UploadError('upload_part_order_invalid')
        if any(k != key and p['status'] != 'submitted' for k, p in parts.items()):
            raise UploadError('upload_previous_part_blocked')
        if not current:
            current = parts[key] = {'status': 'pending', 'part_index': index, 'sha256': checksum, 'size': size}
        if not journal.data.get('metadata'):
            journal.data['metadata'] = self.metadata(segment, path)
        remote = None
        if journal.data['bvid']:
            remote = self.remote(journal.data['bvid'])
            if [v['filename'] for v in remote.videos] != [p['filename'] for p in confirmed]:
                raise UploadError('upload_remote_parts_mismatch')
        elif index != 1:
            raise UploadError('upload_bvid_missing')
        if not current.get('filename'):
            current.update(status='uploading')
            journal.save()
            transferred = self.api.upload_media(str(path), lines=self.config['line'])
            filename = transferred.get('filename')
            if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', filename):
                raise UploadError('upload_remote_identifier_invalid')
            current.update(status='uploaded', filename=filename)
            journal.save()
        self.check_identity()
        new_part = {'filename': current['filename'],
                    'title': part_title if part_title is not None else 'P' + str(index), 'desc': ''}
        if remote:
            payload = asdict(remote)
            payload.pop('extra_kwargs', None)
        else:
            payload = copy.deepcopy(journal.data['metadata'])
        payload['bvid'] = journal.data['bvid']
        payload['videos'] = ([{k: v for k, v in p.items() if k in ('filename', 'title', 'desc', 'cid')}
                              for p in remote.videos] if remote else []) + [new_part]
        current.update(status='submitting', expected_filenames=[v['filename'] for v in payload['videos']],
                       operation_id=uuid.uuid4().hex, intent_at=datetime.now(SHANGHAI).isoformat(),
                       operation='append' if journal.data['bvid'] else 'create',
                       elapsed_seconds=time.monotonic() - started)
        # 此次读回持久化失败则绝不发送 POST。
        journal.save()
        try:
            response = self.api.submit_web(payload, edit=bool(journal.data['bvid']))
            if not isinstance(response, dict) or type(response.get('code')) is not int:
                raise UploadError('upload_submit_unresolved')
            if response['code'] != 0:
                # 服务端明确拒绝，不能在锁定/删除后退回新建。
                current.update(status='failed', error='upload_submit_rejected', submission_rejected=True)
                journal.save()
                return journal.result(key)
            result = response.get('data', {})
            bvid = result.get('bvid') or journal.data['bvid']
            if not isinstance(bvid, str) or not BVID.fullmatch(bvid):
                raise UploadError('upload_submit_unresolved')
            journal.data['bvid'] = bvid
            aid = result.get('aid')
            if isinstance(aid, int) and aid > 0:
                journal.data['aid'] = aid
            journal.save()
        except Exception:
            current.update(status='unknown', error='upload_submit_unresolved')
            journal.save()
        try:
            confirmed = self.reconcile(journal, key)
        except Exception:
            confirmed = False
        if not confirmed:
            current.update(status='unknown', error='upload_submit_unresolved')
        current['elapsed_seconds'] = time.monotonic() - started
        journal.save()
        return journal.result(key)
