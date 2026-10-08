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

    def test_read_friend_jobs_posts_batch_and_filters_empty_job(self):
        import urllib.parse
        calls = []
        def handle(req):
            calls.append(req)
            self.assertEqual(req.method, 'POST')
            self.assertEqual(req.url.path, '/wapi/zprelation/friend/getBossFriendListV2.json')
            form = urllib.parse.parse_qs(req.content.decode())
            self.assertEqual(form['friendIds'], ['123,456'])
            return httpx.Response(200, json={'code': 0, 'zpData': {'friendList': [
                {'uid': 123, 'encryptJobId': 'encryptJob123', 'jobName': '电子工程师'},
                {'uid': 456, 'encryptJobId': '', 'jobName': ''},
                {'uid': 789, 'encryptJobId': None},
            ]}})
        with patch('bosshunter.recruiting.local_session.time.sleep'):
            result = self.adapter(handle).read_friend_jobs(['123', '456'])
        self.assertEqual(result, {'123': 'encryptJob123'})
        self.assertEqual(len(calls), 1)

    def test_read_friend_jobs_batches_large_uid_list(self):
        import urllib.parse
        from bosshunter.recruiting.local_session import FRIEND_BATCH_SIZE
        calls = []
        def handle(req):
            calls.append(req)
            form = urllib.parse.parse_qs(req.content.decode())
            uids = form['friendIds'][0].split(',')
            return httpx.Response(200, json={'code': 0, 'zpData': {'friendList': [
                {'uid': int(u), 'encryptJobId': 'job-' + u, 'jobName': ''} for u in uids
            ]}})
        uids = [str(i) for i in range(200)]
        with patch('bosshunter.recruiting.local_session.time.sleep'):
            result = self.adapter(handle).read_friend_jobs(uids)
        self.assertEqual(len(calls), 4)  # 200 个 → 50 × 4
        self.assertEqual(len(result), 200)
        self.assertEqual(result['0'], 'job-0')
        self.assertEqual(result['199'], 'job-199')
        for call in calls:
            form = urllib.parse.parse_qs(call.content.decode())
            self.assertLessEqual(len(form['friendIds'][0].split(',')), FRIEND_BATCH_SIZE)

    def test_read_friend_jobs_empty_uids_makes_no_request(self):
        calls = []
        adapter = self.adapter(lambda req: calls.append(req) or httpx.Response(500))
        with patch('bosshunter.recruiting.local_session.time.sleep'):
            self.assertEqual(adapter.read_friend_jobs([]), {})
        self.assertEqual(calls, [])

    def test_read_conversation_incremental_returns_none_when_no_new(self):
        calls = []
        def handle(req):
            calls.append(req)
            return response([message(5), message(6)], False, 5)
        with patch('bosshunter.recruiting.local_session.time.sleep'):
            result = self.adapter(handle).read_conversation('123-0', '测试候选人', '测试岗位', since_mid=6)
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)  # 只读第一页，不翻页

    def test_read_conversation_incremental_returns_only_new(self):
        def handle(req):
            return response([message(7), message(6)], False, 6)
        with patch('bosshunter.recruiting.local_session.time.sleep'):
            result = self.adapter(handle).read_conversation('123-0', '测试候选人', '测试岗位', since_mid=6)
        self.assertIsNotNone(result)
        self.assertEqual([m['id'] for m in result['messages']], ['7'])  # 只返回新消息（mid > 6）

    def test_no_new_messages_still_verify_account(self):
        item = message(6)
        item['to']['uid'] = 999
        with self.assertRaises(BrowserError):
            self.read(self.adapter(lambda req: response([item])), expected_account='456', since_mid=6)

    def test_incremental_page_order_does_not_drop_new_message(self):
        value = self.read(self.adapter(lambda req: response([message(6), message(7)])), since_mid=6)
        self.assertEqual([m['id'] for m in value['messages']], ['7'])

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
