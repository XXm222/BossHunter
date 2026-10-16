"""PR #3: quota is occupied before dispatch, committed only on confirmation."""
from threading import Event
from unittest import TestCase
from unittest.mock import patch

from bosshunter.recruiting.browser import TaskCancelled
from bosshunter.recruiting.store import Store, encode
from bosshunter.recruiting.jobs import day_key
import test_recruiting_issue_fixes


class ReplyReservationTests(TestCase):
    setUp = test_recruiting_issue_fixes.IssueFixTests.setUp
    tearDown = test_recruiting_issue_fixes.IssueFixTests.tearDown

    def draft(self, content='测试普通回复'):
        self.service.set_auto_send(self.cid, True)
        self.service.store.set_setting('auto_reply_daily_limit', 1)
        return self.service.prepare_reply(self.cid, content)

    def claim(self, draft):
        return self.service.store.claim(draft['id'],auto=True,daily_limit=1)

    def budget(self, sent, reserved):
        self.assertEqual(self.service.store.auto_reply_budget(),{'sent':sent,'reserved':reserved})
        self.assertEqual(self.service._auto_reply_sent_today(),sent)

    def test_claim_reserves_without_reporting_sent(self):
        draft = self.draft(); self.claim(draft)
        self.budget(0,1)
        self.assertTrue(self.service._reply_quota_exhausted())
        self.assertEqual(self.service.state()['auto_send']['reserved_today'],1)

    def test_stop_toggle_and_cooldown_after_claim_release_without_clicking(self):
        for reason in ('worker_stop','auto_off','cooldown'):
            with self.subTest(reason=reason):
                self.service._stop_event = Event()
                self.service.set_monitor_enabled(True)
                self.service.store.set_setting('request_paused_until',0)
                draft = self.draft(reason)
                original = self.service.store.claim
                def interrupted(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if reason == 'worker_stop': self.service._stop_event.set()
                    elif reason == 'auto_off': self.service.set_auto_send(self.cid,False)
                    else: self.service._pause_platform_requests(1800)
                    return result
                with patch.object(self.service.store,'claim',side_effect=interrupted):
                    with self.assertRaises(TaskCancelled): self.service.execute(draft['id'],auto=True)
                self.assertEqual(self.service.store.row('outbox',draft['id'])['status'],'cancelled')
                self.budget(0,0)
                self.assertEqual(self.browser.calls,0)
        self.service._stop_event = None
        self.service.store.set_setting('request_paused_until',0)
        next_draft = self.draft('真实的一条后续回复')
        with patch('bosshunter.recruiting.service.time.sleep'):
            self.assertEqual(self.service.execute(next_draft['id'],auto=True)['status'],'sent')
        self.budget(1,0)

    def test_confirmed_send_commits_once_and_other_store_observes_limit(self):
        first=self.draft(); self.claim(first)
        self.service.store.finish(first['id'],'sent','确认回执')
        self.service.store.finish(first['id'],'sent','重复处理同一回执')
        self.budget(1,0)
        second=self.draft('第二条'); other=Store(self.service.store.path)
        with self.assertRaisesRegex(ValueError,'上限'): other.claim(second['id'],auto=True,daily_limit=1)

    def test_uncertain_occupancy_survives_restart_and_releases_on_not_sent(self):
        first=self.draft(); self.claim(first)
        self.service.store.finish(first['id'],'uncertain','回执丢失')
        self.budget(0,1)
        other=Store(self.service.store.path)
        self.assertEqual(other.auto_reply_budget(),{'sent':0,'reserved':1})
        other.resolve_outbound('reply',first['id'],'not_sent','已核实平台没有发送这条消息')
        self.budget(0,0)
        self.claim(self.draft('核实后下一条'))
        self.budget(0,1)

    def test_manual_confirmation_commits_and_cannot_repeat(self):
        first=self.draft(); self.claim(first)
        self.service.store.finish(first['id'],'uncertain','回执丢失')
        self.service.store.resolve_outbound('reply',first['id'],'sent','已核实平台存在这条本人消息')
        self.budget(1,0)
        with self.assertRaises(ValueError):
            self.service.store.resolve_outbound('reply',first['id'],'sent','重复核实同一条平台消息')
        self.budget(1,0)

    def test_crash_recovery_keeps_reservation_until_human_verifies(self):
        first=self.draft(); self.claim(first)
        with patch.object(self.service.store,'owner_alive',return_value=False): self.service.store.recover_outbox()
        self.assertEqual(self.service.store.row('outbox',first['id'])['status'],'uncertain')
        self.budget(0,1)

    def test_manual_reply_does_not_consume_auto_quota(self):
        first=self.draft()
        with patch('bosshunter.recruiting.service.time.sleep'): self.service.execute(first['id'])
        self.budget(0,0)

    def test_settlement_uses_original_reservation_day(self):
        first=self.draft()
        with patch('bosshunter.recruiting.jobs.day_key',return_value='2026-10-09'): self.claim(first)
        self.service.store.finish(first['id'],'uncertain','跨日待核实')
        self.service.store.resolve_outbound('reply',first['id'],'sent','核实昨天发送的消息确实存在')
        self.assertEqual(self.service.store.auto_reply_budget('2026-10-09'),{'sent':1,'reserved':0})
        self.assertEqual(self.service.store.auto_reply_budget('2026-10-10'),{'sent':0,'reserved':0})

    def test_legacy_counts_migrate_once_without_guessing_historical_outcomes(self):
        store=self.service.store
        with store.db() as db:
            db.execute("DELETE FROM settings WHERE key='auto_reply_reservation_migrated'")
            db.execute('DELETE FROM auto_reply_days')
            db.execute("INSERT OR REPLACE INTO settings VALUES ('auto_reply_sent',?)",(encode({'date':day_key(),'count':3}),))
        other=Store(store.path)
        self.assertEqual(other.auto_reply_budget(),{'sent':3,'reserved':0})
        self.assertEqual(Store(store.path).auto_reply_budget(),{'sent':3,'reserved':0})
