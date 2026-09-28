"""绑定向导 service 方法：预览核实与确认写入 pilot_conversation。"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.service import RecruitingService


def snapshot(ident='123456-0', name='张三', position_title='前端工程师', account_uid='98765'):
    """构造一个 read_conversation 返回的会话快照（与真实返回字段对齐）。"""
    return {'id': ident, 'name': name, 'position_title': position_title,
            'messages': [{'id': '1', 'direction': 'in', 'kind': 'text', 'text': '你好',
                          'timestamp': 1700000000000, 'time': '01-01 08:00'}],
            'account_uid': account_uid, 'editor_empty': False, 'stable_message_ids': True,
            'source': 'boss_local_cookie_http', 'history_complete': False,
            'coverage': 'BOSS 历史消息接口当前可返回的全部记录'}


class BindingTests(unittest.TestCase):
    def make_service(self, read_conversation):
        """用 mock 的 local_session 建服务；browser 也用 Mock，走本地 Cookie 模式。"""
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        local_session = Mock()
        local_session.read_conversation = read_conversation
        return RecruitingService(Path(folder.name) / 'recruiting.db', lambda: {}, Mock(), local_session)

    def test_preview_returns_account_and_does_not_bind(self):
        service = self.make_service(Mock(return_value=snapshot()))
        result = service.preview_binding('123456-0', '张三', '前端工程师')
        self.assertEqual(result['account_uid'], '98765')
        self.assertEqual(result['name'], '张三')
        self.assertEqual(result['position_title'], '前端工程师')
        self.assertEqual(result['message_count'], 1)
        # 预览只读，不写入绑定
        self.assertIsNone(service.store.setting('pilot_conversation'))

    def test_preview_rejects_empty_fields(self):
        service = self.make_service(Mock())
        with self.assertRaises(ValueError):
            service.preview_binding('', '张三', '前端工程师')

    def test_confirm_passes_expected_account_and_binds(self):
        read = Mock(return_value=snapshot())
        service = self.make_service(read)
        result = service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        # 确认时必须把预览拿到的 account_uid 传回去做二次核实
        read.assert_called_once_with('123456-0', '张三', '前端工程师', expected_account='98765')
        self.assertEqual(result['id'], '123456-0')
        self.assertEqual(service.store.setting('pilot_conversation'), '123456-0')

    def test_confirm_propagates_verification_failure(self):
        # 模拟 read_conversation 因账号不一致/登录失效抛错，确认应原样抛出且不写入
        read = Mock(side_effect=BrowserError('当前登录招聘账号与绑定会话不一致，停止同步'))
        service = self.make_service(read)
        with self.assertRaises(BrowserError):
            service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        self.assertIsNone(service.store.setting('pilot_conversation'))

    def test_confirm_rejects_rebinding_different_conversation(self):
        # 绑定一个会话后，再绑另一个应被 import_conversation 拒绝（试点只允许一个会话）
        read = Mock(side_effect=lambda ident, name, position_title, expected_account=None: snapshot(ident, name, position_title))
        service = self.make_service(read)
        service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        with self.assertRaises(ValueError):
            service.confirm_binding('999999-0', '李四', '后端工程师', '88888')


if __name__ == '__main__':
    unittest.main()