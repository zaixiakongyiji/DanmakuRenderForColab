"""本地开播检测、Colab 请求和运行回执。"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
import webbrowser
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

DEFAULT_NOTEBOOK_URL = (
    "https://colab.research.google.com/github/zaixiakongyiji/"
    "DanmakuRenderForColab/blob/codex/colab-poc/notebooks/colab_record.ipynb"
)
TRIGGER_SCHEMA_VERSION = 2
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
ACK_STATES = {"accepted", "running", "draining", "success", "failed"}


def control_paths(root, run_id):
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError('trigger_run_id_invalid')
    folder = Path(root) / 'DMRColab/control/runs' / run_id
    if not folder.resolve().is_relative_to((Path(root) / 'DMRColab/control').resolve()):
        raise ValueError('trigger_path_invalid')
    return folder / 'request.json', folder / 'claim.json', folder / 'ack.json'


def validate_cdp(url):
    parsed = urlparse(url or '')
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or not parsed.port or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise ValueError('auto_run_requires_loopback_cdp')
    return url


def claim_request(root, payload, now=None):
    payload = validate_trigger_request(payload, now)
    request, claim, ack = control_paths(root, payload['run_id'])
    if not request.exists() or json.loads(request.read_text(encoding='utf-8')) != payload:
        raise ValueError('trigger_request_mismatch')
    if ack.exists():
        raise ValueError('trigger_already_acknowledged')
    claim.parent.mkdir(parents=True, exist_ok=True)
    # 单运行时约定：Drive 文件认领不提供跨机器分布式锁。
    with claim.open('x', encoding='utf-8') as stream:
        json.dump({'schema_version': TRIGGER_SCHEMA_VERSION, 'run_id': payload['run_id'],
                   'claimed_at': (now or _utc_now()).isoformat()}, stream)
    write_ack(ack, payload['run_id'], 'accepted')
    return ack


def read_active_request(root, now=None):
    control = Path(root) / 'DMRColab/control'
    active = control / 'active.json'
    if not active.exists():
        return None
    value = json.loads(active.read_text(encoding='utf-8'))
    if value.get('schema_version') != TRIGGER_SCHEMA_VERSION:
        raise ValueError('trigger_schema_version_invalid')
    request, _, _ = control_paths(root, value.get('run_id'))
    return validate_trigger_request(json.loads(request.read_text(encoding='utf-8')), now)


def validate_room_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc.lower() != "live.bilibili.com":
        raise ValueError("只支持 https://live.bilibili.com/<房间号>")
    room_id = parsed.path.strip("/")
    if not room_id.isdigit() or parsed.query or parsed.fragment:
        raise ValueError("B站直播间链接必须是无 query/fragment 的数字房间号")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_run_id(now: Optional[datetime] = None) -> str:
    now = now or _utc_now()
    return now.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def validate_trigger_request(payload: dict, now: Optional[datetime] = None) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != TRIGGER_SCHEMA_VERSION:
        raise ValueError("trigger_schema_version_invalid")
    room_url = validate_room_url(str(payload.get("room_url", "")))
    run_id = str(payload.get("run_id", ""))
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("trigger_run_id_invalid")
    try:
        expires = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("trigger_expiry_invalid") from None
    if expires.tzinfo is None:
        raise ValueError("trigger_expiry_invalid")
    try:
        created = datetime.fromisoformat(str(payload['triggered_at']).replace('Z', '+00:00'))
        if created.tzinfo is None or expires <= created or expires - created > timedelta(minutes=15):
            raise ValueError()
        if created > (now or _utc_now()) + timedelta(seconds=60):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('trigger_time_invalid') from None
    if expires <= (now or _utc_now()):
        raise ValueError("trigger_expired")
    return {"schema_version": TRIGGER_SCHEMA_VERSION, "room_url": room_url,
            "run_id": run_id, "triggered_at": str(payload.get("triggered_at", "")),
            "expires_at": expires.isoformat(), "source": str(payload.get("source", ""))}


def write_ack(path: Path, run_id: str, status: str, error: Optional[str] = None) -> None:
    if status not in ACK_STATES:
        raise ValueError("ack_status_invalid")
    value = {"schema_version": TRIGGER_SCHEMA_VERSION, "run_id": run_id,
             "status": status, "updated_at": _utc_now().isoformat()}
    if error:
        value["error"] = error
    atomic_json(Path(path), value)


@dataclass
class TriggerState:
    room_url: str
    status: str = "unknown"
    live_streak: int = 0
    offline_streak: int = 0
    triggered: bool = False
    run_id: Optional[str] = None
    triggered_at: Optional[str] = None
    last_poll_at: Optional[str] = None
    last_error: Optional[str] = None
    ack_status: Optional[str] = None

    @classmethod
    def load(cls, path: Path, room_url: str) -> "TriggerState":
        if not Path(path).exists():
            return cls(room_url=room_url)
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if payload.get('room_url') != room_url or payload.get('schema_version') != TRIGGER_SCHEMA_VERSION:
                raise ValueError()
            if type(payload.get('triggered')) is not bool or any(type(payload.get(k)) is not int or payload[k] < 0
                    for k in ('live_streak', 'offline_streak')):
                raise ValueError()
            if payload.get('run_id') is not None and (not isinstance(payload['run_id'], str) or not RUN_ID_RE.fullmatch(payload['run_id'])):
                raise ValueError()
            allowed = {field for field in cls.__dataclass_fields__}
            state = cls(**{key: value for key, value in payload.items() if key in allowed})
            if state.status not in {'unknown', 'offline', 'probe_unavailable', 'live', 'dispatching',
                    'needs_attention', 'opened', 'accepted', 'running', 'draining', 'success', 'failed', 'ended'}:
                raise ValueError()
            if state.triggered and not state.run_id:
                raise ValueError()
            if state.run_id and (not state.triggered_at or datetime.fromisoformat(state.triggered_at).tzinfo is None):
                raise ValueError()
            if state.status == 'dispatching':
                state.status, state.last_error = 'needs_attention', 'launch_state_unknown'
            return state
        except (OSError, ValueError, TypeError, AttributeError):
            return cls(room_url=room_url, status='needs_attention', last_error='trigger_state_invalid')

    def save(self, path: Path) -> None:
        atomic_json(path, dict(asdict(self), schema_version=TRIGGER_SCHEMA_VERSION))


@dataclass(frozen=True)
class TriggerResult:
    state: str
    triggered: bool = False
    run_id: Optional[str] = None
    launch_url: Optional[str] = None
    error: Optional[str] = None


class ColabLauncher:
    """写入私有请求文件并打开 Notebook；自动点击必须使用专用 loopback CDP。"""

    def __init__(self, notebook_url: str = DEFAULT_NOTEBOOK_URL,
                 drive_sync_root: Optional[Path] = None,
                 open_browser: bool = True,
                 browser: Callable[[str], bool] = webbrowser.open_new_tab,
                 auto_run: bool = False, cdp_url: Optional[str] = None):
        self.notebook_url = notebook_url
        self.drive_sync_root = Path(drive_sync_root) if drive_sync_root else None
        self.open_browser = open_browser
        self.browser = browser
        self.auto_run = auto_run
        self.cdp_url = cdp_url
        if auto_run:
            if not self.drive_sync_root or not open_browser:
                raise ValueError("auto_run_requires_drive_and_browser")
            validate_cdp(cdp_url)

    def launch(self, room_url: str, run_id: str, triggered_at: str) -> str:
        if self.drive_sync_root:
            request, claim, ack = control_paths(self.drive_sync_root, run_id)
            if request.exists() or claim.exists() or ack.exists():
                raise RuntimeError('trigger_request_exists')
            expires = (datetime.fromisoformat(triggered_at) + timedelta(minutes=15)).isoformat()
            value = validate_trigger_request({'schema_version': TRIGGER_SCHEMA_VERSION,
                'room_url': room_url, 'run_id': run_id, 'triggered_at': triggered_at,
                'expires_at': expires, 'source': 'local_colab_trigger'},
                now=datetime.fromisoformat(triggered_at))
            atomic_json(request, value)
            atomic_json(self.drive_sync_root / 'DMRColab/control/active.json',
                        {'schema_version': TRIGGER_SCHEMA_VERSION, 'run_id': run_id})
        if self.open_browser and self.auto_run:
            if not self._auto_run_browser():
                raise RuntimeError("browser_auto_run_needs_attention")
        elif self.open_browser and not self.browser(self.notebook_url):
            raise RuntimeError("browser_open_failed")
        return self.notebook_url

    def _auto_run_browser(self) -> bool:
        if not self.cdp_url:
            raise RuntimeError("auto_run_requires_cdp_url")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_url)
                pages = [p for context in browser.contexts for p in context.pages]
                base = self.notebook_url.split("#", 1)[0]
                page = next((p for p in pages if p.url.split("#", 1)[0] == base), None)
                if page is None:
                    page = browser.contexts[0].new_page()
                    page.goto(self.notebook_url, wait_until="domcontentloaded")
                return self._click_run_all(page)
        except Exception:
            return False

    @staticmethod
    def _click_run_all(page):
        # 只对已确认空闲页面点击一次；授权、忙碌和无法辨别的状态交给用户。
        # 不搜索整页正文，Notebook 源码自身可能含 running 等状态字样。
        text = '\n'.join(page.get_by_role('alert').all_text_contents() +
                         page.get_by_role('status').all_text_contents())
        if re.search(r'需要授权|授权访问|正在执行|运行中|连接中|重新连接|Sign in|Authorize|Reconnect|Connecting|Running', text, re.I):
            return False
        login = page.get_by_role('button', name=re.compile(r'^(登录|Sign in)$', re.I))
        if login.count() and login.first.is_visible():
            return False
        if page.get_by_role('dialog').count():
            return False
        runtime = page.get_by_role('button', name=re.compile(
            r'已连接|Connected|RAM.*(磁盘|Disk)|显示.*(RAM|内存).*使用|Show.*RAM.*usage', re.I))
        if runtime.count() != 1 or not runtime.is_visible():
            return False
        stop = page.get_by_role('button', name=re.compile(r'停止执行|中断执行|Interrupt execution|Stop execution', re.I))
        if stop.count():
            return False
        button = page.get_by_role('button', name=re.compile(r'^(全部运行|Run all)(\s|$)', re.I))
        if button.count() != 1 or not button.is_enabled() or not button.is_visible():
            return False
        button.click(timeout=5000)
        return True


class TriggerMonitor:
    def __init__(self, room_url: str, launcher: ColabLauncher,
                 state_file: Path = Path(".temp/colab-trigger-state.json"),
                 live_confirmations: int = 2, offline_confirmations: int = 3,
                 now: Callable[[], float] = time.time):
        self.room_url = validate_room_url(room_url)
        if live_confirmations <= 0 or offline_confirmations <= 0:
            raise ValueError("确认次数必须为正数")
        self.launcher = launcher
        self.state_file = Path(state_file)
        self.live_confirmations = live_confirmations
        self.offline_confirmations = offline_confirmations
        self.now = now
        self.state = TriggerState.load(self.state_file, self.room_url)
        self.state.save(self.state_file)

    def observe(self, status: Optional[bool]) -> TriggerResult:
        stamp = datetime.fromtimestamp(self.now(), timezone.utc)
        self.state.last_poll_at = stamp.isoformat()
        if status is None:
            self.state.live_streak = self.state.offline_streak = 0
        self._read_ack(stamp)
        terminal = self.state.ack_status in {'success', 'failed'}
        if self.state.triggered or self.state.run_id:
            if terminal:
                self.state.offline_streak = self.state.offline_streak + 1 if status is False else 0
                if self.state.offline_streak >= self.offline_confirmations:
                    self.state = TriggerState(self.room_url, status='ended')
                self.state.save(self.state_file)
                return TriggerResult(self.state.status, run_id=self.state.run_id, error=self.state.last_error)
            if self.state.triggered_at and self.state.ack_status is None:
                try:
                    age = (stamp - datetime.fromisoformat(self.state.triggered_at)).total_seconds()
                    if age >= 900:
                        self.state.status, self.state.last_error = 'needs_attention', 'claim_timeout'
                except ValueError:
                    self.state.status, self.state.last_error = 'needs_attention', 'trigger_state_invalid'
            self.state.save(self.state_file)
            return TriggerResult(self.state.status, run_id=self.state.run_id, error=self.state.last_error)
        if self.state.status == 'needs_attention':
            return TriggerResult('needs_attention', error=self.state.last_error)
        if status is True:
            self.state.status = 'live'
            self.state.live_streak += 1
            self.state.offline_streak = 0
            if self.state.live_streak >= self.live_confirmations:
                run_id = make_run_id(stamp)
                self.state.status, self.state.run_id = 'dispatching', run_id
                self.state.triggered_at = stamp.isoformat()
                # 保存不可自动重试的启动意图，再调用浏览器。
                self.state.save(self.state_file)
                try:
                    launch_url = self.launcher.launch(self.room_url, run_id, stamp.isoformat())
                except Exception:
                    self.state.status, self.state.last_error = 'needs_attention', 'launch_state_unknown'
                    self.state.save(self.state_file)
                    return TriggerResult('needs_attention', run_id=run_id, error=self.state.last_error)
                self.state.triggered, self.state.status = True, 'opened'
                self.state.save(self.state_file)
                return TriggerResult('opened', True, run_id, launch_url)
        elif status is False:
            self.state.status, self.state.live_streak = 'offline', 0
        else:
            self.state.status, self.state.last_error = 'probe_unavailable', 'probe_unavailable'
        self.state.save(self.state_file)
        return TriggerResult(self.state.status, run_id=self.state.run_id, error=self.state.last_error)

    def _read_ack(self, now):
        root, run_id = self.launcher.drive_sync_root, self.state.run_id
        if not root or not run_id:
            return
        path = control_paths(root, run_id)[2]
        if not path.exists():
            return
        try:
            ack = json.loads(path.read_text(encoding='utf-8'))
            if ack.get('schema_version') != TRIGGER_SCHEMA_VERSION or ack.get('run_id') != run_id or ack.get('status') not in ACK_STATES:
                raise ValueError()
            updated = datetime.fromisoformat(ack['updated_at'].replace('Z', '+00:00'))
            if updated.tzinfo is None or updated > now + timedelta(seconds=60):
                raise ValueError()
            self.state.ack_status = ack['status']
            self.state.status, self.state.last_error = ack['status'], ack.get('error')
            if ack.get('error') and ack['status'] not in {'success', 'failed'}:
                self.state.status = 'needs_attention'
            if ack['status'] not in {'success', 'failed'} and (now - updated).total_seconds() > 300:
                self.state.status, self.state.last_error = 'needs_attention', 'runtime_heartbeat_stale'
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.state.ack_status = None
            self.state.status, self.state.last_error = 'needs_attention', 'ack_invalid'


def probe_bilibili(room_url: str) -> Optional[bool]:
    try:
        from DMR.LiveAPI import LiveAPI
        return LiveAPI(validate_room_url(room_url)).Onair()
    except Exception:
        return None
