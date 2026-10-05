"""OCR fallback for scanned (text-layer-less) PDF resumes, recruiting side."""
import sys
import unittest
from io import BytesIO
from unittest.mock import Mock, patch

from pypdf import PdfWriter

from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.local_session import (
    LocalBossSession,
    _ocr_available,
    _ocr_entry_text,
    _ocr_image_text,
    _ocr_pdf_pages,
)


def blank_pdf(page_count=1):
    """Build an in-memory PDF with no text layer, simulating a scan."""
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=600, height=800)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


class OcrEntryTextTests(unittest.TestCase):
    """版本兼容的文字提取：不同返回形态都能取出识别文字。"""

    def test_list_shape_returns_text(self):
        # 1.x 形态：[box(4 点坐标), text(识别文字), score(置信度)]
        self.assertEqual(_ocr_entry_text([[0, 0, 10, 10], '本科', 0.99]), '本科')

    def test_object_text_attribute(self):
        self.assertEqual(_ocr_entry_text(Mock(text='  前端工程师  ')), '前端工程师')

    def test_plain_string(self):
        self.assertEqual(_ocr_entry_text('  直接文字  '), '直接文字')

    def test_unrecognized_returns_empty(self):
        self.assertEqual(_ocr_entry_text(None), '')
        self.assertEqual(_ocr_entry_text([]), '')
        self.assertEqual(_ocr_entry_text([[0, 0], 123, 0.5]), '')


class OcrImageTextTests(unittest.TestCase):
    """单页识别结果 → 按行拼接的文字。"""

    def test_unwraps_tuple_and_joins_lines(self):
        result = ([ [[0, 0, 1, 1], '第一行', 0.9], [[0, 1, 1, 2], '第二行', 0.8] ], 0.5)
        self.assertEqual(_ocr_image_text(Mock(return_value=result), b'png'), '第一行\n第二行')

    def test_none_result_returns_empty(self):
        self.assertEqual(_ocr_image_text(Mock(return_value=(None, 0.0)), b'png'), '')

    def test_empty_result_returns_empty(self):
        self.assertEqual(_ocr_image_text(Mock(return_value=([], 0.0)), b'png'), '')


class OcrAvailableTests(unittest.TestCase):
    """依赖探测：只判断 import，不初始化引擎。"""

    def test_true_when_deps_importable(self):
        with patch.dict(sys.modules, {'fitz': Mock(), 'rapidocr_onnxruntime': Mock()}):
            self.assertTrue(_ocr_available())

    def test_false_when_deps_missing(self):
        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name in ('fitz', 'rapidocr_onnxruntime'):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=blocking_import):
            self.assertFalse(_ocr_available())


class OcrPdfPagesTests(unittest.TestCase):
    """渲染循环：每页转 PNG → OCR → 逐页收集文字。"""

    def test_renders_each_page_and_collects_text(self):
        page1 = Mock()
        page1.get_pixmap.return_value.tobytes.return_value = b'png1'
        page2 = Mock()
        page2.get_pixmap.return_value.tobytes.return_value = b'png2'
        doc = Mock()
        doc.__enter__ = Mock(return_value=doc)
        doc.__exit__ = Mock(return_value=False)
        doc.__iter__ = Mock(return_value=iter([page1, page2]))

        fitz = Mock()
        fitz.open.return_value = doc
        engine = Mock(side_effect=[
            ([ [[0, 0, 1, 1], '第一页', 0.9] ], 0.1),
            ([ [[0, 0, 1, 1], '第二页', 0.8] ], 0.1),
        ])
        rapidocr = Mock()
        rapidocr.RapidOCR.return_value = engine

        with patch.dict(sys.modules, {'fitz': fitz, 'rapidocr_onnxruntime': rapidocr}):
            pages = _ocr_pdf_pages(b'%PDF-fake')

        self.assertEqual(pages, ['第一页', '第二页'])
        fitz.open.assert_called_once_with(stream=b'%PDF-fake', filetype='pdf')
        self.assertEqual(engine.call_count, 2)


class ExtractPdfOcrTests(unittest.TestCase):
    """extract_pdf 对「整份无文字层 PDF」的 OCR 兜底行为。"""

    def test_scanned_pdf_is_ocrd_when_available(self):
        with patch('bosshunter.recruiting.local_session._ocr_available', return_value=True), \
             patch('bosshunter.recruiting.local_session._ocr_pdf_pages', return_value=['识别出的第一页']):
            result = LocalBossSession.extract_pdf(blank_pdf())

        self.assertEqual(result['meta']['page_count'], 1)
        self.assertIn('识别出的第一页', result['text'])
        self.assertEqual(result['meta']['empty_pages'], [])
        self.assertEqual(result['meta']['page_characters'], [len('识别出的第一页')])
        self.assertIn('已通过 OCR 识别', result['meta']['note'])

    def test_scanned_pdf_raises_when_ocr_unavailable(self):
        with patch('bosshunter.recruiting.local_session._ocr_available', return_value=False):
            with self.assertRaises(BrowserError) as ctx:
                LocalBossSession.extract_pdf(blank_pdf())
        self.assertIn('未安装 OCR 依赖', str(ctx.exception))

    def test_scanned_pdf_raises_when_ocr_fails(self):
        with patch('bosshunter.recruiting.local_session._ocr_available', return_value=True), \
             patch('bosshunter.recruiting.local_session._ocr_pdf_pages', side_effect=RuntimeError('boom')):
            with self.assertRaises(BrowserError) as ctx:
                LocalBossSession.extract_pdf(blank_pdf())
        self.assertIn('OCR 识别失败', str(ctx.exception))

    def test_scanned_pdf_raises_on_page_count_mismatch(self):
        with patch('bosshunter.recruiting.local_session._ocr_available', return_value=True), \
             patch('bosshunter.recruiting.local_session._ocr_pdf_pages', return_value=['一', '二']):
            with self.assertRaises(BrowserError) as ctx:
                LocalBossSession.extract_pdf(blank_pdf())  # 1 页，OCR 却返回 2 页
        self.assertIn('页数不一致', str(ctx.exception))

    def test_multi_page_scanned_pdf_is_ocrd(self):
        """多页扫描件应一次性 OCR 全部页，而不是在首个空页就误判页数不一致。"""
        with patch('bosshunter.recruiting.local_session._ocr_available', return_value=True), \
             patch('bosshunter.recruiting.local_session._ocr_pdf_pages', return_value=['第一页识别', '第二页识别']):
            result = LocalBossSession.extract_pdf(blank_pdf(2))

        self.assertEqual(result['meta']['page_count'], 2)
        self.assertIn('第一页识别', result['text'])
        self.assertIn('第二页识别', result['text'])
        self.assertEqual(result['meta']['empty_pages'], [])
        self.assertEqual(result['meta']['page_characters'], [len('第一页识别'), len('第二页识别')])
        self.assertIn('已通过 OCR 识别', result['meta']['note'])


if __name__ == '__main__':
    unittest.main()