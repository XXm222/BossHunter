"""Recommend-page refresh verification: patchright chain + dedup boundary."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

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
    def __init__(self, url, frames):
        self.url = url
        self.frames = frames
        self.reload_calls = 0

    def reload(self, **kwargs):
        self.reload_calls += 1


class Context:
    def __init__(self, pages):
        self.pages = pages


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


def verify(frame_result, page_url=RECOMMEND_PAGE, frame_url=FRAME_URL, wait_timeout=0.05):
    frames = [Frame(frame_url, frame_result)] if frame_result is not None else []
    page = Page(page_url, frames)
    pw = PW(Browser(Context([page])))
    verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222", wait_timeout=wait_timeout)
    with patch("bosshunter.recruiting.recommend.sync_playwright", return_value=SyncPlaywright(pw)):
        result = verifier.verify()
    return result, pw, page


class RecommendVerifierTests(unittest.TestCase):
    def test_verify_reloads_and_reads_fresh_candidate(self):
        result, pw, page = verify({"name": "丁SH", "uid": "geek123"})
        self.assertTrue(result["verified"])
        self.assertEqual(result["name"], "丁SH")
        self.assertEqual(result["uid"], "geek123")
        self.assertEqual(page.reload_calls, 1)
        self.assertTrue(pw.stopped)

    def test_verify_fails_closed_when_identity_incomplete(self):
        for bad in ({"name": "", "uid": "geek123"}, {"name": "丁SH", "uid": ""}, {"name": "", "uid": ""}):
            result, _, _ = verify(bad)
            self.assertFalse(result["verified"], bad)

    def test_verify_fails_when_no_frame_or_card(self):
        for frames in (None, {"name": None, "uid": None}):
            result, _, _ = verify(frames)
            self.assertFalse(result["verified"])

    def test_verify_requires_recommend_page(self):
        with patch("bosshunter.recruiting.recommend.sync_playwright",
                   return_value=SyncPlaywright(PW(Browser(Context([Page("https://www.zhipin.com/web/chat/index", [Frame(FRAME_URL, {"name": "丁SH", "uid": "g"})])]))))):
            verifier = RecommendVerifier(lambda: {}, cdp_url="http://127.0.0.1:9222")
            with self.assertRaisesRegex(BrowserError, "推荐页"):
                verifier.verify()

    def test_verify_cdp_not_found_raises(self):
        verifier = RecommendVerifier(lambda: {}, cdp_url=None)
        with patch("bosshunter.recruiting.recommend.httpx.get", return_value=Mock(status_code=404)):
            with self.assertRaisesRegex(BrowserError, "调试端口"):
                verifier.verify()


class FakeVerifier:
    def __init__(self, result):
        self.result = result

    def verify(self):
        return self.result


class FakeLocalSession:
    def __init__(self, contacts=None):
        self._contacts = contacts or []

    def list_contacts(self):
        return self._contacts


class VerifyDiscoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, verifier, contacts=None):
        return RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, Mock(),
                                 FakeLocalSession(contacts), verifier)

    def test_ready_when_not_duplicate(self):
        service = self.service(FakeVerifier({"verified": True, "name": "丁SH", "uid": "geek123"}))
        result = service.verify_discover()
        self.assertTrue(result["verified"])
        self.assertFalse(result["duplicate"])
        self.assertTrue(result["ready"])

    def test_dedupes_by_greeting_attempts(self):
        service = self.service(FakeVerifier({"verified": True, "name": "丁SH", "uid": "geek123"}))
        with service.store.db() as db:
            db.execute("INSERT INTO greeting_attempts(day, job_id, candidate_id, status, created_at) VALUES (?,?,?,?,?)",
                       ("2026-09-29", "job1", "geek123", "sent", "2026-09-29T00:00:00"))
        result = service.verify_discover()
        self.assertTrue(result["duplicate"])
        self.assertFalse(result["ready"])

    def test_contact_list_friendid_is_not_used_for_dedup(self):
        # 推荐卡 data-geekid 与联系人列表 friendId 不是同一 ID 空间，去重不应比对联系人。
        service = self.service(FakeVerifier({"verified": True, "name": "丁SH", "uid": "geek123"}),
                               [{"ident": "other-friend-id-0", "name": "别人"}])
        result = service.verify_discover()
        self.assertFalse(result["duplicate"])
        self.assertTrue(result["ready"])

    def test_unverified_is_never_ready(self):
        service = self.service(FakeVerifier({"verified": False, "name": None, "uid": None, "reason": "身份不完整"}))
        result = service.verify_discover()
        self.assertFalse(result["verified"])
        self.assertFalse(result["ready"])
        self.assertFalse(result["duplicate"])


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


class FakeGreetVerifier:
    def __init__(self, result):
        self.result = result

    def greet(self, uid):
        return self.result


class GreetDiscoveredTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, verifier):
        return RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, Mock(),
                                 FakeLocalSession(), verifier)

    def test_records_attempt_with_boss_job_id(self):
        service = self.service(FakeGreetVerifier({"sent": True, "job_id": "encryptJob123", "reason": "已发出"}))
        result = service.greet_discovered("geek123")
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
            service.greet_discovered("geek123")

    def test_missing_job_id_raises(self):
        service = self.service(FakeGreetVerifier({"sent": True, "job_id": None}))
        with self.assertRaisesRegex(ValueError, "岗位"):
            service.greet_discovered("geek123")


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


class FakeDiscoveryVerifier:
    def __init__(self, candidates, greet_result=None, select_ok=True):
        self.candidates = candidates
        self.greet_result = greet_result or {"sent": True, "job_id": "job1", "reason": "已发出"}
        self.select_ok = select_ok
        self.selected_jobs = []
        self.greeted = []

    def select_job(self, job_id):
        self.selected_jobs.append(job_id)
        if not self.select_ok:
            raise BrowserError("切岗位失败")
        return True

    def read_candidates(self):
        return self.candidates

    def greet(self, uid):
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


if __name__ == "__main__":
    unittest.main()
