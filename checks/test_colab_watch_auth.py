import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from colab_support.cli import check_watch_login, preflight, prepare_cookie


class WatchLoginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        source = self.root / 'source.json'
        source.write_text(json.dumps({'cookie_info': {'cookies': [
            {'name': 'SESSDATA', 'value': 'test-secret'},
        ]}}), encoding='utf-8')
        self.cookie = prepare_cookie(source, self.root / 'private')
        self.get = self.enterContext(patch('requests.get'))
        self.response = self.get.return_value.__enter__.return_value
        self.response.status_code = 200

    def test_logged_in_uses_official_readonly_endpoint_without_redirects(self):
        self.response.json.return_value = {'code': 0, 'data': {'isLogin': True}}
        check_watch_login(self.cookie)
        self.get.assert_called_once_with(
            'https://api.bilibili.com/x/web-interface/nav',
            cookies={'SESSDATA': 'test-secret'},
            headers={'Referer': 'https://www.bilibili.com/', 'User-Agent': 'Mozilla/5.0'},
            timeout=15, allow_redirects=False,
        )

    def test_explicit_logged_out_stops_with_update_instruction(self):
        for result in ({'code': -101}, {'code': 0, 'data': {'isLogin': False}}):
            with self.subTest(result=result):
                self.response.json.return_value = result
                with self.assertRaisesRegex(ValueError, '登录已失效.*更新'):
                    check_watch_login(self.cookie)

    def test_unknown_responses_stop_without_claiming_cookie_expired(self):
        for result in (None, [], {}, {'code': -412}, {'code': 0},
                       {'code': 0, 'data': {}}, {'code': 0, 'data': {'isLogin': 'true'}}):
            with self.subTest(result=result):
                self.response.json.return_value = result
                with self.assertRaisesRegex(RuntimeError, '无法确认'):
                    check_watch_login(self.cookie)

    def test_http_failure_and_redirect_stop_before_reading_body(self):
        for status in (302, 412, 500):
            with self.subTest(status=status):
                self.response.status_code = status
                with self.assertRaisesRegex(RuntimeError, '无法确认'):
                    check_watch_login(self.cookie)
        self.response.json.assert_not_called()

    def test_network_and_json_errors_do_not_expose_response_or_cookie(self):
        for error in (requests.Timeout('test-secret'), requests.ConnectionError('test-secret'),
                      ValueError('test-secret')):
            with self.subTest(error=type(error).__name__):
                self.response.json.side_effect = error
                with self.assertRaises(RuntimeError) as caught:
                    check_watch_login(self.cookie)
                self.assertNotIn('test-secret', str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_connection_failure_before_response_is_sanitized(self):
        self.get.side_effect = requests.ConnectionError('test-secret')
        with self.assertRaisesRegex(RuntimeError, '无法确认') as caught:
            check_watch_login(self.cookie)
        self.assertNotIn('test-secret', str(caught.exception))

    def test_preflight_checks_private_cookie_before_expensive_probes(self):
        args = SimpleNamespace(drive_root=self.root, cookie=self.cookie, run_dir=self.root / 'run')
        with patch('colab_support.cli.sys.platform', 'linux'), \
                patch('shutil.which', return_value='/tool'), \
                patch('colab_support.cli.check_watch_login', side_effect=ValueError('expired')) as check, \
                patch('colab_support.cli.prepare_upload') as upload, \
                patch('colab_support.cli.run_checked') as probe:
            with self.assertRaisesRegex(ValueError, 'expired'):
                preflight(args)
            check.assert_called_once_with(args.run_dir / 'private' / 'watch.json')
            upload.assert_not_called()
            probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
