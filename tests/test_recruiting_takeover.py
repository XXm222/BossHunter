"""Human takeover keeps reads, stops model generation and automatic sends."""
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch
import test_recruiting_review
from bosshunter.recruiting.browser import TaskCancelled
from bosshunter.recruiting.service import RecruitingService
from test_recruiting import FakeLocalSession


class TakeoverTests(unittest.TestCase):
    setUp = test_recruiting_review.ReviewTests.setUp
    tearDown = test_recruiting_review.ReviewTests.tearDown

    def test_takeover_keeps_monitor_sync_without_model_or_auto_send(self):
        cid = self.c['id']
        self.service.control(cid, True, False)
        self.service.browser.snapshot['messages'].append({'direction': 'in', 'kind': 'text', 'text': '合成新消息', 'time': '11:00'})
        with patch('bosshunter.recruiting.agent.reply') as reply, patch('bosshunter.recruiting.agent.assess') as assess, patch.object(self.service, '_auto_send_if_allowed') as send:
            result = self.service.monitor_once()
        self.assertTrue(result['changed'])
        self.assertEqual(self.service._allowed_conversations(), [cid])
        self.assertEqual(self.service.state()['monitor']['allowed_count'], 1)
        self.assertEqual(self.service.state()['reply_progress'][cid]['stage'], 'taken_over')
        self.assertEqual(self.service.store.setting('reply_work:' + cid)['status'], 'waiting')
        reply.assert_not_called(); assess.assert_not_called(); send.assert_not_called()

    def test_takeover_reads_received_resume_without_scoring(self):
        cid = self.c['id']
        source = deepcopy(self.service.browser.snapshot)
        source.update(received_resume_message_id='9', account_uid='456')
        session = FakeLocalSession(source)
        session.read_resume = Mock(return_value={'source': 'boss_attachment_pdf_http', 'text': '合成简历经历' * 20, 'complete': False, 'meta': {'message_id': '9', 'page_count': 1}})
        service = RecruitingService(self.service.store.path, lambda: self.config, self.service.browser, session)
        service.control(cid, True, False)
        with patch('bosshunter.recruiting.agent.assess') as model:
            service.sync(cid)
        session.read_resume.assert_called_once()
        model.assert_not_called()
        self.assertEqual(len(service.store.rows('documents')), 1)
        self.assertEqual(service.state()['resume_processing'][cid]['status'], 'paused')

    def test_takeover_during_model_reply_discards_result_but_allows_human_draft(self):
        cid = self.c['id']
        def generate(*args):
            self.service.control(cid, True, False)
            return {'text': '合成模型回复', 'needs_human': False, 'basis': ['conversation'], 'missing': []}
        with patch('bosshunter.recruiting.agent.reply', side_effect=generate):
            with self.assertRaises(TaskCancelled):
                self.service.prepare_reply(cid)
        self.assertEqual(self.service.store.rows('outbox'), [])
        self.assertEqual(self.service.prepare_reply(cid, '合成人工回复')['status'], 'draft')

    def test_takeover_blocks_explicit_model_score_but_stop_contact_still_excludes_reads(self):
        cid = self.c['id']
        self.service.control(cid, True, False)
        with patch('bosshunter.recruiting.agent.assess') as model:
            with self.assertRaises(TaskCancelled):
                self.service.assess(cid)
        model.assert_not_called()
        self.service.control(cid, True, True)
        self.assertEqual(self.service._allowed_conversations(), [])
