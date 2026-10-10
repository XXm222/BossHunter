"""Second recruiting review regressions. All external inputs are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import httpx

from bosshunter.recruiting.browser import BrowserError, TaskCancelled
from bosshunter.recruiting.jobs import day_key
from bosshunter.recruiting.local_session import LocalBossSession
from bosshunter.recruiting.service import RecruitingService
from bosshunter.recruiting.store import now
from test_recruiting import FakeBrowser, FakeLocalSession, http_msg
from recruiting_fixtures import authorize


class SecondFixTests(unittest.TestCase):
    def test_confirmed_relink_after_same_job_rename_updates_binding_and_version(self):
        position = self.service.store.save_position(self.c['position_id'], '测试岗位', '已核实的岗位职责', 'human', True)
        other_snapshot = {**deepcopy(self.browser.snapshot), 'id': 'other'}
        self.service.store.import_conversation(other_snapshot)
        first = self.service.prepare_reply(self.cid, '合成普通回复')
        other = self.service.prepare_reply('other', '其他会话草稿')
        self.service.set_auto_send(self.cid, True)
        self.service.jobs.import_snapshot({'complete': True, 'total': 1, 'jobs': [
            {'platform_id': 'test-job', 'title': '已改名岗位', 'status': '开放中', 'details': []}]})
        linked = self.service.link_position(self.cid, 'test-job')
        updated = self.service.store.row('positions', linked['position_id'])
        self.assertEqual(updated['title'], '已改名岗位')
        self.assertEqual(updated['version'], position['version'] + 1)
        self.assertEqual(updated['jd'], '已核实的岗位职责')
        self.assertFalse(linked['auto_send'])
        self.service.store.import_conversation(json.loads(linked['snapshot']))
        self.assertEqual(self.service.store.row('outbox', first['id'])['status'], 'expired')
        self.assertEqual(self.service.store.row('outbox', other['id'])['status'], 'expired')
        self.service.link_position(self.cid, 'test-job')
        self.assertEqual(self.service.store.row('positions', linked['position_id'])['version'], updated['version'])

    def test_cancellation_cannot_overwrite_a_claimed_action(self):
        draft = self.service.prepare_reply(self.cid, '合成普通回复')
        other = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser())
        other.store.claim(draft['id'])
        with self.assertRaisesRegex(ValueError, '只能取消'):
            self.service.store.cancel_draft(draft['id'])
        self.assertEqual(self.service.store.row('outbox', draft['id'])['status'], 'sending')
        other._send_guard(draft['id'], claimed=True)
        with self.assertRaises(TaskCancelled):
            self.service._send_guard(draft['id'], claimed=True)

    def test_cancelled_action_fails_send_guard_and_cannot_be_claimed(self):
        draft = self.service.prepare_reply(self.cid, '合成普通回复')
        self.service.store.cancel_draft(draft['id'])
        with self.assertRaises(ValueError):
            self.service.store.claim(draft['id'])
        with self.assertRaises(TaskCancelled):
            self.service._send_guard(draft['id'])
        self.assertEqual(self.browser.calls, 0)

    def test_concurrent_cancel_and_claim_have_one_winner(self):
        other = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser())
        for index in range(8):
            draft = self.service.prepare_reply(self.cid, '合成普通回复' + str(index))
            barrier = threading.Barrier(2)
            def attempt(call):
                barrier.wait(timeout=3)
                try:
                    call(draft['id'])
                    return True
                except ValueError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                cancel = pool.submit(attempt, self.service.store.cancel_draft)
                claim = pool.submit(attempt, other.store.claim)
                cancelled, claimed = cancel.result(timeout=3), claim.result(timeout=3)
            self.assertNotEqual(cancelled, claimed)
            self.assertEqual(self.service.store.row('outbox', draft['id'])['status'], 'cancelled' if cancelled else 'sending')
            if claimed:
                other.store.finish(draft['id'], 'cancelled', '合成测试未点击')

    def test_resolved_environment_endpoint_invalidates_assessment_cache(self):
        config = {'ai': {'model': 'synthetic-model', 'api_key': 'synthetic-key'}}
        self.service.config_provider = lambda: config
        self.service.store.save_document(self.cid, 'human_verified_text', '合成职业经历' * 20, False, {})
        response = {'score': 80, 'earned': 80, 'coverage': 100, 'assessed_weight': 100, 'components': {}, 'questions': []}
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://synthetic-one.invalid'}), patch('bosshunter.recruiting.agent.assess', side_effect=lambda *args: deepcopy(response)) as model:
            self.service.assess(self.cid)
            first = self.service.reply_context(self.cid)['assessment']['id']
            os.environ['ANTHROPIC_BASE_URL'] = 'https://synthetic-two.invalid'
            self.assertIsNone(self.service.reply_context(self.cid)['assessment'])
            self.assertIsNone(self.service.state()['current_assessment_ids'][self.cid])
            self.service.assess(self.cid)
            second = self.service.reply_context(self.cid)['assessment']['id']
            self.service.assess(self.cid)
        self.assertNotEqual(first, second)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len(self.service.store.rows('assessments')), 2)
        self.assertEqual(self.browser.calls, 0)

    def test_explicit_endpoint_takes_precedence_over_unused_environment_change(self):
        config = {'ai': {'model': 'synthetic-model', 'api_key': 'synthetic-key', 'base_url': 'https://synthetic-config.invalid'}}
        self.service.config_provider = lambda: config
        self.service.store.save_document(self.cid, 'human_verified_text', '合成职业经历' * 20, False, {})
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://synthetic-one.invalid'}):
            first = self.service.assessment_input(self.cid)[3]
            os.environ['ANTHROPIC_BASE_URL'] = 'https://synthetic-two.invalid'
            self.assertEqual(self.service.assessment_input(self.cid)[3], first)

    def test_return_to_agent_restores_regenerated_identical_draft_once(self):
        response = {'text': '合成普通回复', 'needs_human': False, 'basis': ['conversation'], 'missing': []}
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.reply', return_value=response) as model:
            first = self.service.prepare_reply(self.cid)
            self.service.store.set_setting('reply_work:' + self.cid, {'status': 'drafted', 'draft_id': first['id'], 'context_hash': first['context_hash']})
            self.service.control(self.cid, True, False)
            self.service.control(self.cid, False, False)
            self.service.monitor_once()
            self.service.monitor_once()
        self.assertEqual(model.call_count, 2)
        restored = self.service.store.row('outbox', first['id'])
        self.assertEqual(restored['status'], 'draft')
        self.assertEqual(json.loads(restored['refs'])['source'], 'ai_draft')
        self.assertEqual(self.browser.calls, 0)

    def test_takeover_then_explicit_same_human_text_can_be_prepared_again(self):
        old = self.service.prepare_reply(self.cid, '合成普通回复')
        self.service.control(self.cid, True, False)
        reviewed = self.service.prepare_reply(self.cid, '合成普通回复')
        self.assertEqual(reviewed['id'], old['id'])
        self.assertEqual(reviewed['status'], 'draft')
        self.assertEqual(self.service.execute(reviewed['id'])['status'], 'sent')

    def test_explicit_human_edit_after_takeover_can_replace_confirmation_draft(self):
        with patch('bosshunter.recruiting.agent.reply', return_value={'text': '合成普通回复', 'needs_human': True, 'basis': ['conversation'], 'missing': ['需人工核实']}):
            ai = self.service.prepare_reply(self.cid)
        self.service.control(self.cid, True, False)
        human = self.service.prepare_reply(self.cid, '合成普通回复')
        self.assertEqual(human['id'], ai['id'])
        self.assertEqual(json.loads(human['refs'])['source'], 'human_draft')
        self.assertEqual(self.service.execute(human['id'])['status'], 'sent')

    def test_changed_refs_do_not_revive_submitted_or_cancelled_actions(self):
        for status in ('sent', 'sending', 'uncertain', 'cancelled'):
            with self.subTest(status=status):
                draft = self.service.prepare_reply(self.cid, '合成普通回复' + status)
                self.service.store.finish(draft['id'], status, '合成结果')
                changed = self.service.store.draft(self.cid, 'reply', draft['content'], {**json.loads(draft['refs']), 'needs_human': True})
                self.assertEqual(changed['id'], draft['id'])
                self.assertEqual(changed['status'], status)
                self.assertEqual(changed['refs'], draft['refs'])

    def test_model_change_without_resume_invalidates_cache_and_old_send(self):
        config = {'ai': {'model': 'synthetic-old', 'api_key': 'synthetic-secret'}}
        self.service.config_provider = lambda: config
        response = {'text': '合成普通回复', 'needs_human': False, 'basis': ['conversation'], 'missing': []}
        with patch('bosshunter.recruiting.agent.reply', return_value=response) as model:
            first = self.service.prepare_reply(self.cid)
            config['ai']['model'] = 'synthetic-new'
            with self.assertRaisesRegex(ValueError, '回复依据已变化'):
                self.service.execute(first['id'])
            second = self.service.prepare_reply(self.cid)
            reused = self.service.prepare_reply(self.cid)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(second['id'], reused['id'])
        self.assertNotEqual(json.loads(first['refs'])['context_signature'], json.loads(second['refs'])['context_signature'])
        public = json.dumps(self.service.state())
        self.assertNotIn('synthetic-secret', public)
        self.assertNotIn(self.service.store.setting('reply_model_identity')['digest'], public)
        self.assertEqual(self.browser.calls, 0)

    def test_model_change_during_generation_discards_output(self):
        config = {'ai': {'model': 'synthetic-old'}}
        self.service.config_provider = lambda: config
        def changed(*args):
            config['ai']['model'] = 'synthetic-new'
            return {'text': '合成普通回复', 'needs_human': False, 'basis': ['conversation'], 'missing': []}
        with patch('bosshunter.recruiting.agent.reply', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                self.service.prepare_reply(self.cid)
        self.assertEqual(self.service.store.rows('outbox'), [])

    def test_saving_previous_resume_selects_it_for_context_scoring_and_ui(self):
        a = self.service.store.save_document(self.cid, 'human_verified_text', '合成版本A' * 20, False, {})
        b = self.service.store.save_document(self.cid, 'human_verified_text', '合成版本B' * 20, False, {})
        self.assertEqual(self.service.reply_context(self.cid)['resume']['id'], b['id'])
        restored = self.service.store.save_document(self.cid, 'human_verified_text', '合成版本A' * 20, False, {})
        self.assertEqual(restored['id'], a['id'])
        self.assertEqual(self.service.reply_context(self.cid)['resume']['id'], a['id'])
        self.assertEqual(self.service.assessment_input(self.cid)[1]['id'], a['id'])
        self.assertEqual(self.service.state()['documents'][0]['id'], a['id'])
        reopened = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser())
        self.assertEqual(reopened.reply_context(self.cid)['resume']['id'], a['id'])
        self.assertEqual(len(reopened.store.rows('documents')), 2)

    def test_legacy_resume_without_selection_uses_latest_and_isolated_candidates(self):
        a = self.service.store.save_document(self.cid, 'human_verified_text', '合成版本A' * 20, False, {})
        b = self.service.store.save_document(self.cid, 'human_verified_text', '合成版本B' * 20, False, {})
        with self.service.store.db() as db:
            db.execute('DELETE FROM settings WHERE key=?', ('current_document:' + self.cid,))
        self.assertEqual(self.service.store.current_document(self.cid)['id'], b['id'])
        self.service.store.import_conversation({**self.browser.snapshot, 'id': 'other'})
        self.service.store.set_setting('current_document:other', a['id'])
        self.assertIsNone(self.service.store.current_document('other'))

    def test_identical_human_and_ai_text_preserves_required_human_confirmation(self):
        human = self.service.prepare_reply(self.cid, '合成普通回复')
        self.service.set_auto_send(self.cid, True)
        with patch('bosshunter.recruiting.agent.reply', return_value={'text': '合成普通回复', 'needs_human': True, 'basis': ['conversation'], 'missing': ['需人工核实']}):
            ai = self.service.prepare_reply(self.cid)
        self.assertEqual(ai['id'], human['id'])
        self.assertTrue(json.loads(ai['refs'])['needs_human'])
        self.assertFalse(self.service._auto_send_if_allowed(ai))
        with self.assertRaisesRegex(ValueError, '人工'):
            self.service.store.claim(ai['id'], auto=True, daily_limit=10)
        self.assertEqual(self.browser.calls, 0)

    def test_latest_confirmation_flag_interrupts_an_older_send_request(self):
        human = self.service.prepare_reply(self.cid, '合成普通回复')
        self.service.store.draft(self.cid, 'reply', human['content'], {
            **json.loads(human['refs']), 'source': 'ai_draft', 'needs_human': True})
        with self.assertRaises(TaskCancelled):
            self.service._send_guard(human['id'])
        self.assertEqual(self.browser.calls, 0)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.browser = FakeBrowser()
        self.service = RecruitingService(Path(self.temp.name) / 'db', lambda: {}, self.browser)
        authorize(self.service)
        self.c = self.service.import_current()
        self.cid = self.c['id']
        self.service.store.set_setting('greeting_quota', {
            'date': day_key(), 'updated_at': now(), 'remaining': 100,
            'unlimited': False, 'local_used_at_read': 0})

    def tearDown(self):
        self.temp.cleanup()

    def test_cancelled_greeting_releases_sending_without_retry(self):
        self.service.verifier = Mock()
        self.service.verifier.greet.side_effect = TaskCancelled('模拟点击前停止')
        with self.assertRaises(TaskCancelled):
            self.service.greet_discovered('candidate', job_id='test-job')
        state = self.service.jobs.state()
        self.assertEqual(state['attempts'][0]['status'], 'cancelled')
        self.assertFalse(any('待核实' in b for b in state['blockers']))
        with self.assertRaisesRegex(ValueError, '跳过'):
            self.service.greet_discovered('candidate', job_id='test-job')
        self.service.verifier.greet.assert_called_once()

    def test_late_stale_sync_cannot_erase_newer_saved_messages(self):
        initial = {**deepcopy(self.browser.snapshot), 'account_uid': '456', 'messages': [http_msg(1, 'in', 'text', '旧消息')]}
        older_reply = {**deepcopy(initial), 'messages': [http_msg(2, 'in', 'text', '第二条')]}
        newer_reply = {**deepcopy(initial), 'messages': [http_msg(2, 'in', 'text', '第二条'), http_msg(3, 'in', 'text', '第三条')]}
        entered, newer_written = threading.Event(), threading.Event()
        old_session, new_session = FakeLocalSession(older_reply), FakeLocalSession(newer_reply)
        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(newer_written.wait(3))
            return deepcopy(older_reply)
        old_session.read_conversation = delayed
        older = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser(), old_session)
        newer = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser(), new_session)
        self.service.store.import_conversation(initial)
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(older.sync, self.cid, process=False)
            self.assertTrue(entered.wait(2))
            try:
                # A newer cursor is a distinct read. Identical in-flight reads
                # now intentionally share the first request's result.
                self.service.store.import_conversation(older_reply, append=True)
                newer.sync(self.cid, process=False)
            finally:
                newer_written.set()
            work.result(timeout=3)
        final = json.loads(self.service.store.row('conversations', self.cid)['snapshot'])
        self.assertEqual([m['id'] for m in final['messages']], ['1', '2', '3'])

    def test_stop_during_startup_quota_read_prevents_worker_start(self):
        self.service.store.set_setting('greeting_quota', {})
        entered, release = threading.Event(), threading.Event()
        def quota():
            entered.set()
            self.assertTrue(release.wait(3))
            return {'remaining': 1, 'unlimited': False}
        self.service.local_session = Mock()
        self.service.local_session.read_greeting_quota.side_effect = quota
        with patch.object(self.service, 'run_discovery') as run:
            with ThreadPoolExecutor(max_workers=1) as pool:
                start = pool.submit(self.service.start_discovery)
                self.assertTrue(entered.wait(2))
                self.service.stop_discovery()
                release.set()
                with self.assertRaises(TaskCancelled):
                    start.result(timeout=3)
        run.assert_not_called()
        self.assertTrue(self.service.discovery_stop.is_set())

    def test_custom_mode_requires_platform_quota_and_stops_on_read_failure(self):
        self.service.jobs.save_budget('custom', 10)
        self.service.store.set_setting('greeting_quota', {})
        self.service.local_session = Mock()
        self.service.local_session.read_greeting_quota.side_effect = BrowserError('合成额度读取失败')
        self.service.verifier = Mock()
        with self.assertRaisesRegex(ValueError, '额度'):
            self.service.run_discovery(throttle_delay=(0, 0))
        self.service.local_session.read_greeting_quota.assert_called_once()
        self.service.verifier.reload.assert_not_called()
        self.service.verifier.greet.assert_not_called()

    def test_custom_reservation_rejects_unknown_platform_quota(self):
        self.service.jobs.save_budget('custom', 10)
        self.service.store.set_setting('greeting_quota', {})
        with self.assertRaisesRegex(ValueError, '额度'):
            self.service.jobs.reserve_greeting('boss-test-job', 'candidate')

    def test_quota_rejects_negative_and_boolean_fields(self):
        for limit, used in ((10, -5), (True, 0), (10, False), (-1, -5), (-2, 0)):
            with self.subTest(limit=limit, used=used):
                body = {'code': 0, 'zpData': {'dailyRightStates': {'chatRightState': {'progressBarList': [{'limitCount': limit, 'usedCount': used}]}}}}
                session = LocalBossSession(lambda: CookieJar(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
                with self.assertRaisesRegex(BrowserError, '额度字段'):
                    session.read_greeting_quota()

    def test_takeover_allows_explicit_human_reply_but_blocks_automatic_send(self):
        self.service.control(self.cid, True, False)
        draft = self.service.prepare_reply(self.cid, '人工确认的普通回复')
        with self.assertRaises(ValueError):
            self.service.execute(draft['id'], auto=True)
        self.assertEqual(self.browser.calls, 0)
        result = self.service.execute(draft['id'])
        self.assertEqual(result['status'], 'sent')

    def test_paused_position_allows_human_reply_but_stop_contact_does_not(self):
        self.service.store.save_position(self.c['position_id'], '测试岗位', '', 'human', False)
        draft = self.service.prepare_reply(self.cid, '人工处理暂停任务的回复')
        self.assertEqual(self.service.execute(draft['id'])['status'], 'sent')
        self.service.control(self.cid, True, True)
        with self.assertRaisesRegex(ValueError, '停止联系'):
            self.service.prepare_reply(self.cid, '停止联系后不能发送')

    def test_previous_day_unresolved_greeting_is_available_for_resolution(self):
        self.service.jobs.reserve_greeting('boss-test-job', 'candidate')
        with self.service.store.db() as db:
            db.execute("UPDATE greeting_attempts SET day='2000-01-01',status='uncertain'")
        state = self.service.jobs.state()
        self.assertEqual(len(state['attempts']), 1)
        self.assertEqual(state['daily']['attempted'], 0)
        result = self.service.store.resolve_outbound('greeting', state['attempts'][0]['id'], 'not_sent', '核对平台记录，确认没有发送')
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(any('待核实' in b for b in self.service.jobs.state()['blockers']))

    def test_system_record_after_candidate_question_does_not_hide_reply(self):
        self.browser.snapshot['messages'] += [
            {'direction': 'in', 'kind': 'text', 'text': '新的待回复问题', 'time': '11:00'},
            {'direction': 'system', 'kind': 'system', 'text': '平台系统记录', 'time': '11:01'},
        ]
        response = {'text': '合成普通回复', 'needs_human': False, 'basis': ['conversation'], 'missing': []}
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.reply', return_value=response) as model:
            self.service.monitor_once()
        model.assert_called_once()
        drafts = self.service.store.rows('outbox')
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]['content'], '合成普通回复')

    def test_system_record_after_employer_reply_does_not_trigger_reply(self):
        self.browser.snapshot['messages'] += [
            {'direction': 'out', 'kind': 'text', 'text': '已人工回答', 'time': '11:00'},
            {'direction': 'system', 'kind': 'system', 'text': '平台系统记录', 'time': '11:01'},
        ]
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.reply') as model:
            self.service.monitor_once()
        model.assert_not_called()

    def test_forbidden_model_output_becomes_attention_without_regeneration(self):
        self.browser.snapshot['messages'].append({'direction': 'in', 'kind': 'text', 'text': '有新的问题', 'time': '11:00'})
        response = {'text': '面试事项交由人工确认。', 'needs_human': True, 'basis': ['conversation'], 'missing': []}
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.reply', return_value=response) as model:
            self.service.monitor_once()
            self.service.monitor_once()
        model.assert_called_once()
        self.assertEqual(self.browser.calls, 0)
        self.assertEqual(self.service.store.setting('reply_work:' + self.cid)['status'], 'needs_attention')

    def test_invalid_reply_output_waits_for_human_without_repeated_model_calls(self):
        self.browser.snapshot['messages'].append({'direction': 'in', 'kind': 'text', 'text': '合成新问题', 'time': '11:00'})
        valid = json.dumps({'text': '合成普通回复', 'needs_human': False, 'basis': ['conversation'], 'missing': []})
        for invalid in ['invalid-json', '[]', json.dumps({'text': '合成回复', 'basis': ['conversation']})]:
            with self.subTest(output=invalid):
                self.service.store.set_setting('reply_work:' + self.cid, {'status': 'waiting'})
                with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.call_anthropic_text', return_value=invalid) as model:
                    self.service.monitor_once()
                    self.service.monitor_once()
                    restarted = RecruitingService(self.service.store.path, lambda: {}, self.browser)
                    restarted.monitor_once()
                self.assertEqual(model.call_count, 1)
                self.assertEqual(self.service.store.setting('reply_work:' + self.cid)['status'], 'needs_attention')
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.call_anthropic_text', return_value=valid) as model:
            draft = self.service.prepare_reply(self.cid)
        self.assertEqual(draft['status'], 'draft')
        model.assert_called_once()
        self.assertEqual(self.browser.calls, 0)

    def test_new_candidate_message_reopens_reply_after_invalid_model_output(self):
        self.browser.snapshot['messages'].append({'direction': 'in', 'kind': 'text', 'text': '合成新问题', 'time': '11:00'})
        valid = json.dumps({'text': '合成普通回复', 'needs_human': False, 'basis': ['conversation'], 'missing': []})
        with patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic'), patch('bosshunter.recruiting.agent.call_anthropic_text', side_effect=['invalid-json', valid]) as model:
            self.service.monitor_once()
            self.browser.snapshot['messages'].append({'direction': 'in', 'kind': 'text', 'text': '另一个合成问题', 'time': '11:01'})
            self.service.monitor_once()
            self.service.monitor_once()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(self.service.store.setting('reply_work:' + self.cid)['status'], 'drafted')
        self.assertEqual(self.browser.calls, 0)

    def test_takeover_and_disable_auto_send_can_interrupt_held_service_lock(self):
        self.service.set_auto_send(self.cid, True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.lock:
                work = pool.submit(self.service.control, self.cid, True, False)
                work.result(timeout=.5)
                disable = pool.submit(self.service.set_auto_send, self.cid, False)
                disable.result(timeout=.5)
        c = self.service.store.row('conversations', self.cid)
        self.assertTrue(c['taken_over'])
        self.assertFalse(c['auto_send'])

    def test_reply_generation_is_owned_until_saved_and_reused(self):
        other = RecruitingService(self.service.store.path, lambda: {}, FakeBrowser())
        entered, release = threading.Event(), threading.Event()
        def reply(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return {'text': '合成普通回复', 'needs_human': False, 'basis': ['conversation'], 'missing': []}
        with patch('bosshunter.recruiting.agent.reply', side_effect=reply) as model:
            with ThreadPoolExecutor(max_workers=1) as pool:
                work = pool.submit(self.service.prepare_reply, self.cid)
                self.assertTrue(entered.wait(2))
                try:
                    with self.assertRaisesRegex(ValueError, '其他请求或进程'):
                        other.prepare_reply(self.cid)
                finally:
                    release.set()
                first = work.result(timeout=3)
            reused = other.prepare_reply(self.cid)
        model.assert_called_once()
        self.assertEqual(first['id'], reused['id'])
        self.assertEqual(len(self.service.store.rows('outbox')), 1)

    def test_state_and_reply_context_agree_after_model_config_changes(self):
        config = {'ai': {'model': 'synthetic-old'}}
        self.service.config_provider = lambda: config
        self.service.store.save_document(self.cid, 'human_verified_text', '合成简历经历' * 20, False, {})
        response = {'score': 80, 'earned': 80, 'coverage': 100, 'assessed_weight': 100, 'components': {}, 'questions': []}
        with patch('bosshunter.recruiting.agent.assess', return_value=response):
            self.service.assess(self.cid)
        before = self.service.state()['current_assessment_ids'][self.cid]
        self.assertTrue(before)
        self.assertEqual(self.service.reply_context(self.cid)['assessment']['id'], before)
        config['ai']['model'] = 'synthetic-new'
        self.assertIsNone(self.service.state()['current_assessment_ids'][self.cid])
        self.assertIsNone(self.service.reply_context(self.cid)['assessment'])
        self.assertEqual(len(self.service.state()['assessments']), 1)

    def test_mixed_pdf_ocr_reads_only_missing_page_and_preserves_text(self):
        reader = Mock()
        reader.is_encrypted = False
        reader.pages = [Mock(extract_text=Mock(return_value='文字层第一页')), Mock(extract_text=Mock(return_value=''))]
        with patch('pypdf.PdfReader', return_value=reader), patch('bosshunter.recruiting.local_session._ocr_available', return_value=True), patch('bosshunter.recruiting.local_session._ocr_pdf_pages', return_value=['扫描第二页']) as ocr:
            result = LocalBossSession.extract_pdf(b'%PDF-synthetic')
        ocr.assert_called_once_with(b'%PDF-synthetic', [2])
        self.assertIn('文字层第一页', result['text'])
        self.assertIn('扫描第二页', result['text'])
        self.assertEqual(result['meta']['ocr_pages'], [2])
        self.assertEqual(result['meta']['empty_pages'], [])
        self.assertFalse(result['complete'])

    def test_mixed_pdf_ocr_failure_keeps_known_text_with_explicit_gap(self):
        reader = Mock()
        reader.is_encrypted = False
        reader.pages = [Mock(extract_text=Mock(return_value='已提取的文字')), Mock(extract_text=Mock(return_value=''))]
        with patch('pypdf.PdfReader', return_value=reader), patch('bosshunter.recruiting.local_session._ocr_available', return_value=False):
            result = LocalBossSession.extract_pdf(b'%PDF-synthetic')
        self.assertIn('已提取的文字', result['text'])
        self.assertIn('OCR 未完成', result['meta']['note'])
        self.assertEqual(result['meta']['empty_pages'], [2])
        self.assertEqual(result['meta']['ocr_pages'], [])
        self.assertFalse(result['complete'])
