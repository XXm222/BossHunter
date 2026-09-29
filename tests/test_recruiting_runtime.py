"""Recruiting uses the original runtime and never picks an unrelated tab."""
import json
import unittest
from unittest.mock import Mock, patch
import httpx
from bosshunter.browser.client import RuntimeClient
from bosshunter.recruiting.browser import BossBrowser, BrowserError


def target(ident='boss', url='https://www.zhipin.com/web/chat/index', kind='page'):
    return {'targetId': ident, 'url': url, 'type': kind}


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.ensure_patch = patch('bosshunter.recruiting.browser.ensure_runtime', return_value=True)
        self.ensure_patch.start()
        self.addCleanup(self.ensure_patch.stop)
        self.runtime = Mock(spec=RuntimeClient)
        self.runtime.health.return_value = {'runtime': 'bosshunter', 'connected': True}
        self.runtime.targets.return_value = [target('other', 'https://example.org'), target()]
        self.runtime.evaluate.return_value = json.dumps({'connected': True, 'host': 'www.zhipin.com', 'path': '/web/chat/index', 'recruiter': True})
        self.browser = BossBrowser(runtime=self.runtime)

    def test_uses_existing_target_without_tab_mutations(self):
        self.assertTrue(self.browser.status()['recruiter'])
        self.assertEqual(self.runtime.evaluate.call_args.args[0], 'boss')
        self.assertIn("location.origin!=='https://www.zhipin.com'", self.runtime.evaluate.call_args.args[1])
        for name in ('new_tab', 'navigate', 'close_tab', 'click', 'type_text'):
            getattr(self.runtime, name).assert_not_called()

    def test_missing_or_multiple_or_invalid_targets_stop_before_eval(self):
        for targets in ([], [target('a'), target('b')], [target(url='https://www.zhipin.com.evil.test/web/chat/index')],
                        [target(url='http://www.zhipin.com/web/chat/index')], [target(kind='iframe')],
                        [target(url='https://www.zhipin.com/web/geek/chat')]):
            self.runtime.targets.return_value = targets
            with self.assertRaises(BrowserError): self.browser.status()
        self.runtime.evaluate.assert_not_called()

    def test_bound_target_never_silently_switches(self):
        self.browser.status()
        self.runtime.targets.return_value = [target('different')]
        with self.assertRaisesRegex(BrowserError, '绑定的招聘标签页'): self.browser.status()
        self.assertEqual(self.runtime.evaluate.call_count, 1)
        self.assertEqual(self.browser.target_id, 'boss')

    def test_explicit_target_is_respected_among_multiple_pages(self):
        self.runtime.targets.return_value = [target('a'), target('b')]
        BossBrowser(runtime=self.runtime, target_id='b').status()
        self.assertEqual(self.runtime.evaluate.call_args.args[0], 'b')

    def test_wrong_runtime_or_error_results_are_not_success(self):
        self.runtime.health.return_value = {'runtime': 'other'}
        with self.assertRaisesRegex(BrowserError, 'Browser Runtime'): self.browser.status()
        self.runtime.evaluate.assert_not_called()
        self.runtime.health.return_value = {'runtime': 'bosshunter'}
        for value in (None, {}, {'error': 'Runtime exception'}, 'undefined', {'adapter_error': '页面已变化'}):
            self.runtime.evaluate.return_value = value
            with self.assertRaises(BrowserError): self.browser.status()

    def test_conversation_mismatch_or_changing_content_never_clicks(self):
        with patch.object(self.browser, 'read_current', return_value={'id': 'other'}):
            with self.assertRaisesRegex(BrowserError, '不是绑定会话'): self.browser.open_conversation('bound')
        with patch.object(self.browser, 'read_current', side_effect=[{'id': 'bound', 'text': 'old'}, {'id': 'bound', 'text': 'new'}]):
            with self.assertRaisesRegex(BrowserError, '仍在变化'): self.browser.open_conversation('bound')
        self.runtime.evaluate.assert_not_called()
        self.runtime.navigate.assert_not_called()

    def test_actual_client_contract_uses_configured_builtin_runtime(self):
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            value = {'runtime': 'bosshunter'} if url.endswith('/health') else [target()]
            return httpx.Response(200, json=value)
        def post(url, **kwargs):
            calls.append(url)
            self.assertEqual(kwargs['params'], {'target': 'boss'})
            return httpx.Response(200, json={'value': json.dumps({'recruiter': True})})
        client = RuntimeClient({'browser': {'proxy_host': '127.0.0.1', 'proxy_port': 3457}})
        with patch('bosshunter.browser.client.httpx.get', side_effect=get), patch('bosshunter.browser.client.httpx.post', side_effect=post):
            self.assertTrue(BossBrowser(runtime=client).status()['recruiter'])
        self.assertEqual(calls, ['http://127.0.0.1:3457/health', 'http://127.0.0.1:3457/targets', 'http://127.0.0.1:3457/eval'])


if __name__ == '__main__': unittest.main()
