"""专用 CDP 浏览器中，仅推进目标 Notebook 发起的 Drive 授权。"""
import json
import logging
import re
import threading
import time
from urllib.parse import unquote, urlparse

_log = logging.getLogger(__name__)
_log.addHandler(logging.NullHandler())

_URL_RE = re.compile(r'https?://(?!(?:127\.0\.0\.1|localhost)(?:[:/\s]|$))\S+')
_PATH_RE = re.compile(r'[A-Za-z]:\\[^\s"\']+|/(?:Users|home|tmp|var|mnt|Volumes)/[^\s"\']+')


def _is_loopback_interception_error(exc: BaseException) -> bool:
    """判断异常或其因果链是否为本地回环被代理/防火墙劫持导致的连接异常。"""
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ConnectionError) and 'unexpected peer connection' in str(cur).lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def format_browser_exception(exc: BaseException) -> str:
    """将浏览器或 Playwright 异常格式化为不含敏感路径与私有 URL 的诊断文本。"""
    if _is_loopback_interception_error(exc):
        return (
            '本地回环通信异常（ConnectionError: Unexpected peer connection），'
            'Playwright 无法初始化；若使用 Proxifier 等代理/防火墙工具，'
            '请关闭直连拦截（如 Handle Direct Connections）并放行 127.0.0.1'
        )
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or 'TimeoutError' in name:
        return f'{name}（页面或元素等待超时）'
    if isinstance(exc, OSError):
        msg = _scrub_sensitive_text(exc.strerror or '')
        if msg:
            return f'{name}: {msg}'
        return f'{name} (errno={exc.errno})' if exc.errno is not None else name
    first_line = next((line.strip() for line in str(exc).splitlines() if line.strip()), '')
    cleaned = _scrub_sensitive_text(first_line)[:160]
    return f'{name}: {cleaned}' if cleaned else name


def _scrub_sensitive_text(text: str) -> str:
    """脱敏诊断文本中的私有 URL 与本地路径，保留 loopback 调试地址。"""
    text = _URL_RE.sub('<url>', text)
    return _PATH_RE.sub('<path>', text)


DRIVE_BUTTON = re.compile(
    r'^(连接到 Google 云端硬盘|連接到 Google 雲端硬碟|Connect to Google Drive|'
    r'仍然运行|仍要執行|照常執行|仍然執行|Run anyway)$', re.I)
CONSENT_BUTTON = re.compile(r'^(继续|繼續|Continue|允许|允許|Allow)$', re.I)
RUN_ALL_BUTTON = re.compile(r'^(全部运行|全部執行|執行全部|Run all)(\s|$)', re.I)
ACCOUNT_CHOOSER = re.compile(r'^(选择账号|选择帐号|選擇帳戶|選擇帳號|Choose an account)$', re.I)


def normalize_notebook_url(url):
    return unquote(str(url or '').split('#', 1)[0])


def click_drive_dialog(page, may_act=lambda: True):
    button = page.get_by_role('button', name=DRIVE_BUTTON)
    count = button.count()
    if count == 1:
        if button.first.is_visible() and button.first.is_enabled() and may_act():
            button.first.click(timeout=3000)
            return True
        return False
    visible = [button.nth(index) for index in range(count)
               if button.nth(index).is_visible() and button.nth(index).is_enabled()]
    if len(visible) == 1 and may_act():
        visible[0].click(timeout=3000)
        return True
    return False


