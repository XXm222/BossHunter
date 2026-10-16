"""Regression cases from the October recruiting audit; synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.jobs import day_key
from bosshunter.recruiting.service import RecruitingService
from bosshunter.recruiting.store import Store, now
from test_recruiting import FakeBrowser, FakeLocalSession, http_msg
from recruiting_fixtures import authorize


class IssueFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.browser = FakeBrowser()
        self.browser.snapshot['position_platform_id'] = 'test-job'
        self.service = RecruitingService(Path(self.temp.name) / 'db', lambda: {}, self.browser)
        authorize(self.service)
        self.c = self.service.import_current()
        self.cid = self.c['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_reservation_rechecks_withdrawn_job_authorization(self):
        self.service.jobs.save_budget('custom', 10)
        self.service.jobs.select([])
        with self.assertRaisesRegex(ValueError, '勾选'):
            self.service.jobs.reserve_greeting('boss-test-job', 'candidate')
        self.assertEqual(self.service.jobs.state()['attempts'], [])

    def test_monitor_excludes_unselected_closed_and_unlinked_jobs(self):
        self.assertEqual(self.service._allowed_conversations(), [self.cid])
        self.service.jobs.select([])
        self.assertEqual(self.service._allowed_conversations(), [])
        self.service.jobs.select(['boss-test-job'])
        with self.service.store.db() as db:
            db.execute("UPDATE published_jobs SET status='已关闭'")
        self.assertEqual(self.service._allowed_conversations(), [])
        snapshot = deepcopy(self.browser.snapshot)
        snapshot['id'] = 'unlinked'
        snapshot.pop('position_platform_id')
        self.service.store.import_conversation(snapshot)
        self.assertEqual(self.service._allowed_conversations(), [])

    def test_auto_reply_rechecks_job_after_generation(self):
        draft = self.service.prepare_reply(self.cid, '测试回复')
        self.service.set_auto_send(self.cid, True)
        self.service.jobs.select([])
        with self.assertRaisesRegex(ValueError, '勾选'):
            self.service.execute(draft['id'], auto=True)
        self.assertEqual(self.browser.calls, 0)

    def test_platform_start_refreshes_yesterdays_quota_before_blockers(self):
        self.service.store.set_setting('greeting_quota', {'date': '2000-01-01', 'remaining': 0})
        self.service.local_session = Mock()
        self.service.local_session.read_greeting_quota.return_value = {'remaining': 1, 'unlimited': False}
        with patch.object(self.service, 'run_discovery', return_value={'greeted': 0, 'stopped': False}):
            self.service.start_discovery()
            self.service.discovery_worker.join(2)
        self.service.local_session.read_greeting_quota.assert_called_once()

    def test_stale_unlimited_is_not_valid_quota(self):
        self.service.store.set_setting('greeting_quota', {'date': '2000-01-01', 'unlimited': True})
        self.assertTrue(self.service.jobs.state()['blockers'])

    def test_quota_read_blocks_concurrent_greeting_reservation(self):
        self.service.jobs.save_budget('custom', 10)
        attempted = []
        def read_quota():
            try:
                self.service.jobs.reserve_greeting('boss-test-job', 'candidate')
            except ValueError:
                attempted.append('blocked')
            return {'remaining': 1, 'unlimited': False}
        self.service.local_session = Mock()
        self.service.local_session.read_greeting_quota.side_effect = read_quota
        self.service.read_greeting_quota()
        self.assertEqual(attempted, ['blocked'])
        self.assertEqual(self.service.jobs.state()['attempts'], [])

    def test_new_service_does_not_recover_live_greeting(self):
        self.service.jobs.save_budget('custom', 10)
        self.service.jobs.reserve_greeting('boss-test-job', 'candidate')
        other = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser())
        self.assertEqual(other.jobs.state()['attempts'][0]['status'], 'sending')
        self.service.jobs.finish_greeting('candidate', 'sent')
        self.assertEqual(other.jobs.state()['attempts'][0]['status'], 'sent')

    def test_new_service_does_not_recover_live_reply(self):
        draft = self.service.prepare_reply(self.cid, '测试回复')
        self.service.store.claim(draft['id'])
        other = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser())
        self.assertEqual(other.store.row('outbox', draft['id'])['status'], 'sending')

    def test_uncertain_reply_blocks_another_draft(self):
        first = self.service.prepare_reply(self.cid, '第一次回复')
        self.browser.fail = True
        with self.assertRaises(BrowserError):
            self.service.execute(first['id'])
        second = self.service.prepare_reply(self.cid, '第二次回复')
        self.browser.fail = False
        with self.assertRaisesRegex(ValueError, '待核实'):
            self.service.execute(second['id'])
        self.assertEqual(self.browser.calls, 1)

    def test_auto_reply_budget_is_reserved_atomically_across_services(self):
        self.service.store.set_setting('auto_reply_daily_limit', 1)
        first = self.service.prepare_reply(self.cid, '第一条')
        second = self.service.prepare_reply(self.cid, '第二条')
        self.service.set_auto_send(self.cid, True)
        other = Store(self.service.store.path)
        self.service.store.claim(first['id'], auto=True, daily_limit=1)
        self.service.store.finish(first['id'], 'sent', '测试回执')
        with self.assertRaisesRegex(ValueError, '上限'):
            other.claim(second['id'], auto=True, daily_limit=1)
        self.assertEqual(self.service._auto_reply_sent_today(), 1)

    def test_uncertain_auto_reply_does_not_report_confirmed_send(self):
        self.service.set_auto_send(self.cid, True)
        draft = self.service.prepare_reply(self.cid, '测试回复')
        with patch.object(self.browser, 'execute', return_value=None):
            self.assertFalse(self.service._auto_send_if_allowed(draft))
        self.assertFalse(any(e['kind'] == 'auto_sent' for e in self.service.store.rows('events')))

    def test_two_services_claim_score_before_model_call(self):
        self.service.store.save_position(self.c['position_id'], '测试岗位', '职责', 'human', True)
        self.service.store.save_document(self.cid, 'human', '工作经历' * 30, True, {})
        other = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser())
        entered, release = threading.Event(), threading.Event()
        def score(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return {'score': None}
        with patch('bosshunter.recruiting.agent.assess', side_effect=score) as model:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.service.assess, self.cid)
                self.assertTrue(entered.wait(2))
                try:
                    with self.assertRaisesRegex(ValueError, '正在'):
                        other.assess(self.cid)
                finally:
                    release.set()
                first.result(timeout=3)
            self.assertEqual(model.call_count, 1)
            other.assess(self.cid)
            self.assertEqual(model.call_count, 1)

    def test_no_new_messages_still_process_waiting_resume(self):
        snapshot = {**deepcopy(self.browser.snapshot), 'messages': [http_msg(6, 'in', 'text', '你好')], 'account_uid': '456'}
        session = FakeLocalSession(snapshot)
        session.return_none_on_since = True
        service = RecruitingService(Path(self.temp.name) / 'local.db', lambda: {}, FakeBrowser(), session)
        authorize(service)
        service.store.import_conversation(snapshot)
        with patch.object(service, 'process_received_resume') as process:
            service.sync(self.cid)
        process.assert_called_once()

    def test_send_check_preserves_locally_retained_history(self):
        old = {**deepcopy(self.browser.snapshot), 'messages': [http_msg(1, 'in', 'text', '旧消息'), http_msg(2, 'in', 'text', '新消息')], 'account_uid': '456'}
        self.browser.snapshot = old
        current = {**old, 'messages': [old['messages'][-1]]}
        service = RecruitingService(Path(self.temp.name) / 'local.db', lambda: {}, self.browser, FakeLocalSession(current))
        authorize(service)
        service.store.import_conversation(old)
        draft = service.prepare_reply(self.cid, '测试回复')
        service.execute(draft['id'])
        saved = json.loads(service.store.row('conversations', self.cid)['snapshot'])
        self.assertEqual([m['id'] for m in saved['messages']], ['1', '2'])

    def test_stopped_task_does_not_score(self):
        self.service.store.save_document(self.cid, 'human', '经历' * 50, True, {})
        self.service._stop_event = threading.Event()
        self.service._stop_event.set()
        with patch('bosshunter.recruiting.agent.assess') as model:
            with self.assertRaises(BrowserError):
                self.service.assess(self.cid)
        model.assert_not_called()

    def test_disabling_monitor_during_read_prevents_score(self):
        self.service._stop_event = threading.Event()
        self.service.set_monitor_enabled(True)
        self.service.store.save_document(self.cid, 'human', '经历' * 50, True, {})
        self.service.set_monitor_enabled(False)
        with patch('bosshunter.recruiting.agent.assess') as model:
            with self.assertRaises(BrowserError):
                self.service.assess(self.cid)
        model.assert_not_called()

    def test_stop_during_send_check_prevents_click(self):
        self.service._stop_event = threading.Event()
        self.service.set_monitor_enabled(True)
        draft = self.service.prepare_reply(self.cid, '测试回复')
        def open_chat(*args):
            self.service.set_monitor_enabled(False)
            return deepcopy(self.browser.snapshot)
        with patch.object(self.browser, 'open_conversation', side_effect=open_chat):
            with self.assertRaises(BrowserError):
                self.service.execute(draft['id'])
        self.assertEqual(self.browser.calls, 0)
        self.assertEqual(self.service.store.row('outbox', draft['id'])['status'], 'draft')

    def test_changed_platform_job_id_requires_human_reassociation(self):
        snapshot = deepcopy(self.browser.snapshot)
        snapshot['position_platform_id'] = 'different-job'
        with self.assertRaisesRegex(ValueError, '岗位.*变化'):
            self.service.store.import_conversation(snapshot)

    def test_human_reassociation_invalidates_shared_jd_and_drafts(self):
        snapshot = deepcopy(self.browser.snapshot)
        snapshot['id'] = 'legacy'
        snapshot.pop('position_platform_id')
        c = self.service.store.import_conversation(snapshot)
        self.service.store.save_position(c['position_id'], '测试岗位', '旧的同名 JD', 'human', True)
        draft = self.service.prepare_reply('legacy', '旧回复')
        linked = self.service.link_position('legacy', 'test-job')
        self.assertEqual(linked['position_id'], 'boss-test-job')
        self.assertEqual(self.service.store.row('positions', linked['position_id'])['jd'], '')
        self.assertEqual(self.service.store.row('outbox', draft['id'])['status'], 'expired')

    def test_manual_resolution_releases_block_without_resending(self):
        draft = self.service.prepare_reply(self.cid, '测试回复')
        self.service.store.finish(draft['id'], 'uncertain', '测试断线')
        with self.assertRaises(ValueError):
            self.service.store.resolve_outbound('reply', draft['id'], 'not_sent', '')
        result = self.service.store.resolve_outbound('reply', draft['id'], 'not_sent', '已在平台核实没有这条消息')
        self.assertFalse(result['retried'])
        self.assertEqual(self.browser.calls, 0)
        second = self.service.prepare_reply(self.cid, '另外一条回复')
        self.service.execute(second['id'])
        self.assertEqual(self.browser.calls, 1)

    def test_manual_resolution_cannot_override_live_sender(self):
        draft = self.service.prepare_reply(self.cid, '测试回复')
        self.service.store.claim(draft['id'])
        with self.assertRaisesRegex(ValueError, '仍在运行'):
            self.service.store.resolve_outbound('reply', draft['id'], 'sent', '合成的平台核实依据')

    def test_received_attachment_is_cached_across_services(self):
        snapshot = {**deepcopy(self.browser.snapshot), 'account_uid': '456', 'received_resume_message_id': '42', 'messages': []}
        session = Mock()
        session.read_resume.return_value = {'source': 'synthetic', 'text': '经历' * 50, 'complete': False, 'meta': {'message_id': '42', 'page_count': 1}}
        service = RecruitingService(Path(self.temp.name) / 'local.db', lambda: {}, FakeBrowser(), session)
        authorize(service)
        service.store.import_conversation(snapshot)
        service.resume(self.cid)
        other = RecruitingService(service.store.path, lambda: {}, FakeBrowser(), session)
        other.resume(self.cid)
        self.assertEqual(session.read_resume.call_count, 1)

    def test_context_changed_reply_remains_pending_without_another_message(self):
        self.browser.snapshot['messages'].append({'direction': 'in', 'kind': 'text', 'text': '新的问题', 'time': '11:00'})
        def draft(cid):
            return self.service.store.draft(cid, 'reply', '合成回复', {'source': 'ai_draft', 'needs_human': True})
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), \
             patch.object(self.service, 'prepare_reply', side_effect=[ValueError('生成期间消息变化'), draft(self.cid)]) as prepare:
            self.service.monitor_once()
            self.assertEqual(self.service.store.setting('reply_work:' + self.cid)['status'], 'waiting')
            self.service.monitor_once()
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(self.service.store.setting('reply_work:' + self.cid)['status'], 'drafted')

    def test_account_pause_is_not_swallowed_by_position_lookup(self):
        from bosshunter.recruiting.browser import AccountPauseError
        self.service.use_local_session = True
        self.service.local_session = Mock()
        self.service.local_session.read_friend_jobs.side_effect = AccountPauseError('合成账号验证')
        with self.assertRaises(AccountPauseError):
            self.service._friend_job_map(['123'])

    def test_monitor_disable_wakes_local_throttle_before_request(self):
        from http.cookiejar import CookieJar
        from bosshunter.recruiting.local_session import LocalBossSession
        from bosshunter.throttle import PageThrottle
        import httpx
        calls = []
        session = LocalBossSession(lambda: CookieJar(), httpx.MockTransport(lambda r: calls.append(r)),
                                   throttle=PageThrottle(10, 10))
        self.service._stop_event = threading.Event()
        self.service.set_monitor_enabled(True)
        session.set_stop_event(self.service._stop_event)
        session.set_cancel_check(self.service._check_stopped)
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(session.read_conversation, '123-0', '测试候选人', '测试岗位')
            self.service.set_monitor_enabled(False)
            with self.assertRaises(BrowserError):
                task.result(timeout=2)
        self.assertEqual(calls, [])

    def test_live_sender_in_another_process_is_recovered_only_after_exit(self):
        draft = self.service.prepare_reply(self.cid, '跨进程合成回复')
        src_dir = str(Path(__file__).resolve().parent.parent / 'src')
        code = ("import sys; sys.path.insert(0, sys.argv[3]); "
                "from pathlib import Path; from bosshunter.recruiting.store import Store; "
                "s=Store(Path(sys.argv[1])); s.claim(sys.argv[2]); print('claimed',flush=True); sys.stdin.read()")
        proc = subprocess.Popen([sys.executable, '-c', code, str(self.service.store.path), draft['id'], src_dir],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'claimed')
            other = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser())
            self.assertEqual(other.store.row('outbox', draft['id'])['status'], 'sending')
        finally:
            proc.terminate()
            proc.communicate(timeout=3)
        self.service.store.recover_outbox()
        self.assertEqual(self.service.store.row('outbox', draft['id'])['status'], 'uncertain')

    def test_cancelled_pending_reply_is_not_regenerated(self):
        draft = self.service.prepare_reply(self.cid, '人工放弃的回复')
        self.service.store.set_setting('reply_work:' + self.cid, {'status': 'drafted', 'draft_id': draft['id']})
        self.service.store.finish(draft['id'], 'cancelled', '人工取消')
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch.object(self.service, 'prepare_reply') as prepare:
            self.service.monitor_once()
        prepare.assert_not_called()
        self.assertEqual(self.service.store.setting('reply_work:' + self.cid), {})

    def test_uncertain_pending_reply_does_not_generate_another_draft(self):
        draft = self.service.prepare_reply(self.cid, '待核实的回复')
        self.service.store.set_setting('reply_work:' + self.cid, {'status': 'drafted', 'draft_id': draft['id']})
        self.service.store.finish(draft['id'], 'uncertain', '回执丢失')
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch.object(self.service, 'prepare_reply') as prepare:
            self.service.monitor_once()
        prepare.assert_not_called()

    def test_stale_quota_allows_start_to_refresh_but_unresolved_does_not(self):
        self.assertTrue(self.service.jobs.state()['can_start'])
        self.service.jobs.save_budget('custom', 10)
        self.service.jobs.reserve_greeting('boss-test-job', 'candidate')
        self.assertFalse(self.service.jobs.state()['can_start'])

    def test_incremental_merge_retains_legacy_messages_without_platform_ids(self):
        old = {'messages': [{'direction': 'in', 'kind': 'text', 'text': '旧版已读内容', 'time': '09:00'},
                            {'id': '10', 'timestamp': 10, 'text': '已核对内容'}]}
        new = {'messages': [{'id': '10', 'timestamp': 10, 'text': '已核对内容'},
                            {'id': '11', 'timestamp': 11, 'text': '新消息'}]}
        merged = self.service._append_new_messages(old, new)
        self.assertEqual([m['text'] for m in merged['messages']], ['旧版已读内容', '已核对内容', '新消息'])
        self.assertEqual(merged['unidentified_history_count'], 1)

    def test_auto_score_rechecks_authorization_after_resume_read(self):
        self.service.store.save_document(self.cid, 'human', '工作经历' * 30, True, {})
        self.service.store.save_position(self.c['position_id'], '测试岗位', '职责', 'human', True)
        self.service.jobs.select([])
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.assess') as model:
            self.service.auto_assess(self.cid)
        model.assert_not_called()
        self.assertEqual(self.service.store.setting('resume_processing:' + self.cid)['status'], 'paused')

    def test_auto_score_does_not_publish_result_after_authorization_withdrawn(self):
        self.service.store.save_document(self.cid, 'human', '工作经历' * 30, True, {})
        self.service.store.save_position(self.c['position_id'], '测试岗位', '职责', 'human', True)
        def score(*args):
            self.service.jobs.select([])
            return {'score': None}
        from bosshunter.recruiting.browser import TaskCancelled
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.assess', side_effect=score):
            with self.assertRaises(TaskCancelled):
                self.service.auto_assess(self.cid)
        self.assertEqual(self.service.store.rows('assessments'), [])
