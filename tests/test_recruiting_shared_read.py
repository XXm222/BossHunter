"""Synthetic in-flight sharing, failure propagation and reply freshness guards."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from bosshunter.recruiting.browser import AccountPauseError, BrowserError, TaskCancelled
from bosshunter.recruiting.shared_read import shared_read
from bosshunter.recruiting.store import Store, now
from bosshunter.recruiting.service import RecruitingService
import test_recruiting_review
from test_recruiting import FakeLocalSession


def process_reader(path, entered, release, results):
    store = Store(path)
    def read():
        entered.set()
        if not release.wait(5):
            raise RuntimeError('process reader not released')
        return {'messages': ['cross-process']}
    results.put(shared_read(store, 'process', read, lambda: None))


class SharedReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.a = Store(Path(self.temp.name) / 'db')
        self.b = Store(self.a.path)

    def tearDown(self):
        self.temp.cleanup()

    def concurrent(self, reader, follower_reader):
        entered, release, joined = Event(), Event(), Event()
        def first():
            entered.set()
            if not release.wait(3):
                raise AssertionError('reader not released')
            return reader()
        checks = []
        def follower_check():
            checks.append(1)
            if len(checks) >= 2:
                joined.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            lead = pool.submit(shared_read, self.a, 'same', first, lambda: None)
            self.assertTrue(entered.wait(2))
            follow = pool.submit(shared_read, self.b, 'same', follower_reader, follower_check)
            try:
                self.assertTrue(joined.wait(2))
            finally:
                release.set()
            return lead, follow

    def test_same_read_shared_between_independent_stores_but_later_read_is_fresh(self):
        second = Mock(return_value={'messages': ['should not run']})
        lead, follow = self.concurrent(lambda: {'messages': ['one']}, second)
        self.assertEqual(lead.result(), follow.result())
        second.assert_not_called()
        self.assertEqual(shared_read(self.b, 'same', lambda: {'messages': ['new']}, lambda: None), {'messages': ['new']})

    def test_platform_rejection_propagates_without_a_second_request(self):
        def reject():
            raise AccountPauseError('synthetic refusal')
        second = Mock()
        lead, follow = self.concurrent(reject, second)
        for future in (lead, follow):
            with self.assertRaises(AccountPauseError):
                future.result()
        second.assert_not_called()

    def test_none_result_is_shared_as_no_change(self):
        second = Mock()
        lead, follow = self.concurrent(lambda: None, second)
        self.assertIsNone(lead.result())
        self.assertIsNone(follow.result())
        second.assert_not_called()

    def test_different_keys_do_not_share(self):
        entered, release = Event(), Event()
        def read():
            entered.set()
            release.wait(3)
            return 'old'
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(shared_read, self.a, 'cursor-1', read, lambda: None)
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(shared_read(self.b, 'cursor-2', lambda: 'new', lambda: None), 'new')
            finally:
                release.set()
            self.assertEqual(first.result(), 'old')

    def test_dead_leader_can_be_replaced_by_a_later_explicit_read(self):
        with self.a.db() as db:
            db.execute('INSERT INTO read_flights VALUES (?,?,?,?,?,?,?)',
                       ('dead','same','0:dead','running',None,None,now()))
        self.assertEqual(shared_read(self.b,'same',lambda: 'new',lambda: None),'new')
        with self.a.db() as db:
            self.assertEqual(db.execute("SELECT status FROM read_flights WHERE token='dead'").fetchone()[0],'failed')

    def test_waiter_does_not_retry_if_leader_dies(self):
        with self.a.db() as db:
            db.execute('INSERT INTO read_flights VALUES (?,?,?,?,?,?,?)',
                       ('lost','same',self.a.owner,'running',None,None,now()))
        read=Mock()
        with patch.object(self.b,'owner_alive',side_effect=[True,False]):
            with self.assertRaises(BrowserError):
                shared_read(self.b,'same',read,lambda: None)
        read.assert_not_called()

    def test_waiter_cancellation_does_not_cancel_leader(self):
        entered, release = Event(), Event()
        def read():
            entered.set()
            release.wait(3)
            return 'done'
        count = []
        def cancel():
            count.append(1)
            if len(count) == 2:
                raise TaskCancelled('synthetic stop')
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(shared_read, self.a, 'same', read, lambda: None)
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(TaskCancelled):
                    shared_read(self.b, 'same', Mock(), cancel)
            finally:
                release.set()
            self.assertEqual(first.result(), 'done')

    def test_web_and_worker_processes_share_one_read(self):
        context = multiprocessing.get_context('spawn')
        entered, release, results = context.Event(), context.Event(), context.Queue()
        process = context.Process(target=process_reader, args=(str(self.a.path), entered, release, results))
        process.start()
        duplicate = Mock(return_value='must not run')
        checks = []
        def check():
            checks.append(1)
            if len(checks) >= 2:
                release.set()
        try:
            self.assertTrue(entered.wait(5))
            result = shared_read(self.b, 'process', duplicate, check)
            self.assertEqual(result, results.get(timeout=3))
            duplicate.assert_not_called()
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join()
            results.close()
        self.assertEqual(process.exitcode, 0)


class ReplyReuseTests(unittest.TestCase):
    setUp = test_recruiting_review.ReviewTests.setUp
    tearDown = test_recruiting_review.ReviewTests.tearDown

    def test_monitor_reuses_just_synced_context_but_still_checks_after_generation(self):
        self.service.browser.snapshot['messages'].append({'direction': 'in', 'kind': 'text', 'text': '合成新问题', 'time': '11:00'})
        with patch.object(self.service, 'sync', wraps=self.service.sync) as sync, \
             patch('bosshunter.recruiting.agent.reply', return_value={'text': '合成答复', 'basis': ['conversation'], 'needs_human': False, 'missing': []}), \
             patch.object(self.service, '_auto_send_if_allowed', return_value=False):
            self.service.monitor_once()
        self.assertEqual(sync.call_count, 2)  # monitor + post-model; no redundant pre-model read
        self.assertTrue(sync.call_args.kwargs['fresh'])
        self.assertIsNone(self.service._reply_progress_local.fresh_sync)

    def test_safety_reads_bypass_inflight_sharing(self):
        self.service.local_session = Mock()
        self.service.local_session.read_conversation.return_value = None
        with patch('bosshunter.recruiting.shared_read.shared_read') as share:
            self.service._read_conversation('peer','name','job',_fresh=True)
        share.assert_not_called()
        self.service.local_session.read_conversation.assert_called_once_with('peer','name','job')

    def test_old_or_changed_context_cannot_skip_generation_preflight(self):
        for age, digest in ((31, self.c['context_hash']), (0, 'changed')):
            self.service._reply_progress_local.cid = self.c['id']
            self.service._reply_progress_local.fresh_sync = (self.c['id'], digest, time.monotonic() - age)
            with patch.object(self.service, 'sync', wraps=self.service.sync) as sync, \
                 patch('bosshunter.recruiting.agent.reply', return_value={'text': '合成答复', 'basis': ['conversation'], 'needs_human': False, 'missing': []}):
                self.service.prepare_reply(self.c['id'])
            self.assertGreaterEqual(sync.call_count, 1)

    def test_service_call_identity_includes_account_and_cursor(self):
        self.service.local_session = Mock()
        self.service.local_session.read_conversation.return_value = None
        with patch('bosshunter.recruiting.shared_read.shared_read', side_effect=lambda store,key,read,check: (key, read())) as share:
            keys = [self.service._read_conversation('peer', 'name', 'job', account, since_mid=cursor)[0]
                    for account,cursor in [('one',1), ('one',2), ('two',1)]]
        self.assertEqual(len(set(keys)), 3)

    def test_overlapping_syncs_in_same_service_share_http_read(self):
        entered, release, joined = Event(), Event(), Event()
        local = FakeLocalSession(deepcopy(self.service.browser.snapshot))
        def read(*args, **kwargs):
            entered.set()
            release.wait(3)
            return None
        local.read_conversation = Mock(side_effect=read)
        service = RecruitingService(self.service.store.path, lambda: self.config, self.service.browser, local)
        original = service._check_stopped
        checks = []
        def check():
            original()
            checks.append(1)
            if len(checks) >= 3:
                joined.set()
        service._check_stopped = check
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.sync, self.c['id'], process=False)
            self.assertTrue(entered.wait(2))
            second = pool.submit(service.sync, self.c['id'], process=False)
            try:
                self.assertTrue(joined.wait(2))
            finally:
                release.set()
            self.assertEqual(first.result()['id'], second.result()['id'])
        self.assertEqual(local.read_conversation.call_count, 1)
