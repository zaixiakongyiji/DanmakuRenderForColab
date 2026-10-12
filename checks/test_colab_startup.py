"""授权页面作用域、提前认领与准备阶段回执的离线回归。"""
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from colab_support.drive_authorization import DriveAuthorization, DriveAuthorizationWatcher
from colab_support.notebook_session import PreparationHeartbeat, prepare_session
from colab_monitor import validate_notebook_url
from colab_support.trigger import (ColabLauncher, TriggerMonitor, TriggerResult,
                                   control_paths, write_ack)


class Locator:
    def __init__(self, elements=()):
        self.elements = list(elements)

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    @property
    def first(self):
        return self.elements[0]


def element(label='', **attributes):
    value = Mock(label=label)
    value.is_visible.return_value = value.is_enabled.return_value = True
    value.get_attribute.side_effect = lambda name, **kwargs: attributes.get(name)
    return value


class Page:
    def __init__(self, url, opener=None, buttons=(), accounts=(), login=False, identity=''):
        self.url, self.parent = url, opener
        self.buttons, self.accounts = buttons, accounts
        self.login, self.identity = login, identity
        self.on, self.remove_listener = Mock(), Mock()
        self.wait_for_timeout = Mock()

    def opener(self):
        return self.parent

    def is_closed(self):
        return False

    def get_by_role(self, role, name):
        return Locator(button for button in self.buttons if name.search(button.label))

    def get_by_text(self, text, exact=False):
        return Locator([element(text)] if text == self.identity else [])

    def locator(self, selector):
        if selector.startswith('[data-identifier]'):
            return Locator(self.accounts)
        if selector.startswith('input[type="password"]') and self.login:
            return Locator([element()])
        return Locator()


