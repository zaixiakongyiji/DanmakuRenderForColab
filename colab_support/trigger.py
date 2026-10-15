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

from .drive_authorization import DriveAuthorizationWatcher, RUN_ALL_BUTTON, click_drive_dialog

DEFAULT_NOTEBOOK_URL = (
    "https://colab.research.google.com/github/zaixiakongyiji/"
    "DanmakuRenderForColab/blob/codex/colab-poc/notebooks/colab_record.ipynb"
)
TRIGGER_SCHEMA_VERSION = 2
TRIGGER_CLAIM_SECONDS = 900
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
ACK_STATES = {"accepted", "running", "draining", "success", "failed"}
CONNECTED_BUTTON = re.compile(
    r'^(已连接|已連線|已連接|Connected)(\s|$)|RAM.*(磁盘|磁碟|硬碟|Disk)|'
    r'(显示|顯示).*(RAM|内存|記憶體).*使用|Show.*RAM.*usage', re.I)
CONNECT_BUTTON = re.compile(
    r'^(连接|連線|連接|Connect|重新连接|重新連線|重新連接|Reconnect|连接到托管运行时|'
    r'連線至託管執行階段|連線至代管執行階段|Connect to hosted runtime)(\s.*)?$', re.I)


def validate_ack(payload, run_id, now=None):
    if (not isinstance(payload, dict) or payload.get('schema_version') != TRIGGER_SCHEMA_VERSION
            or payload.get('run_id') != run_id or payload.get('status') not in ACK_STATES):
        raise ValueError('ack_invalid')
    try:
        updated = datetime.fromisoformat(payload['updated_at'].replace('Z', '+00:00'))
        if updated.tzinfo is None or updated > (now or _utc_now()) + timedelta(seconds=60):
            raise ValueError()
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ValueError('ack_invalid') from None
    return updated


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
        if created.tzinfo is None or expires <= created or expires - created > timedelta(seconds=TRIGGER_CLAIM_SECONDS):
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


