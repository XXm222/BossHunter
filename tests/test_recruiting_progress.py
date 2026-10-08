"""Reply progress is observable without extra platform requests."""
import unittest
from unittest.mock import patch

import test_recruiting_review
from bosshunter.recruiting.browser import ConversationNotSelected
from bosshunter.recruiting.store import RequestThrottled


class ProgressTests(unittest.TestCase):
    setUp = test_recruiting_review.ReviewTests.setUp
    tearDown = test_recruiting_review.ReviewTests.tearDown
    def test_throttle_exposes_deadline_and_restores_actual_phase(self):
        service = self.service
        service._reply_progress_local.cid = self.c['id']
        service._reply_stage('preflight', '发送前重新核对最新消息')
        seen = []
        def wait(_):
            seen.append(service.state()['reply_progress'][self.c['id']])
        with patch.object(service.store, 'count_request', side_effect=[RequestThrottled(120), {}]), patch.object(service.local_session, '_wait_cancelled', side_effect=wait), patch('bosshunter.recruiting.service.time.time', return_value=1000):
            service._count_request('conversation')
        self.assertEqual(seen[0]['stage'], 'waiting_throttle')
        self.assertEqual(seen[0]['wait_until'], 1120)
        self.assertIn('发送前', seen[0]['message'])
        final = service.state()['reply_progress'][self.c['id']]
        self.assertEqual(final['stage'], 'preflight')
        self.assertNotIn('wait_until', final)

    def test_wrong_browser_conversation_is_explicit_and_keeps_draft(self):
        self.service._reply_progress_local.cid = self.c['id']
        self.service.set_auto_send(self.c['id'], True)
        draft = self.service.prepare_reply(self.c['id'], '合成普通回复')
        with patch.object(self.service, 'execute', side_effect=ConversationNotSelected('合成会话未打开')):
            self.assertFalse(self.service._auto_send_if_allowed(draft))
        self.assertEqual(self.service.state()['reply_progress'][self.c['id']]['stage'], 'waiting_browser')
        self.assertEqual(self.service.store.row('outbox', draft['id'])['status'], 'draft')

    def test_monitor_progress_finishes_and_does_not_contaminate_other_requests(self):
        self.service.monitor_once()
        data = self.service.state()
        self.assertFalse(data['reply_progress'][self.c['id']]['active'])
        self.assertEqual(data['reply_progress'][self.c['id']]['stage'], 'idle')
        self.assertIsNone(self.service._reply_progress_local.cid)
        original = data['reply_progress'][self.c['id']]
        self.service._reply_stage('sending', '不应影响刚结束的会话')
        self.assertEqual(self.service.state()['reply_progress'][self.c['id']], original)
