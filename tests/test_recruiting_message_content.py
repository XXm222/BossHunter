"""message_content 的消息类型解析：语音/图片/动作 + 联系信息文本清理。"""
import unittest

from bosshunter.recruiting.local_session import LocalBossSession


def body(kind, **extra):
    """构造一个消息 body：type 为 kind，附带其它字段。"""
    value = {'type': kind, 'templateId': 1, 'headTitle': ''}
    value.update(extra)
    return value


class MessageContentTests(unittest.TestCase):
    def parse(self, kind, **extra):
        return LocalBossSession.message_content({'type': 1, 'body': body(kind, **extra)})

    def test_voice_message_is_system(self):
        text, kind, system = self.parse(2, sound={'duration': 4, 'url': 'https://...'})
        self.assertEqual(text, '语音消息（4 秒）')
        self.assertEqual(kind, 'system')
        self.assertTrue(system)

    def test_image_message_is_system(self):
        text, kind, system = self.parse(3, image={'tinyImage': {'width': 300, 'url': 'https://...', 'height': 300}})
        self.assertEqual(text, '图片消息')
        self.assertEqual(kind, 'system')
        self.assertTrue(system)

    def test_action_message_is_system(self):
        text, kind, system = self.parse(4, action={'aid': 41, 'extend': '{}'})
        self.assertEqual(text, '系统操作记录')
        self.assertEqual(kind, 'system')
        self.assertTrue(system)

    def test_text_strips_copy_tag(self):
        text, kind, system = self.parse(1, text='张三的微信号：&lt;copy&gt;zhangsan_wx&lt;/copy&gt;', templateId=5)
        self.assertEqual(text, '张三的微信号：zhangsan_wx')
        self.assertEqual(kind, 'text')
        self.assertFalse(system)

    def test_text_strips_phone_tag(self):
        text, kind, system = self.parse(1, text='张三的手机号：&lt;phone&gt;13800000000&lt;/phone&gt;', templateId=5)
        self.assertEqual(text, '张三的手机号：13800000000')
        self.assertEqual(kind, 'text')
        self.assertFalse(system)

    def test_plain_text_unchanged(self):
        text, kind, system = self.parse(1, text='你好，还在看机会吗？')
        self.assertEqual(text, '你好，还在看机会吗？')
        self.assertEqual(kind, 'text')
        self.assertFalse(system)

    def test_unknown_type_still_falls_back(self):
        text, kind, system = self.parse(99)
        self.assertIn('尚未解析', text)
        self.assertEqual(kind, 'system')
        self.assertTrue(system)


if __name__ == '__main__':
    unittest.main()