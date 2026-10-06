"""HTTP conversation tests: synthetic data only; no browser or real credentials."""
from copy import deepcopy
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock
import httpx
from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.local_session import LocalBossSession
from bosshunter.recruiting.service import RecruitingService


def message(mid=1, incoming=True):
    peer = {'uid': 123, 'source': 0, 'name': '测试候选人'}
    employer = {'uid': 456, 'source': 0, 'name': '测试招聘方'}
    return {'mid': mid, 'time': 1700000000000 + mid, 'type': 1,
            'from': peer if incoming else employer, 'to': employer if incoming else peer,
            'body': {'type': 1, 'text': '测试正文'}}


def response(items, more=False, cursor=1):
    return httpx.Response(200, json={'code': 0, 'zpData': {'messages': items, 'hasMore': more, 'minMsgId': cursor}})


class LocalChatTests(unittest.TestCase):
    def adapter(self, handler):
        return LocalBossSession(lambda: CookieJar(), httpx.MockTransport(handler))

    def read(self, adapter, **kwargs):
        return adapter.read_conversation('123-0', '测试候选人', '测试岗位', **kwargs)

    def test_reads_paginates_and_sorts_without_marking_read(self):
        calls = []
        def handle(req):
            calls.append(req)
            self.assertEqual(req.method, 'GET')
            self.assertEqual(req.url.path, '/wapi/zpchat/boss/historyMsg')
            self.assertEqual(req.url.params['gid'], '123')
            return response([message(2, False)], True, 2) if req.url.params['page'] == '1' else response([message(1)])
        with patch('bosshunter.recruiting.local_session.time.sleep'):
            value = self.read(self.adapter(handle))
        self.assertEqual([m['id'] for m in value['messages']], ['1', '2'])
        self.assertEqual([m['direction'] for m in value['messages']], ['in', 'out'])
        self.assertEqual(calls[1].url.params['maxMsgId'], '2')
        self.assertFalse(value['history_complete'])
        self.assertFalse(value['editor_empty'])
        self.assertTrue(value['stable_message_ids'])

    def test_rejects_wrong_person_name_source_or_account(self):
        bad = []
        for field, value in [('uid', 999), ('source', 1), ('name', '另一人')]:
            item = message(); item['from'][field] = value; bad.append(item)
        for item in bad:
            with self.assertRaises(BrowserError): self.read(self.adapter(lambda req: response([item])))
        with self.assertRaises(BrowserError): self.read(self.adapter(lambda req: response([message()])), expected_account='789')

    def test_rejects_duplicate_or_partial_pagination(self):
        with self.assertRaises(BrowserError): self.read(self.adapter(lambda req: response([message(), message()])))
        with self.assertRaises(BrowserError): self.read(self.adapter(lambda req: response([], True)))

    def test_auth_failure_is_sanitized_and_not_retried(self):
        calls = []
        def handle(req):
            calls.append(req)
            return httpx.Response(200, json={'code': 37, 'message': 'secret-response'})
        with self.assertRaises(BrowserError) as error: self.read(self.adapter(handle))
        self.assertNotIn('secret-response', str(error.exception)); self.assertEqual(len(calls), 1)

    def test_cards_and_unknown_actions_are_not_guessed_as_replies(self):
        item = message(); item['body'] = {'type': 4, 'action': {'aid': 999, 'extend': 'secret'}}
        value = self.read(self.adapter(lambda req: response([item])))
        self.assertEqual(value['messages'][0]['direction'], 'system')
        self.assertNotIn('secret', str(value))
        item['body'] = {'type': 9, 'resume': {'position': '测试职业', 'age': '敏感信息', 'securityId': 'secret'}}
        value = self.read(self.adapter(lambda req: response([item])))
        self.assertIn('测试职业', str(value)); self.assertNotIn('敏感信息', str(value)); self.assertNotIn('secret', str(value))

    def test_service_uses_http_preserves_binding_and_uncertain_outbox(self):
        with TemporaryDirectory() as folder:
            browser = Mock()
            adapter = self.adapter(lambda req: response([message()]))
            service = RecruitingService(Path(folder) / 'test.db', lambda: {}, browser, adapter)
            old = {'id': '123-0', 'name': '测试候选人', 'position_title': '测试岗位', 'messages': [], 'coverage': 'old'}
            service.store.import_conversation(old)
            draft = service.store.draft('123-0', 'reply', '此前尝试', {})
            service.store.finish(draft['id'], 'uncertain', '待核实')
            service.sync()
            self.assertEqual(service.reply_context('123-0')['conversation']['messages'][0]['id'], '1')
            self.assertEqual(service.store.row('outbox', draft['id'])['status'], 'uncertain')
            self.assertTrue(service.connection['read_bound_conversation'])
            browser.open_conversation.assert_not_called(); browser.status.assert_not_called()
            before = service.store.row('conversations', '123-0')['snapshot']
            service.local_session = self.adapter(lambda req: httpx.Response(403))
            with self.assertRaises(BrowserError): service.sync()
            self.assertEqual(service.store.row('conversations', '123-0')['snapshot'], before)
            self.assertFalse(service.connection['connected'])


    def test_daily_request_limit_stops_background_reads(self):
        import time
        # MockTransport 让节流间隔为 0，但每日上限仍生效
        session = LocalBossSession(lambda: CookieJar(), httpx.MockTransport(lambda r: httpx.Response(200, json={'code': 0})))
        session._request_day = time.strftime('%Y-%m-%d')
        session._request_count = 10000  # 远超单日上限
        with self.assertRaises(BrowserError):
            session._count('conversation')


if __name__ == '__main__': unittest.main()
