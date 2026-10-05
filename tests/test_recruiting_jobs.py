"""Local cookie transport, complete sync and explicit job-selection boundaries."""
from http.cookiejar import CookieJar
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.jobs import RecruitingJobs
from bosshunter.recruiting.local_session import LocalBossSession
from bosshunter.recruiting.store import Store


def raw(ident='1', status=0):
    return {'encryptJobId': ident, 'jobName': '测试岗位', 'jobStatus': status, 'jobAuditStatus': 3,
            'salaryDesc': '测试薪资', 'locationName': '测试城市'}


def page(items, number=1, total=None, more=False):
    return {'code': 0, 'zpData': {'data': items, 'page': number, 'totalSize': total if total is not None else len(items), 'hasMore': more}}


class CookieTransportTests(unittest.TestCase):
    def session(self, handler):
        return LocalBossSession(lambda: CookieJar(), httpx.MockTransport(handler))

    def test_all_pages_scoped_read_only_and_platform_identity(self):
        requests = []
        def handle(request):
            requests.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.host, 'www.zhipin.com')
            self.assertEqual(request.url.path, '/wapi/zpjob/job/data/list')
            num = int(request.url.params['page'])
            return httpx.Response(200, json=page([raw(str(num))], num, 2, num == 1))
        with patch('bosshunter.recruiting.local_session.time.sleep'):
            result = self.session(handle).read_jobs()
        self.assertTrue(result['complete'])
        self.assertEqual([j['platform_id'] for j in result['jobs']], ['1', '2'])
        self.assertEqual(len(requests), 2)

    def test_login_failure_does_not_retry_or_leak_response(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={'code': 37, 'message': 'secret-cookie'})
        with self.assertRaises(BrowserError) as caught:
            self.session(handle).read_jobs()
        self.assertNotIn('secret-cookie', str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_does_not_follow_redirects_with_login_cookie(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(302, headers={'Location': 'https://example.org'})
        with self.assertRaises(BrowserError):
            self.session(handle).read_jobs()
        self.assertEqual(len(calls), 1)

    def test_partial_duplicate_and_unknown_status(self):
        with self.assertRaises(BrowserError):
            self.session(lambda r: httpx.Response(200, json=page([raw()], total=2))).read_jobs()
        with self.assertRaises(BrowserError):
            self.session(lambda r: httpx.Response(200, json=page([raw(), raw()]))).read_jobs()
        result = self.session(lambda r: httpx.Response(200, json=page([raw(status=99)]))).read_jobs()
        self.assertEqual(result['jobs'][0]['status'], '状态待核实')


class JobSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'jobs.db')
        self.jobs = RecruitingJobs(self.store)
        self.snapshot = {'total': 1, 'complete': True, 'jobs': [{'platform_id': 'one', 'title': '岗位', 'details': [], 'status': '开放中'}]}
        self.jobs.import_snapshot(self.snapshot)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_jobs_unselected_selection_survives_sync_and_restart(self):
        self.assertEqual(self.jobs.state()['selected_count'], 0)
        self.jobs.select(['boss-one'])
        self.jobs.import_snapshot(self.snapshot)
        self.assertEqual(RecruitingJobs(self.store).state()['selected_count'], 1)

    def test_closed_and_missing_jobs_are_deselected_and_do_not_reenable(self):
        self.jobs.select(['boss-one'])
        self.snapshot['jobs'][0]['status'] = '已关闭'
        self.jobs.import_snapshot(self.snapshot)
        self.assertEqual(self.jobs.state()['selected_count'], 0)
        with self.assertRaises(ValueError):
            self.jobs.select(['boss-one'])
        self.snapshot['jobs'][0]['status'] = '开放中'
        self.jobs.import_snapshot(self.snapshot)
        self.assertEqual(self.jobs.state()['selected_count'], 0)
        self.jobs.select(['boss-one'])
        self.jobs.import_snapshot({'jobs': [], 'total': 0, 'complete': True})
        self.assertEqual(self.jobs.state()['selected_count'], 0)

    def test_incomplete_sync_preserves_previous_selection(self):
        self.jobs.select(['boss-one'])
        with self.assertRaises(ValueError):
            self.jobs.import_snapshot({'jobs': [], 'total': 1, 'complete': False})
        self.assertEqual(self.jobs.state()['selected_count'], 1)

    def test_unknown_ids_are_not_allowed(self):
        with self.assertRaises(ValueError):
            self.jobs.select(['someone-else'])
        self.assertEqual(self.jobs.state()['selected_count'], 0)

    def test_account_budget_defaults_and_validation(self):
        self.assertEqual(self.jobs.config()['mode'], 'platform')
        self.jobs.save_budget('custom', 3)
        self.assertEqual(RecruitingJobs(self.store).state()['daily']['custom_remaining'], 3)
        for mode, value in [('custom', True), ('custom', 0), ('custom', 3.2), ('unlimited', 100)]:
            with self.assertRaises(ValueError):
                self.jobs.save_budget(mode, value)
        self.assertIsNone(self.jobs.state()['daily']['platform_remaining'])
        self.assertFalse(self.jobs.state()['running'])

    def test_day_rollover_does_not_clear_uncertain_attempts(self):
        with self.store.db() as db:
            db.execute("INSERT INTO greeting_attempts(day,job_id,candidate_id,status,created_at) VALUES ('2000-01-01','boss-one','c','sending','2000-01-01')")
        state = RecruitingJobs(self.store).state()
        self.assertEqual(state['daily']['attempted'], 0)
        self.assertTrue(any('待核实' in b for b in state['blockers']))

    def test_console_records_are_today_only_and_preserve_exact_identity(self):
        from bosshunter.recruiting.jobs import day_key
        with self.store.db() as db:
            db.executemany('INSERT INTO greeting_attempts(day,job_id,candidate_id,status,created_at) VALUES (?,?,?,?,?)', [
                (day_key(), 'boss-one', 'exact-id', 'uncertain', '2026-09-19T01:00:00Z'),
                ('2000-01-01', 'boss-one', 'old-id', 'sent', '2000-01-01'),
            ])
        state = self.jobs.state()
        self.assertEqual(len(state['attempts']), 1)
        self.assertEqual(state['attempts'][0]['candidate_id'], 'exact-id')
        self.assertEqual(state['attempts'][0]['status'], 'uncertain')
        self.assertEqual(state['daily']['sent'], 0)

if __name__ == '__main__':
    unittest.main()