def write_ack(path: Path, run_id: str, status: str, error: Optional[str] = None, *, phase=None) -> None:
    if status not in ACK_STATES:
        raise ValueError("ack_status_invalid")
    value = {"schema_version": TRIGGER_SCHEMA_VERSION, "run_id": run_id,
             "status": status, "updated_at": _utc_now().isoformat()}
    if error:
        value["error"] = error
    if phase:
        value['phase'] = phase
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
    notebook_url: Optional[str] = None

    @classmethod
    def load(cls, path: Path, room_url: str) -> "TriggerState":
        if not Path(path).exists():
            return cls(room_url=room_url)
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if payload.get('schema_version') != TRIGGER_SCHEMA_VERSION:
                raise ValueError()
            if payload.get('notebook_url') is not None and (
                    not isinstance(payload['notebook_url'], str) or not payload['notebook_url'].strip()):
                raise ValueError()
            valid_statuses = {'unknown', 'offline', 'probe_unavailable', 'live', 'dispatching',
                    'needs_attention', 'opened', 'accepted', 'running', 'draining', 'success', 'failed', 'ended'}
            if payload.get('room_url') != room_url:
                is_active = payload.get('triggered') or payload.get('status') in {
                    'dispatching', 'opened', 'accepted', 'running', 'draining'
                }
                if is_active:
                    if (payload.get('status') not in valid_statuses
                            or type(payload.get('triggered')) is not bool
                            or any(type(payload.get(k)) is not int or payload[k] < 0
                                   for k in ('live_streak', 'offline_streak'))):
                        raise ValueError()
                    if payload.get('run_id') is not None and (
                            not isinstance(payload['run_id'], str)
                            or not RUN_ID_RE.fullmatch(payload['run_id'])):
                        raise ValueError()
                    allowed = {field for field in cls.__dataclass_fields__}
                    state = cls(**{key: value for key, value in payload.items() if key in allowed})
                    if state.triggered and not state.run_id:
                        raise ValueError()
                    if state.run_id and (
                            not state.triggered_at
                            or datetime.fromisoformat(state.triggered_at).tzinfo is None):
                        raise ValueError()
                    state.room_url = room_url
                    state.status, state.last_error = 'needs_attention', 'active_run_conflict'
                    return state
                return cls(room_url=room_url)
            if type(payload.get('triggered')) is not bool or any(type(payload.get(k)) is not int or payload[k] < 0
                    for k in ('live_streak', 'offline_streak')):
                raise ValueError()
            if payload.get('run_id') is not None and (not isinstance(payload['run_id'], str) or not RUN_ID_RE.fullmatch(payload['run_id'])):
                raise ValueError()
            allowed = {field for field in cls.__dataclass_fields__}
            state = cls(**{key: value for key, value in payload.items() if key in allowed})
            if state.status not in valid_statuses:
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
                 auto_run: bool = False, cdp_url: Optional[str] = None, google_account: str = ''):
        self.notebook_url = notebook_url
        self.drive_sync_root = Path(drive_sync_root) if drive_sync_root else None
        self.open_browser = open_browser
        self.browser = browser
        self.auto_run = auto_run
        self.cdp_url = cdp_url
        self.google_account = google_account
        self.authorization = None
        self._pending_run = None
        if auto_run:
            if not self.drive_sync_root or not open_browser:
                raise ValueError("auto_run_requires_drive_and_browser")
            validate_cdp(cdp_url)

    def launch(self, room_url: str, run_id: str, triggered_at: str) -> str:
        if self.drive_sync_root:
            request, claim, ack = control_paths(self.drive_sync_root, run_id)
            if request.exists() or claim.exists() or ack.exists():
                raise RuntimeError('trigger_request_exists')
            expires = (datetime.fromisoformat(triggered_at) + timedelta(seconds=TRIGGER_CLAIM_SECONDS)).isoformat()
            value = validate_trigger_request({'schema_version': TRIGGER_SCHEMA_VERSION,
                'room_url': room_url, 'run_id': run_id, 'triggered_at': triggered_at,
                'expires_at': expires, 'source': 'local_colab_trigger'},
                now=datetime.fromisoformat(triggered_at))
            atomic_json(request, value)
            atomic_json(self.drive_sync_root / 'DMRColab/control/active.json',
                        {'schema_version': TRIGGER_SCHEMA_VERSION, 'run_id': run_id})
        if self.open_browser and self.auto_run:
            self._pending_run = (run_id, datetime.fromisoformat(triggered_at).timestamp() + TRIGGER_CLAIM_SECONDS)
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
                # 等待“全部运行”按钮渲染可见，避免 SPA 加载时差误判
                try:
                    btn = page.get_by_role('button', name=RUN_ALL_BUTTON)
                    btn.first.wait_for(state="visible", timeout=30000)
                except Exception:
                    pass
                if not self._click_run_all(page):
                    return False
                # 监听不阻塞监控主线程，认领回执到达或请求过期时停止。
                self.try_authorize_drive()
                return True
        except Exception:
            return False

    def restore_notebook(self, notebook_url):
        """显示现有任务的原页面，只导航，绝不重放全部运行。"""
        if not self.auto_run or not self.cdp_url:
            return False
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
                pages = [p for context in browser.contexts for p in context.pages]
                base = notebook_url.split('#', 1)[0]
                page = next((p for p in pages if p.url.split('#', 1)[0] == base), None)
                if page is None:
                    if not browser.contexts:
                        return False
                    # 新启动的专用浏览器通常只有空白标签，直接复用。
                    page = next((p for p in pages if p.url == 'about:blank'), None)
                    if page is None:
                        page = browser.contexts[0].new_page()
                    page.goto(notebook_url, wait_until='domcontentloaded', timeout=30000)
                page.get_by_role('button', name=RUN_ALL_BUTTON).first.wait_for(
                    state='visible', timeout=30000)
                page.bring_to_front()
                return page.url.split('#', 1)[0] == base
        except Exception:
            return False

    @staticmethod
    def _click_authorize_drive(page) -> bool:
        try:
            return click_drive_dialog(page)
        except Exception:
            return False

    def try_authorize_drive(self) -> bool:
        """启动或恢复专用监听线程，由该线程每两秒扫描一次。"""
        if not self.auto_run or not self.cdp_url or self._pending_run is None:
            return False
        run_id, expires = self._pending_run
        if time.time() >= expires:
            return False
        if self.authorization is None or not self.authorization.thread.is_alive():
            ack_path = control_paths(self.drive_sync_root, run_id)[2]
            self.authorization = DriveAuthorizationWatcher(self.notebook_url, self.cdp_url,
                self.google_account, expires, ack_path, run_id)
            self.authorization.start()
        return True

    def close(self):
        self._pending_run = None
        if self.authorization is not None:
            if self.authorization.stop():
                self.authorization = None
            else:
                self.authorization.error = 'authorization_stop_timeout'

    @staticmethod
    def _click_run_all(page):
        # 只对已确认空闲页面点击一次；授权、忙碌和无法辨别的状态交给用户。
        # 不搜索整页正文，Notebook 源码自身可能含 running 等状态字样。
        text = '\n'.join(page.get_by_role('alert').all_text_contents() +
                         page.get_by_role('status').all_text_contents())
        if re.search(r'需要授权|需要授權|授权访问|授權存取|正在执行|正在執行|运行中|執行中|连接中|連線中|重新连接|重新連線|重新連接|Sign in|Authorize|Reconnect|Connecting|Running', text, re.I):
            return False
        login = page.get_by_role('button', name=re.compile(r'^(登录|登入|Sign in)$', re.I))
        if login.count() and login.first.is_visible():
            return False
        if page.get_by_role('dialog').count():
            return False
        stop = page.get_by_role('button', name=re.compile(r'停止执行|中断执行|停止執行|中斷執行|Interrupt execution|Stop execution', re.I))
        if stop.count():
            return False
        button = page.get_by_role('button', name=RUN_ALL_BUTTON)
        if button.count() != 1 or not button.is_enabled() or not button.is_visible():
            return False
        button.click(timeout=5000)
        return True

    def runtime_connection_state(self, notebook_url=None, *, reopen_missing=False):
        """返回 True=明确连接、False=明确未连接、None=无法判断。"""
        if not self.auto_run or not self.cdp_url:
            return None
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
                pages = [p for context in browser.contexts for p in context.pages]
                target = notebook_url or self.notebook_url
                base = target.split('#', 1)[0]
                matches = [p for p in pages if p.url.split('#', 1)[0] == base]
                if not matches:
                    if not reopen_missing or not browser.contexts:
                        return None
                    # 关闭标签不代表运行时已释放；恢复时重开原地址，仅检查状态。
                    page = browser.contexts[0].new_page()
                    page.goto(target, wait_until='domcontentloaded', timeout=30000)
                    matches = [page]
                deadline = time.monotonic() + 15
                while True:
                    states = [self._connection_state(page) for page in matches]
                    if any(state is True for state in states):
                        return True
                    if all(state is False for state in states):
                        return False
                    if time.monotonic() >= deadline:
                        return None
                    matches[0].wait_for_timeout(250)
        except Exception:
            return None

    @staticmethod
    def _connection_state(page):
        if page.get_by_role('dialog').count():
            return None
        connected = page.get_by_role('button', name=CONNECTED_BUTTON)
        if connected.count() == 1 and connected.is_visible():
            return True
        connect = page.get_by_role('button', name=CONNECT_BUTTON)
        if connect.count() == 1 and connect.is_visible() and connect.is_enabled():
            return False
        try:
            connect_btn = page.locator('colab-connect-button #connect, #connect')
            if connect_btn.count() == 1 and connect_btn.first.is_visible():
                text = connect_btn.first.inner_text() or ''
                tooltip = connect_btn.first.get_attribute('tooltiptext') or ''
                for label in (text, tooltip):
                    label = label.strip()
                    if CONNECTED_BUTTON.search(label):
                        return True
                    if CONNECT_BUTTON.search(label):
                        return False
        except Exception:
            pass
        return None


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

    def resume_pending(self):
        """监控重启时显示待认领或仍活动的原任务，不发请求或重复点击。"""
        state = self.state
        if (not self.launcher.auto_run or not state.triggered or not state.run_id
                or state.status not in {'opened', 'accepted', 'running', 'draining', 'needs_attention'}
                or state.last_error in {'active_run_conflict', 'trigger_state_invalid', 'launch_state_unknown'}):
            return
        stamp = datetime.fromtimestamp(self.now(), timezone.utc)
        self._read_ack(stamp)
        # 认领期限只约束尚未启动的请求；活动任务即使超过该期限也需显示页面。
        if state.ack_status in {'accepted', 'running', 'draining'}:
            restored = self.launcher.restore_notebook(state.notebook_url or self.launcher.notebook_url)
            state.save(self.state_file)
            return restored
        if state.ack_status is not None or state.last_error not in {None, 'pending_notebook_unavailable'}:
            return
        age = (stamp - datetime.fromisoformat(state.triggered_at)).total_seconds()
        if not 0 <= age < TRIGGER_CLAIM_SECONDS:
            return
        restored = self.launcher.restore_notebook(state.notebook_url or self.launcher.notebook_url)
        if restored:
            state.status, state.last_error = 'opened', None
        else:
            state.status, state.last_error = 'needs_attention', 'pending_notebook_unavailable'
        state.save(self.state_file)
        return restored

    def recover_expired(self):
        """显式恢复未认领的过期请求；调用方须持有监控状态进程锁。"""
        state = self.state
        if (state.status not in {'opened', 'needs_attention'} or state.last_error not in {None, 'claim_timeout'}
                or not state.triggered or not state.run_id or state.ack_status is not None):
            raise ValueError('恢复失败：只有认领超时且未收到回执的请求可以恢复。')
        if not self.launcher.drive_sync_root:
            raise ValueError('恢复失败：需要可读取的 Drive 同步目录。')
        try:
            created = datetime.fromisoformat(state.triggered_at)
            now = datetime.fromtimestamp(self.now(), timezone.utc)
            request, claim, ack = control_paths(self.launcher.drive_sync_root, state.run_id)
            payload = validate_trigger_request(json.loads(request.read_text(encoding='utf-8')), created)
            if (payload['run_id'] != state.run_id or payload['room_url'] != self.room_url
                    or datetime.fromisoformat(payload['triggered_at']) != created):
                raise ValueError()
            if datetime.fromisoformat(payload['expires_at']) > now:
                raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            raise ValueError('恢复失败：无法核实旧请求已过期，请检查 Drive 同步和请求文件。') from None
        def require_unclaimed():
            for path in (claim, ack):
                try:
                    path.stat()
                except FileNotFoundError:
                    continue
                except OSError:
                    raise ValueError('恢复失败：无法检查认领和回执文件，请检查 Drive 同步。') from None
                raise ValueError('恢复失败：旧请求已有认领或回执，不能按未启动任务重置。')

        require_unclaimed()
        connected = self.launcher.runtime_connection_state(
            notebook_url=state.notebook_url, reopen_missing=True)
        if connected is True:
            raise ValueError('恢复失败：旧 Notebook 仍连接，请先检查旧任务并断开运行时，再执行 --recover-expired。')
        if connected is not False:
            raise ValueError('恢复失败：无法确认旧 Notebook 状态，请在专用浏览器检查原页面的登录、弹窗和连接状态，再执行 --recover-expired。')
        self.launcher.close()
        if self.launcher.authorization is not None:
            raise ValueError('恢复失败：旧授权监听尚未退出。')
        require_unclaimed()
        # 仅归档本地状态，保留 Drive 的过期请求供核对；不延长有效期。
        archive = self.state_file.with_name(
            self.state_file.stem + '.expired-' + state.run_id + '.bak.json')
        try:
            with archive.open('x', encoding='utf-8') as stream:
                stream.write(self.state_file.read_text(encoding='utf-8'))
            fresh = TriggerState(self.room_url)
            fresh.save(self.state_file)
        except OSError:
            raise ValueError('恢复失败：无法归档本地状态，请检查备份文件和目录权限。') from None
        self.state = fresh
        return archive

    def observe(self, status: Optional[bool]) -> TriggerResult:
        stamp = datetime.fromtimestamp(self.now(), timezone.utc)
        self.state.last_poll_at = stamp.isoformat()
        if status is None:
            self.state.live_streak = self.state.offline_streak = 0
        self._read_ack(stamp)
        terminal = self.state.ack_status in {'success', 'failed'}
        if self.state.triggered or self.state.run_id:
            if terminal:
                if self.state.status == 'needs_attention':
                    self.state.save(self.state_file)
                    return TriggerResult('needs_attention', run_id=self.state.run_id, error=self.state.last_error)
                self.state.offline_streak = self.state.offline_streak + 1 if status is False else 0
                if self.state.offline_streak >= self.offline_confirmations:
                    self.state = TriggerState(self.room_url, status='ended')
                self.state.save(self.state_file)
                return TriggerResult(self.state.status, run_id=self.state.run_id, error=self.state.last_error)
            if self.state.triggered_at and self.state.ack_status is None:
                try:
                    age = (stamp - datetime.fromisoformat(self.state.triggered_at)).total_seconds()
                    if age >= TRIGGER_CLAIM_SECONDS:
                        self.launcher.close()
                        self.state.status, self.state.last_error = 'needs_attention', 'claim_timeout'
                    elif self.state.status in {'opened', 'needs_attention'} and self.state.last_error not in {
                            'launch_state_unknown', 'active_run_conflict', 'ack_invalid', 'trigger_state_invalid'}:
                        self.launcher._pending_run = (self.state.run_id,
                            datetime.fromisoformat(self.state.triggered_at).timestamp() + TRIGGER_CLAIM_SECONDS)
                        self.launcher.try_authorize_drive()
                        auth = self.launcher.authorization
                        if auth is not None and auth.error:
                            self.state.status, self.state.last_error = 'needs_attention', auth.error
                        elif self.state.last_error and self.state.last_error.startswith(('google_', 'authorization_')):
                            self.state.status, self.state.last_error = 'opened', None
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
                self.state.notebook_url = self.launcher.notebook_url.split('#', 1)[0]
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
            updated = validate_ack(ack, run_id, now)
            self.state.ack_status = ack['status']
            self.launcher.close()
            self.state.status, self.state.last_error = ack['status'], ack.get('error')
            if ack.get('error') and ack['status'] not in {'success', 'failed'}:
                self.state.status = 'needs_attention'
            if ack['status'] in {'success', 'failed'} and self.launcher.auto_run:
                release = ack.get('runtime_release', {})
                if not isinstance(release, dict):
                    raise ValueError()
                if release.get('status') == 'unknown':
                    self.state.status, self.state.last_error = 'needs_attention', 'runtime_release_unknown'
                elif release.get('status') != 'requested' or (now - updated).total_seconds() < 60:
                    # 给最终清单落盘、释放请求和 UI 状态变化留出时间，期间不重新武装。
                    self.state.status, self.state.last_error = 'needs_attention', 'runtime_release_pending'
                    if (now - updated).total_seconds() >= 60:
                        self.state.last_error = 'runtime_release_unknown'
                elif self.launcher.runtime_connection_state(notebook_url=self.state.notebook_url) is not False:
                    self.state.status, self.state.last_error = 'needs_attention', 'runtime_release_unconfirmed'
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
