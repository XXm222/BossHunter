"""Attachment reads are bound to the verified peer and never operate browser tabs."""
from io import BytesIO
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import unittest
import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from bosshunter.recruiting.local_session import LocalBossSession
from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.service import RecruitingService


def pdf(blank_second=False):
    writer = PdfWriter()
    for index in range(2):
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        if not (blank_second and index == 1):
            stream = DecodedStreamObject(); stream.set_data(f'BT /F1 12 Tf 30 700 Td (Professional experience page {index + 1}) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
    out = BytesIO(); writer.write(out); return out.getvalue()


class ResumeTests(unittest.TestCase):
    def adapter(self, target='https://m.zhipin.com/wflow/zpgeek/download/preview4boss?encryptParam=secret', download=None):
        self.calls = []
        def handle(request):
            self.calls.append(request)
            if request.url.path == '/wapi/zpchat/boss/historyMsg':
                return httpx.Response(200, json={'code': 0, 'zpData': {'hasMore': False, 'minMsgId': 1, 'messages': [{
                    'mid': 1, 'time': 1700000000000, 'type': 1,
                    'from': {'uid': 123, 'name': '测试候选人', 'source': 0}, 'to': {'uid': 456},
                    'body': {'type': 12, 'hyperLink': {'hyperLinkType': 1, 'text': 'test.pdf', 'url': 'https://bosszhipin.app/openwith?' + urlencode({'encryptId': 'x', 'url': target})}}
                }]}})
            return download or httpx.Response(200, content=pdf(), headers={'content-type': 'application/pdf'})
        return LocalBossSession(lambda: CookieJar(), httpx.MockTransport(handle))

    def read(self, adapter): return adapter.read_resume('123-0', '测试候选人', '测试岗位', '456')

    def test_fetches_received_pdf_and_all_pages_without_leaking_url(self):
        result = self.read(self.adapter())
        self.assertEqual(result['meta']['page_count'], 2)
        self.assertIn('Professional experience page 1', result['text'])
        self.assertIn('Professional experience page 2', result['text'])
        self.assertNotIn('secret', str(result))
        self.assertFalse(result['complete'])
        self.assertTrue(all(req.method == 'GET' for req in self.calls))

    def test_download_never_follows_untrusted_url_or_redirect(self):
        for url in ['https://example.org/resume.pdf', 'http://m.zhipin.com/wflow/zpgeek/download/preview4boss?encryptParam=x', 'https://m.zhipin.com/other?encryptParam=x']:
            with self.assertRaises(BrowserError): self.read(self.adapter(url))
            self.assertEqual(len(self.calls), 1)
        with self.assertRaises(BrowserError): self.read(self.adapter(download=httpx.Response(302, headers={'Location': 'https://example.org/'})))
        self.assertEqual(len(self.calls), 2)

    def test_missing_text_page_stays_explicitly_incomplete(self):
        result = LocalBossSession.extract_pdf(pdf(blank_second=True))
        self.assertEqual(result['meta']['empty_pages'], [2]); self.assertFalse(result['complete'])
        self.assertIn('本页没有可提取文字', result['text'])

    def test_non_pdf_and_entirely_scanned_pdf_fail_cleanly(self):
        for content in [b'<html>login secret</html>', b'%PDF-invalid']:
            with self.assertRaises(BrowserError) as e: LocalBossSession.extract_pdf(content)
            self.assertNotIn('secret', str(e.exception))
        writer = PdfWriter(); writer.add_blank_page(width=600, height=800); out = BytesIO(); writer.write(out)
        # 未安装 OCR 依赖时，扫描版 PDF 应明确报错（不静默）；已装则走 OCR 兜底
        with patch('bosshunter.recruiting.local_session._ocr_available', return_value=False):
            with self.assertRaises(BrowserError): LocalBossSession.extract_pdf(out.getvalue())

    def test_service_replaces_partial_extraction_only_after_success(self):
        with TemporaryDirectory() as folder:
            browser = Mock(); session = self.adapter()
            service = RecruitingService(Path(folder) / 'db', lambda: {}, browser, session)
            service.store.import_conversation({'id': '123-0', 'name': '测试候选人', 'position_title': '测试岗位', 'messages': [], 'account_uid': '456'})
            service.store.save_document('123-0', 'pdf_viewer', 'old partial text', False, {})
            result = service.resume('123-0')
            self.assertEqual(result['source'], 'boss_attachment_pdf_http')
            browser.read_resume.assert_not_called()
            before = len(service.state()['documents'])
            service.local_session = self.adapter(download=httpx.Response(403))
            with self.assertRaises(BrowserError): service.resume('123-0')
            self.assertEqual(len(service.state()['documents']), before)

    def test_normal_chat_sync_does_not_persist_attachment_tokens(self):
        result = self.adapter().read_conversation('123-0', '测试候选人', '测试岗位')
        self.assertNotIn('_attachments', result)
        self.assertNotIn('secret', str(result))

    def test_verification_redirect_is_explained_without_following_it(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(302, headers={'Location': '/web/passport/zp/verify.html?callbackUrl=secret'})
        session = LocalBossSession(lambda: CookieJar(), httpx.MockTransport(handle))
        with self.assertRaisesRegex(BrowserError, 'BOSS 要求账号验证') as error:
            self.read(session)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('secret', str(error.exception))


if __name__ == '__main__': unittest.main()
