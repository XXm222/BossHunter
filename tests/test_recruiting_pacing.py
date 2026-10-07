"""Shared request pacing with synthetic clocks, HTTP and browser data only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch
import httpx

from bosshunter.recruiting.browser import AccountPauseError, BossBrowser, BrowserError, TaskCancelled
from bosshunter.recruiting.local_session import LocalBossSession
from bosshunter.recruiting.service import RecruitingService
from bosshunter.recruiting.store import RequestPaused, RequestThrottled, Store
from recruiting_fixtures import authorize
from test_recruiting import FakeBrowser
from test_recruiting_local_chat import message, response


class RequestPacingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'

    def tearDown(self):
        self.temp.cleanup()

    def service(self, **cfg):
        return RecruitingService(self.path, lambda: {'recruiting': cfg}, FakeBrowser())

    def test_concurrent_instances_share_one_request_slot_and_restart_deadline(self):
        stores = [Store(self.path) for _ in range(4)]
        stamp = datetime.now(timezone.utc)
        def attempt(store):
            try:
                store.count_request('conversation', 20, min_interval=120)
                return True
            except RequestThrottled:
                return False
        with patch('bosshunter.recruiting.store.datetime') as clock:
            clock.now.return_value = stamp
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(sum(pool.map(attempt, stores)), 1)
            restarted = Store(self.path)
            self.assertEqual(restarted.request_budget()['count'], 1)
            with self.assertRaises(RequestThrottled) as caught:
                restarted.count_request('jobs', 20, min_interval=120)
            self.assertEqual(caught.exception.seconds, 120)
            clock.now.return_value = datetime.fromtimestamp(stamp.timestamp() + 120, timezone.utc)
            restarted.count_request('jobs', 20, min_interval=180)
        self.assertEqual(restarted.request_budget()['by_kind'], {'conversation': 1, 'jobs': 1})

    def test_shared_wait_rechecks_slot_without_counting_waits(self):
        service = self.service(read_delay_min=120, read_delay_max=120)
        with patch.object(service.store, 'count_request', side_effect=[RequestThrottled(2), {}]) as counter, patch.object(service.local_session, '_wait_cancelled') as wait:
            service._count_request('identity')
        self.assertEqual(counter.call_count, 2)
        wait.assert_called_once_with(1.0)
        self.assertEqual(counter.call_args.kwargs['min_interval'], 120)

    def test_stop_interrupts_shared_cooldown_before_another_request(self):
        service = self.service()
        service._stop_event = Event()
        service.store.set_setting('monitor_enabled', True)
        with patch.object(service.store, 'count_request', side_effect=RequestThrottled(120)) as counter, patch.object(service.local_session, '_wait_cancelled', side_effect=lambda _: service.store.set_setting('monitor_enabled', False)):
            with self.assertRaises(TaskCancelled):
                service._count_request('conversation')
        counter.assert_called_once()
        self.assertEqual(service.store.request_budget()['count'], 0)

    def test_exhausted_budget_pauses_worker_immediately(self):
        service = self.service(read_daily_limit=1)
        service.store.count_request('jobs', 1)
        service._stop_event = Event()
        service.store.set_setting('monitor_enabled', True)
        with self.assertRaises(BrowserError):
            service._count_request('conversation')
        self.assertFalse(service.store.setting('monitor_enabled'))
        self.assertEqual(service.store.request_budget()['count'], 1)

    def test_every_page_uses_shared_counter_instead_of_operation_wait(self):
        calls = []
        counter = Mock()
        throttle = Mock()
        def handle(request):
            calls.append(request)
            return response([message(2)], True, 2) if len(calls) == 1 else response([message(1)])
        session = LocalBossSession(lambda: CookieJar(), httpx.MockTransport(handle), throttle=throttle, request_counter=counter)
        session.read_conversation('123-0', '测试候选人', '测试岗位', throttle=False)
        self.assertEqual(counter.call_count, 2)
        self.assertEqual(len(calls), 2)
        throttle.wait.assert_not_called()

    def test_standalone_send_precheck_cannot_skip_request_wait(self):
        throttle = Mock()
        throttle.wait.return_value = False
        session = LocalBossSession(lambda: CookieJar(), httpx.MockTransport(lambda _: response([message()])), throttle=throttle)
        session.read_conversation('123-0', '测试候选人', '测试岗位', throttle=False)
        throttle.wait.assert_called_once()

    def test_http_403_and_429_are_account_pauses_without_retry(self):
        for status in (403, 429):
            for operation in ('conversation', 'jobs', 'quota', 'friend'):
                with self.subTest(status=status, operation=operation):
                    calls = []
                    session = LocalBossSession(lambda: CookieJar(), httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(status)))
                    with self.assertRaises(AccountPauseError):
                        if operation == 'conversation':
                            session.read_conversation('123-0', '测试候选人', '测试岗位')
                        elif operation == 'jobs':
                            session.read_jobs()
                        elif operation == 'quota':
                            session.read_greeting_quota()
                        else:
                            session.read_friend_jobs(['123'])
                    self.assertEqual(len(calls), 1)

    def test_account_pause_does_not_continue_discovery_at_next_job(self):
        service = self.service()
        authorize(service)
        service.verifier = Mock()
        service.verifier.select_job.side_effect = AccountPauseError('合成账号验证要求')
        with self.assertRaises(AccountPauseError):
            service.run_discovery(throttle_delay=(0, 0))
        service.verifier.select_job.assert_called_once()
        service.verifier.greet.assert_not_called()

    def test_contact_loading_checks_gate_before_scrolling_and_has_bound(self):
        browser = BossBrowser(runtime=Mock())
        browser.evaluate = Mock()
        with self.assertRaises(TaskCancelled):
            browser.read_contact_list(load_all=True, before_load=Mock(side_effect=TaskCancelled('合成停止')))
        browser.evaluate.assert_not_called()
        browser.evaluate.side_effect = [{'count': i, 'contacts': []} for i in range(10)] + [{'contacts': []}]
        gate = Mock()
        with patch('bosshunter.recruiting.browser.time.sleep'):
            browser.read_contact_list(load_all=True, before_load=gate)
        self.assertEqual(gate.call_count, 10)
        self.assertEqual(browser.contact_coverage['reason'], 'scroll_limit')

    def test_conservative_defaults_and_explicit_monitor_interval(self):
        self.assertEqual(self.service().monitor['interval_seconds'], 600)
        self.assertEqual(self.service(monitor_interval_seconds=900).monitor['interval_seconds'], 900)

    def test_refusal_cooldown_is_shared_and_does_not_resume_monitor_automatically(self):
        store = Store(self.path)
        stamp = datetime.now(timezone.utc)
        store.set_setting('monitor_enabled', True)
        with patch('bosshunter.recruiting.store.datetime') as clock:
            clock.now.return_value = stamp
            store.pause_requests(1800)
            restarted = Store(self.path)
            with self.assertRaises(RequestPaused):
                restarted.count_request('conversation', 20)
            self.assertEqual(restarted.request_budget()['count'], 0)
            clock.now.return_value = datetime.fromtimestamp(stamp.timestamp() + 1800, timezone.utc)
            restarted.count_request('conversation', 20)
        self.assertFalse(restarted.setting('monitor_enabled'))

    def test_retry_after_respects_longer_platform_cooldown(self):
        pause = Mock()
        session = LocalBossSession(account_pause_handler=pause)
        for header, expected in [('60', 1800), ('3600', 3600), ('invalid', 1800)]:
            with self.subTest(header=header):
                with self.assertRaises(AccountPauseError):
                    session._check_response(httpx.Response(429, headers={'Retry-After': header}))
                pause.assert_called_with(expected)

    def test_other_instance_refusal_blocks_final_send_guard(self):
        service = self.service()
        authorize(service)
        c = service.import_current()
        draft = service.prepare_reply(c['id'], '合成普通回复')
        service.store.claim(draft['id'])
        other = self.service()
        other._pause_platform_requests(1800)
        with self.assertRaises(TaskCancelled):
            service._send_guard(draft['id'], claimed=True)
        self.assertEqual(service.browser.calls, 0)
        with self.assertRaises(AccountPauseError):
            other._count_request('jobs')
        self.assertEqual(other.store.request_budget()['count'], 0)

    def test_shared_cooldown_never_shortens_a_previous_pause(self):
        store = Store(self.path)
        until = store.pause_requests(3600)
        store.pause_requests(1800)
        self.assertEqual(store.setting('request_paused_until'), until)

    def test_browser_identity_fetch_refusal_establishes_cooldown(self):
        browser = BossBrowser(runtime=Mock())
        browser.evaluate = Mock(return_value={'account_verified': False, 'refusal_status': 429, 'retry_after': '3600'})
        pause = Mock()
        with self.assertRaises(AccountPauseError):
            browser.verify_account('123-0', '456', on_refusal=pause)
        pause.assert_called_once_with(3600)


if __name__ == '__main__':
    unittest.main()
