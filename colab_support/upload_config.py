"""私有投稿配置校验；导入时不加载 DMR 或登录工具。"""
import json
import math
from pathlib import Path

import yaml

UPLOAD_KEYS = {'enabled', 'account', 'expected_uid', 'min_length', 'line', 'limit',
               'copyright', 'source', 'tid', 'title', 'desc', 'tag', 'dynamic', 'cover',
               'no_reprint', 'dolby', 'hires', 'is_only_self', 'charging_pay',
               'open_subtitle', 'part_timeout', 'drain_timeout'}


def load_upload_config(path):
    path = Path(path)
    if not path.exists():
        return {'enabled': False}
    try:
        value = yaml.safe_load(path.read_text(encoding='utf-8'))
    except Exception:
        raise ValueError('upload_config_unreadable') from None
    if not isinstance(value, dict) or set(value) - UPLOAD_KEYS:
        raise ValueError('upload_config_invalid')
    result = dict(value)
    result.setdefault('enabled', False)
    if not isinstance(result['enabled'], bool):
        raise ValueError('upload_enabled_invalid')
    if not result['enabled']:
        return result
    for key in ('account', 'title', 'desc'):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError('upload_' + key + '_required')
    for key in ('expected_uid', 'tid'):
        value = result.get(key)
        if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
            raise ValueError('upload_' + key + '_required')
        result[key] = int(value)
    if type(result.get('copyright')) is not int or result['copyright'] not in (1, 2):
        raise ValueError('upload_copyright_required')
    if not isinstance(result.get('source'), str):
        raise ValueError('upload_source_required')
    if result['copyright'] == 2 and not result['source'].strip():
        raise ValueError('upload_repost_source_required')
    tags = result.get('tag')
    if not isinstance(tags, (str, list)) or not tags or (isinstance(tags, list) and
            any(not isinstance(tag, str) or not tag.strip() for tag in tags)):
        raise ValueError('upload_tags_required')
    if isinstance(tags, str) and not tags.strip():
        raise ValueError('upload_tags_required')
    for key, default in (('min_length', 120), ('part_timeout', 7200), ('drain_timeout', 7200)):
        value = result.setdefault(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('upload_' + key + '_invalid')
    if result['min_length'] < 120:
        raise ValueError('upload_min_length_below_120')
    limit = result.setdefault('limit', 3)
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError('upload_limit_invalid')
    if result.setdefault('line', 'AUTO') not in {'AUTO', 'alia', 'bda', 'bda2', 'bldsa',
            'qn', 'tx', 'txa', 'jd-alia', 'jd-bd', 'jd-bldsa', 'jd-tx', 'jd-txa'}:
        raise ValueError('upload_line_invalid')
    for key in ('cover', 'dynamic'):
        if key in result and not isinstance(result[key], str):
            raise ValueError('upload_' + key + '_invalid')
    for key in ('no_reprint', 'dolby', 'hires', 'is_only_self', 'charging_pay'):
        if key in result and (type(result[key]) is not int or result[key] not in (0, 1)):
            raise ValueError('upload_' + key + '_invalid')
    if 'open_subtitle' in result and not isinstance(result['open_subtitle'], bool):
        raise ValueError('upload_subtitle_invalid')
    return result


def load_upload_cookie(path):
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        cookies = payload['cookie_info']['cookies']
        if not isinstance(cookies, list) or not cookies:
            raise ValueError()
        values = {}
        for item in cookies:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('value'), str):
                raise ValueError()
            values[item['name']] = item['value']
        if not values.get('SESSDATA') or not values.get('bili_jct'):
            raise ValueError()
        return values
    except Exception:
        raise ValueError('upload_cookie_invalid') from None
