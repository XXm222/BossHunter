"""One-account, one-conversation recruiting pilot with persistent outbound guards."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import json
import random
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
from bosshunter.throttle import PageThrottle


class RecruitingService:
    def __init__(self, path, config_provider, browser=None, local_session=None, verifier=None):
        self.store = Store(path)
        self.jobs = RecruitingJobs(self.store)
        self.config_provider = config_provider
        self.browser = browser or BossBrowser(runtime=RuntimeClient(config_provider()),
                                              target_id=config_provider().get("browser", {}).get("recruiting_target_id"))
        cfg = self._recruiting_cfg()
        self.local_session = local_session or LocalBossSession(
            user_data_dir=config_provider().get("browser", {}).get("recruiting_user_data_dir"),
            read_delay=(cfg.get("read_delay_min", 20.0), cfg.get("read_delay_max", 40.0)),
            daily_limit=cfg.get("read_daily_limit", 50),
            page_delay=cfg.get("read_page_delay", 3.0))
        self.verifier = verifier
        self.use_local_session = browser is None or local_session is not None
        self.lock = RLock()
        self.discovery_worker = None
        self.discovery_stop = Event()
        saved_monitor = self.store.setting('monitor_state', {})
        self.monitor = {"running": False, "last_success": saved_monitor.get('last_success'),
                        "error": saved_monitor.get('error') or "", "interval_seconds": 120,
                        "mode": "read_assess_and_draft", "note": "监测绑定会话，收到简历自动读取和评分；不会自动外发"}
        self.connection = {"connected": False, "message": "尚未核实本地 BOSS 登录状态", "transport": "local_cookie_http"}
        self.store.recover_outbox()

    def _recruiting_cfg(self):
        config = self.config_provider() or {}
        return config.get("recruiting") or {}

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
        jobs_state = self.jobs.state()
        discovery_running = bool(self.discovery_worker and self.discovery_worker.is_alive() and not self.discovery_stop.is_set())
        jobs_state["running"] = discovery_running
        worker_alive = self._worker_alive()
        monitor_enabled = bool(s.setting('monitor_enabled', False))
        monitor_state = self.monitor.copy()
        monitor_state["running"] = worker_alive and monitor_enabled
        return {"positions": s.rows("positions"), "conversations": conversations,
                "documents": docs, "resume_processing": {c["id"]: s.setting("resume_processing:" + c["id"], {}) for c in conversations}, "company": self.company(), "recruiting_jobs": jobs_state, "assessments": assessments,
                "outbox": drafts, "events": s.rows("events", "ORDER BY id DESC LIMIT 30"),
                "connection": self.connection, "monitor": monitor_state,
                "worker": {"alive": worker_alive, "monitor_enabled": monitor_enabled},
                "discovery": {"running": discovery_running},
                "auto_send": {"daily_limit": s.setting('auto_reply_daily_limit', self._recruiting_cfg().get("auto_reply_daily_limit", 10)),
                              "sent_today": self._reply_sent_today()},
                "model_ready": bool(agent.get_ai_api_key(self.config_provider())),
                "pilot": {"max_conversations": 20, "invitation_sending": False,
                          "conversation_id": s.setting("pilot_conversation"),
                          "message_identity": "platform_ids" if conversations and all(c['snapshot'].get('stable_message_ids') for c in conversations) else "snapshot_only", "coverage": "已同步多个候选人会话（非全量）"}}

    def sync_jobs(self):
        previous = self.store.setting("jobs_sync", {})
        try:
            result = self.jobs.import_snapshot(self.local_session.read_jobs())
            self.connection = {"connected": True, "transport": "local_cookie_http", "checked_at": now(),
                               "message": "已通过本地登录状态核实 BOSS 岗位读取",
                               "read_jobs": True, "read_bound_conversation": False}
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
        """读取聊天页左侧联系人列表（含岗位名），供绑定向导从列表选人。"""
        contacts = self.browser.read_contact_list()
        return [{"ident": c.get("ident"), "name": c.get("name"),
                 "position_title": c.get("position_title", ""), "last_ts": None}
                for c in contacts]

    def sync_all_contacts(self):
        """批量导入联系人列表里的所有会话（姓名+岗位，不读消息、不调接口）。

        导入为最小快照，让所有候选人都进入「候选人沟通」列表；消息历史在选中该
        会话后由 sync()/monitor 按需读取并核对归属。已导入的会话跳过。
        """
        contacts = self.browser.read_contact_list()
        existing = {c["id"] for c in self.store.rows("conversations")}
        imported = 0
        for c in contacts:
            ident = c.get("ident")
            name = c.get("name")
            if not ident or not name:
                continue
            if ident in existing:
                continue
            snapshot = {
                "id": ident, "name": name,
                "position_title": c.get("position_title", "").strip() or "待关联岗位",
                "messages": [], "editor_empty": True, "stable_message_ids": False,
                "coverage": "尚未同步消息；选中该会话后自动读取",
            }
            self.store.import_conversation(snapshot)
            imported += 1
        self.store.event("contacts_synced", "", f"全账号导入 {imported} 个会话（共 {len(contacts)} 个联系人）")
        return {"imported": imported, "total": len(contacts)}

    def read_greeting_quota(self):
        """读取今日剩余打招呼额度并缓存，返回结果。"""
        quota = self.local_session.read_greeting_quota()
        self.store.set_setting('greeting_quota', {**quota, 'updated_at': now()})
        remaining = quota['remaining']
        self.store.event('greeting_quota_read', '', f"今日打招呼额度：剩余 {remaining if remaining is not None else '不限'}")
        return quota

    def _is_duplicate(self, uid):
        # 推荐卡的 data-geekid 与联系人列表的 friendId 不是同一 ID 空间，去重只
        # 按我们自己记录的 greeting_attempts.candidate_id（发招呼时会把 geekid 存进去）。
        with self.store.db() as db:
            return bool(db.execute("SELECT 1 FROM greeting_attempts WHERE candidate_id=?", (uid,)).fetchone())

    def greet_discovered(self, uid):
        """对已通过跨刷新验证的候选人点「打招呼」（BOSS 自动发默认招呼语）。"""
        if self._is_duplicate(uid):
            raise ValueError("该候选人已打过招呼，跳过")
        if self.verifier is None:
            from .recommend import RecommendVerifier
            self.verifier = RecommendVerifier(self.config_provider)
        result = self.verifier.greet(uid)
        job_id = result.get("job_id")
        if not job_id:
            raise ValueError("未读取到推荐页当前岗位")
        stored_job_id = "boss-" + job_id
        status = "sent" if result.get("sent") else "uncertain"
        self.jobs.record_greeting(stored_job_id, uid, status)
        return {**result, "status": status, "job_id": stored_job_id}

    def _quota_exhausted_now(self):
        daily = self.jobs.state()['daily']
        config = self.jobs.config()
        if config['mode'] == 'custom':
            return daily['custom_remaining'] is not None and daily['custom_remaining'] <= 0
        return daily['platform_remaining'] is not None and daily['platform_remaining'] <= 0

    def run_discovery(self, per_job_min=None, per_job_max=None, throttle_delay=None):
        """循环勾选的开放岗位，每岗位招呼若干个候选人（同步执行，节流防封号）。"""
        cfg = self._recruiting_cfg()
        per_job_min = per_job_min if per_job_min is not None else cfg.get("greet_per_job_min", 1)
        per_job_max = per_job_max if per_job_max is not None else cfg.get("greet_per_job_max", 2)
        throttle_delay = throttle_delay if throttle_delay is not None else (cfg.get("greet_delay_min", 30.0), cfg.get("greet_delay_max", 60.0))
        state = self.jobs.state()
        if state['blockers']:
            raise ValueError('；'.join(state['blockers']))
        selected = [j for j in state['jobs'] if j['selected'] and j['status'] == '开放中' and j['platform_id']]
        if not selected:
            raise ValueError('没有可处理的开放岗位')
        config = self.jobs.config()
        if config['mode'] == 'platform':
            try:
                self.read_greeting_quota()
            except BrowserError as exc:
                raise ValueError(f"无法读取平台剩余额度，停止执行：{exc}")
        if self.verifier is None:
            from .recommend import RecommendVerifier
            self.verifier = RecommendVerifier(self.config_provider)
        throttle = PageThrottle(delay_min=throttle_delay[0], delay_max=throttle_delay[1])
        greeted = 0
        for job in selected:
            if self.discovery_stop.is_set() or self._quota_exhausted_now():
                break
            try:
                self.verifier.select_job(job['platform_id'])
                candidates = self.verifier.read_candidates()
            except BrowserError as exc:
                self.store.event('discovery_error', job['platform_id'], str(exc))
                continue
            per_job = random.randint(per_job_min, per_job_max)
            done = 0
            for cand in candidates:
                if self.discovery_stop.is_set() or done >= per_job or self._quota_exhausted_now():
                    break
                if not cand.get('greetable') or self._is_duplicate(cand['uid']):
                    continue
                result = self.greet_discovered(cand['uid'])
                if result.get('sent'):
                    greeted += 1
                    done += 1
                throttle.wait()
            throttle.wait()
        stopped = self.discovery_stop.is_set()
        return {'greeted': greeted, 'stopped': stopped,
                'reason': '主动打招呼已手动停止' if stopped else f'本轮主动招呼 {greeted} 次'}

    def start_discovery(self, per_job_min=None, per_job_max=None):
        """后台启动「按额度持续主动打招呼」循环；立即返回，循环在线程内运行。"""
        if self.discovery_worker and self.discovery_worker.is_alive():
            return {'running': True, 'message': '主动打招呼循环已在运行'}
        state = self.jobs.state()
        if state['blockers']:
            raise ValueError('；'.join(state['blockers']))
        self.discovery_stop.clear()

        def run():
            try:
                result = self.run_discovery(per_job_min, per_job_max)
                self.store.event('discovery_done', '', result['reason'])
            except Exception as exc:
                self.store.event('discovery_stopped', '', str(exc)[:300])

        self.discovery_worker = Thread(target=run, daemon=True, name='recruiting-discovery')
        self.discovery_worker.start()
        return {'running': True, 'message': '主动打招呼循环已启动'}

    def stop_discovery(self):
        """请求停止正在运行的主动打招呼循环；在下一个节流点生效（≤30 秒）。"""
        self.discovery_stop.set()
        return {'running': bool(self.discovery_worker and self.discovery_worker.is_alive()),
                'message': '已请求停止主动打招呼循环'}

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
                                   "read_bound_conversation": True}
                self.store.event("conversation_synced", ident, f"后台同步绑定会话 {len(snapshot['messages'])} 条平台消息；未操作标签页或发送消息")
            else:
                result = self.store.import_conversation(self.browser.open_conversation(ident))
            if process:
                self.process_received_resume(result)
            self.monitor["last_success"] = now()
            self.monitor["error"] = ""
            return result

    def control(self, cid, taken_over, do_not_contact, auto_send=None):
        if type(taken_over) is not bool or type(do_not_contact) is not bool:
            raise ValueError("开关必须是布尔值")
        if auto_send is not None and type(auto_send) is not bool:
            raise ValueError("自动外发开关必须是布尔值")
        self.store.row("conversations", cid)
        with self.lock, self.store.db() as db:
            if auto_send is None:
                db.execute("UPDATE conversations SET taken_over=?,do_not_contact=? WHERE id=?", (taken_over, do_not_contact, cid))
            else:
                db.execute("UPDATE conversations SET taken_over=?,do_not_contact=?,auto_send=? WHERE id=?", (taken_over, do_not_contact, auto_send, cid))
            if taken_over or do_not_contact:
                db.execute("UPDATE outbox SET status='cancelled',updated_at=? WHERE conversation_id=? AND status='draft'", (now(), cid))
        self.store.event("contact_control", cid, "已更新人工接管／停止联系设置")

    def set_auto_send(self, cid, enabled):
        """切换单个会话的自动外发开关（不改变人工接管/停止联系）。"""
        if type(enabled) is not bool:
            raise ValueError("自动外发开关必须是布尔值")
        self.store.row("conversations", cid)
        with self.lock, self.store.db() as db:
            db.execute("UPDATE conversations SET auto_send=? WHERE id=?", (int(enabled), cid))
        self.store.event("auto_send_toggled", cid, f"会话自动外发已{'开启' if enabled else '关闭'}")
        return self.store.row("conversations", cid)

    def set_monitor_enabled(self, enabled):
        """开启/停止回复监测：写入控制标志，由独立 worker 进程轮询执行。"""
        if type(enabled) is not bool:
            raise ValueError("开关必须是布尔值")
        self.store.set_setting('monitor_enabled', enabled)
        self.store.event('monitor_control', '', f"回复监测已{'开启' if enabled else '停止'}")
        return {'monitor_enabled': enabled}

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
            if draft["kind"] == "reply" and self._reply_quota_exhausted():
                raise ValueError("今日自动回复已达上限，请人工处理")
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

    def _worker_alive(self):
        """独立 worker 进程是否存活（通过心跳时间戳判断）。"""
        hb = self.store.setting('worker_heartbeat')
        if not hb:
            return False
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds()
            return 0 <= age < 30
        except (ValueError, TypeError):
            return False

    def _write_heartbeat(self):
        self.store.set_setting('worker_heartbeat', now())

    def worker_loop(self, stop_event):
        """独立进程主循环：写心跳、轮询 monitor_enabled 标志、定期跑 monitor_once。"""
        self.store.event('worker_started', '', '独立监测进程已启动')
        last_run = 0.0
        try:
            while not stop_event.is_set():
                self._write_heartbeat()
                if self.store.setting('monitor_enabled', False):
                    if time.time() - last_run >= self.monitor['interval_seconds']:
                        try:
                            self.monitor_once()
                            last_run = time.time()
                        except Exception as exc:
                            self.store.event('monitor_paused', '', str(exc)[:250])
                stop_event.wait(10)  # 每 10 秒轮询一次标志 + 心跳
        finally:
            self.store.event('worker_stopped', '', '独立监测进程已停止')

    def _persist_monitor_state(self):
        """把监测状态写回 DB，供服务重启后恢复显示。"""
        self.store.set_setting('monitor_state', {
            'last_success': self.monitor['last_success'],
            'error': self.monitor['error'],
        })

    def _auto_send_enabled(self, cid):
        """该会话的自动外发开关开启时才允许自动外发。"""
        c = self.store.row("conversations", cid)
        return bool(c.get("auto_send"))

    def _reply_sent_today(self):
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        count = 0
        with self.store.db() as db:
            for r in db.execute("SELECT updated_at FROM outbox WHERE kind='reply' AND status IN ('sending','sent','uncertain')").fetchall():
                if r['updated_at']:
                    d = datetime.fromisoformat(r['updated_at']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                    if d == day:
                        count += 1
        return count

    def _reply_quota_exhausted(self):
        limit = self.store.setting('auto_reply_daily_limit', self._recruiting_cfg().get("auto_reply_daily_limit", 10))
        if not limit:
            return False
        return self._reply_sent_today() >= limit

    def _auto_send_if_allowed(self, draft):
        """按条件自动发送一条回复草稿；不满足则留草稿给人工。"""
        refs = json.loads(draft["refs"])
        if refs.get("needs_human"):
            return False
        cid = draft["conversation_id"]
        if not self._auto_send_enabled(cid):
            return False
        if self._reply_quota_exhausted():
            self.store.event("auto_send_skipped", cid, "今日自动回复已达上限")
            return False
        try:
            self.execute(draft["id"])
            self.store.event("auto_sent", cid, "已自动发送回复")
            return True
        except (ValueError, BrowserError) as exc:
            self.store.event("auto_send_skipped", cid, str(exc))
            return False

    def monitor_once(self):
        cid = self.store.setting("pilot_conversation")
        if not cid:
            self._persist_monitor_state()
            return {"changed": False, "last_success": self.monitor["last_success"]}
        try:
            before = self.store.row("conversations", cid)
            after = self.sync(cid, process=True)
            if before["context_hash"] != after["context_hash"]:
                self.store.event("conversation_changed", cid, "会话发生变化，旧草稿已过期")
                messages = json.loads(after["snapshot"])["messages"]
                if agent.get_ai_api_key(self.config_provider()) and messages and messages[-1]["direction"] == "in" and messages[-1]["kind"] == "text" and not after["taken_over"] and not after["do_not_contact"]:
                    try:
                        draft = self.prepare_reply(cid)
                        self._auto_send_if_allowed(draft)
                    except ValueError as exc:
                        self.store.event("reply_needs_attention", cid, str(exc))
            return {"changed": before["context_hash"] != after["context_hash"], "last_success": self.monitor["last_success"]}
        finally:
            self._persist_monitor_state()
