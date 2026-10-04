"""Local boundary tests use one synthetic conversation, never a live browser."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

from bosshunter.recruiting import agent
from bosshunter.recruiting.browser import BossBrowser, BrowserError
from bosshunter.recruiting.service import RecruitingService


def sample():
    return {"id": "sample-1", "name": "测试候选人", "position_title": "测试岗位",
            "messages": [{"direction": "in", "kind": "text", "text": "请问工作时间？", "time": "10:00"}],
            "editor_empty": True, "coverage": "test", "stable_message_ids": False}


class FakeBrowser:
    def __init__(self):
        self.snapshot = sample()
        self.calls = 0
        self.fail = False

    def status(self):
        return {"connected": True, "host": "www.zhipin.com", "path": "/web/chat/index", "recruiter": True}

    def read_current(self):
        return deepcopy(self.snapshot)

    def open_conversation(self, ident):
        return self.read_current()

    def execute(self, kind, before, content):
        self.calls += 1
        if self.fail:
            raise BrowserError("simulated disconnect after dispatch")
        if kind == "reply":
            self.snapshot["messages"].append({"direction": "out", "kind": "text", "text": content, "time": ""})


class RecruitingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.browser = FakeBrowser()
        self.service = RecruitingService(Path(self.temp.name) / "recruiting.db", lambda: {}, self.browser)
        self.c = self.service.import_current()
        self.cid = self.c["id"]

    def tearDown(self):
        self.temp.cleanup()

    def fact(self, **overrides):
        data = {"title": "工作时间", "content": "测试制度：周一到周五。", "keywords": "工作时间,上班", "source": "测试材料", "public": True, "approved": True, "position_id": self.c["position_id"]}
        data.update(overrides)
        return self.service.knowledge(data)

    def test_import_is_idempotent_and_second_person_allowed(self):
        self.service.import_current()
        self.assertEqual(len(self.service.store.rows("conversations")), 1)
        self.browser.snapshot["id"] = "sample-2"
        self.service.import_current()  # 多会话：第二个会话应允许导入
        self.assertEqual(len(self.service.store.rows("conversations")), 2)

    def test_changed_position_is_not_silently_rebound(self):
        self.browser.snapshot["position_title"] = "另一岗位"
        with self.assertRaisesRegex(ValueError, "职位已变化"):
            self.service.sync()

    def test_document_dedup_and_complete_version(self):
        store = self.service.store
        a = store.save_document(self.cid, "pdf", "resume" * 20, False, {})
        b = store.save_document(self.cid, "pdf", "resume" * 20, False, {})
        self.assertEqual(a["id"], b["id"])
        c = store.save_document(self.cid, "pdf", "resume" * 20, True, {})
        self.assertNotEqual(a["id"], c["id"])

    def test_invitation_is_blocked_at_service_store_and_adapter(self):
        draft = self.service.invitation({"conversation_id": self.cid, "mode": "online", "scheduled_at": (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(), "location": "人工安排", "contact": "测试联系人"})
        with self.assertRaises(PermissionError):
            self.service.execute(draft["id"])
        with self.assertRaises(ValueError):
            self.service.store.claim(draft["id"])
        with patch.object(BossBrowser, "evaluate") as command:
            with self.assertRaises(BrowserError):
                BossBrowser().execute("invitation", {}, "")
            command.assert_not_called()
        self.assertEqual(self.browser.calls, 0)

    def test_send_once_and_persistent_budget(self):
        draft = self.service.prepare_reply(self.cid, "测试回复")
        self.assertEqual(self.service.execute(draft["id"])["status"], "sent")
        with self.assertRaises(ValueError):
            self.service.execute(draft["id"])
        other = self.service.prepare_reply(self.cid, "另一回复")
        self.assertEqual(self.service.execute(other["id"])["status"], "sent")
        self.assertEqual(self.browser.calls, 2)

    def test_daily_reply_quota(self):
        self.service.store.set_setting('auto_reply_daily_limit', 1)
        draft = self.service.prepare_reply(self.cid, "测试回复")
        self.assertEqual(self.service.execute(draft["id"])["status"], "sent")
        other = self.service.prepare_reply(self.cid, "另一回复")
        with self.assertRaisesRegex(ValueError, "上限"):
            self.service.execute(other["id"])
        self.assertEqual(self.browser.calls, 1)

    def test_auto_send_per_conversation_switch(self):
        self.assertFalse(self.service._auto_send_enabled(self.cid))
        self.service.set_auto_send(self.cid, True)
        self.assertTrue(self.service._auto_send_enabled(self.cid))
        self.service.set_auto_send(self.cid, False)
        self.assertFalse(self.service._auto_send_enabled(self.cid))

    def test_auto_send_if_allowed_sends_and_skips(self):
        self.service.set_auto_send(self.cid, True)
        ok = self.service.prepare_reply(self.cid, "你好")
        self.assertTrue(self.service._auto_send_if_allowed(ok))
        self.assertEqual(self.browser.calls, 1)
        nh = self.service.store.draft(self.cid, "reply", "需要确认", {"source": "ai_draft", "needs_human": True})
        self.assertFalse(self.service._auto_send_if_allowed(nh))
        self.assertEqual(self.browser.calls, 1)

    def test_set_monitor_enabled(self):
        self.assertTrue(self.service.set_monitor_enabled(True)["monitor_enabled"])
        self.assertTrue(self.service.store.setting("monitor_enabled"))
        self.service.set_monitor_enabled(False)
        self.assertFalse(self.service.store.setting("monitor_enabled"))

    def test_worker_alive_and_monitor_running(self):
        self.assertFalse(self.service._worker_alive())
        self.assertFalse(self.service.state()["monitor"]["running"])
        self.service.set_monitor_enabled(True)
        self.assertFalse(self.service.state()["monitor"]["running"])  # worker 未存活
        self.service._write_heartbeat()
        self.assertTrue(self.service._worker_alive())
        self.assertTrue(self.service.state()["monitor"]["running"])
        self.assertTrue(self.service.state()["worker"]["alive"])

    def test_list_contacts_uses_browser(self):
        self.browser.read_contact_list = lambda: [
            {"ident": "96429428-0", "name": "陈健", "position_title": "电子工程师"},
        ]
        result = self.service.list_contacts()
        self.assertEqual(result, [
            {"ident": "96429428-0", "name": "陈健", "position_title": "电子工程师", "last_ts": None},
        ])

    def test_sync_all_contacts_imports_and_skips_existing(self):
        self.browser.read_contact_list = lambda: [
            {"ident": "96429428-0", "name": "陈健", "position_title": "电子工程师"},
            {"ident": "84519593-0", "name": "李四", "position_title": "产品研发经理"},
        ]
        first = self.service.sync_all_contacts()
        self.assertEqual(first["imported"], 2)
        self.assertEqual(first["total"], 2)
        ids = {c["id"] for c in self.service.store.rows("conversations")}
        self.assertIn("96429428-0", ids)
        self.assertIn("84519593-0", ids)
        # 再次同步：跳过已存在的，imported 为 0
        second = self.service.sync_all_contacts()
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["total"], 2)

    def test_sync_all_contacts_shares_same_title_position(self):
        self.browser.read_contact_list = lambda: [
            {"ident": "96429428-0", "name": "陈健", "position_title": "电子工程师"},
            {"ident": "84519593-0", "name": "李四", "position_title": "电子工程师"},
            {"ident": "81667022-0", "name": "王五", "position_title": "产品经理"},
        ]
        self.service.sync_all_contacts()
        a = self.service.store.row("conversations", "96429428-0")
        b = self.service.store.row("conversations", "84519593-0")
        c = self.service.store.row("conversations", "81667022-0")
        # 同名岗位共享同一条 position
        self.assertEqual(a["position_id"], b["position_id"])
        # 不同岗位各一条 position
        self.assertNotEqual(a["position_id"], c["position_id"])
        titles = sorted(p["title"] for p in self.service.store.rows("positions") if p["title"] in {"电子工程师", "产品经理"})
        self.assertEqual(titles, ["产品经理", "电子工程师"])

    def test_invitation_cannot_use_reply_channel(self):
        with self.assertRaises(PermissionError):
            self.service.prepare_reply(self.cid, "邀请您明天来公司面试")
        with patch.object(BossBrowser, "evaluate") as command:
            with self.assertRaises(PermissionError):
                BossBrowser().execute("reply", {}, "Please attend an interview")
            command.assert_not_called()
        p = self.service.store.row("positions", self.c["position_id"])
        draft = self.service.store.draft(self.cid, "reply", "来公司聊聊", {"position_version": p["version"]})
        with self.assertRaises(PermissionError):
            self.service.execute(draft["id"])
        self.assertEqual(self.browser.calls, 0)

    def test_reply_yields_to_render_and_rechecks_before_click(self):
        with patch.object(BossBrowser, "evaluate", side_effect=[{"prepared": True}, BrowserError("changed")]) as call:
            with self.assertRaises(BrowserError):
                BossBrowser().execute("reply", sample(), "已收到")
            prepare, submit = [x.args[0] for x in call.call_args_list]
            self.assertNotIn("send.click()", prepare)
            self.assertIn("editor.innerText!==", submit)
            self.assertIn("JSON.stringify(snapshot.messages)", submit)
            self.assertIn(".submit.active", submit)

    def test_new_message_invalidates_draft_without_sending(self):
        draft = self.service.prepare_reply(self.cid, "旧回复")
        self.browser.snapshot["messages"].append({"direction": "in", "kind": "text", "text": "先不用了", "time": ""})
        with self.assertRaisesRegex(ValueError, "新消息"):
            self.service.execute(draft["id"])
        self.assertEqual(self.service.store.row("outbox", draft["id"])["status"], "expired")
        self.assertEqual(self.browser.calls, 0)

    def test_human_editor_blocks_send(self):
        draft = self.service.prepare_reply(self.cid, "测试")
        self.browser.snapshot["editor_empty"] = False
        with self.assertRaises(ValueError):
            self.service.execute(draft["id"])
        self.assertEqual(self.browser.calls, 0)

    def test_do_not_contact_cancels_drafts(self):
        draft = self.service.prepare_reply(self.cid, "测试")
        self.service.control(self.cid, False, True)
        self.assertEqual(self.service.store.row("outbox", draft["id"])["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.service.execute(draft["id"])
        self.assertEqual(self.browser.calls, 0)

    def test_ambiguous_send_never_retries(self):
        self.browser.fail = True
        draft = self.service.prepare_reply(self.cid, "测试")
        with self.assertRaises(BrowserError):
            self.service.execute(draft["id"])
        self.assertEqual(self.service.store.row("outbox", draft["id"])["status"], "uncertain")
        with self.assertRaises(ValueError):
            self.service.execute(draft["id"])
        self.assertEqual(self.browser.calls, 1)

    def test_restart_marks_in_flight_as_uncertain(self):
        draft = self.service.prepare_reply(self.cid, "测试")
        self.service.store.claim(draft["id"])
        restarted = RecruitingService(self.service.store.path, lambda: {}, self.browser)
        self.assertEqual(restarted.store.row("outbox", draft["id"])["status"], "uncertain")

    def test_knowledge_scope_private_expired_draft_filters(self):
        public = self.fact()
        hidden = self.fact(title="内部工作时间", public=False)
        expired = self.fact(title="旧工作时间", valid_until="2000-01-01")
        draft = self.fact(title="待审核工作时间", approved=False)
        foreign = {**public, "id": "foreign", "position_id": "elsewhere"}
        hits = agent.retrieve([public, hidden, expired, draft, foreign], "工作时间是什么", self.c["position_id"])
        self.assertEqual([x["id"] for x in hits], [public["id"]])

    def test_knowledge_change_expires_referencing_draft(self):
        fact = self.fact()
        draft = self.service.store.draft(self.cid, "reply", "测试", {"knowledge": {fact["id"]: 1}})
        self.fact(id=fact["id"], content="更新测试制度")
        self.assertEqual(self.service.store.row("outbox", draft["id"])["status"], "expired")

    def test_position_pause_cancels_execution(self):
        draft = self.service.prepare_reply(self.cid, "测试")
        self.service.position({"id": self.c["position_id"], "jd": "真实JD", "enabled": False})
        with self.assertRaises(ValueError):
            self.service.execute(draft["id"])
        self.assertEqual(self.browser.calls, 0)

    def test_monitor_baseline_does_not_reply_to_history(self):
        self.assertFalse(self.service.monitor_once()["changed"])
        self.assertEqual(self.service.store.rows("outbox"), [])

    def test_model_missing_is_not_a_fake_score(self):
        with patch.object(agent, "get_ai_api_key", return_value=None), patch.object(agent, "call_anthropic_text") as call:
            with self.assertRaisesRegex(ValueError, "尚未配置"):
                agent.assess({"jd": "测试"}, {"text": "test", "complete": True}, {})
            call.assert_not_called()

    def test_unknown_dimensions_keep_total_null(self):
        payload = {"components": {k: {"status": "unknown", "score": None, "quotes": [], "reason": "待确认"} for k in agent.LIMITS}, "questions": ["请补充实际项目"]}
        result = agent.validate_assessment(payload, "资料", True)
        self.assertIsNone(result["score"])
        self.assertEqual(result["coverage"], 0)

    def test_fabricated_evidence_and_missing_as_zero_rejected(self):
        payload = {"components": {k: {"status": "supported", "score": 1, "quotes": ["编造的证据"], "reason": "测试"} for k in agent.LIMITS}, "questions": []}
        with self.assertRaises(ValueError):
            agent.validate_assessment(payload, "实际资料", True)
        for part in payload["components"].values():
            part.update(status="unknown", score=0, quotes=[])
        with self.assertRaises(ValueError):
            agent.validate_assessment(payload, "实际资料", True)

    def test_company_text_is_versioned_and_invalidates_ai_drafts(self):
        company = self.service.save_company("真实公司作息说明")
        self.assertEqual(company["version"], 1)
        self.assertEqual(self.service.save_company("真实公司作息说明")["version"], 1)
        draft = self.service.store.draft(self.cid, "reply", "旧口径", {"source": "ai_draft"})
        manual = self.service.prepare_reply(self.cid, "人工内容")
        self.service.save_company("已调整的作息")
        self.assertEqual(self.service.store.row("outbox", draft["id"])["status"], "expired")
        self.assertEqual(self.service.store.row("outbox", manual["id"])["status"], "draft")

    def test_reply_model_gets_full_loaded_conversation_job_company_resume(self):
        self.service.save_company("每周工作五天")
        self.service.position({"id": self.c["position_id"], "jd": "需要电商财务经验", "enabled": True})
        self.service.store.save_document(self.cid, "test", "做过电商平台核算" * 12, False, {})
        self.browser.snapshot["messages"] = [
            {"direction": "out", "kind": "text", "text": "你可以处理平台对账吗？", "time": "10:00"},
            {"direction": "in", "kind": "text", "text": "可以，去年一直在做。", "time": "10:01"}]
        response = {"text": "了解，谢谢补充。", "basis": ["conversation"], "needs_human": False, "missing": []}
        with patch.object(agent, "get_ai_api_key", return_value="test-only"), patch.object(agent, "call_anthropic_text", return_value=json.dumps(response)) as call:
            draft = self.service.prepare_reply(self.cid)
        prompt = call.call_args.args[0]
        context = json.loads(prompt.split("资料 JSON：\n", 1)[1])
        self.assertEqual(context["conversation"]["messages"], self.browser.snapshot["messages"])
        self.assertEqual(context["company"]["text"], "每周工作五天")
        self.assertEqual(context["job"]["jd"], "需要电商财务经验")
        self.assertIn("平台核算", context["resume"]["text"])
        self.assertFalse(context["conversation"]["history_complete"])
        self.assertEqual(json.loads(draft["refs"])["message_count"], 2)
        self.assertEqual(self.browser.calls, 0)

    def test_context_ignores_stale_assessments(self):
        doc = self.service.store.save_document(self.cid, "test", "完整资料" * 20, True, {})
        with self.service.store.db() as db:
            db.execute("INSERT INTO assessments VALUES (?,?,?,?,?)", ("old", self.cid, "hash", json.dumps({"document_id": doc["id"], "position_version": 999}), "2026-01-01"))
        self.assertIsNone(self.service.reply_context(self.cid)["assessment"])

    def test_regenerated_identical_wording_uses_new_company_context(self):
        with patch.object(agent, "reply", return_value={"text": "了解，谢谢。", "basis": ["conversation"], "needs_human": False}):
            old = self.service.prepare_reply(self.cid)
            self.service.save_company("已更新的公司说明")
            fresh = self.service.prepare_reply(self.cid)
        self.assertEqual(fresh["status"], "draft")
        self.assertNotEqual(old["refs"], fresh["refs"])
        self.assertEqual(json.loads(fresh["refs"])["company_version"], 1)
        self.assertEqual(self.browser.calls, 0)

    def test_message_arriving_during_generation_discards_reply(self):
        def changed(*args):
            self.browser.snapshot["messages"].append({"direction": "in", "kind": "text", "text": "刚才说错了", "time": ""})
            return {"text": "旧回答", "basis": ["conversation"], "needs_human": False}
        with patch.object(agent, "reply", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "生成期间"):
                self.service.prepare_reply(self.cid)
        self.assertEqual(self.service.store.rows("outbox"), [])
        self.assertEqual(self.browser.calls, 0)

    def test_resume_change_blocks_existing_ai_reply(self):
        with patch.object(agent, "reply", return_value={"text": "了解", "basis": ["conversation"], "needs_human": False}):
            draft = self.service.prepare_reply(self.cid)
        self.service.store.save_document(self.cid, "test", "新资料" * 30, False, {})
        with self.assertRaisesRegex(ValueError, "回复依据已变化"):
            self.service.execute(draft["id"])
        self.assertEqual(self.browser.calls, 0)

    def test_reply_cannot_cite_absent_company_or_hide_missing_facts(self):
        context = self.service.reply_context(self.cid)
        response = {"text": "需要确认具体作息", "basis": ["company"], "needs_human": False, "missing": ["作息"]}
        with patch.object(agent, "get_ai_api_key", return_value="test-only"), patch.object(agent, "call_anthropic_text", return_value=json.dumps(response)):
            with self.assertRaisesRegex(ValueError, "未提供"):
                agent.reply(context, {})
        response["basis"] = ["conversation"]
        with patch.object(agent, "get_ai_api_key", return_value="test-only"), patch.object(agent, "call_anthropic_text", return_value=json.dumps(response)):
            self.assertTrue(agent.reply(context, {})["needs_human"])

    def test_oversized_context_is_not_silently_truncated(self):
        context = self.service.reply_context(self.cid)
        context["company"]["text"] = "资料" * 90000
        with patch.object(agent, "get_ai_api_key", return_value="test-only"), patch.object(agent, "call_anthropic_text") as call:
            with self.assertRaisesRegex(ValueError, "未截断"):
                agent.reply(context, {})
            call.assert_not_called()


class RecruitingHTTPTests(unittest.TestCase):
    def invoke(self, path, payload, origin="http://127.0.0.1:8686", peer="127.0.0.1"):
        from bosshunter.web.server import app
        body = json.dumps(payload).encode()
        env = {}
        setup_testing_defaults(env)
        env.update(REQUEST_METHOD="POST", PATH_INFO=path, CONTENT_TYPE="application/json", CONTENT_LENGTH=str(len(body)), HTTP_HOST="127.0.0.1:8686", HTTP_ORIGIN=origin, REMOTE_ADDR=peer)
        env["wsgi.input"] = io.BytesIO(body)
        result = {}
        response = app(env, lambda status, headers, exc_info=None: result.update(status=int(status.split()[0])))
        result["body"] = b"".join(response)
        return result

    def test_invitation_endpoint_forbidden_without_browser(self):
        response = self.invoke("/api/recruiting/invitations/send", {})
        self.assertEqual(response["status"], 403)
        self.assertIn(b"invitation_send_disabled", response["body"])

    def test_cross_origin_and_remote_recruiting_actions_forbidden(self):
        self.assertEqual(self.invoke("/api/recruiting/import-current", {}, origin="https://untrusted.example")["status"], 403)
        self.assertEqual(self.invoke("/api/recruiting/import-current", {}, peer="192.0.2.1")["status"], 403)


if __name__ == "__main__":
    unittest.main()
