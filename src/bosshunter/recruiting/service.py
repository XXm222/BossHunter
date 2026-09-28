"""One-account, one-conversation recruiting pilot with persistent outbound guards."""
from datetime import date, datetime
import json
import time
from threading import Event, RLock, Thread
from uuid import uuid4

from . import agent
from .browser import BossBrowser, BrowserError
from .store import Store, encode, fingerprint, now
from .policy import check_reply
from .jobs import RecruitingJobs
from .local_session import LocalBossSession
from bosshunter.browser.client import RuntimeClient


class RecruitingService:
    def __init__(self, path, config_provider, browser=None, local_session=None):
        self.store = Store(path)
        self.jobs = RecruitingJobs(self.store)
        self.config_provider = config_provider
        self.browser = browser or BossBrowser(runtime=RuntimeClient(config_provider()),
                                              target_id=config_provider().get("browser", {}).get("recruiting_target_id"))
        self.local_session = local_session or LocalBossSession()
        self.use_local_session = browser is None or local_session is not None
        self.lock = RLock()
        self.stop_event = Event()
        self.worker = None
        self.monitor = {"running": False, "last_success": None, "error": "", "interval_seconds": 120,
                        "mode": "read_assess_and_draft", "note": "监测绑定会话，收到简历自动读取和评分；不会自动外发"}
        self.connection = {"connected": False, "message": "尚未核实本地 BOSS 登录状态", "transport": "local_cookie_http"}
        self.store.recover_outbox()

    def state(self):
        s = self.store
        conversations = s.rows("conversations")
        for c in conversations:
            c["snapshot"] = json.loads(c["snapshot"])
        docs = s.rows("documents", "ORDER BY created_at DESC, rowid DESC")
        for d in docs:
            d["meta"] = json.loads(d["meta"])
        assessments = s.rows("assessments", "ORDER BY created_at DESC, rowid DESC")
        for a in assessments:
            a["result"] = json.loads(a["result"])
        drafts = s.rows("outbox", "ORDER BY created_at DESC, rowid DESC")
        for d in drafts:
            d["refs"] = json.loads(d["refs"])
        return {"positions": s.rows("positions"), "conversations": conversations,
                "documents": docs, "resume_processing": {c["id"]: s.setting("resume_processing:" + c["id"], {}) for c in conversations}, "company": self.company(), "recruiting_jobs": self.jobs.state(), "assessments": assessments,
                "outbox": drafts, "events": s.rows("events", "ORDER BY id DESC LIMIT 30"),
                "connection": self.connection, "monitor": self.monitor.copy(),
                "model_ready": bool(agent.get_ai_api_key(self.config_provider())),
                "pilot": {"max_conversations": 20, "max_outbound_per_action": 1,
                          "invitation_sending": False, "automatic_sending": False,
                          "conversation_id": s.setting("pilot_conversation"),
                          "message_identity": "platform_ids" if conversations and all(c['snapshot'].get('stable_message_ids') for c in conversations) else "snapshot_only", "coverage": "已同步多个候选人会话（非全量）"}}

    def sync_jobs(self):
        previous = self.store.setting("jobs_sync", {})
        try:
            result = self.jobs.import_snapshot(self.local_session.read_jobs())
            self.connection = {"connected": True, "transport": "local_cookie_http", "checked_at": now(),
                               "message": "已通过本地登录状态核实 BOSS 岗位读取；自动外发未接通",
                               "read_jobs": True, "read_bound_conversation": False, "automatic_sending": False}
            return result
        except Exception as exc:
            self.store.set_setting("jobs_sync", {**previous, "attempted_at": now(), "error": str(exc)[:300]})
            self.connection = {"connected": False, "transport": "local_cookie_http", "checked_at": now(),
                               "message": "BOSS 岗位读取未完成，请核实本地登录状态"}
            raise

    def connect(self):
        if self.use_local_session:
            self.sync_jobs()
            return self.connection
        try:
            result = self.browser.status()
            if not result.get("recruiter") or result.get("host") != "www.zhipin.com":
                raise BrowserError("请在绑定标签页打开已登录的 BOSS 招聘端")
            self.connection = {**result, "message": "已连接 Chrome 招聘端（BossHunter Browser Runtime）", "checked_at": now()}
        except BrowserError as exc:
            self.connection = {"connected": False, "message": str(exc), "checked_at": now()}
            raise
        return self.connection

    def import_current(self):
        with self.lock:
            if self.use_local_session:
                if not self.store.setting("pilot_conversation"):
                    raise ValueError("尚未绑定候选人；本地模式不会操作 Chrome 标签页或自动选择候选人")
                return self.sync()
            self.connect()
            result = self.store.import_conversation(self.browser.read_current())
            self.store.event("conversation_synced", result["id"], "读取当前一个会话；未发送消息")
            return result

    def list_contacts(self):
        """读取招聘账号的联系人列表，供绑定向导从列表选人。"""
        return self.local_session.list_contacts()

    def preview_binding(self, conversation_id, name, position_title):
        """读取并核实一个候选会话，返回账号身份供用户确认；不写入绑定。

        本地模式不操作 Chrome 标签页，只通过登录 Cookie 后台读取。
        read_conversation 会校验候选人姓名、招聘账号唯一性，并返回 account_uid。
        """
        if not all(str(x).strip() for x in (conversation_id, name, position_title)):
            raise ValueError("请填写会话标识、候选人姓名和沟通岗位")
        snapshot = self.local_session.read_conversation(
            str(conversation_id).strip(), str(name).strip(), str(position_title).strip())
        return {"account_uid": snapshot["account_uid"], "name": snapshot["name"],
                "position_title": snapshot["position_title"],
                "message_count": len(snapshot["messages"]),
                "coverage": snapshot.get("coverage", "")}

    def confirm_binding(self, conversation_id, name, position_title, expected_account):
        """核实后把会话写入 pilot_conversation（首次绑定的唯一入口）。

        重新读取一次并带上预览时拿到的 account_uid 作 expected_account，
        read_conversation 会校验当前登录账号未变化，再导入并绑定。
        """
        snapshot = self.local_session.read_conversation(
            str(conversation_id).strip(), str(name).strip(), str(position_title).strip(),
            expected_account=str(expected_account).strip())
        result = self.store.import_conversation(snapshot)
        self.store.select_conversation(result["id"])  # 新绑定的会话设为当前选中
        self.store.event("conversation_bound", result["id"], "已核实并绑定会话；未发送消息")
        return result

    def sync(self, cid=None, *, process=True):
        """同步一个会话的消息；process=True 时同时读简历/评分。

        cid 缺省时同步当前选中会话；process=False 只更新消息快照（监测同步所有会话时用）。
        """
        with self.lock:
            ident = cid or self.store.setting("pilot_conversation")
            if not ident:
                raise ValueError("请先绑定一个会话")
            if self.use_local_session:
                current = self.store.row("conversations", ident)
                position = self.store.row("positions", current['position_id'])
                previous = json.loads(current['snapshot'])
                try:
                    snapshot = self.local_session.read_conversation(ident, current['name'], position['title'], previous.get('account_uid'))
                    result = self.store.import_conversation(snapshot)
                except BrowserError as exc:
                    self.monitor['error'] = str(exc)
                    self.connection = {"connected": False, "transport": "local_cookie_http", "checked_at": now(), "message": str(exc)}
                    raise
                self.connection = {"connected": True, "transport": "local_cookie_http", "checked_at": now(),
                                   "message": "已连接 BOSS：本地登录会话，只读同步绑定候选人的消息",
                                   "read_jobs": bool(self.jobs.state()['sync'].get('synced_at')),
                                   "read_bound_conversation": True, "automatic_sending": False}
                self.store.event("conversation_synced", ident, f"后台同步绑定会话 {len(snapshot['messages'])} 条平台消息；未操作标签页或发送消息")
            else:
                result = self.store.import_conversation(self.browser.open_conversation(ident))
            if process:
                self.process_received_resume(result)
            self.monitor["last_success"] = now()
            self.monitor["error"] = ""
            return result

    def control(self, cid, taken_over, do_not_contact):
        if type(taken_over) is not bool or type(do_not_contact) is not bool:
            raise ValueError("开关必须是布尔值")
        self.store.row("conversations", cid)
        with self.lock, self.store.db() as db:
            db.execute("UPDATE conversations SET taken_over=?,do_not_contact=? WHERE id=?", (taken_over, do_not_contact, cid))
            if taken_over or do_not_contact:
                db.execute("UPDATE outbox SET status='cancelled',updated_at=? WHERE conversation_id=? AND status='draft'", (now(), cid))
        self.store.event("contact_control", cid, "已更新人工接管／停止联系设置")

    def position(self, payload):
        old = self.store.row("positions", payload["id"])
        return self.store.save_position(old["id"], old["title"], str(payload.get("jd", "")), old["source"], payload.get("enabled") is True)

    def read_position(self, ident):
        p = self.store.row("positions", ident)
        value = self.browser.read_position(p["title"])
        result = self.store.save_position(p["id"], p["title"], value["jd"], value["source"], bool(p["enabled"]))
        self.store.event("position_read", ident, "读取平台现有职位描述；没有修改或发布职位")
        return result

    def knowledge(self, payload):
        title, content, source = [str(payload.get(k, "")).strip() for k in ("title", "content", "source")]
        if not title or not content or not source or len(content) > 12000:
            raise ValueError("知识需填写标题、内容和来源；内容最多12000字")
        position_id = str(payload.get("position_id", ""))
        if position_id:
            self.store.row("positions", position_id)
        expires = str(payload.get("valid_until", ""))
        if expires:
            date.fromisoformat(expires)
        ident = str(payload.get("id") or uuid4().hex)
        with self.lock, self.store.db() as db:
            old = db.execute("SELECT version FROM knowledge WHERE id=?", (ident,)).fetchone()
            version = old[0]+1 if old else 1
            db.execute("INSERT OR REPLACE INTO knowledge VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (ident, title, content, position_id, str(payload.get("keywords", "")), source,
                        int(payload.get("public") is True), int(payload.get("approved") is True), expires, version, now()))
            # Invalidate only drafts citing this item, never resubmit a historical answer.
            for row in db.execute("SELECT id,refs FROM outbox WHERE status='draft'").fetchall():
                refs = json.loads(row["refs"])
                if ident in refs.get("knowledge", {}):
                    db.execute("UPDATE outbox SET status='expired',updated_at=? WHERE id=?", (now(), row["id"]))
        self.store.event("knowledge_saved", ident, "知识版本已更新")
        return self.store.row("knowledge", ident)

    def resume(self, cid):
        with self.lock:
            if self.use_local_session:
                current = self.store.row('conversations', cid)
                position = self.store.row('positions', current['position_id'])
                snapshot = json.loads(current['snapshot'])
                value = self.local_session.read_resume(cid, current['name'], position['title'], snapshot.get('account_uid'))
                result = self.store.save_document(cid, value['source'], value['text'], value['complete'], value['meta'])
                self.store.event('resume_read', cid, f"后台读取已收到的 PDF 全部 {value['meta']['page_count']} 页文字；完整性待核对，未操作标签页")
                self.auto_assess(cid)
                return result
            value = self.browser.read_resume(cid)
            result = self.store.save_document(cid, value["source"], value["text"], False,
                                              {"pages": [p["page"] for p in value["pages"]], "note": value["note"]})
            self.store.event("resume_read", cid, "读取PDF文字层；完整性待人工核对")
            self.auto_assess(cid)
            return result

    def save_resume(self, payload):
        text = str(payload.get("text", "")).strip()
        if not 50 <= len(text) <= 100000:
            raise ValueError("简历文字需要50–100000字")
        with self.lock:
            result = self.store.save_document(payload["conversation_id"], "human_verified_text", text,
                                             payload.get("complete") is True,
                                             {"note": "由本地用户核对／补全的资料"})
            self.auto_assess(payload["conversation_id"])
            return result

    def resume_status(self, cid, status, message, **details):
        value = {"status": status, "message": message, **details}
        old = self.store.setting("resume_processing:" + cid, {})
        if {k: v for k, v in old.items() if k != "updated_at"} != value:
            self.store.set_setting("resume_processing:" + cid, {**value, "updated_at": now()})

    def process_received_resume(self, conversation):
        """Run on every sync, even when chat text is unchanged. No external writes."""
        cid = conversation["id"]
        position = self.store.row("positions", conversation["position_id"])
        if conversation["taken_over"] or conversation["do_not_contact"] or not position["enabled"]:
            reason = "已停止联系" if conversation["do_not_contact"] else "人工接管中" if conversation["taken_over"] else "岗位已暂停"
            self.resume_status(cid, "paused", reason + "，简历自动处理已暂停")
            return
        message_id = json.loads(conversation["snapshot"]).get("received_resume_message_id")
        docs = self.store.rows("documents", "WHERE conversation_id=?", (cid,))
        if self.use_local_session and message_id and not any(json.loads(d["meta"]).get("message_id") == message_id for d in docs):
            previous = self.store.setting("resume_processing:" + cid, {})
            if previous.get("message_id") == message_id and previous.get("status") in {"read_failed", "reading"}:
                self.resume_status(cid, "read_failed", "附件自动读取未完成，请点击重新读取；已有资料保留", message_id=message_id)
                return
            self.resume_status(cid, "reading", "收到新简历，正在读取附件", message_id=message_id)
            try:
                self.resume(cid)
            except Exception:
                self.resume_status(cid, "read_failed", "附件自动读取未完成，请点击重新读取；已有资料保留", message_id=message_id)
                raise
        elif docs:
            self.auto_assess(cid)

    def assessment_input(self, cid):
        c = self.store.row("conversations", cid)
        position = self.store.row("positions", c["position_id"])
        docs = self.store.rows("documents", "WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (cid,))
        if not docs:
            raise ValueError("请先读取或导入该候选人的简历")
        config = self.config_provider()
        # Include candidate/document identity and model configuration, never persist credentials.
        digest = fingerprint([cid, docs[0]["id"], docs[0]["content_hash"], position["id"], position["version"],
                              config.get("ai", {}), agent.get_ai_api_key(config), "assessment-v2"])
        return position, docs[0], config, digest

    def auto_assess(self, cid):
        with self.lock:
            position, doc, config, digest = self.assessment_input(cid)
            details = {"input_hash": digest, "document_id": doc["id"]}
            if not agent.get_ai_api_key(config):
                self.resume_status(cid, "waiting_model", "简历已读取，等待配置模型；配置后下次监测自动评分", **details)
                return
            if not position["jd"].strip():
                self.resume_status(cid, "waiting_jd", "简历已读取，等待补充岗位 JD；补充后下次监测自动评分", **details)
                return
            old = self.store.setting("resume_processing:" + cid, {})
            if old.get("input_hash") == digest and old.get("status") in {"failed", "scoring"}:
                self.resume_status(cid, "failed", "自动评分未完成，简历已保留；请点击重试评分", **details)
                return
            try:
                return self.assess(cid)
            except Exception:
                self.resume_status(cid, "failed", "自动评分未完成，简历已保留；请点击重试评分", **details)
                self.store.event("assessment_failed", cid, "自动评分失败；未覆盖已有评估，不自动反复调用模型")

    def assess(self, cid):
        # Serialize automatic/manual attempts so concurrent requests cannot double-charge.
        with self.lock:
            p, doc, config, digest = self.assessment_input(cid)
            details = {"input_hash": digest, "document_id": doc["id"]}
            existing = self.store.rows("assessments", "WHERE input_hash=?", (digest,))
            if existing:
                self.resume_status(cid, "completed", "已按当前岗位 JD 自动评估，缺失证据保留待确认", **details)
                return json.loads(existing[0]["result"])
            self.resume_status(cid, "scoring", "正在按岗位 JD 评分", **details)
            try:
                result = agent.assess(p, doc, config)
            except Exception:
                self.resume_status(cid, "failed", "评分未完成，简历已保留；请检查模型配置后重试", **details)
                raise
            result.update({"document_id": doc["id"], "position_version": p["version"], "model": config.get("ai", {}).get("model")})
            ident = uuid4().hex
            with self.store.db() as db:
                db.execute("INSERT OR IGNORE INTO assessments VALUES (?,?,?,?,?)", (ident, cid, digest, encode(result), now()))
            self.resume_status(cid, "completed", "已按当前岗位 JD 自动评估，缺失证据保留待确认", **details)
            self.store.event("assessment_completed", cid, "完成有来源的岗位评估，未作录用决定")
            return result

    def company(self):
        return self.store.setting("company_brief", {"text": "", "version": 0, "updated_at": None})

    def save_company(self, text):
        if not isinstance(text, str) or len(text) > 30000:
            raise ValueError("公司说明最多 30000 字")
        with self.lock:
            old = self.company()
            if text.strip() == old["text"]:
                return old
            value = {"text": text.strip(), "version": old["version"] + 1, "updated_at": now()}
            with self.store.db() as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('company_brief',?)", (encode(value),))
                for row in db.execute("SELECT id,refs FROM outbox WHERE kind='reply' AND status='draft'").fetchall():
                    if json.loads(row["refs"]).get("source") == "ai_draft":
                        db.execute("UPDATE outbox SET status='expired',updated_at=? WHERE id=?", (now(), row["id"]))
            self.store.event("company_saved", "", "公司说明已更新，旧 AI 回复草稿已失效")
            return value

    def reply_context(self, cid):
        c = self.store.row("conversations", cid)
        p = self.store.row("positions", c["position_id"])
        snapshot = json.loads(c["snapshot"])
        docs = self.store.rows("documents", "WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (cid,))
        doc = docs[0] if docs else None
        assessment = None
        for item in self.store.rows("assessments", "WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC", (cid,)):
            result = json.loads(item["result"])
            if doc and result.get("document_id") == doc["id"] and result.get("position_version") == p["version"]:
                assessment = {"id": item["id"], "result": result}
                break
        return {
            "conversation": {"id": c["id"], "context_hash": c["context_hash"],
                "messages": snapshot["messages"], "coverage": snapshot.get("coverage", "仅已加载消息"),
                "history_complete": False, "synced_at": c["updated_at"]},
            "company": self.company(),
            "job": {"id": p["id"], "title": p["title"], "jd": p["jd"], "version": p["version"]},
            "resume": {"id": doc["id"], "text": doc["text"], "complete": bool(doc["complete"])} if doc else None,
            "assessment": assessment,
        }

    def prepare_reply(self, cid, text="", question=""):
        c = self.store.row("conversations", cid)
        p = self.store.row("positions", c["position_id"])
        if c["taken_over"] or c["do_not_contact"] or not p["enabled"]:
            raise ValueError("当前会话已人工接管、停止联系或岗位暂停")
        refs = {"position_id": p["id"], "position_version": p["version"], "knowledge": {}}
        if text:
            if len(text) > 500:
                raise ValueError("测试回复最多500字")
            refs["source"] = "human_draft"
        else:
            with self.lock:
                self.sync()
                context = self.reply_context(cid)
                messages = context["conversation"]["messages"]
                if not messages or messages[-1]["direction"] != "in":
                    raise ValueError("最新消息不是候选人发来的消息，请核对会话后处理")
                if question and question != messages[-1]["text"]:
                    raise ValueError("问题与实际会话不一致，不使用脱离上下文的问题生成回复")
                result = agent.reply(context, self.config_provider())
                # Human messages can change the live browser even while our queue is locked.
                self.sync()
                fresh = self.reply_context(cid)
                if self.reply_context_signature(fresh) != self.reply_context_signature(context):
                    raise ValueError("生成期间会话或资料发生变化，本次回复未保存，请基于新上下文重新生成")
                text = result["text"]
                refs.update({"source": "ai_draft", "needs_human": result["needs_human"],
                    "company_version": context["company"]["version"],
                    "document_id": context["resume"]["id"] if context["resume"] else None,
                    "assessment_id": context["assessment"]["id"] if context["assessment"] else None,
                    "message_count": len(messages), "context_signature": self.reply_context_signature(context),
                    "context_coverage": context["conversation"]["coverage"],
                    "basis": result["basis"], "missing": result.get("missing", [])})
        check_reply(text)
        return self.store.draft(cid, "reply", text.strip(), refs)

    @staticmethod
    def reply_context_signature(context):
        return fingerprint([context["conversation"]["context_hash"], context["company"]["version"],
                            context["job"]["version"], context["resume"], context["assessment"]])

    def prepare_action(self, cid, kind):
        if kind not in {"request_resume", "accept_resume"}:
            raise ValueError("动作不支持")
        c = self.store.row("conversations", cid)
        p = self.store.row("positions", c["position_id"])
        return self.store.draft(cid, kind, "", {"position_id": p["id"], "position_version": p["version"], "knowledge": {}})

    def invitation(self, payload):
        cid = payload["conversation_id"]
        self.store.row("conversations", cid)
        mode = payload.get("mode")
        if mode not in {"online", "offline"}:
            raise ValueError("请选择线上或线下")
        when = datetime.fromisoformat(str(payload.get("scheduled_at", "")))
        if when.tzinfo is None:
            raise ValueError("邀约时间必须包含时区")
        if when <= datetime.now(when.tzinfo):
            raise ValueError("面试时间必须晚于当前时间")
        place = str(payload.get("location", "")).strip()
        contact = str(payload.get("contact", "")).strip()
        if not place or not contact:
            raise ValueError("请填写真实会议方式／地址和联系人")
        content = encode({"mode": mode, "scheduled_at": when.isoformat(), "location": place,
                          "contact": contact, "note": str(payload.get("note", ""))[:140]})
        return self.store.draft(cid, "invitation", content, {"source": "local_draft", "send_allowed": False})

    def execute(self, ident):
        with self.lock:
            draft = self.store.row("outbox", ident)
            if draft["kind"] == "invitation":
                raise PermissionError("本次测试禁止发送面试邀约；仅支持保存本地草稿")
            if draft["kind"] == "reply":
                check_reply(draft["content"])
            c = self.store.row("conversations", draft["conversation_id"])
            p = self.store.row("positions", c["position_id"])
            if c["taken_over"] or c["do_not_contact"] or not p["enabled"]:
                raise ValueError("已人工接管、停止联系或岗位暂停")
            if draft["status"] != "draft":
                raise ValueError("动作不在待执行状态")
            refs = json.loads(draft["refs"])
            if refs.get("needs_human") or refs.get("position_version") != p["version"]:
                raise ValueError("草稿需要人工处理或岗位版本变化，请重新准备")
            for kid, version in refs.get("knowledge", {}).items():
                fact = self.store.row("knowledge", kid)
                if fact["version"] != version or not fact["approved"] or not fact["public"] or (fact["valid_until"] and fact["valid_until"] < date.today().isoformat()):
                    raise ValueError("知识已变更／过期，请重新生成回复")
            if refs.get("source") == "ai_draft" and refs.get("context_signature") != self.reply_context_signature(self.reply_context(c["id"])):
                raise ValueError("回复依据已变化，请使用最新公司说明、JD、简历和会话重新生成")
            before = self.browser.open_conversation(c["id"])
            updated = self.store.import_conversation(before)
            if updated["context_hash"] != draft["context_hash"] or not before["editor_empty"]:
                raise ValueError("会话有新消息或人工正在输入，已停止发送")
            self.store.claim(ident)
            try:
                self.browser.execute(draft["kind"], before, draft["content"])
                # Give the page a short bounded render interval, never click twice.
                time.sleep(.5)
                after = self.browser.read_current()
                if after["id"] != c["id"] or after["position_title"] != before["position_title"]:
                    raise BrowserError("发送后会话身份不一致")
                before_matches = sum(m["direction"] == "out" and m["kind"] == "text" and m["text"] == draft["content"] for m in before["messages"])
                after_matches = sum(m["direction"] == "out" and m["kind"] == "text" and m["text"] == draft["content"] for m in after["messages"])
                confirmed = draft["kind"] == "reply" and after_matches == before_matches+1 and after["editor_empty"]
                # Card submissions require a separate proven platform result, never just click success.
                status = "sent" if confirmed else "uncertain"
                message = "页面出现一条对应的本人消息；不代表对方已读" if confirmed else "已尝试操作，需核对平台结果；不会自动重试"
                self.store.finish(ident, status, message)
                self.store.import_conversation(after)
            except Exception:
                self.store.finish(ident, "uncertain", "操作结果待核实；请查看 Chrome，禁止盲目重试")
                self.store.event("outbound_uncertain", ident, "执行中断，未自动重试")
                raise BrowserError("操作结果待核实；请检查 Chrome，已阻止再次发送")
            self.store.event("outbound_" + status, ident, message)
            return self.store.row("outbox", ident)

    def monitor_once(self):
        cid = self.store.setting("pilot_conversation")
        # 1. 同步所有会话的消息（只读，不读简历/评分/草稿）
        changed = {}
        for conv in self.store.rows("conversations"):
            try:
                result = self.sync(conv["id"], process=False)
                changed[conv["id"]] = conv["context_hash"] != result["context_hash"]
            except BrowserError as exc:
                self.store.event("monitor_sync_failed", conv["id"], str(exc)[:200])
        # 2. 只对当前选中会话处理（读简历/评分/草稿）
        if not cid:
            return {"changed": False, "last_success": self.monitor["last_success"]}
        current = self.store.row("conversations", cid)
        self.process_received_resume(current)
        if changed.get(cid):
            self.store.event("conversation_changed", cid, "会话发生变化，旧草稿已过期")
            messages = json.loads(current["snapshot"])["messages"]
            if agent.get_ai_api_key(self.config_provider()) and messages and messages[-1]["direction"] == "in" and messages[-1]["kind"] == "text" and not current["taken_over"] and not current["do_not_contact"]:
                try:
                    self.prepare_reply(cid)
                except ValueError as exc:
                    self.store.event("reply_needs_attention", cid, str(exc))
        return {"changed": changed.get(cid, False), "last_success": self.monitor["last_success"]}

    def start_monitor(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return self.monitor.copy()
            self.sync()  # Baseline only: never answer historical messages on startup.
            self.stop_event.clear()
            self.monitor.update(running=True, error="")

            def run():
                try:
                    while not self.stop_event.wait(self.monitor["interval_seconds"]):
                        try:
                            with self.lock:
                                self.monitor_once()
                        except Exception as exc:
                            self.monitor["error"] = str(exc)[:250]
                            self.store.event("monitor_paused", "", "读取／生成失败，监测已暂停")
                            break
                finally:
                    self.monitor["running"] = False

            self.worker = Thread(target=run, daemon=True, name="recruiting-monitor")
            self.worker.start()
            return self.monitor.copy()

    def stop_monitor(self):
        self.stop_event.set()
        self.monitor["running"] = False
        return self.monitor.copy()
