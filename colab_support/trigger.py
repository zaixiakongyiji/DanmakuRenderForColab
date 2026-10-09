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
TRIGGER_SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
ACK_STATES = {"accepted", "running", "draining", "success", "failed"}


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
    return now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


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
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if payload.get("room_url") != room_url:
                return cls(room_url=room_url)
            allowed = {field for field in cls.__dataclass_fields__}
            return cls(**{key: value for key, value in payload.items() if key in allowed})
        except (OSError, ValueError, TypeError):
            return cls(room_url=room_url)

    def save(self, path: Path) -> None:
        atomic_json(path, asdict(self))


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

    def launch(self, room_url: str, run_id: str, triggered_at: str) -> str:
        if self.drive_sync_root:
            control = self.drive_sync_root / "DMRColab/control/trigger.json"
            expires = (_utc_now() + timedelta(minutes=15)).isoformat()
            atomic_json(control, {"schema_version": TRIGGER_SCHEMA_VERSION,
                "room_url": room_url, "run_id": run_id, "triggered_at": triggered_at,
                "expires_at": expires, "source": "local_colab_trigger"})
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
                button = page.get_by_role("button", name=re.compile(r"^(全部运行|Run all)$"))
                if button.count() != 1:
                    return False
                button.click()
                return True
        except Exception:
            return False


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
        self.state.last_poll_at = datetime.fromtimestamp(self.now(), timezone.utc).isoformat()
        self.state.last_error = None if status is not None else "probe_unavailable"
        self._read_ack()
        if self.state.ack_status in {"accepted", "running", "draining"}:
            self.state.save(self.state_file)
            return TriggerResult(self.state.status, False, self.state.run_id, error=self.state.last_error)
        if self.state.ack_status in {"success", "failed"} and status is not False:
            self.state.save(self.state_file)
            return TriggerResult(self.state.status, False, self.state.run_id, error=self.state.last_error)
        if self.state.ack_status in {"success", "failed"} and status is False:
            self.state.ack_status = None
        if self.state.status in {"dispatching", "needs_attention"}:
            self.state.save(self.state_file)
            return TriggerResult("needs_attention", False, self.state.run_id, error="launch_state_unknown")
        if status is True:
            self.state.status = "live"
            self.state.live_streak += 1
            self.state.offline_streak = 0
            if not self.state.triggered and self.state.live_streak >= self.live_confirmations:
                run_id = make_run_id(datetime.fromtimestamp(self.now(), timezone.utc))
                triggered_at = datetime.fromtimestamp(self.now(), timezone.utc).isoformat()
                self.state.status, self.state.run_id, self.state.triggered_at = "dispatching", run_id, triggered_at
                self.state.save(self.state_file)
                try:
                    launch_url = self.launcher.launch(self.room_url, run_id, triggered_at)
                except Exception as error:
                    self.state.status = "needs_attention"
                    self.state.last_error = type(error).__name__
                    self.state.live_streak = 0
                    self.state.save(self.state_file)
                    return TriggerResult("needs_attention", error=self.state.last_error)
                self.state.triggered = True
                self.state.status = "opened"
                self.state.save(self.state_file)
                return TriggerResult("opened", True, run_id, launch_url)
        elif status is False:
            self.state.live_streak = 0
            if self.state.triggered:
                self.state.offline_streak += 1
                if self.state.offline_streak >= self.offline_confirmations:
                    self.state.triggered = False
                    self.state.status = "ended"
                    self.state.offline_streak = 0
                else:
                    self.state.status = "ending"
            else:
                self.state.status = "offline"
                self.state.offline_streak = 0
        else:
            self.state.live_streak = 0
            self.state.offline_streak = 0
            self.state.status = "probe_unavailable"
        self.state.save(self.state_file)
        return TriggerResult(self.state.status, False, self.state.run_id)

    def _read_ack(self) -> None:
        root, run_id = self.launcher.drive_sync_root, self.state.run_id
        if not root or not run_id:
            return
        try:
            ack = json.loads((Path(root) / "DMRColab/control/ack.json").read_text(encoding="utf-8"))
            if ack.get("run_id") != run_id or ack.get("status") not in ACK_STATES:
                return
            self.state.ack_status = ack["status"]
            self.state.status = ack["status"]
            self.state.last_error = ack.get("error")
            if ack["status"] in {"success", "failed"}:
                self.state.triggered = False
        except (OSError, ValueError, TypeError):
            return


def probe_bilibili(room_url: str) -> Optional[bool]:
    try:
        from DMR.LiveAPI import LiveAPI
        return LiveAPI(validate_room_url(room_url)).Onair()
    except Exception:
        return None
