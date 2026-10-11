"""专用 CDP 浏览器中，仅推进目标 Notebook 发起的 Drive 授权。"""
import json
import re
import threading
import time
from urllib.parse import urlparse


DRIVE_BUTTON = re.compile(
    r'^(连接到 Google 云端硬盘|連接到 Google 雲端硬碟|Connect to Google Drive)$', re.I)
CONSENT_BUTTON = re.compile(r'^(继续|繼續|Continue|允许|允許|Allow)$', re.I)
RUN_ALL_BUTTON = re.compile(r'^(全部运行|全部執行|執行全部|Run all)(\s|$)', re.I)
ACCOUNT_CHOOSER = re.compile(r'^(选择账号|选择帐号|選擇帳戶|選擇帳號|Choose an account)$', re.I)


def click_drive_dialog(page, may_act=lambda: True):
    button = page.get_by_role('button', name=DRIVE_BUTTON)
    if (button.count() == 1 and button.first.is_visible() and button.first.is_enabled()
            and may_act()):
        button.first.click(timeout=3000)
        return True
    return False


class DriveAuthorization:
    def __init__(self, notebook_url, google_account='', may_act=lambda: True):
        self.notebook_url = notebook_url.split('#', 1)[0]
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
            related = page.url.split('#', 1)[0] == self.notebook_url
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
                    if page.url.split('#', 1)[0] == self.notebook_url:
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
        actor = DriveAuthorization(self.notebook_url, self.google_account,
                                   may_act=lambda: not self._finished())
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(self.cdp_url, timeout=10000)
                try:
                    while not self._finished():
                        if not browser.is_connected():
                            self.error = 'authorization_browser_unavailable'
                            return
                        actor.scan(browser)
                        self.error = actor.error
                        pages = [p for c in browser.contexts for p in c.pages if not p.is_closed()]
                        if not pages:
                            self.error = 'authorization_browser_unavailable'
                            return
                        # Playwright 的等待会分发 page/popup 事件；不使用 time.sleep 阻塞事件。
                        pages[0].wait_for_timeout(2000)
                finally:
                    actor.close()
        except Exception:
            if not self._finished():
                self.error = 'authorization_browser_unavailable'
