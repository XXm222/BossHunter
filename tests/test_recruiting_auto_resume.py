"""Receipt -> extraction -> assessment, with durable dedup and no external sends."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from bosshunter.recruiting.service import RecruitingService
from bosshunter.recruiting.browser import BrowserError
from recruiting_fixtures import authorize


class AutoResumeTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.config = {'ai': {'model': 'fixture-model', 'api_key': 'fixture-key'}}
        self.model_key = patch('bosshunter.recruiting.agent.get_ai_api_key', side_effect=lambda c: c.get('ai', {}).get('api_key', ''))
        self.model_key.start()
        self.score = patch('bosshunter.recruiting.agent.assess', side_effect=lambda *args: {'score': None, 'earned': 20, 'coverage': 40, 'assessed_weight': 40, 'components': {}, 'questions': []})
        self.assess = self.score.start()
        self.browser = Mock()
        self.session = Mock()
        self.snapshot = {'id': '123-0', 'name': '测试候选人', 'position_title': '测试岗位', 'position_platform_id': 'test-job', 'messages': [], 'account_uid': '456', 'received_resume_message_id': '789'}
        self.session.read_conversation.side_effect = lambda *args, **kwargs: dict(self.snapshot)
        self.session.read_resume.side_effect = lambda *args: {'source': 'boss_attachment_pdf_http', 'text': '岗位相关经历。' * 30, 'complete': False, 'meta': {'page_count': 1, 'message_id': self.snapshot['received_resume_message_id']}}
        self.service = self.make_service()
        authorize(self.service)
        c = self.service.store.import_conversation(self.snapshot)
        self.cid = c['id']; self.pid = c['position_id']
        self.service.store.save_position(self.pid, '测试岗位', '岗位职责与经验要求', 'human', True)

    def make_service(self):
        return RecruitingService(Path(self.temp.name) / 'db', lambda: self.config, self.browser, self.session)

    def tearDown(self):
        self.score.stop(); self.model_key.stop(); self.temp.cleanup()

    def status(self):
        return self.service.state()['resume_processing'][self.cid]['status']

    def test_receipt_reads_and_scores_once_even_after_restart_or_concurrent_sync(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.service.sync(), range(2)))
        self.service = self.make_service()
        self.service.monitor_once()
        self.assertEqual(self.session.read_resume.call_count, 1)
        self.assertEqual(self.assess.call_count, 1)
        self.assertEqual(self.status(), 'completed')
        self.assertFalse(self.service.state()['documents'][0]['complete'])
        self.assertEqual(self.service.state()['outbox'], [])
        self.browser.execute.assert_not_called()
        self.browser.read_resume.assert_not_called()

    def test_waits_for_model_then_scores_on_unchanged_conversation(self):
        self.config['ai']['api_key'] = ''
        self.service.sync()
        self.assertEqual(self.status(), 'waiting_model'); self.assess.assert_not_called()
        self.config['ai']['api_key'] = 'configured'
        self.service.sync()
        self.assertEqual(self.status(), 'completed'); self.assess.assert_called_once()
        self.assertEqual(self.session.read_resume.call_count, 1)

    def test_waits_for_jd_and_rescores_when_jd_or_resume_changes(self):
        self.service.store.save_position(self.pid, '测试岗位', '', 'human', True)
        self.service.sync(); self.assertEqual(self.status(), 'waiting_jd'); self.assess.assert_not_called()
        self.service.store.save_position(self.pid, '测试岗位', '真实职责', 'human', True)
        self.service.sync(); self.assess.assert_called_once()
        self.service.store.save_position(self.pid, '测试岗位', '新的职责要求', 'human', True)
        self.service.sync(); self.assertEqual(self.assess.call_count, 2)
        self.snapshot['received_resume_message_id'] = '790'
        self.service.sync(); self.assertEqual(self.assess.call_count, 3)
        self.assertEqual(self.session.read_resume.call_count, 2)

    def test_manual_read_and_save_also_auto_score_without_duplicate_charge(self):
        self.service.resume(self.cid); self.service.resume(self.cid)
        self.assess.assert_called_once()
        self.service.save_resume({'conversation_id': self.cid, 'text': '补全后的工作经历。' * 30, 'complete': True})
        self.assertEqual(self.assess.call_count, 2)
        self.service.sync()
        self.assertEqual(self.assess.call_count, 2)

    def test_scoring_failure_keeps_document_and_requires_explicit_retry(self):
        self.assess.side_effect = RuntimeError('provider private payload')
        self.service.sync()
        self.assertEqual(self.status(), 'failed')
        self.assertEqual(len(self.service.state()['documents']), 1)
        self.assertNotIn('provider private payload', json.dumps(self.service.state()))
        self.service = self.make_service(); self.service.sync()
        self.assertEqual(self.assess.call_count, 1)
        self.assess.side_effect = lambda *args: {'score': None}
        self.service.assess(self.cid)
        self.assertEqual(self.status(), 'completed')
        self.assertEqual(self.assess.call_count, 2)

    def test_read_failure_does_not_score_previous_resume(self):
        self.service.store.save_document(self.cid, 'old', '旧的职业经历' * 30, False, {})
        self.session.read_resume.side_effect = BrowserError('需要验证')
        with self.assertRaises(BrowserError): self.service.sync()
        self.service.sync()
        self.assertEqual(self.status(), 'read_failed'); self.assess.assert_not_called()
        self.assertEqual(self.session.read_resume.call_count, 1)
        self.assertEqual(len(self.service.state()['documents']), 1)

    def test_takeover_reads_without_scoring_and_paused_job_still_stops_reads(self):
        self.service.control(self.cid, True, False); self.service.sync()
        self.session.read_resume.assert_called_once(); self.assess.assert_not_called()
        self.session.read_resume.reset_mock()
        self.snapshot['received_resume_message_id'] = '790'
        self.service.control(self.cid, False, False)
        self.service.store.save_position(self.pid, '测试岗位', '职责', 'human', False)
        self.service.sync(); self.session.read_resume.assert_not_called()
        self.assertEqual(self.status(), 'paused')

    def test_manual_and_automatic_score_share_lock(self):
        self.service.resume(self.cid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.service.assess(self.cid), range(2)))
        self.assess.assert_called_once()
