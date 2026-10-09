"""Recommend browser failures share the same persistent platform pause."""
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from bosshunter.recruiting.browser import AccountPauseError, BrowserError
from bosshunter.recruiting.recommend import RecommendVerifier
import test_recruiting_review
from test_recruiting_recommend import Page, Frame, PW, Browser, Context, SyncPlaywright, RECOMMEND_PAGE, FRAME_URL


class RecommendPauseTests(unittest.TestCase):
    setUp = test_recruiting_review.ReviewTests.setUp
    tearDown = test_recruiting_review.ReviewTests.tearDown

    def verifier(self, page):
        pw = PW(Browser(Context([page])))
        verifier = RecommendVerifier(lambda: {}, cdp_url='http://127.0.0.1:9222',
                                     account_pause_handler=self.service._pause_platform_requests)
        return verifier, SyncPlaywright(pw)

    def emit(self, page, status=429, url='https://www.zhipin.com/wapi/zpgeek/test', headers=None):
        page.listeners['response'](SimpleNamespace(status=status, url=url, headers=headers or {}))

    def test_reload_refusal_pauses_monitor_and_greetings_and_respects_retry_after(self):
        for status in (403,429):
            page = Page(RECOMMEND_PAGE, [Frame(FRAME_URL, None)])
            page.reload = Mock(side_effect=lambda **kwargs: self.emit(page,status,headers={'retry-after':'3600'}))
            verifier, runtime = self.verifier(page)
            self.service.set_monitor_enabled(True)
            self.service.discovery_stop.clear()
            with patch('bosshunter.recruiting.recommend.sync_playwright',return_value=runtime):
                with self.assertRaises(AccountPauseError):
                    verifier.reload()
            self.assertFalse(self.service.store.setting('monitor_enabled'))
            self.assertTrue(self.service.discovery_stop.is_set())
            self.assertGreater(self.service.store.setting('request_paused_until'), time.time()+3590)
            self.assertEqual(page.listeners,{})
            page.reload.assert_called_once()

    def test_refusal_during_job_selection_stops_before_reading_candidates(self):
        frame = Frame(FRAME_URL,'selected')
        page = Page(RECOMMEND_PAGE,[frame])
        page.wait_for_timeout = Mock(side_effect=lambda _:self.emit(page))
        verifier,runtime = self.verifier(page)
        with patch('bosshunter.recruiting.recommend.sync_playwright',return_value=runtime):
            with self.assertRaises(AccountPauseError):
                verifier.select_job('job')
        self.assertTrue(self.service.discovery_stop.is_set())

    def test_verification_redirect_triggers_same_pause(self):
        page = Page(RECOMMEND_PAGE,[Frame(FRAME_URL,None)])
        verifier,_ = self.verifier(page)
        with self.assertRaises(AccountPauseError):
            with verifier._page_guard(page):
                self.emit(page,302,headers={'location':'/web/passport/zp/verify.html'})
        self.assertTrue(self.service.discovery_stop.is_set())

    def test_recommend_frame_document_refusal_is_not_ignored(self):
        page = Page(RECOMMEND_PAGE,[])
        verifier,_ = self.verifier(page)
        with self.assertRaises(AccountPauseError):
            with verifier._page_guard(page):
                self.emit(page,403,FRAME_URL)
        self.assertTrue(self.service.discovery_stop.is_set())

    def test_verification_page_navigation_triggers_pause_once(self):
        page = Page(RECOMMEND_PAGE,[])
        verifier,_ = self.verifier(page)
        verifier._account_pause_handler=Mock()
        with self.assertRaises(AccountPauseError):
            with verifier._page_guard(page):
                page.url='https://www.zhipin.com/web/passport/zp/verify.html'
        verifier._account_pause_handler.assert_called_once_with(1800)

    def test_static_external_and_normal_api_responses_do_not_pause(self):
        page = Page(RECOMMEND_PAGE,[])
        verifier,_ = self.verifier(page)
        with verifier._page_guard(page):
            self.emit(page,200)
            self.emit(page,403,'https://www.zhipin.com/static/logo.png')
            self.emit(page,429,'https://zhipin.com.example.org/wapi/test')
        self.assertEqual(self.service.store.setting('request_paused_until',0),0)
        self.assertFalse(self.service.discovery_stop.is_set())

    def test_loading_error_is_not_misclassified_but_refusal_is_not_hidden_by_it(self):
        page = Page(RECOMMEND_PAGE,[])
        verifier,_ = self.verifier(page)
        with self.assertRaises(BrowserError):
            with verifier._page_guard(page):
                raise BrowserError('synthetic frame error')
        self.assertEqual(self.service.store.setting('request_paused_until',0),0)
        with self.assertRaises(AccountPauseError):
            with verifier._page_guard(page):
                self.emit(page)
                raise BrowserError('synthetic frame error')

    def test_cached_restriction_page_and_security_check_response_pause(self):
        for url,status in [('https://www.zhipin.com/web/passport/zp/403.html?code=32',304),
                           ('https://www.zhipin.com/?_security_check=1_1791510234512',302)]:
            page=Page(RECOMMEND_PAGE,[])
            verifier,_=self.verifier(page)
            self.service.discovery_stop.clear()
            with self.assertRaises(AccountPauseError):
                with verifier._page_guard(page):
                    self.emit(page,status,url)
            self.assertTrue(self.service.discovery_stop.is_set())

    def test_bound_target_already_at_restriction_page_pauses_without_switching(self):
        restricted=Page('https://www.zhipin.com/web/passport/zp/403.html?code=32',[],target_id='bound')
        other=Page(RECOMMEND_PAGE,[],target_id='other')
        browser=Browser(Context([restricted,other]))
        verifier=RecommendVerifier(lambda: {'browser': {'recruiting_target_id':'bound'}},
                                   account_pause_handler=self.service._pause_platform_requests)
        with self.assertRaises(AccountPauseError):
            verifier._recommend_page(browser)
        self.assertTrue(self.service.discovery_stop.is_set())
        self.assertEqual(other.reload_calls,0)

    def test_http_redirects_and_cached_restriction_page_share_pause(self):
        import httpx
        from bosshunter.recruiting.local_session import LocalBossSession
        pause=Mock()
        session=LocalBossSession(account_pause_handler=pause)
        for destination in ('/web/passport/zp/403.html?code=32','/?_security_check=1_123','/web/passport/zp/verify.html'):
            response=httpx.Response(302,headers={'Location':destination},
                                    request=httpx.Request('GET','https://www.zhipin.com/wapi/test'))
            with self.assertRaises(AccountPauseError):
                session._check_response(response)
        response=httpx.Response(304,request=httpx.Request('GET','https://www.zhipin.com/web/passport/zp/403.html?code=32'))
        with self.assertRaises(AccountPauseError):
            session._check_response(response)
        self.assertEqual(pause.call_count,4)

    def test_security_marker_on_external_or_unrelated_path_is_not_misclassified(self):
        from bosshunter.recruiting.browser import platform_security_url
        for url in ('https://example.org/web/passport/zp/403.html?code=32',
                    'https://www.zhipin.com/web/chat/recommend?_security_check=1',
                    'https://www.zhipin.com/?normal=1'):
            self.assertFalse(platform_security_url(url))
