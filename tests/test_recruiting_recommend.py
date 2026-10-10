"""Recommend-page patchright automation: greet, select job, read candidates."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.recommend import RecommendVerifier
from bosshunter.recruiting.service import RecruitingService

RECOMMEND_PAGE = "https://www.zhipin.com/web/chat/recommend"
FRAME_URL = "https://www.zhipin.com/web/frame/recommend/xxx"


class Frame:
    def __init__(self, url, result):
        self.url = url
        self._result = result

    def evaluate(self, js):
        return self._result


class Page:
    def __init__(self, url, frames, target_id='test-target'):
        self.url = url
        self.frames = frames
        for frame in frames:
            frame.page = self
        self.target_id = target_id
        self.reload_calls = 0
        self.listeners = {}

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        self.listeners.pop(event, None)

    def reload(self, **kwargs):
        self.reload_calls += 1

    def wait_for_timeout(self, milliseconds):
        pass


class Context:
    def __init__(self, pages):
        self.pages = pages
        for page in pages:
            page.context = self

    def new_cdp_session(self, page):
        session = Mock()
        session.send.return_value = {'targetInfo': {'targetId': page.target_id}}
        return session


class Browser:
    def __init__(self, context):
        self.contexts = [context]


class Chromium:
    def __init__(self, browser):
        self._browser = browser

    def connect_over_cdp(self, url):
        return self._browser


class PW:
    def __init__(self, browser):
        self.chromium = Chromium(browser)
        self.stopped = False

    def stop(self):
        self.stopped = True


class SyncPlaywright:
    def __init__(self, pw):
        self._pw = pw

    def start(self):
        return self._pw


class FakeLocalSession:
    def __init__(self, contacts=None):
        self._contacts = contacts or []

    def list_contacts(self):
        return self._contacts

    def read_greeting_quota(self):
        return {'remaining': 100, 'unlimited': False}


class GreetFrame:
    """frame 的 evaluate 按 JS 内容分派：读岗位、点击、读文案分别返回。"""

    def __init__(self, url, job_id="encryptJob123", click_result="clicked", greet_text="继续沟通"):
        self.url = url
        self._job_id = job_id
        self._click_result = click_result
        self._greet_text = greet_text

    def evaluate(self, js):
        if "btn.click()" in js:
            return self._click_result
        if "job-item.curr" in js:
            return self._job_id
        return self._greet_text


def greet(uid, frame, wait_timeout=0.05):
    page = Page(RECOMMEND_PAGE, [frame])
    pw = PW(Browser(Context([page])))
    verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222", wait_timeout=wait_timeout)
    with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)):
        result = verifier.greet(uid)
    return result, pw, page


class GreetVerifierTests(unittest.TestCase):
    FRAME_URL = "https://www.zhipin.com/web/frame/recommend/"

    def test_final_preflight_blocks_click_after_connect(self):
        frame = GreetFrame(self.FRAME_URL)
        frame.evaluate = Mock(wraps=frame.evaluate)
        pw = PW(Browser(Context([Page(RECOMMEND_PAGE, [frame])])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
        from bosshunter.recruiting.browser import TaskCancelled
        guard = Mock(side_effect=TaskCancelled('停止'))
        with patch('bosshunter.recruiting.recommend.sync_playwright', return_value=SyncPlaywright(pw)):
            with self.assertRaises(TaskCancelled):
                verifier.greet('geek123', preflight=guard)
        self.assertFalse(any('btn.click()' in call.args[0] for call in frame.evaluate.call_args_list))

    def test_greet_clicks_and_confirms(self):
        result, pw, page = greet("geek123", GreetFrame(self.FRAME_URL, job_id="encryptJob123"))
        self.assertTrue(result["sent"])
        self.assertEqual(result["job_id"], "encryptJob123")
        self.assertTrue(pw.stopped)

    def test_greet_requires_selected_job(self):
        frame = GreetFrame(self.FRAME_URL, job_id=None)
        pw = PW(Browser(Context([Page(RECOMMEND_PAGE, [frame])])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)):
            with self.assertRaisesRegex(BrowserError, "岗位"):
                verifier.greet("geek123")

    def test_greet_missing_card_raises(self):
        frame = GreetFrame(self.FRAME_URL, job_id="encryptJob123", click_result="missing")
        pw = PW(Browser(Context([Page(RECOMMEND_PAGE, [frame])])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)):
            with self.assertRaisesRegex(BrowserError, "未找到"):
                verifier.greet("geek123")

    def test_greet_already_sent(self):
        result, _, _ = greet("geek123", GreetFrame(self.FRAME_URL, job_id="encryptJob123", click_result="already"))
        self.assertTrue(result["sent"])
        self.assertIn("已打过招呼", result["reason"])

    def test_bound_same_url_target_is_selected_by_identity(self):
        wrong = Page(RECOMMEND_PAGE, [], 'wrong')
        bound = Page(RECOMMEND_PAGE, [], 'bound')
        verifier = RecommendVerifier(lambda: {'browser': {'recruiting_target_id': 'bound'}})
        self.assertIs(verifier._recommend_page(Context([wrong, bound])), bound)

    def test_closed_implicit_target_never_switches_to_other_page(self):
        original = Page(RECOMMEND_PAGE, [], 'original')
        verifier = RecommendVerifier(lambda: {})
        verifier._recommend_page(Context([original]))
        with self.assertRaisesRegex(BrowserError, '绑定'):
            verifier._recommend_page(Context([Page(RECOMMEND_PAGE, [], 'replacement')]))

    def test_target_in_second_context_is_selected(self):
        wrong = Context([Page(RECOMMEND_PAGE, [], 'wrong')])
        bound = Page(RECOMMEND_PAGE, [], 'bound')
        browser = Mock(contexts=[wrong, Context([bound])])
        verifier = RecommendVerifier(lambda: {'browser': {'recruiting_target_id': 'bound'}})
        self.assertIs(verifier._recommend_page(browser), bound)

    def test_missing_button_does_not_prove_success(self):
        with self.assertRaises(BrowserError):
            greet('geek123', GreetFrame(self.FRAME_URL, click_result='unavailable'))

    def test_mismatched_browser_login_never_clicks(self):
        from types import SimpleNamespace
        frame = Mock(url=self.FRAME_URL)
        page = Page(RECOMMEND_PAGE, [frame])
        context = Context([page])
        context.cookies = Mock(return_value=[{'name': 'wt2', 'value': 'other-synthetic-session'}])
        pw = PW(Browser(context))
        verifier = RecommendVerifier(lambda: {}, cdp_url='http://127.0.0.1:9222',
            cookie_loader=lambda: [SimpleNamespace(name='wt2', value='expected-synthetic-session')])
        with patch('bosshunter.recruiting.recommend.sync_playwright', return_value=SyncPlaywright(pw)):
            with self.assertRaisesRegex(BrowserError, '登录账号'):
                verifier.greet('candidate', 'test-job')
        frame.evaluate.assert_not_called()

    def test_greet_does_not_treat_other_text_as_success(self):
        # 问题 5：按钮文案不是「继续沟通」时，不能判为发送成功
        frame = GreetFrame(self.FRAME_URL, job_id="encryptJob123", greet_text="请稍后重试")
        pw = PW(Browser(Context([Page(RECOMMEND_PAGE, [frame])])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222", wait_timeout=0.01)
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)):
            result = verifier.greet("geek123")
        self.assertFalse(result["sent"])

    def test_greet_rejects_multiple_recommend_pages(self):
        # 问题 5：多个推荐页时不应「取第一个」，而是停止
        page1 = Page(RECOMMEND_PAGE, [])
        page2 = Page(RECOMMEND_PAGE, [])
        pw = PW(Browser(Context([page1, page2])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)):
            with self.assertRaisesRegex(BrowserError, "多个推荐页"):
                verifier.greet("geek123")

    def test_greet_rejects_mismatched_job_before_click(self):
        # 问题 5：点击前核对岗位，不一致就不点，而非点完再补救
        frame = GreetFrame(self.FRAME_URL, job_id="encryptJob123")
        pw = PW(Browser(Context([Page(RECOMMEND_PAGE, [frame])])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)):
            with self.assertRaisesRegex(BrowserError, "岗位不一致"):
                verifier.greet("geek123", expected_job_id="different-job")


class FakeGreetVerifier:
    def __init__(self, result):
        self.result = result

    def greet(self, uid, expected_job_id=None):
        return self.result


class GreetDiscoveredTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, verifier):
        service = RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, Mock(),
                                    FakeLocalSession(), verifier)
        with service.store.db() as db:
            db.execute("INSERT INTO published_jobs(id, platform_id, title, details, status, selected, last_seen) VALUES ('boss-encryptJob123','encryptJob123','岗位','[]','开放中',1,'now')")
        service.jobs.save_budget("custom", 100)
        return service

    def test_records_attempt_with_boss_job_id(self):
        service = self.service(FakeGreetVerifier({"sent": True, "job_id": "encryptJob123", "reason": "已发出"}))
        result = service.greet_discovered("geek123", "", "encryptJob123")
        self.assertTrue(result["sent"])
        self.assertEqual(result["job_id"], "boss-encryptJob123")
        with service.store.db() as db:
            row = db.execute("SELECT * FROM greeting_attempts WHERE candidate_id='geek123'").fetchone()
        self.assertEqual(row["job_id"], "boss-encryptJob123")
        self.assertEqual(row["status"], "sent")

    def test_dedups_by_existing_attempt(self):
        service = self.service(FakeGreetVerifier({"sent": True, "job_id": "encryptJob123"}))
        with service.store.db() as db:
            db.execute("INSERT INTO greeting_attempts(day, job_id, candidate_id, status, created_at) VALUES (?,?,?,?,?)",
                       ("2026-09-29", "boss-encryptJob123", "geek123", "sent", "2026-09-29T00:00:00"))
        with self.assertRaisesRegex(ValueError, "已打过招呼"):
            service.greet_discovered("geek123", "", "encryptJob123")

    def test_missing_job_id_raises(self):
        # 第3条：discover/greet 缺岗位 ID 必须拒绝，且不产生任何占用记录
        service = self.service(FakeGreetVerifier({"sent": True, "job_id": "encryptJob123"}))
        with self.assertRaisesRegex(ValueError, "缺少岗位 ID"):
            service.greet_discovered("geek123")
        with service.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM greeting_attempts").fetchone()[0], 0)


class SelectAndReadTests(unittest.TestCase):
    def _verifier(self, evaluate_result):
        frame = Mock()
        frame.url = "https://www.zhipin.com/web/frame/recommend/"
        frame.evaluate.return_value = evaluate_result
        pw = PW(Browser(Context([Page(RECOMMEND_PAGE, [frame])])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
        return verifier, SyncPlaywright(pw)

    def test_select_job_ok(self):
        verifier, spw = self._verifier("selected")
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=spw), \
             patch("bosshunter.recruiting.recommend.time.sleep"):
            self.assertTrue(verifier.select_job("job1"))

    def test_select_job_missing_raises(self):
        verifier, spw = self._verifier("missing")
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=spw), \
             patch("bosshunter.recruiting.recommend.time.sleep"):
            with self.assertRaisesRegex(BrowserError, "下拉框"):
                verifier.select_job("job1")

    def test_read_candidates(self):
        verifier, spw = self._verifier([{"name": "A", "uid": "geek1", "greetable": True},
                                        {"name": "B", "uid": "geek2", "greetable": False}])
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=spw):
            result = verifier.read_candidates()
        self.assertEqual(result, [{"name": "A", "uid": "geek1", "greetable": True},
                                  {"name": "B", "uid": "geek2", "greetable": False}])

    def test_reload_calls_page_reload(self):
        frame = Mock()
        frame.url = "https://www.zhipin.com/web/frame/recommend/"
        page = Page(RECOMMEND_PAGE, [frame])
        pw = PW(Browser(Context([page])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)), \
             patch("bosshunter.recruiting.recommend.time.sleep"):
            self.assertTrue(verifier.reload())
        self.assertEqual(page.reload_calls, 1)

    def test_reload_processes_browser_events_while_waiting_for_new_frame(self):
        page = Page(RECOMMEND_PAGE, [])
        frame = Frame(FRAME_URL, None)
        page.wait_for_timeout = Mock(side_effect=lambda _: page.frames.append(frame))
        pw = PW(Browser(Context([page])))
        verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
        with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)):
            self.assertTrue(verifier.reload())
        page.wait_for_timeout.assert_called_once_with(500)
        self.assertEqual(page.reload_calls, 1)


class FakeDiscoveryVerifier:
    def __init__(self, candidates, greet_result=None, select_ok=True):
        self.candidates = candidates
        self.greet_result = greet_result or {"sent": True, "job_id": "job1", "reason": "已发出"}
        self.select_ok = select_ok
        self.selected_jobs = []
        self.greeted = []
        self.reload_calls = 0

    def reload(self):
        self.reload_calls += 1
        return True

    def select_job(self, job_id):
        self.selected_jobs.append(job_id)
        if not self.select_ok:
            raise BrowserError("切岗位失败")
        return True

    def read_candidates(self):
        return self.candidates

    def greet(self, uid, expected_job_id=None):
        self.greeted.append(uid)
        return dict(self.greet_result)


class RunDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def _service(self, verifier):
        service = RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, Mock(),
                                    FakeLocalSession(), verifier)
        with service.store.db() as db:
            db.execute("INSERT INTO published_jobs(id, platform_id, title, details, status, selected, last_seen) VALUES ('boss-job1','job1','岗位1','[]','开放中',1,'now')")
        service.jobs.save_budget("custom", 100)
        return service

    def test_run_discovery_greets_per_job(self):
        verifier = FakeDiscoveryVerifier([{"name": "A", "uid": "geek1", "greetable": True},
                                          {"name": "B", "uid": "geek2", "greetable": True}])
        service = self._service(verifier)
        result = service.run_discovery(per_job_min=1, per_job_max=1, throttle_delay=(0, 0))
        self.assertEqual(result["greeted"], 1)
        self.assertEqual(verifier.selected_jobs, ["job1"])
        self.assertEqual(verifier.greeted, ["geek1"])
        # 每轮开始先刷新推荐页拿新候选人
        self.assertEqual(verifier.reload_calls, 1)

    def test_run_discovery_records_candidate_name(self):
        verifier = FakeDiscoveryVerifier([{"name": "张三", "uid": "geek1", "greetable": True}])
        service = self._service(verifier)
        service.run_discovery(per_job_min=1, per_job_max=1, throttle_delay=(0, 0))
        self.assertEqual(service.jobs.state()["attempts"][0]["name"], "张三")

    def test_greet_discovered_requires_any_selected_job(self):
        # discover/greet 单次入口也必须检查岗位勾选，不能绕过 run_discovery 的检查
        verifier = FakeDiscoveryVerifier([])
        service = RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, Mock(),
                                    FakeLocalSession(), verifier)
        service.jobs.save_budget("custom", 100)
        with self.assertRaisesRegex(ValueError, "勾选"):
            service.greet_discovered("geek1", "A", "job1")
        self.assertEqual(verifier.greeted, [])

    def test_greet_discovered_rejects_unselected_job(self):
        verifier = FakeDiscoveryVerifier([])
        service = RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, Mock(),
                                    FakeLocalSession(), verifier)
        with service.store.db() as db:
            db.execute("INSERT INTO published_jobs(id, platform_id, title, details, status, selected, last_seen) VALUES ('boss-job1','job1','岗位1','[]','开放中',0,'now')")
            db.execute("INSERT INTO published_jobs(id, platform_id, title, details, status, selected, last_seen) VALUES ('boss-job2','job2','岗位2','[]','开放中',1,'now')")
        service.jobs.save_budget("custom", 100)
        with self.assertRaisesRegex(ValueError, "该岗位未人工勾选"):
            service.greet_discovered("geek1", "A", "job1")
        self.assertEqual(verifier.greeted, [])

    def test_greet_discovered_checks_quota(self):
        from bosshunter.recruiting.jobs import day_key
        verifier = FakeDiscoveryVerifier([])
        service = self._service(verifier)
        service.jobs.save_budget("custom", 1)
        with service.store.db() as db:
            db.execute("INSERT INTO greeting_attempts(day, job_id, candidate_id, name, status, created_at) VALUES (?,?,?,?,?,?)",
                       (day_key(), 'boss-job1', 'other-geek', '', 'sent', 'now'))
        with self.assertRaisesRegex(ValueError, "额度"):
            service.greet_discovered("geek1", "A", "job1")
        self.assertEqual(verifier.greeted, [])

    def test_run_discovery_stops_when_platform_quota_exhausted(self):
        # 问题 2 核心场景：平台剩余 1 次、列表有两人，只能发 1 个就停，不能发 2 个
        verifier = FakeDiscoveryVerifier([{"name": "A", "uid": "geek1", "greetable": True},
                                          {"name": "B", "uid": "geek2", "greetable": True}])
        local_session = Mock()
        local_session.read_greeting_quota = Mock(return_value={'limit': 1, 'used': 0, 'remaining': 1, 'unlimited': False})
        service = RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, Mock(),
                                    local_session, verifier)
        with service.store.db() as db:
            db.execute("INSERT INTO published_jobs(id, platform_id, title, details, status, selected, last_seen) VALUES ('boss-job1','job1','岗位1','[]','开放中',1,'now')")
        service.jobs.save_budget("platform", 100)
        result = service.run_discovery(per_job_min=2, per_job_max=2, throttle_delay=(0, 0))
        self.assertEqual(result["greeted"], 1)
        self.assertEqual(verifier.greeted, ["geek1"])

    def test_greet_discovered_reserves_before_click(self):
        # 问题 3：点击前先占用 sending；点击断线后保留 uncertain，重复调用被拦住
        verifier = Mock()
        verifier.greet = Mock(side_effect=BrowserError("连接断开"))
        service = self._service(verifier)
        with self.assertRaises(BrowserError):
            service.greet_discovered("geek1", "A", "job1")
        state = service.jobs.state()
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(state["attempts"][0]["status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "已打过招呼"):
            service.greet_discovered("geek1", "A", "job1")

    def test_greet_discovered_blocks_after_uncertain(self):
        # 问题 3：第一次结果不确定（uncertain），后续招呼被「待核实」拦住
        verifier = FakeDiscoveryVerifier([], greet_result={"sent": False, "job_id": "job1", "reason": "结果待核实"})
        service = self._service(verifier)
        result = service.greet_discovered("geek1", "A", "job1")
        self.assertEqual(result["status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "待核实"):
            service.greet_discovered("geek2", "B", "job1")

    def test_run_discovery_skips_duplicate(self):
        verifier = FakeDiscoveryVerifier([{"name": "A", "uid": "geek1", "greetable": True}])
        service = self._service(verifier)
        with service.store.db() as db:
            db.execute("INSERT INTO greeting_attempts(day, job_id, candidate_id, status, created_at) VALUES ('2026-09-29','boss-job1','geek1','sent','now')")
        result = service.run_discovery(per_job_min=1, per_job_max=1, throttle_delay=(0, 0))
        self.assertEqual(result["greeted"], 0)
        self.assertEqual(verifier.greeted, [])

    def test_run_discovery_requires_selected_jobs(self):
        service = RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, Mock(),
                                    FakeLocalSession(), FakeDiscoveryVerifier([]))
        service.jobs.save_budget("custom", 100)
        with self.assertRaisesRegex(ValueError, "开放岗位"):
            service.run_discovery(per_job_min=1, per_job_max=1, throttle_delay=(0, 0))

    def test_run_discovery_rejects_invalid_per_job_range(self):
        service = self._service(FakeDiscoveryVerifier([]))
        with self.assertRaisesRegex(ValueError, "招呼"):
            service.run_discovery(per_job_min=3, per_job_max=1, throttle_delay=(0, 0))

    def test_run_discovery_stops_when_requested(self):
        verifier = FakeDiscoveryVerifier([{"name": "A", "uid": "geek1", "greetable": True}])
        service = self._service(verifier)
        service.discovery_stop.set()
        result = service.run_discovery(per_job_min=1, per_job_max=1, throttle_delay=(0, 0))
        self.assertEqual(result["greeted"], 0)
        self.assertTrue(result["stopped"])
        self.assertEqual(verifier.greeted, [])

    def test_stop_discovery_sets_flag(self):
        service = self._service(FakeDiscoveryVerifier([]))
        service.discovery_stop.clear()
        service.stop_discovery()
        self.assertTrue(service.discovery_stop.is_set())

    def test_start_discovery_stops_when_quota_exhausted(self):
        # 问题 8：持续调度在额度用完后停止，并记录停止原因
        service = self._service(FakeDiscoveryVerifier([]))
        with patch.object(service, "run_discovery", return_value={"greeted": 1, "stopped": False, "reason": "x"}), \
             patch.object(service, "_quota_exhausted_now", return_value=True):
            service.start_discovery()
            service.discovery_worker.join(timeout=2)
        self.assertTrue(any("额度已用完" in e["detail"] for e in service.store.rows("events")))

    def test_start_discovery_stops_when_no_candidates(self):
        # 问题 8：本轮没有招呼到任何人时停止，不为用完额度而空转
        service = self._service(FakeDiscoveryVerifier([]))
        with patch.object(service, "run_discovery", return_value={"greeted": 0, "stopped": False, "reason": "x"}):
            service.start_discovery()
            service.discovery_worker.join(timeout=2)
        self.assertTrue(any("没有更多可招呼的候选人" in e["detail"] for e in service.store.rows("events")))

    def test_quota_stale_detects_ttl(self):
        # 遗漏 3：额度缓存超过 TTL 或跨日视为过期，需重新读取（覆盖人工打招呼导致的失真）
        from bosshunter.recruiting.jobs import day_key
        from bosshunter.recruiting.store import now
        service = self._service(FakeDiscoveryVerifier([]))
        # 未读额度 → 过期
        self.assertTrue(service._quota_stale())
        # 刚读的额度 → 不过期
        service.store.set_setting('greeting_quota', {'remaining': 1, 'unlimited': False, 'date': day_key(), 'updated_at': now()})
        self.assertFalse(service._quota_stale())
        # 很久以前读的 → 过期
        service.store.set_setting('greeting_quota', {'remaining': 1, 'unlimited': False, 'date': day_key(), 'updated_at': '2000-01-01T00:00:00+00:00'})
        self.assertTrue(service._quota_stale())


if __name__ == "__main__":
    unittest.main()
