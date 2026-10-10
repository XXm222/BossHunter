"""绑定向导 service 方法：预览核实与确认写入 pilot_conversation。"""
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bosshunter.recruiting.browser import BrowserError
from bosshunter.recruiting.service import RecruitingService
from bosshunter.recruiting.store import Store


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
        self.assertEqual(result['binding_confirmed'], 1)

    def test_discovered_contacts_do_not_enter_monitor_until_confirmed(self):
        service = self.make_service(Mock(return_value=snapshot()))
        service.local_session.read_friend_jobs.return_value = {'123456': 'platform-job'}
        service.jobs.import_snapshot({'complete': True, 'total': 1, 'jobs': [
            {'platform_id': 'platform-job', 'title': '前端工程师', 'status': '开放中', 'details': []}]})
        service.jobs.select(['boss-platform-job'])
        discovered = {**snapshot(), 'messages': [], 'position_platform_id': 'platform-job'}
        service.store.import_conversation(discovered)
        self.assertEqual(service._allowed_conversations(), [])
        self.assertEqual(service.state()['monitor']['allowed_count'], 0)
        service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        self.assertEqual(service._allowed_conversations(), ['123456-0'])
        # Routine imports preserve confirmation; new contacts stay unconfirmed.
        service.store.import_conversation(discovered)
        other = {**discovered, 'id': 'unbound-0'}
        service.store.import_conversation(other)
        self.assertEqual(service._allowed_conversations(), ['123456-0'])
        self.assertEqual(service.state()['monitor']['allowed_count'], 1)
        with service.store.db() as db:
            db.execute("UPDATE conversations SET auto_send=1 WHERE id='unbound-0'")
        self.assertFalse(service._auto_send_enabled('unbound-0'))

    def test_legacy_migration_restores_only_explicitly_confirmed_contacts(self):
        service = self.make_service(Mock(return_value=snapshot()))
        service.store.import_conversation(snapshot())
        service.store.import_conversation(snapshot('other-0'))
        service.store.event('conversation_bound', '123456-0', '合成确认绑定记录')
        service.store.event('position_reassociated', 'other-0', '仅关联岗位')
        with service.store.db() as db:
            db.execute('ALTER TABLE conversations DROP COLUMN binding_confirmed')
        restarted = Store(service.store.path)
        self.assertEqual(restarted.row('conversations', '123456-0')['binding_confirmed'], 1)
        self.assertEqual(restarted.row('conversations', 'other-0')['binding_confirmed'], 0)
        restarted = Store(service.store.path)
        self.assertEqual(restarted.row('conversations', 'other-0')['binding_confirmed'], 0)

    def test_confirm_propagates_verification_failure(self):
        # 模拟 read_conversation 因账号不一致/登录失效抛错，确认应原样抛出且不写入
        read = Mock(side_effect=BrowserError('当前登录招聘账号与绑定会话不一致，停止同步'))
        service = self.make_service(read)
        with self.assertRaises(BrowserError):
            service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        self.assertIsNone(service.store.setting('pilot_conversation'))

    def test_confirm_allows_binding_multiple_conversations(self):
        # 多会话：绑定第二个会话应成功，且新绑定的成为当前选中
        read = Mock(side_effect=lambda ident, name, position_title, expected_account=None: snapshot(ident, name, position_title))
        service = self.make_service(read)
        service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        second = service.confirm_binding('999999-0', '李四', '后端工程师', '88888')
        self.assertEqual(second['id'], '999999-0')
        self.assertEqual(service.store.setting('pilot_conversation'), '999999-0')

    def test_placeholder_binding_requires_manual_link_and_then_can_be_confirmed(self):
        service = self.make_service(Mock(return_value=snapshot()))
        service.local_session.read_friend_jobs.return_value = {'123456': 'platform-job'}
        old = service.store.import_conversation({**snapshot(), 'messages': []})
        self.assertTrue(old['position_id'].startswith('context-'))
        service.jobs.import_snapshot({'complete': True, 'total': 1, 'jobs': [
            {'platform_id': 'platform-job', 'title': '前端工程师', 'status': '开放中', 'details': []}]})
        with self.assertRaisesRegex(ValueError, '尚未关联平台岗位'):
            service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        self.assertEqual(service.store.row('conversations', old['id'])['position_id'], old['position_id'])
        service.link_position(old['id'], 'platform-job')
        result = service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        self.assertEqual(result['position_id'], 'boss-platform-job')
        self.assertEqual(len(json.loads(result['snapshot'])['messages']), 1)
        self.assertFalse(result['auto_send'])

    def test_real_platform_identity_change_still_rejects_binding(self):
        service = self.make_service(Mock(return_value=snapshot()))
        service.store.import_conversation({**snapshot(), 'position_platform_id': 'original-job'})
        service.local_session.read_friend_jobs.return_value = {'123456': 'different-job'}
        with self.assertRaisesRegex(ValueError, '平台岗位身份已变化'):
            service.confirm_binding('123456-0', '张三', '前端工程师', '98765')
        self.assertEqual(service.store.row('conversations', '123456-0')['position_id'], 'boss-original-job')


    def test_monitor_state_persists_across_restart(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'recruiting.db'
        service = RecruitingService(path, lambda: {}, Mock(), Mock())
        service.monitor['last_success'] = '2026-09-28T10:00:00'
        service.monitor['error'] = '登录失效'
        service._persist_monitor_state()
        # 重启：新实例读同一 DB，应恢复上次的监测状态
        service2 = RecruitingService(path, lambda: {}, Mock(), Mock())
        self.assertEqual(service2.monitor['last_success'], '2026-09-28T10:00:00')
        self.assertEqual(service2.monitor['error'], '登录失效')


if __name__ == '__main__':
    unittest.main()