class DriveAuthorization:
    def __init__(self, notebook_url, google_account='', may_act=lambda: True):
        self.notebook_url = normalize_notebook_url(notebook_url)
        self.google_account = google_account.strip().lower()
        self.may_act = may_act
        self.error = None
        self.pages = set()
        self.listeners = []
        self.contexts = set()

    def attach(self, browser):
        for context in browser.contexts:
            if context not in self.contexts:
                context.on('page', self._remember)
                self.listeners.append((context, 'page', self._remember))
                self.contexts.add(context)
            for page in context.pages:
                self._remember(page)

    def _remember(self, page):
        if page in self.pages:
            return True
        try:
            if page.is_closed():
                return False
            related = normalize_notebook_url(page.url) == self.notebook_url
            if not related:
                opener = page.opener()
                related = opener is not None and self._remember(opener)
        except Exception:
            return False
        if related:
            self.pages.add(page)
            page.on('popup', self._remember)
            self.listeners.append((page, 'popup', self._remember))
        return related

    def close(self):
        for emitter, event, listener in self.listeners:
            try:
                emitter.remove_listener(event, listener)
            except Exception:
                pass
        self.listeners.clear()

    @staticmethod
    def _visible(locator):
        return [locator.nth(index) for index in range(locator.count())
                if locator.nth(index).is_visible()]

    def _oauth(self, page):
        parsed = urlparse(page.url)
        if parsed.scheme != 'https' or parsed.hostname != 'accounts.google.com':
            return False
        if self._visible(page.locator('input[type="password"], input[type="email"]')):
            self.error = 'google_login_required'
            return False
        if (re.search(r'/challenge/(?!selection)', parsed.path)
                or self._visible(page.locator('iframe[src*="recaptcha"], input[name="totpPin"], input[name="idvPin"]'))):
            self.error = 'google_auth_challenge'
            return False
        options = {}
        for option in self._visible(page.locator('[data-identifier], [data-email]')):
            email = (option.get_attribute('data-identifier', timeout=1000)
                     or option.get_attribute('data-email', timeout=1000) or '')
            if '@' in email:
                options.setdefault(email.lower(), option)
        if options:
            if self.google_account:
                selected = options.get(self.google_account)
                if selected is None:
                    self.error = 'google_account_mismatch'
                    return False
            elif len(options) == 1:
                selected = next(iter(options.values()))
            else:
                self.error = 'google_account_ambiguous'
                return False
            # 授权页的账号信息也可能可点击，仅在账号选择页选择账号。
            chooser = ('/accountchooser' in parsed.path.lower()
                       or self._visible(page.get_by_role('heading', name=ACCOUNT_CHOOSER)))
            if chooser and self.may_act():
                selected.click(timeout=3000)
                return True
        buttons = self._visible(page.get_by_role('button', name=CONSENT_BUTTON))
        if len(buttons) == 1 and buttons[0].is_enabled():
            if self.google_account and not options:
                identity = page.get_by_text(self.google_account, exact=True)
                if not self._visible(identity):
                    self.error = 'google_account_unconfirmed'
                    return False
            if self.may_act():
                buttons[0].click(timeout=3000)
                return True
        self.error = 'google_auth_page_unrecognized'
        return False

    def scan(self, browser):
        self.error = None
        if not self.may_act():
            return False
        self.attach(browser)
        acted = False
        for context in browser.contexts:
            for page in list(context.pages):
                try:
                    if not self.may_act():
                        return acted
                    if page.is_closed() or not self._remember(page):
                        continue
                    if normalize_notebook_url(page.url) == self.notebook_url:
                        acted |= click_drive_dialog(page, self.may_act)
                    else:
                        acted |= self._oauth(page)
                except Exception:
                    # 页面跳转或关闭可能使本轮 locator 失效，下一轮重新读取。
                    continue
        return acted


class DriveAuthorizationWatcher:
    """Playwright 对象只在监听线程使用，直播查询保持原来的轮询周期。"""
    def __init__(self, notebook_url, cdp_url, google_account, expires_at, ack_path, run_id):
        self.notebook_url, self.cdp_url = notebook_url, cdp_url
        self.google_account, self.expires_at = google_account, expires_at
        self.ack_path, self.run_id = ack_path, run_id
        self.error = None
        self.last_error_detail = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='colab-drive-authorization', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=15)
        return not self.thread.is_alive()

    def _finished(self):
        if self.stop_event.is_set() or time.time() >= self.expires_at:
            return True
        if self.ack_path is not None:
            try:
                from .trigger import validate_ack
                ack = json.loads(self.ack_path.read_text(encoding='utf-8'))
                validate_ack(ack, self.run_id)
                return True
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return False

    def _run(self):
        self.last_error_detail = None
        actor = DriveAuthorization(self.notebook_url, self.google_account,
                                   may_act=lambda: not self._finished())
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
                try:
                    while not self._finished():
                        if not browser.is_connected():
                            self.last_error_detail = '专用浏览器连接已断开'
                            self.error = 'authorization_browser_unavailable'
                            return
                        actor.scan(browser)
                        self.error = actor.error
                        pages = [p for c in browser.contexts for p in c.pages if not p.is_closed()]
                        if not pages:
                            self.last_error_detail = '专用浏览器没有打开的页面'
                            self.error = 'authorization_browser_unavailable'
                            return
                        # Playwright 的等待会分发 page/popup 事件；不使用 time.sleep 阻塞事件。
                        pages[0].wait_for_timeout(2000)
                finally:
                    actor.close()
        except Exception as exc:
            detail = format_browser_exception(exc)
            _log.warning('Drive 授权监听浏览器异常: %s', detail)
            if not self._finished():
                self.last_error_detail = detail
                self.error = 'authorization_browser_unavailable'
