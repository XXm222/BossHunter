"""Offline hybrid scheduling tests; no platform/browser/model connections."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch, Mock

from bosshunter.recruiting.ws_monitor import HybridMonitor
from bosshunter.recruiting.store import Store
from bosshunter.recruiting.browser import AccountPauseError
import test_recruiting_review


class HybridTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'db')
        self.h = HybridMonitor(self.store)
        self.rows = [{'id': '123-0', 'snapshot': json.dumps({'account_uid':'456'})}]
        self.result = {'connected':True,'epoch':'a','generation':1,'cursor':0,'events':[]}

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, **kw):
        self.h.observe({**self.result, **kw}, self.rows)

    def finish(self):
        job = self.h.next_job(['123-0'],600)
        self.h.finish(job,True,600)
        return job

    def event(self, mid='99', **kw):
        return {'mid':mid,'from':{'uid':'123','source':'0'},'to':{'uid':'456'},**kw}

    def test_start_and_reconnect_backfill_then_idle_has_no_http_job(self):
        self.observe(); self.assertEqual(self.finish()[0],'123-0')
        self.assertEqual(self.h.queue,{})
        with patch.object(self.store,'row',return_value={'taken_over':False}):
            self.assertIsNone(self.h.next_job(['123-0'],600))
        self.observe(connected=False); self.assertIsNone(self.h.next_job(['123-0'],600))
        self.observe(); self.assertIsNotNone(self.h.next_job(['123-0'],600))

    def test_duplicate_delivery_coalesced_and_persisted_across_restart(self):
        self.observe(); self.finish()
        self.observe(events=[self.event(),self.event()]); self.assertEqual(len(self.h.seen),1)
        job = self.finish(); self.observe(events=[self.event()]); self.assertFalse(self.h.queue)
        restarted = HybridMonitor(self.store)
        self.assertEqual(restarted.seen,self.h.seen)
        self.assertIsNotNone(job)

    def test_wrong_account_unbound_source_and_missing_ids_ignored(self):
        self.observe(); self.finish()
        bad = [self.event(to={'uid':'wrong'}),self.event(**{'from':{'uid':'unknown','source':'0'}}),self.event(**{'from':{'uid':'123'}}), self.event(mid=''),self.event(**{'from':{'uid':'456','source':'0'},'to':{'uid':'123'}})]
        self.observe(events=bad); self.assertFalse(self.h.queue)

    def test_outgoing_message_syncs_bound_peer_and_deduplicates_server_echo(self):
        self.observe(); self.finish()
        event = self.event(**{'from': {'uid': '456'}, 'to': {'uid': '123', 'source': '0'}})
        self.observe(events=[event]); self.assertEqual(set(self.h.queue), {'123-0'})
        self.finish()
        self.observe(events=[event]); self.assertFalse(self.h.queue)

    def test_outgoing_wrong_account_unknown_peer_and_source_are_ignored(self):
        self.observe(); self.finish()
        for sender, recipient in [('999', {'uid':'123','source':'0'}),
                                  ('456', {'uid':'123','source':'1'}),
                                  ('456', {'uid':'789','source':'0'}),
                                  ('456', {'uid':'123'})]:
            self.observe(events=[self.event(**{'from':{'uid':sender},'to':recipient})])
            self.assertFalse(self.h.queue)

    def test_outgoing_during_sync_survives_completion_and_removed_binding_is_ignored(self):
        self.observe(); job = self.h.next_job(['123-0'],600)
        event = self.event(**{'from':{'uid':'456'},'to':{'uid':'123','source':'0'}})
        self.observe(events=[event]); self.h.finish(job,True,600)
        self.assertIsNotNone(self.h.next_job(['123-0'],600))
        self.rows = []; self.observe(events=[event]); self.assertFalse(self.h.queue)

    def test_new_message_during_processing_survives_and_is_not_delayed(self):
        self.observe(); job = self.h.next_job(['123-0'],600)
        self.observe(events=[self.event()]); self.h.finish(job,True,600)
        self.assertIsNotNone(self.h.next_job(['123-0'],600))
        self.finish(); self.assertFalse(self.h.queue)

    def test_new_event_clears_pending_retry_delay(self):
        self.observe(); self.finish()
        self.observe(events=[self.event()]); self.assertIsNotNone(self.h.next_job(['123-0'],600))

    def test_failure_retains_queue_and_backs_off(self):
        self.observe(); job = self.h.next_job(['123-0'],600)
        self.h.finish(job,False,600)
        self.assertTrue(self.h.queue)
        with patch.object(self.store,'row',return_value={'taken_over':False}):
            self.assertIsNone(self.h.next_job(['123-0'],600))

    def test_gap_runtime_restart_and_new_binding_backfill(self):
        self.observe(); self.finish(); self.observe(gap=True); self.assertTrue(self.h.queue)
        self.finish(); self.observe(epoch='new'); self.assertTrue(self.h.queue)
        self.finish(); self.rows.append({'id':'789-1','snapshot':'{"account_uid":"456"}'}); self.observe(epoch='new')
        self.assertIn('789-1',self.h.queue)

    def test_removed_permission_drops_queued_work(self):
        self.observe(); self.rows=[]; self.observe(); self.assertFalse(self.h.queue)

    def test_pending_draft_retries_without_new_message_takeover_does_not(self):
        self.observe(); self.finish(); self.h.retry_at.clear()
        self.store.set_setting('reply_work:123-0', {'status':'drafted'})
        with patch.object(self.store,'row',return_value={'taken_over':False}):
            self.assertEqual(self.h.next_job(['123-0'],600),('123-0',None))
        with patch.object(self.store,'row',return_value={'taken_over':True}):
            self.assertIsNone(self.h.next_job(['123-0'],600))

    def test_needs_attention_does_not_retry(self):
        self.observe(); self.finish(); self.h.retry_at.clear()
        self.store.set_setting('reply_work:123-0', {'status':'needs_attention'})
        with patch.object(self.store,'row',return_value={'taken_over':False}):
            self.assertIsNone(self.h.next_job(['123-0'],600))

    def test_connection_failure_preserves_pending_notifications(self):
        self.observe(events=[self.event()]); self.h.observe(None,self.rows)
        self.assertTrue(self.h.queue); self.assertEqual(self.h.state['transport'],'http')


class HybridServiceTests(TestCase):
    setUp = test_recruiting_review.ReviewTests.setUp
    tearDown = test_recruiting_review.ReviewTests.tearDown

    def test_targeted_monitor_rechecks_permissions(self):
        with patch.object(self.service,'sync') as sync:
            result = self.service.monitor_once('unbound')
        sync.assert_not_called(); self.assertFalse(result['changed'])

    def test_observer_restriction_uses_shared_pause(self):
        self.service.browser = Mock()
        self.service.browser.runtime.recruiting_ws.return_value = {'paused':True}
        self.service.set_monitor_enabled(True)
        with self.assertRaises(AccountPauseError): self.service._poll_ws()
        self.assertFalse(self.service.store.setting('monitor_enabled'))
        self.assertGreater(self.service.store.setting('request_paused_until'),0)

    def test_status_reads_local_state_only(self):
        self.service.store.set_setting('ws_monitor_state',{'transport':'ws','pending_count':2})
        with patch.object(self.service,'_poll_ws') as poll:
            self.assertEqual(self.service.state()['monitor']['listener']['transport'],'ws')
        poll.assert_not_called()

    def test_worker_ws_mode_targets_notifications_and_does_not_empty_poll(self):
        self.service.set_monitor_enabled(True)
        stop = Mock()
        waits = []
        stop.is_set.side_effect = lambda: len(waits) >= 3
        stop.wait.side_effect = lambda _: waits.append(1)
        hybrid = Mock(connected=True)
        hybrid.next_job.side_effect = [(self.c['id'],1),None,None]
        self.service.ws_monitor = hybrid
        with patch('bosshunter.recruiting.service.Thread'), patch.object(self.service,'monitor_once') as monitor:
            self.service.worker_loop(stop)
        monitor.assert_called_once_with(self.c['id'])
        hybrid.finish.assert_called_once_with((self.c['id'],1),True,600)

    def test_security_navigation_of_bound_target_still_pauses(self):
        from bosshunter.recruiting.browser import BrowserError
        self.service.browser = Mock(target_id='same-target')
        self.service.browser.bound_target.side_effect = BrowserError('绑定页面已离开招聘端')
        self.service.browser.runtime.recruiting_ws.return_value = {'paused':True}
        with self.assertRaises(AccountPauseError): self.service._poll_ws()
        self.service.browser.runtime.recruiting_ws.assert_called_once_with('same-target',0)

    def test_stale_listener_display_falls_back(self):
        self.service.store.set_setting('ws_monitor_state',{'transport':'ws','updated_at':1})
        self.assertEqual(self.service.state()['monitor']['listener']['transport'],'http')


class HybridBoundaryTests(TestCase):
    setUp = HybridTests.setUp
    tearDown = HybridTests.tearDown

    def test_observer_timeout_reenables_http(self):
        self.h.observe(self.result,self.rows)
        self.assertTrue(self.h.connected)
        with patch('bosshunter.recruiting.ws_monitor.time.time',return_value=self.h.last_observed + 36):
            self.assertFalse(self.h.connected)
            self.assertIsNone(self.h.next_job(['123-0'],600))

    def test_legacy_binding_without_account_keeps_http(self):
        self.rows[0]['snapshot']='{}'
        self.h.observe(self.result,self.rows)
        self.assertFalse(self.h.connected)
        self.assertEqual(self.h.state['transport'],'http')

    def test_queue_is_persisted_before_cursor_advances(self):
        with patch.object(self.store,'set_setting',side_effect=RuntimeError('synthetic DB failure')):
            with self.assertRaises(RuntimeError):
                self.h.observe({**self.result,'cursor':10,'events':[{'mid':'99','from':{'uid':'123','source':'0'},'to':{'uid':'456'}}]},self.rows)
        self.assertEqual(self.h.cursor,0)
