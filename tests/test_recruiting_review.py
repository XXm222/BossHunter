"""Review regressions; synthetic databases, clocks and platform records only."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from bosshunter.recruiting.service import RecruitingService
from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.store import encode, now
from recruiting_fixtures import authorize
from test_recruiting import FakeBrowser, FakeLocalSession, http_msg


class ReviewTests(unittest.TestCase):
    def test_manual_sync_preserves_new_message_work_for_worker_after_restart(self):
        self.service.browser.snapshot['messages'].append({'direction': 'in', 'kind': 'text', 'text': '合成新问题', 'time': '11:00'})
        self.service.sync(self.c['id'], process=False)
        self.assertEqual(self.service.store.setting('reply_work:' + self.c['id'])['status'], 'waiting')
        restarted = RecruitingService(self.service.store.path, lambda: self.config, self.service.browser)
        draft = restarted.store.draft(self.c['id'], 'reply', '合成答复', {})
        with patch.object(restarted, 'prepare_reply', return_value=draft) as prepare, patch.object(restarted, '_auto_send_if_allowed', return_value=False):
            result = restarted.monitor_once()
        self.assertFalse(result['changed'])
        prepare.assert_called_once_with(self.c['id'])
        self.assertEqual(restarted.store.setting('reply_work:' + self.c['id'])['status'], 'drafted')

    def test_unchanged_sync_or_unconfirmed_contact_does_not_queue_history(self):
        self.service.sync(self.c['id'], process=False)
        self.assertIsNone(self.service.store.setting('reply_work:' + self.c['id']))
        source = {**deepcopy(self.service.browser.snapshot), 'id': 'unconfirmed'}
        self.service.store.import_conversation(source)
        source['messages'].append({'direction': 'in', 'kind': 'text', 'text': '合成新增', 'time': '11:01'})
        self.service.store.import_conversation(source, track_reply=True)
        self.assertIsNone(self.service.store.setting('reply_work:unconfirmed'))

    def test_daily_budget_override_expires_without_clearing_usage(self):
        from datetime import datetime, timezone, timedelta
        from zoneinfo import ZoneInfo
        stamp = datetime.now(ZoneInfo('Asia/Shanghai'))
        self.service.store.count_request('conversation', 100)
        self.service.store.set_setting('request_daily_override', {'date': stamp.date().isoformat(), 'limit': 120})
        self.assertEqual(self.service.store.request_daily_limit(100), 120)
        with patch('bosshunter.recruiting.store.datetime') as clock:
            clock.now.return_value = stamp + timedelta(days=1)
            self.assertEqual(self.service.store.request_daily_limit(100), 100)
        self.assertEqual(self.service.store.request_budget()['count'], 1)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.config = {'ai': {'model': 'synthetic-model', 'api_key': 'synthetic-key'}}
        self.service = RecruitingService(Path(self.temp.name) / 'db', lambda: self.config, FakeBrowser())
        authorize(self.service)
        self.c = self.service.import_current()

    def tearDown(self):
        self.temp.cleanup()

    def test_batch_state_matches_individual_current_assessments_and_documents(self):
        cids = [self.c['id'], 'other', 'empty']
        for cid in cids[1:]:
            snapshot = {**deepcopy(self.service.browser.snapshot), 'id': cid}
            self.service.store.import_conversation(snapshot)
        first = self.service.store.save_document(cids[0], 'human', '合成经历甲' * 20, True, {})
        self.service.store.save_document(cids[0], 'human', '合成经历乙' * 20, False, {})
        self.service.store.save_document(cids[0], 'human', first['text'], True, {})
        self.service.store.save_document(cids[1], 'human', '其他合成经历' * 20, True, {})
        with patch('bosshunter.recruiting.agent.assess', side_effect=lambda *args: {'components': {}, 'questions': [], 'score': 80}):
            self.service.assess(cids[0])
            self.service.assess(cids[1])
        # A newer result with matching hash but false document evidence must not
        # hide a genuine older valid result.
        digest = self.service.assessment_input(cids[0])[3]
        with self.service.store.db() as db:
            db.execute('UPDATE assessments SET input_hash=? WHERE conversation_id=?', ('old-digest', cids[0]))
            valid = self.service.reply_context(cids[1])['assessment']
            result = {**valid['result'], 'document_id': first['id']}
            db.execute('INSERT INTO assessments VALUES (?,?,?,?,?)', ('valid', cids[0], digest, encode(result), now()))
            db.execute('INSERT INTO assessments VALUES (?,?,?,?,?)', ('invalid-evidence', cids[0], digest + '-invalid', encode({**result, 'document_id': 'wrong'}), now()))
        expected = {cid: (self.service.reply_context(cid)['assessment'] or {}).get('id') for cid in cids}
        data = self.service.state()
        self.assertEqual(data['current_assessment_ids'], expected)
        self.assertEqual(next(d for d in data['documents'] if d['conversation_id'] == cids[0])['id'], first['id'])
        # Invalid document selections retain the individual lookup's fallback,
        # including a pointer to another candidate's document.
        self.service.store.set_setting('current_document:' + cids[0], self.service.store.current_document(cids[1])['id'])
        self.service.store.set_setting('current_document:' + cids[1], 'missing-document')
        data = self.service.state()
        for cid in cids:
            expected = (self.service.reply_context(cid)['assessment'] or {}).get('id')
            self.assertEqual(data['current_assessment_ids'][cid], expected)
        self.config['ai']['model'] = 'another-synthetic-model'
        self.assertTrue(all(value is None for value in self.service.state()['current_assessment_ids'].values()))

    def test_state_query_count_and_model_resolution_do_not_grow_with_contacts(self):
        with patch.object(self.service.store, 'db', wraps=self.service.store.db) as connections:
            self.service.state()
            baseline = connections.call_count
        stamp = now()
        with self.service.store.db() as db:
            snapshot = self.service.browser.snapshot
            db.executemany('INSERT INTO conversations(id,name,position_id,snapshot,context_hash,updated_at) VALUES (?,?,?,?,?,?)',
                           [(f'bulk-{i}', '合成联系人', self.c['position_id'], encode({**snapshot, 'id': f'bulk-{i}'}), f'hash-{i}', stamp) for i in range(950)])
        with patch.object(self.service.store, 'db', wraps=self.service.store.db) as connections, patch('bosshunter.recruiting.agent.get_ai_api_key', return_value='synthetic') as key:
            data = self.service.state()
        self.assertEqual(connections.call_count, baseline)
        key.assert_called_once()
        self.assertEqual(len(data['conversations']), 951)
        self.assertEqual(data['monitor']['allowed_count'], 1)
        self.assertNotIn('settings', data)
        self.assertNotIn('synthetic-key', json.dumps(data))

    def test_reenable_monitor_resets_errors_even_if_worker_misses_disabled_state(self):
        self.service.set_monitor_enabled(True)
        other = RecruitingService(self.service.store.path, lambda: self.config, FakeBrowser())
        clock = {'time': 1000, 'waits': 0, 'stop': False}
        signal = Mock()
        signal.is_set.side_effect = lambda: clock['stop']
        def wait(_):
            clock['time'] += 1000
            clock['waits'] += 1
            if clock['waits'] == 3:
                self.assertFalse(self.service.store.setting('monitor_enabled'))
                other.set_monitor_enabled(True)
            if clock['waits'] >= 5:
                clock['stop'] = True
        signal.wait.side_effect = wait
        with patch('bosshunter.recruiting.service.Thread'), patch('bosshunter.recruiting.service.time.time', side_effect=lambda: clock['time']), patch.object(self.service, 'monitor_once', side_effect=BrowserError('合成读取失败')) as monitor:
            self.service.worker_loop(signal)
        self.assertEqual(monitor.call_count, 5)
        self.assertTrue(self.service.store.setting('monitor_enabled'))

    def test_previous_run_failure_cannot_pause_a_newly_enabled_run(self):
        self.service.set_monitor_enabled(True)
        other = RecruitingService(self.service.store.path, lambda: self.config, FakeBrowser())
        clock = {'time': 1000, 'waits': 0, 'stop': False, 'attempts': 0}
        signal = Mock()
        signal.is_set.side_effect = lambda: clock['stop']
        def monitor():
            clock['attempts'] += 1
            if clock['attempts'] == 3:
                other.set_monitor_enabled(False)
                other.set_monitor_enabled(True)
            raise BrowserError('合成旧任务失败')
        def wait(_):
            clock['time'] += 1000
            clock['waits'] += 1
            if clock['waits'] >= 3:
                clock['stop'] = True
        signal.wait.side_effect = wait
        with patch('bosshunter.recruiting.service.Thread'), patch('bosshunter.recruiting.service.time.time', side_effect=lambda: clock['time']), patch.object(self.service, 'monitor_once', side_effect=monitor):
            self.service.worker_loop(signal)
        self.assertTrue(self.service.store.setting('monitor_enabled'))

    def test_failure_pause_checks_current_run_atomically(self):
        self.service.set_monitor_enabled(True)
        old = self.service.store.setting('monitor_run_id')
        other = RecruitingService(self.service.store.path, lambda: self.config, FakeBrowser())
        other.set_monitor_enabled(True)
        self.assertFalse(self.service.store.pause_monitor_run(old))
        self.assertTrue(self.service.store.setting('monitor_enabled'))
        self.assertTrue(self.service.store.pause_monitor_run(other.store.setting('monitor_run_id')))
        self.assertFalse(self.service.store.setting('monitor_enabled'))

    def test_send_precheck_merges_once_and_preserves_concurrent_history(self):
        snapshot = {**deepcopy(self.service.browser.snapshot), 'account_uid': '456',
                    'messages': [http_msg(1, 'in', 'text', '合成问题')], 'stable_message_ids': True}
        browser = FakeBrowser()
        browser.snapshot = deepcopy(snapshot)
        session = FakeLocalSession(snapshot)
        service = RecruitingService(self.service.store.path, lambda: self.config, browser, session)
        service.store.import_conversation(snapshot)
        draft = service.prepare_reply(snapshot['id'], '合成普通回复')
        def read(*args, **kwargs):
            newer = {**deepcopy(snapshot), 'received_resume_message_id': '2',
                     'messages': snapshot['messages'] + [http_msg(2, 'in', 'card', '合成附件') ]}
            service.store.import_conversation(newer, append=True)
            return {**snapshot, 'messages': []}
        session.read_conversation = read
        with patch.object(service, '_append_new_messages', side_effect=AssertionError('不应在服务层再次合并')):
            with self.assertRaisesRegex(ValueError, '会话有新消息'):
                service.execute(draft['id'])
        stored = json.loads(service.store.row('conversations', snapshot['id'])['snapshot'])
        self.assertEqual([m['id'] for m in stored['messages']], ['1', '2'])
        self.assertEqual(stored['account_uid'], '456')
        self.assertEqual(stored['position_platform_id'], 'test-job')
        self.assertEqual(stored['received_resume_message_id'], '2')
        self.assertEqual(browser.calls, 0)


if __name__ == '__main__':
    unittest.main()