def browser_with(*pages):
    context = SimpleNamespace(pages=list(pages), on=Mock(), remove_listener=Mock())
    # Playwright 的真实 context 可哈希，测试使用 Mock 避免 SimpleNamespace 不可哈希。
    context = Mock(pages=context.pages, on=context.on, remove_listener=context.remove_listener)
    return SimpleNamespace(contexts=[context], is_connected=lambda: True)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.url = 'https://colab.research.google.com/drive/test'
        self.notebook = Page(self.url)

    def test_drive_dialog_in_three_languages(self):
        for label in ('连接到 Google 云端硬盘', '連接到 Google 雲端硬碟', 'Connect to Google Drive'):
            with self.subTest(label=label):
                button = element(label)
                notebook = Page(self.url, buttons=[button])
                actor = DriveAuthorization(self.url)
                self.assertTrue(actor.scan(browser_with(notebook)))
                button.click.assert_called_once()

    def test_oauth_steps_in_three_languages(self):
        for label in ('继续', '繼續', 'Continue', '允许', '允許', 'Allow'):
            with self.subTest(label=label):
                button = element(label)
                popup = Page('https://accounts.google.com/signin/oauth/consent', self.notebook,
                             buttons=[button])
                actor = DriveAuthorization(self.url)
                self.assertTrue(actor.scan(browser_with(self.notebook, popup)))
                button.click.assert_called_once()

    def test_unique_account_is_selected(self):
        account = element(**{'data-identifier': 'only@example.test', 'role': 'link'})
        popup = Page('https://accounts.google.com/v3/signin/accountchooser', self.notebook,
                     accounts=[account])
        self.assertTrue(DriveAuthorization(self.url).scan(browser_with(self.notebook, popup)))
        account.click.assert_called_once()

    def test_multiple_accounts_need_selection_or_matching_configuration(self):
        first = element(**{'data-email': 'first@example.test', 'role': 'link'})
        second = element(**{'data-email': 'second@example.test', 'role': 'link'})
        popup = Page('https://accounts.google.com/v3/signin/accountchooser', self.notebook,
                     accounts=[first, second])
        browser = browser_with(self.notebook, popup)
        actor = DriveAuthorization(self.url)
        self.assertFalse(actor.scan(browser))
        self.assertEqual(actor.error, 'google_account_ambiguous')
        first.click.assert_not_called()
        second.click.assert_not_called()
        selected = DriveAuthorization(self.url, 'SECOND@example.test')
        self.assertTrue(selected.scan(browser))
        second.click.assert_called_once()
        wrong = DriveAuthorization(self.url, 'missing@example.test')
        self.assertFalse(wrong.scan(browser))
        self.assertEqual(wrong.error, 'google_account_mismatch')

    def test_consent_requires_configured_identity(self):
        button = element('Continue')
        popup = Page('https://accounts.google.com/signin/oauth/consent', self.notebook,
                     buttons=[button], identity='another@example.test')
        actor = DriveAuthorization(self.url, 'wanted@example.test')
        browser = browser_with(self.notebook, popup)
        self.assertFalse(actor.scan(browser))
        self.assertEqual(actor.error, 'google_account_unconfirmed')
        popup.identity = 'wanted@example.test'
        self.assertTrue(actor.scan(browser))

    def test_consent_clicks_continue_instead_of_account_information(self):
        account = element(**{'data-email': 'only@example.test', 'role': 'button'})
        button = element('Continue')
        popup = Page('https://accounts.google.com/signin/oauth/consent', self.notebook,
                     buttons=[button], accounts=[account])
        actor = DriveAuthorization(self.url, 'only@example.test')
        self.assertTrue(actor.scan(browser_with(self.notebook, popup)))
        account.click.assert_not_called()
        button.click.assert_called_once()

    def test_stopping_during_locator_read_prevents_click(self):
        stop = threading.Event()
        button = element('Allow')
        button.is_enabled.side_effect = lambda: (stop.set() or True)
        popup = Page('https://accounts.google.com/signin/oauth/consent', self.notebook,
                     buttons=[button])
        actor = DriveAuthorization(self.url, may_act=lambda: not stop.is_set())
        self.assertFalse(actor.scan(browser_with(self.notebook, popup)))
        button.click.assert_not_called()

    def test_unrelated_and_lookalike_pages_are_not_clicked(self):
        button = element('Allow')
        for url, opener in (
            ('https://accounts.google.com/signin/oauth/consent', None),
            ('https://accounts.google.com.evil.test/consent', self.notebook),
            ('http://accounts.google.com/consent', self.notebook),
        ):
            with self.subTest(url=url):
                page = Page(url, opener, buttons=[button])
                self.assertFalse(DriveAuthorization(self.url).scan(browser_with(self.notebook, page)))
        button.click.assert_not_called()

    def test_login_and_challenges_require_attention(self):
        for path, login, expected in (
            ('/signin/identifier', True, 'google_login_required'),
            ('/signin/challenge/totp', False, 'google_auth_challenge'),
        ):
            with self.subTest(path=path):
                button = element('Continue')
                popup = Page('https://accounts.google.com' + path, self.notebook,
                             buttons=[button], login=login)
                actor = DriveAuthorization(self.url)
                self.assertFalse(actor.scan(browser_with(self.notebook, popup)))
                self.assertEqual(actor.error, expected)
                button.click.assert_not_called()

    def test_page_and_popup_events_track_blank_popup_after_navigation(self):
        actor = DriveAuthorization(self.url)
        browser = browser_with(self.notebook)
        actor.attach(browser)
        browser.contexts[0].on.assert_called_once_with('page', actor._remember)
        self.notebook.on.assert_called_once_with('popup', actor._remember)
        popup = Page('about:blank', self.notebook)
        actor._remember(popup)
        popup.parent = None
        popup.url = 'https://accounts.google.com/signin/oauth/consent'
        button = element('Allow')
        popup.buttons = [button]
        browser.contexts[0].pages.append(popup)
        self.assertTrue(actor.scan(browser))
        button.click.assert_called_once()
        actor.close()
        browser.contexts[0].remove_listener.assert_called_once()
        popup.remove_listener.assert_called_once()

    def test_watcher_handles_late_popup_and_stops_on_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            ack = Path(folder) / 'ack.json'
            watcher = DriveAuthorizationWatcher(self.url, 'http://127.0.0.1:9222', '',
                time.time() + 900, ack, '20261011-000000-abcdef12')
            browser = browser_with(self.notebook)
            button = element('繼續')
            calls = []
            def wait(milliseconds):
                calls.append(milliseconds)
                if len(calls) == 1:
                    browser.contexts[0].pages.append(Page(
                        'https://accounts.google.com/signin/oauth/consent', self.notebook,
                        buttons=[button]))
                else:
                    write_ack(ack, watcher.run_id, 'accepted')
            self.notebook.wait_for_timeout.side_effect = wait
            pw = Mock()
            pw.chromium.connect_over_cdp.return_value = browser
            context = Mock()
            context.__enter__ = Mock(return_value=pw)
            context.__exit__ = Mock(return_value=False)
            api = SimpleNamespace(sync_playwright=Mock(return_value=context))
            with patch.dict(sys.modules, {'playwright.sync_api': api}):
                watcher._run()
            self.assertEqual(calls, [2000, 2000])
            button.click.assert_called_once()
            self.assertIsNone(watcher.error)
            browser.contexts[0].remove_listener.assert_called_once()

    def test_watcher_stops_at_expiry_and_ignores_other_run_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            ack = Path(folder) / 'ack.json'
            watcher = DriveAuthorizationWatcher(self.url, '', '', time.time() + 900,
                                                 ack, '20261011-000000-abcdef12')
            write_ack(ack, '20261011-000000-abcdef13', 'accepted')
            self.assertFalse(watcher._finished())
            watcher.expires_at = time.time() - 1
            self.assertTrue(watcher._finished())

    def test_invalid_ack_does_not_stop_authorization(self):
        with tempfile.TemporaryDirectory() as folder:
            ack = Path(folder) / 'ack.json'
            watcher = DriveAuthorizationWatcher(self.url, '', '', time.time() + 900,
                                                 ack, '20261011-000000-abcdef12')
            for payload in (
                {'schema_version': 2, 'run_id': watcher.run_id, 'status': 'accepted'},
                {'schema_version': 2, 'run_id': watcher.run_id, 'status': 'accepted',
                 'updated_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                [],
            ):
                with self.subTest(payload=payload):
                    ack.write_text(json.dumps(payload), encoding='utf-8')
                    self.assertFalse(watcher._finished())
            write_ack(ack, watcher.run_id, 'accepted')
            self.assertTrue(watcher._finished())

    def test_traditional_connection_labels(self):
        for label, expected in (
            ('已連線', True), ('RAM / 磁碟', True), ('顯示記憶體使用量', True),
            ('連線 GPU', False), ('重新連線', False), ('連線至託管執行階段', False),
        ):
            with self.subTest(label=label):
                page = Mock()
                button = Mock()
                button.count.return_value = 1
                button.is_visible.return_value = button.is_enabled.return_value = True
                absent = Mock()
                absent.count.return_value = 0
                page.get_by_role.side_effect = lambda kind, name=None: (
                    button if kind == 'button' and name.search(label) else absent)
                self.assertIs(ColabLauncher._connection_state(page), expected)


class NotebookStartupTests(unittest.TestCase):
    def test_notebook_url_accepts_drive_and_github_forms(self):
        for url in (
            'https://colab.research.google.com/drive/notebook-id#scrollTo=cell',
            'https://colab.research.google.com/github/example/project/blob/main/notebooks/colab_record.ipynb',
        ):
            self.assertEqual(validate_notebook_url(url), url)
        for url in (
            'https://colab.research.google.com/github/example/project/blob/main/notebooks/record.py',
            'https://colab.research.google.com/github/example/project/tree/main/notebooks/record.ipynb',
            'https://evil.example/github/example/project/blob/main/record.ipynb',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_notebook_url(url)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.launcher = ColabLauncher(drive_sync_root=self.root, open_browser=False)
        self.monitor = TriggerMonitor('https://live.bilibili.com/1', self.launcher,
            self.root / 'state.json', live_confirmations=1)
        self.run_id = self.monitor.observe(True).run_id

    def session(self):
        return prepare_session(self.root, self.root, self.root / 'cookie', '', 'python',
                               work_root=self.root / 'work')

    def ack(self):
        return json.loads(control_paths(self.root, self.run_id)[2].read_text(encoding='utf-8'))

    def test_claim_precedes_dependencies_and_heartbeat_updates_while_installing(self):
        session = self.session()
        self.assertEqual(self.ack()['status'], 'accepted')
        refreshed = threading.Event()
        written = []
        def write(*args, **kwargs):
            write_ack(*args, **kwargs)
            written.append(kwargs.get('phase'))
            if len(written) >= 2:
                refreshed.set()
        class FastHeartbeat(PreparationHeartbeat):
            def __init__(self, session, phase):
                super().__init__(session, phase, interval=0.01)
        def checked(command):
            self.assertEqual(self.ack()['status'], 'accepted')
            self.assertTrue(refreshed.wait(1))
        with patch('colab_support.notebook_session.PreparationHeartbeat', FastHeartbeat), \
             patch('colab_support.notebook_session.write_ack', side_effect=write):
            session.install_dependencies(checked)
        self.assertGreaterEqual(len(written), 2)
        self.assertEqual(self.ack()['phase'], 'dependencies')
        self.assertEqual(session.phase, 'parameters')
        self.assertEqual(self.monitor.observe(True).state, 'accepted')
        self.assertFalse(any(t.name == 'colab-preparation-heartbeat' for t in threading.enumerate()))

    def test_preparation_failure_and_interrupt_write_terminal_ack(self):
        for error in (RuntimeError('private details'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                # 同一编号不能二次认领；本例在既有会话上核对每种失败边界。
                if control_paths(self.root, self.run_id)[2].exists():
                    session = getattr(self, 'existing_session', None)
                else:
                    session = self.session()
                    self.existing_session = session
                session.phase = 'parameters'
                write_ack(control_paths(self.root, self.run_id)[2], self.run_id, 'accepted')
                with self.assertRaises(type(error)):
                    session.install_dependencies(Mock(side_effect=error))
                self.assertEqual(self.ack()['status'], 'failed')
                self.assertEqual(self.ack()['error'], 'notebook_dependencies_failed')
                self.assertEqual(session.phase, 'invalid')
                self.assertNotIn('private details', json.dumps(self.ack()))
                with self.assertRaises(RuntimeError):
                    session.run(Mock())

    def test_heartbeat_never_overwrites_running_or_terminal(self):
        session = self.session()
        heartbeat = PreparationHeartbeat(session, 'dependencies')
        for state in ('running', 'failed', 'success'):
            write_ack(control_paths(self.root, self.run_id)[2], self.run_id, state)
            self.assertFalse(heartbeat.update())
            self.assertEqual(self.ack()['status'], state)

    def test_blocked_heartbeat_prevents_final_ack_and_runtime_release(self):
        from colab_support.runtime_release import finish_session
        session = self.session()
        heartbeat = PreparationHeartbeat(session, 'dependencies')
        heartbeat.start = Mock()
        heartbeat.thread = Mock(ident=1)
        heartbeat.thread.is_alive.return_value = True
        with patch('colab_support.notebook_session.PreparationHeartbeat', return_value=heartbeat):
            with self.assertRaisesRegex(RuntimeError, 'preparation_heartbeat_stop_failed'):
                session.install_dependencies(Mock())
        self.assertEqual(self.ack()['status'], 'accepted')
        release = Mock()
        with self.assertRaisesRegex(RuntimeError, 'preparation_heartbeat_still_active'):
            finish_session(session, 1, release_runtime=release)
        release.assert_not_called()
        self.assertFalse((session.run_dir / 'manifest.json').exists())

    def test_preflight_heartbeat_stops_before_recording(self):
        session = self.session()
        session.preflight(Mock())
        self.assertEqual(self.ack()['phase'], 'preflight')
        runner = Mock(return_value=0)
        self.assertEqual(session.run(runner), 0)
        self.assertFalse(any(t.name == 'colab-preparation-heartbeat' for t in threading.enumerate()))

    def test_notebook_mounts_and_claims_before_installation(self):
        nb = json.loads((Path(__file__).resolve().parents[1] / 'notebooks/colab_record.ipynb').read_text(encoding='utf-8'))
        cells = {cell['id']: ''.join(cell['source']) for cell in nb['cells']}
        self.assertIn('START_MODE = "读取本地触发请求"', cells['colab-parameters'])
        namespace = {'SETUP_PHASE': 'form', 'RUN_PARAMETERS': {'room_url': ''}}
        drive = SimpleNamespace(mount=Mock())
        with patch.dict(sys.modules, {'google.colab': SimpleNamespace(drive=drive)}):
            exec(cells['colab-01'], namespace)
        drive.mount.assert_called_once_with('/content/drive')
        namespace.update(PROJECT=self.root, DRIVE_ROOT=self.root, COOKIE_FILE=self.root / 'cookie',
                         SETUP_PHASE='source', sys=sys)
        original = prepare_session
        def prepare(*args, **kwargs):
            return original(*args, **kwargs, work_root=self.root / 'work')
        with patch('colab_support.notebook_session.prepare_session', side_effect=prepare), \
             patch.object(sys, 'path', list(sys.path)):
            exec(cells['colab-05'], namespace)
        self.assertEqual(self.ack()['status'], 'accepted')
        checked = Mock()
        namespace['checked'] = checked
        exec(cells['colab-07'], namespace)
        self.assertEqual(checked.call_count, 4)
        self.assertEqual(namespace['SETUP_PHASE'], 'dependencies')
        self.assertEqual(self.ack()['phase'], 'dependencies')

    def test_missing_request_does_not_fall_back_to_manual(self):
        with self.assertRaisesRegex(ValueError, 'trigger_request_missing'):
            prepare_session(self.root, self.root / 'missing-drive', self.root / 'cookie', '',
                            'python', work_root=self.root / 'work')

    def test_timeout_stops_authorization_and_ack_stops_it_immediately(self):
        self.launcher.close = Mock()
        self.launcher.try_authorize_drive = Mock()
        self.monitor.now = lambda: datetime.fromisoformat(self.monitor.state.triggered_at).timestamp() + 901
        self.assertEqual(self.monitor.observe(True).error, 'claim_timeout')
        self.launcher.try_authorize_drive.assert_not_called()
        self.launcher.close.assert_called_once()

    def test_once_auto_run_waits_for_claim_without_clicking_again(self):
        import colab_trigger
        args = colab_trigger.parser().parse_args([
            '--url', 'https://live.bilibili.com/1', '--once', '--auto-run',
            '--state-file', str(self.root / 'once-state.json'),
        ])
        launcher = ColabLauncher(drive_sync_root=self.root, auto_run=True,
                                 cdp_url='http://127.0.0.1:9222')
        launcher._auto_run_browser = Mock(return_value=True)
        launcher.try_authorize_drive = Mock(return_value=True)
        polls = []
        def probe(room_url):
            polls.append(room_url)
            if len(polls) == 3:
                state = json.loads(args.state_file.read_text(encoding='utf-8'))
                write_ack(control_paths(self.root, state['run_id'])[2], state['run_id'], 'accepted')
            return True
        with patch.object(colab_trigger, 'probe_bilibili', side_effect=probe), \
             patch.object(colab_trigger, 'resolve_target_info', return_value=('test', '1')), \
             patch.object(colab_trigger, 'setup_logger', return_value=Mock()), \
             patch.object(colab_trigger.time, 'sleep') as sleep:
            self.assertEqual(colab_trigger.monitor_loop(args, args.url, launcher), 0)
        self.assertEqual(len(polls), 3)
        self.assertEqual(sleep.call_count, 2)
        launcher._auto_run_browser.assert_called_once()

    def test_once_keeps_authorizing_until_claim_timeout(self):
        import colab_trigger
        args = colab_trigger.parser().parse_args([
            '--url', 'https://live.bilibili.com/1', '--once', '--auto-run',
        ])
        monitor = Mock(state=SimpleNamespace(ack_status=None))
        monitor.observe.side_effect = [
            TriggerResult('opened', triggered=True, run_id=self.run_id),
            TriggerResult('needs_attention', run_id=self.run_id, error='google_auth_challenge'),
            TriggerResult('needs_attention', run_id=self.run_id, error='claim_timeout'),
        ]
        with patch.object(colab_trigger, 'TriggerMonitor', return_value=monitor), \
             patch.object(colab_trigger, 'probe_bilibili', return_value=True), \
             patch.object(colab_trigger, 'resolve_target_info', return_value=('test', '1')), \
             patch.object(colab_trigger, 'setup_logger', return_value=Mock()), \
             patch.object(colab_trigger.time, 'sleep') as sleep:
            self.assertEqual(colab_trigger.monitor_loop(args, args.url, Mock()), 1)
        self.assertEqual(monitor.observe.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == '__main__':
    unittest.main()
