"""One-account, one-conversation recruiting pilot with persistent outbound guards."""
from datetime import date, datetime, timezone
from copy import deepcopy
from zoneinfo import ZoneInfo
import json
import random
import time
from threading import Event, RLock, Thread, local
from uuid import uuid4

from . import agent
from .browser import AccountPauseError, BossBrowser, BrowserError, TaskCancelled, ConversationNotSelected
from .store import Store, TaskBusy, RequestThrottled, RequestPaused, encode, fingerprint, merge_messages, now, candidate_question
from .policy import check_reply
from .jobs import RecruitingJobs
from .local_session import LocalBossSession
from bosshunter.browser.client import RuntimeClient
from bosshunter.throttle import PageThrottle
from bosshunter.ai.credentials import get_ai_base_url, get_ai_service

# 平台额度缓存有效期（秒）：超过视为过期，主动打招呼循环里重新读取，以覆盖
# 「人工直接在 BOSS 打招呼」导致平台剩余变化、本地缓存失真的情况。
QUOTA_TTL_SECONDS = 300


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
            read_delay=(cfg.get("read_delay_min", 120.0), cfg.get("read_delay_max", 180.0)),
            daily_limit=cfg.get("read_daily_limit", 100),
            page_delay=cfg.get("read_page_delay", 15.0),
            page_delay_max=cfg.get("read_page_delay_max", 30.0),
            request_counter=self._count_request, account_pause_handler=self._pause_platform_requests)
        self.verifier = verifier
        self.use_local_session = browser is None or local_session is not None
        self.lock = RLock()
        self.discovery_worker = None
        self.discovery_stop = Event()
        self._stop_event = None
        saved_monitor = self.store.setting('monitor_state', {})
        self.monitor = {"running": False, "last_success": saved_monitor.get('last_success'),
                        "error": saved_monitor.get('error') or "", "interval_seconds": max(60, cfg.get('monitor_interval_seconds', 600)),
                        "mode": "round_robin_read_assess_and_reply", "note": "轮询已授权绑定会话；自动外发遵循每会话开关，非全账号实时监听"}
        self.connection = {"connected": False, "message": "尚未核实本地 BOSS 登录状态", "transport": "local_cookie_http"}
        self.store.recover_outbox()
        self._reply_progress_local = local()

    def _recruiting_cfg(self):
        config = self.config_provider() or {}
        return config.get("recruiting") or {}

    def _reply_stage(self, stage, message):
        progress = getattr(self, '_reply_progress_local', None)
        if not progress or not getattr(progress, 'cid', None):
            return
        progress.stage, progress.message = stage, message
        progress.wait_until = None
        self.store.set_setting('reply_progress:' + progress.cid,
                               {'stage': stage, 'message': message, 'active': True, 'updated_at': now()})

    def _count_request(self, kind, *, pace=True):
        """LocalBossSession 每次后台请求前的计数回调：按账号存 DB，超限抛 BrowserError。"""
        cfg = self._recruiting_cfg()
        interval = random.uniform(cfg.get('read_delay_min', 120.0), cfg.get('read_delay_max', 180.0))
        if kind == 'greeting':
            interval = random.uniform(cfg.get('greet_delay_min', 60.0), cfg.get('greet_delay_max', 120.0))
        if kind == 'recommend_load':
            pace = False  # Browser loading uses the shared short loading interval.
        page_interval = random.uniform(cfg.get('read_page_delay', 15.0), cfg.get('read_page_delay_max', 30.0))
        daily_limit = self.store.request_daily_limit(cfg.get('read_daily_limit', 100))
        while True:
            self._check_stopped()
            if kind in {'greeting', 'recommend_load'} and self.discovery_stop.is_set():
                raise TaskCancelled('主动招呼已停止')
            try:
                self.store.count_request(kind, daily_limit, min_interval=interval,
                                         pace=pace, page_interval=page_interval)
                progress = getattr(self, '_reply_progress_local', None)
                if progress and getattr(progress, 'cid', None) and progress.wait_until is not None:
                    self._reply_stage(progress.stage, progress.message)
                return
            except RequestThrottled as exc:
                progress = getattr(self, '_reply_progress_local', None)
                if progress and getattr(progress, 'cid', None):
                    deadline = time.time() + exc.seconds
                    if progress.wait_until is None or abs(progress.wait_until - deadline) > .5:
                        progress.wait_until = deadline
                        self.store.set_setting('reply_progress:' + progress.cid,
                                               {'stage': 'waiting_throttle', 'message': '等待节流；下一步：' + progress.message,
                                                'wait_until': deadline, 'active': True, 'updated_at': now()})
                # No DB transaction is held during the wait. Recheck and compete
                # for the slot after waking; Web and worker share this deadline.
                self.local_session._wait_cancelled(min(exc.seconds, 1.0))
            except RequestPaused as exc:
                raise AccountPauseError(str(exc)) from None
            except ValueError as exc:
                if self._stop_event is not None:
                    self.set_monitor_enabled(False)
                raise BrowserError(str(exc)) from None

    def _pause_platform_requests(self, seconds):
        self.store.pause_requests(seconds)
        self.discovery_stop.set()
        self.store.event('platform_cooldown', '', 'BOSS 拒绝或限流；后台任务暂停，共享冷却至少 30 分钟，核实账号后手动恢复')

    def state(self):
        s = self.store
        saved = s.state_snapshot()
        settings = saved['settings']
        positions = saved['positions']
        by_position = {p['id']: p for p in positions}
        conversations = saved['conversations']
        for c in conversations:
            c["snapshot"] = json.loads(c["snapshot"])
        docs = saved['documents']
        by_document = {d['id']: d for d in docs}
        current_documents = {}
        for doc in docs:
            current_documents.setdefault(doc['conversation_id'], doc)
        for c in conversations:
            chosen = by_document.get(settings.get('current_document:' + c['id']))
            if chosen and chosen['conversation_id'] == c['id']:
                current_documents[c['id']] = chosen
        current_document_ids = {d['id'] for d in current_documents.values() if d}
        docs.sort(key=lambda d: d['id'] not in current_document_ids)
        for d in docs:
            d["meta"] = json.loads(d["meta"])
        assessments = saved['assessments']
        for a in assessments:
            a["result"] = json.loads(a["result"])
        config = deepcopy(self.config_provider())
        model_basis = self._assessment_model_basis(config)
        current_assessment_ids = {c['id']: None for c in conversations}
        digests = {}
        by_conversation = {c['id']: c for c in conversations}
        for c in conversations:
            doc = current_documents.get(c['id'])
            if doc:
                digests[c['id']] = self._assessment_digest(c['id'], by_position[c['position_id']], doc, model_basis)
        for assessment in assessments:
            cid = assessment['conversation_id']
            if cid not in digests or current_assessment_ids[cid] is not None:
                continue
            p = by_position[by_conversation[cid]['position_id']]
            doc = current_documents[cid]
            result = assessment['result']
            if (assessment['input_hash'] == digests[cid] and result.get('position_id') == p['id']
                    and result.get('document_id') == doc['id'] and result.get('position_version') == p['version']):
                current_assessment_ids[cid] = assessment['id']
        drafts = saved['outbox']
        for d in drafts:
            d["refs"] = json.loads(d["refs"])
        jobs_state = self.jobs.state()
        discovery_running = bool(self.discovery_worker and self.discovery_worker.is_alive() and not self.discovery_stop.is_set())
        jobs_state["running"] = discovery_running
        worker_alive = self._worker_alive()
        monitor_enabled = bool(s.setting('monitor_enabled', False))
        # 读 DB 里的 monitor_state：worker 独立进程写入的最新成功时间/错误，本进程内存不会自动刷新
        saved_monitor = s.setting('monitor_state', {})
        selected = {j['id'] for j in jobs_state['jobs'] if j['selected'] and j['platform_id'] and j['status'] == '开放中'}
        allowed_count = sum(c['binding_confirmed'] and not c['do_not_contact']
                            and by_position[c['position_id']]['enabled'] and c['position_id'] in selected for c in conversations)
        monitor_state = {
            "running": worker_alive and monitor_enabled,
            "paused": worker_alive and not monitor_enabled,
            "disconnected": not worker_alive,
            "last_success": saved_monitor.get('last_success'),
            "error": saved_monitor.get('error') or "",
            "interval_seconds": self.monitor['interval_seconds'],
            "mode": self.monitor['mode'],
            "note": self.monitor['note'],
            'allowed_count': allowed_count,
            'estimated_cycle_seconds': allowed_count * self.monitor['interval_seconds'],
            'processing_conversation_id': saved_monitor.get('processing_conversation_id') if worker_alive else None,
        }
        budget = s.request_budget()
        read_daily_limit = s.request_daily_limit(self._recruiting_cfg().get("read_daily_limit", 100))
        request_budget = {"date": budget.get("date"), "count": budget.get("count", 0),
                          "daily_limit": read_daily_limit,
                          "remaining": max(0, read_daily_limit - budget.get("count", 0)),
                          "by_kind": budget.get("by_kind", {}),
                          'paused_until': s.setting('request_paused_until', 0)}
        return {"positions": positions, "conversations": conversations,
                "documents": docs, "resume_processing": {c["id"]: settings.get("resume_processing:" + c["id"], {}) for c in conversations},
                'reply_progress': {c['id']: settings.get('reply_progress:' + c['id'], {}) for c in conversations},
                'reply_work': {c['id']: settings.get('reply_work:' + c['id'], {}) for c in conversations},
                "company": self.company(), "recruiting_jobs": jobs_state, "assessments": assessments,
                "current_assessment_ids": current_assessment_ids,
                "outbox": drafts, "events": saved['events'],
                "connection": self.connection, "monitor": monitor_state,
                "send_channel": self._send_channel_status(),
                "worker": {"alive": worker_alive, "monitor_enabled": monitor_enabled},
                "discovery": {"running": discovery_running},
                "auto_send": {"daily_limit": s.setting('auto_reply_daily_limit', self._recruiting_cfg().get("auto_reply_daily_limit", 5)),
                              "sent_today": self._auto_reply_sent_today()},
                "request_budget": request_budget,
                'contact_sync': s.setting('contact_sync', {}),
                "model_ready": bool(model_basis[1]),
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
            result = self.store.import_conversation(self.browser.read_current(), confirmed=True)
            self.store.event("conversation_synced", result["id"], "读取当前一个会话；未发送消息")
            return result

    def list_contacts(self):
        """返回已同步进数据库的候选会话，供绑定向导从列表选人。

        联系人已由 sync_all_contacts 登记进 conversations，这里直接复用数据库里的
        结果，而不是再从浏览器 DOM 滚动读取（DOM 是虚拟滚动、只渲染当前可视区，
        读不全且慢）。
        """
        contacts = []
        for c in self.store.rows("conversations"):
            snapshot = json.loads(c["snapshot"])
            contacts.append({
                "ident": c["id"],
                "name": c["name"],
                "position_title": snapshot.get("position_title", "") or "待关联岗位",
                "last_ts": None,
            })
        return contacts

    def sync_all_contacts(self):
        """批量导入联系人列表里的所有会话（姓名+岗位，不读消息、不调接口）。

        导入为最小快照，让所有候选人都进入「候选人沟通」列表；消息历史在选中该
        会话后由 sync()/monitor 按需读取并核对归属。已导入的会话跳过。
        按联系人 uid 批量查 encryptJobId，把会话关联到已发布岗位（问题7）。
        """
        contacts = (self.browser.read_contact_list(load_all=True, before_load=lambda pace: self._count_request('contacts_load', pace=pace))
                    if isinstance(self.browser, BossBrowser) else self.browser.read_contact_list(load_all=True))
        existing = {c["id"] for c in self.store.rows("conversations")}
        new_contacts = [c for c in contacts if c.get("ident") and c.get("name") and c["ident"] not in existing]
        job_map = self._friend_job_map([c["ident"].split("-", 1)[0] for c in new_contacts])
        imported = 0
        for c in new_contacts:
            ident = c["ident"]
            name = c["name"]
            snapshot = {
                "id": ident, "name": name,
                "position_title": c.get("position_title", "").strip() or "待关联岗位",
                "messages": [], "editor_empty": True, "stable_message_ids": False,
                "coverage": "尚未同步消息；选中该会话后自动读取",
            }
            job_id = job_map.get(ident.split("-", 1)[0])
            if job_id:
                snapshot["position_platform_id"] = job_id
            self.store.import_conversation(snapshot)
            imported += 1
        coverage = getattr(self.browser, 'contact_coverage', {})
        if not isinstance(coverage, dict):
            coverage = {}
        coverage = {'complete': False, 'scope': 'loaded_browser_contacts', **coverage}
        self.store.set_setting('contact_sync', {**coverage, 'imported': imported, 'total': len(contacts), 'updated_at': now()})
        self.store.event("contacts_synced", "", f"导入页面可加载的 {imported} 个会话（共 {len(contacts)} 个联系人，非已证明的全账号范围）")
        return {"imported": imported, "total": len(contacts), 'coverage': coverage}

    def read_greeting_quota(self):
        """读取今日剩余打招呼额度并缓存，返回结果。"""
        with self.store.task('greeting-quota'):
            with self.store.db() as db:
                if db.execute("SELECT 1 FROM greeting_attempts WHERE status IN ('sending','uncertain')").fetchone():
                    raise ValueError('存在发送结果待核实的招呼，请先核实再读取额度')
            daily = self.jobs.state()['daily']
            quota = self.local_session.read_greeting_quota()
            from .jobs import day_key
            if daily['date'] != day_key():
                raise ValueError('读取期间日期变化，请重新读取今日额度')
            self.store.set_setting('greeting_quota', {**quota, 'date': daily['date'],
                                                      'updated_at': now(), 'local_used_at_read': daily['attempted']})
        if quota.get('unlimited'):
            label = '不限'
        else:
            remaining = quota.get('remaining')
            label = remaining if remaining is not None else '未知'
        self.store.event('greeting_quota_read', '', f"今日打招呼额度：剩余 {label}")
        return quota

    def _is_duplicate(self, uid):
        # 推荐卡的 data-geekid 与联系人列表的 friendId 不是同一 ID 空间，去重只
        # 按我们自己记录的 greeting_attempts.candidate_id（发招呼时会把 geekid 存进去）。
        with self.store.db() as db:
            return bool(db.execute("SELECT 1 FROM greeting_attempts WHERE candidate_id=?", (uid,)).fetchone())

    def _check_greeting_allowed(self, job_id=None):
        """校验主动打招呼前置条件：岗位已勾选开放、额度可用、无待核实招呼。

        job_id 为预期的平台岗位 ID（encryptJobId，不带 boss- 前缀）；传 None 时
        只做全局检查。单次招呼（discover/greet）与后台循环（run_discovery）共用，
        避免绕过勾选/额度/待核实检查。
        """
        state = self.jobs.state()
        if state['blockers']:
            raise ValueError('；'.join(state['blockers']))
        if job_id:
            stored = "boss-" + str(job_id)
            job = next((j for j in state['jobs'] if j['id'] == stored), None)
            if not job:
                raise ValueError('岗位不在已同步列表，请先同步岗位')
            if not job['selected']:
                raise ValueError('该岗位未人工勾选，不能主动招呼')
            if job['status'] != '开放中':
                raise ValueError('该岗位当前不是开放中，不能主动招呼')

    def greet_discovered(self, uid, name="", job_id=None):
        with self.store.task('outbound-browser'):
            return self._greet_discovered(uid, name, job_id)

    def _greet_discovered(self, uid, name="", job_id=None):
        """对已通过跨刷新验证的候选人点「打招呼」（BOSS 自动发默认招呼语）。

        job_id 为预期的平台岗位 ID（必填）。点击前先记录 sending 占用，点击后能确认
        成功改 sent，报错或回执不明确保留 uncertain，核实前不会重试或继续联系下一人。
        """
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("缺少岗位 ID，无法核对岗位勾选与额度")
        if self._is_duplicate(uid):
            raise ValueError("该候选人已打过招呼，跳过")
        self._refresh_quota_if_needed()
        self._check_greeting_allowed(job_id)
        if self.verifier is None:
            from .recommend import RecommendVerifier
            self.verifier = RecommendVerifier(self.config_provider, cookie_loader=self.local_session.cookie_loader)
        from .recommend import RecommendVerifier
        if isinstance(self.verifier, RecommendVerifier):
            self._count_request('greeting')
            self._refresh_quota_if_needed()
            self._check_greeting_allowed(job_id)
        self.jobs.reserve_greeting("boss-" + job_id, uid, name)
        try:
            def preflight():
                if self.discovery_stop.is_set():
                    raise TaskCancelled('主动招呼已停止')
                self._check_platform_cooldown()
                try:
                    self.jobs.check_position('boss-' + job_id)
                except ValueError as exc:
                    raise TaskCancelled(str(exc)) from exc
            preflight()
            from .recommend import RecommendVerifier
            if isinstance(self.verifier, RecommendVerifier):
                result = self.verifier.greet(uid, job_id, preflight=preflight)
            else:
                result = self.verifier.greet(uid, job_id)
        except TaskCancelled:
            self.jobs.finish_greeting(uid, 'cancelled')
            raise
        except Exception:
            # 点击后异常：结果不确定，保留 uncertain 并暂停，不自动重试
            self.jobs.finish_greeting(uid, 'uncertain')
            raise
        actual_job_id = result.get("job_id")
        if not actual_job_id:
            self.jobs.finish_greeting(uid, 'uncertain')
            raise ValueError("未读取到推荐页当前岗位")
        if str(actual_job_id) != str(job_id):
            self.jobs.finish_greeting(uid, 'uncertain')
            raise ValueError("推荐页当前岗位与任务岗位不一致，已停止招呼")
        status = "sent" if result.get("sent") else "uncertain"
        self.jobs.finish_greeting(uid, status)
        return {**result, "status": status, "job_id": "boss-" + str(actual_job_id)}

    def _quota_stale(self):
        """平台额度缓存是否过期（跨日或超过 TTL），需重新读取。"""
        from .jobs import day_key
        quota = self.store.setting('greeting_quota', {})
        return not self.jobs.quota_fresh(quota, day_key())

    def _refresh_quota_if_needed(self):
        quota = self.store.setting('greeting_quota', {})
        if self._quota_stale():
            self.read_greeting_quota()

    def _quota_exhausted_now(self):
        daily = self.jobs.state()['daily']
        config = self.jobs.config()
        if config['mode'] == 'custom' and daily['custom_remaining'] is not None and daily['custom_remaining'] <= 0:
            return True
        # 自定义额度也受已知平台剩余额度约束，不能只看本地上限
        return daily['platform_remaining'] is not None and daily['platform_remaining'] <= 0

    def run_discovery(self, per_job_min=None, per_job_max=None, throttle_delay=None):
        """循环勾选的开放岗位，每岗位招呼若干个候选人（同步执行，节流防封号）。"""
        cfg = self._recruiting_cfg()
        per_job_min = per_job_min if per_job_min is not None else cfg.get("greet_per_job_min", 1)
        per_job_max = per_job_max if per_job_max is not None else cfg.get("greet_per_job_max", 1)
        throttle_delay = throttle_delay if throttle_delay is not None else (cfg.get("greet_delay_min", 60.0), cfg.get("greet_delay_max", 120.0))
        if not isinstance(per_job_min, int) or not isinstance(per_job_max, int) or not 1 <= per_job_min <= per_job_max:
            raise ValueError("每岗位招呼数需为整数且满足 1 ≤ 下限 ≤ 上限")
        config = self.jobs.config()
        if self._quota_stale():
            # 先读平台额度，再检查 blockers，否则「未读取额度」会先于读取把任务拦住
            try:
                self._refresh_quota_if_needed()
            except BrowserError as exc:
                raise ValueError(f"无法读取平台剩余额度，停止执行：{exc}")
        state = self.jobs.state()
        if state['blockers']:
            raise ValueError('；'.join(state['blockers']))
        selected = [j for j in state['jobs'] if j['selected'] and j['status'] == '开放中' and j['platform_id']]
        if not selected:
            raise ValueError('没有可处理的开放岗位')
        if self.verifier is None:
            from .recommend import RecommendVerifier
            self.verifier = RecommendVerifier(self.config_provider, cookie_loader=self.local_session.cookie_loader)
        throttle = PageThrottle(delay_min=throttle_delay[0], delay_max=throttle_delay[1])
        greeted = 0
        # 每轮先刷新推荐页拿新候选人；刷新会把岗位重置为默认第一个，下面逐岗重新 select_job
        try:
            from .recommend import RecommendVerifier
            if isinstance(self.verifier, RecommendVerifier):
                self._count_request('recommend_load')
                if self.discovery_stop.is_set():
                    raise TaskCancelled('主动招呼已停止')
            self.verifier.reload()
        except AccountPauseError:
            raise
        except BrowserError as exc:
            self.store.event('discovery_error', '', f'刷新推荐页失败：{exc}')
            return {'greeted': 0, 'stopped': self.discovery_stop.is_set(),
                    'reason': f'刷新推荐页失败：{exc}'}
        for job in selected:
            if self.discovery_stop.is_set():
                break
            if self.store.setting('greeting_quota', {}) and self._quota_stale():
                # 额度缓存过期（可能人工打了招呼）：重新读平台额度，覆盖人工打招呼导致的失真
                try:
                    self.read_greeting_quota()
                except BrowserError as exc:
                    self.store.event('discovery_error', job['platform_id'], str(exc))
                    break
            if self._quota_exhausted_now():
                break
            try:
                if isinstance(self.verifier, RecommendVerifier):
                    self._count_request('recommend_load')
                    if self.discovery_stop.is_set():
                        raise TaskCancelled('主动招呼已停止')
                self.verifier.select_job(job['platform_id'])
                candidates = self.verifier.read_candidates()
            except AccountPauseError:
                raise
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
                result = self.greet_discovered(cand['uid'], cand.get('name') or '', job['platform_id'])
                if result.get('sent'):
                    greeted += 1
                    done += 1
                if not isinstance(self.verifier, RecommendVerifier):
                    throttle.wait(self.discovery_stop)
            if not isinstance(self.verifier, RecommendVerifier):
                throttle.wait(self.discovery_stop)
        stopped = self.discovery_stop.is_set()
        return {'greeted': greeted, 'stopped': stopped,
                'reason': '主动打招呼已手动停止' if stopped else f'本轮主动招呼 {greeted} 次'}

    def start_discovery(self, per_job_min=None, per_job_max=None):
        """后台启动「按额度持续主动打招呼」循环；立即返回，循环在线程内运行。

        每轮 run_discovery 结束后，若额度未用完、未手动停止、且本轮有成功招呼，
        继续下一轮；否则记录累计次数与停止原因并结束。无候选人或结果待核实时不
        为用完额度而继续发送。
        """
        if self.discovery_worker and self.discovery_worker.is_alive():
            return {'running': True, 'message': '主动打招呼循环已在运行'}
        self.discovery_stop.clear()
        self._refresh_quota_if_needed()
        if self.discovery_stop.is_set():
            raise TaskCancelled('启动期间已请求停止，未启动主动招呼任务')
        state = self.jobs.state()
        if state['blockers']:
            raise ValueError('；'.join(state['blockers']))

        def run():
            total = 0
            try:
                while not self.discovery_stop.is_set():
                    result = self.run_discovery(per_job_min, per_job_max)
                    total += result.get('greeted', 0)
                    if result.get('stopped'):
                        self.store.event('discovery_done', '', f'主动打招呼已手动停止，累计 {total} 次')
                        return
                    if self._quota_exhausted_now():
                        self.store.event('discovery_done', '', f'今日招呼额度已用完，累计 {total} 次')
                        return
                    if not result.get('greeted'):
                        self.store.event('discovery_done', '', f'没有更多可招呼的候选人，累计 {total} 次')
                        return
            except Exception as exc:
                self.store.event('discovery_stopped', '', str(exc)[:300])

        self.discovery_worker = Thread(target=run, daemon=True, name='recruiting-discovery')
        self.discovery_worker.start()
        return {'running': True, 'message': '主动打招呼循环已启动'}

    def stop_discovery(self):
        """请求停止正在运行的主动打招呼循环；节流等待可中断，已发出的请求等待返回。"""
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

    def _friend_job_map(self, uids):
        """批量查联系人岗位 ID（encryptJobId），返回 {uid: encryptJobId}；失败返回 {}。

        仅在本地模式且 local_session 支持 read_friend_jobs 时生效，避免测试用
        FakeLocalSession/Mock 或浏览器模式触发真实 HTTP 读取。
        """
        if not self.use_local_session:
            return {}
        reader = getattr(self.local_session, 'read_friend_jobs', None)
        if not callable(reader) or not uids:
            return {}
        try:
            result = reader(uids)
        except AccountPauseError:
            raise
        except BrowserError:
            return {}
        return result if isinstance(result, dict) else {}

    def _enrich_position_platform_id(self, snapshot):
        """按会话候选人的 uid 查岗位 ID，注入 position_platform_id。

        查不到或为空时保持原样，import_conversation 会走 context- 兜底（等价于
        「待关联」状态）；读取失败不阻塞绑定。
        """
        ident = snapshot.get("id") or ""
        gid = ident.split("-", 1)[0]
        if not gid:
            return
        job_id = self._friend_job_map([gid]).get(str(gid))
        if isinstance(job_id, str) and job_id.strip():
            snapshot["position_platform_id"] = job_id

    def confirm_binding(self, conversation_id, name, position_title, expected_account):
        """核实后把会话写入 pilot_conversation（首次绑定的唯一入口）。

        重新读取一次并带上预览时拿到的 account_uid 作 expected_account，
        read_conversation 会校验当前登录账号未变化，再导入并绑定。
        """
        snapshot = self.local_session.read_conversation(
            str(conversation_id).strip(), str(name).strip(), str(position_title).strip(),
            expected_account=str(expected_account).strip())
        self._enrich_position_platform_id(snapshot)
        result = self.store.import_conversation(snapshot, confirmed=True)
        self.store.select_conversation(result["id"])  # 新绑定的会话设为当前选中
        self.store.event("conversation_bound", result["id"], "已核实并绑定会话；未发送消息")
        return result

    @staticmethod
    def _last_message_id(snapshot):
        """会话快照里最后一条消息的 mid（用于增量同步）；没有消息返回 None。"""
        mids = [int(m['id']) for m in (snapshot.get('messages') or [])
                if isinstance(m, dict) and str(m.get('id', '')).isdigit()]
        return max(mids) if mids else None

    @staticmethod
    def _append_new_messages(previous, snapshot):
        """把增量读取到的新消息追加到旧快照，按 id 去重排序，不删旧消息。

        增量模式只拉到新消息（mid > since_mid），旧消息可能已被 BOSS 删除但仍保留
        在本地；追加式合并能保证不覆盖旧记录。新消息的 received_resume_message_id
        由调用方负责合并（新附件优先，否则沿用旧值）。
        """
        return merge_messages(previous, snapshot)

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
                    snapshot = self.local_session.read_conversation(
                        ident, current['name'], position['title'],
                        previous.get('account_uid'),
                        since_mid=self._last_message_id(previous))
                except BrowserError as exc:
                    self.monitor['error'] = str(exc)
                    self.connection = {"connected": False, "transport": "local_cookie_http", "checked_at": now(), "message": str(exc)}
                    raise
                if snapshot is None:
                    current = self.store.row('conversations', ident)
                    # 增量：没有新消息，保持原快照，不导入、不处理简历
                    self.connection = {"connected": True, "transport": "local_cookie_http", "checked_at": now(),
                                       "message": "已连接 BOSS：本地登录会话，无新消息",
                                       "read_jobs": bool(self.jobs.state()['sync'].get('synced_at')),
                                       "read_bound_conversation": True}
                    self.monitor["last_success"] = now()
                    self.monitor["error"] = ""
                    if process:
                        self.process_received_resume(current)
                    return current
                snapshot['position_platform_id'] = snapshot.get('position_platform_id') or previous.get('position_platform_id')
                snapshot['received_resume_message_id'] = snapshot.get('received_resume_message_id') or previous.get('received_resume_message_id')
                result = self.store.import_conversation(snapshot, append=True, track_reply=True)
                self.connection = {"connected": True, "transport": "local_cookie_http", "checked_at": now(),
                                   "message": "已连接 BOSS：本地登录会话，只读同步绑定候选人的消息",
                                   "read_jobs": bool(self.jobs.state()['sync'].get('synced_at')),
                                   "read_bound_conversation": True}
                self.store.event("conversation_synced", ident, f"后台同步绑定会话 {len(snapshot['messages'])} 条平台消息；未操作标签页或发送消息")
            else:
                result = self.store.import_conversation(self.browser.open_conversation(ident), track_reply=True)
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
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if auto_send is None:
                db.execute("UPDATE conversations SET taken_over=?,do_not_contact=? WHERE id=?", (taken_over, do_not_contact, cid))
            else:
                db.execute("UPDATE conversations SET taken_over=?,do_not_contact=?,auto_send=? WHERE id=?", (taken_over, do_not_contact, auto_send, cid))
            if do_not_contact:
                db.execute("UPDATE outbox SET status='cancelled',updated_at=? WHERE conversation_id=? AND status='draft'", (now(), cid))
            elif taken_over:
                # Takeover withdraws unsubmitted drafts; an explicit human edit may
                # prepare the same wording again. Submitted actions remain untouched.
                db.execute("UPDATE outbox SET status='expired',result='人工接管，请编辑核实后重新准备',updated_at=? WHERE conversation_id=? AND status='draft'", (now(), cid))
        self.store.event("contact_control", cid, "已更新人工接管／停止联系设置")

    def set_auto_send(self, cid, enabled):
        """切换单个会话的自动外发开关（不改变人工接管/停止联系）。"""
        if type(enabled) is not bool:
            raise ValueError("自动外发开关必须是布尔值")
        self.store.row("conversations", cid)
        with self.store.db() as db:
            db.execute("UPDATE conversations SET auto_send=? WHERE id=?", (int(enabled), cid))
        self.store.event("auto_send_toggled", cid, f"会话自动外发已{'开启' if enabled else '关闭'}")
        return self.store.row("conversations", cid)

    def set_monitor_enabled(self, enabled):
        """开启/停止回复监测：写入控制标志，由独立 worker 进程轮询执行。"""
        if type(enabled) is not bool:
            raise ValueError("开关必须是布尔值")
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR REPLACE INTO settings VALUES ('monitor_enabled',?)", (encode(enabled),))
            if enabled:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('monitor_run_id',?)", (encode(uuid4().hex),))
        self.store.event('monitor_control', '', f"回复监测已{'开启' if enabled else '停止'}")
        return {'monitor_enabled': enabled}

    def position(self, payload):
        old = self.store.row("positions", payload["id"])
        return self.store.save_position(old["id"], old["title"], str(payload.get("jd", "")), old["source"], payload.get("enabled") is True)

    def read_position(self, ident):
        p = self.store.row("positions", ident)
        if not ident.startswith('boss-'):
            raise ValueError('请先核实并关联平台岗位 ID')
        value = self.browser.read_position(p["title"], ident.removeprefix('boss-'))
        result = self.store.save_position(p["id"], p["title"], value["jd"], value["source"], bool(p["enabled"]))
        self.store.event("position_read", ident, "读取平台现有职位描述；没有修改或发布职位")
        return result

    def link_position(self, cid, platform_id):
        with self.lock, self.store.task('outbound-browser'):
            return self.store.link_position(cid, str(platform_id))

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
        with self.lock, self.store.task('resume:' + cid):
            self._check_stopped()
            if self.use_local_session:
                current = self.store.row('conversations', cid)
                position = self.store.row('positions', current['position_id'])
                snapshot = json.loads(current['snapshot'])
                message_id = snapshot.get('received_resume_message_id')
                if message_id:
                    docs = self.store.rows('documents', 'WHERE conversation_id=?', (cid,))
                    cached = next((d for d in docs if json.loads(d['meta']).get('message_id') == message_id), None)
                    if cached:
                        self.auto_assess(cid)
                        return cached
                value = self.local_session.read_resume(cid, current['name'], position['title'], snapshot.get('account_uid'))
                self._check_stopped()
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
        self._check_stopped()
        cid = conversation["id"]
        position = self.store.row("positions", conversation["position_id"])
        if conversation["do_not_contact"] or not position["enabled"]:
            reason = "已停止联系" if conversation["do_not_contact"] else "岗位已暂停"
            self.resume_status(cid, "paused", reason + "，简历自动处理已暂停")
            return
        try:
            self.jobs.check_position(position['id'])
        except ValueError as exc:
            self.resume_status(cid, 'paused', str(exc))
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
            except TaskCancelled:
                self.resume_status(cid, 'paused', '任务已停止，已有资料保留', message_id=message_id)
                raise
            except Exception:
                self.resume_status(cid, "read_failed", "附件自动读取未完成，请点击重新读取；已有资料保留", message_id=message_id)
                raise
        elif docs:
            self.auto_assess(cid)

    def assessment_input(self, cid):
        c = self.store.row("conversations", cid)
        position = self.store.row("positions", c["position_id"])
        doc = self.store.current_document(cid)
        if not doc:
            raise ValueError("请先读取或导入该候选人的简历")
        config = deepcopy(self.config_provider())
        # Include candidate/document identity and model configuration, never persist credentials.
        digest = self._assessment_digest(cid, position, doc, self._assessment_model_basis(config))
        return position, doc, config, digest

    @staticmethod
    def _assessment_model_basis(config):
        return [config.get('ai', {}), agent.get_ai_api_key(config), get_ai_base_url(config), get_ai_service(config)]

    @staticmethod
    def _assessment_digest(cid, position, doc, model_basis):
        return fingerprint([cid, doc['id'], doc['content_hash'], position['id'], position['version'],
                            *model_basis, 'assessment-v3'])

    def auto_assess(self, cid):
        with self.lock:
            self._check_stopped()
            try:
                self._auto_assessment_guard(cid)
            except TaskCancelled as exc:
                self.resume_status(cid, 'paused', str(exc))
                return
            position, doc, config, digest = self.assessment_input(cid)
            details = {"input_hash": digest, "document_id": doc["id"]}
            if not agent.get_ai_api_key(config):
                self.resume_status(cid, "waiting_model", "简历已读取，等待配置模型；配置后下次监测自动评分", **details)
                return
            if not position["jd"].strip():
                self.resume_status(cid, "waiting_jd", "简历已读取，等待补充岗位 JD；补充后下次监测自动评分", **details)
                return
            old = self.store.setting("resume_processing:" + cid, {})
            if old.get('input_hash') == digest and old.get('status') == 'scoring':
                existing = self.store.rows('assessments', 'WHERE input_hash=?', (digest,))
                with self.store.db() as db:
                    active = self.store.task_active(db, 'assessment:' + digest)
                if active and not existing:
                    return
                if existing:
                    return self.assess(cid, auto=True)
            if old.get("input_hash") == digest and old.get("status") in {"failed", "scoring"}:
                self.resume_status(cid, "failed", "自动评分未完成，简历已保留；请点击重试评分", **details)
                return
            try:
                return self.assess(cid, auto=True)
            except TaskCancelled:
                raise
            except TaskBusy:
                self.resume_status(cid, 'scoring', '相同资料已在另一进程评估，等待结果', **details)
                return
            except Exception:
                self.resume_status(cid, "failed", "自动评分未完成，简历已保留；请点击重试评分", **details)
                self.store.event("assessment_failed", cid, "自动评分失败；未覆盖已有评估，不自动反复调用模型")

    def _auto_assessment_guard(self, cid):
        self._check_stopped()
        c = self.store.row('conversations', cid)
        p = self.store.row('positions', c['position_id'])
        if c['taken_over'] or c['do_not_contact'] or not p['enabled']:
            raise TaskCancelled('已人工接管、停止联系或岗位暂停，自动评分已停止')
        try:
            self.jobs.check_position(p['id'])
        except ValueError as exc:
            raise TaskCancelled(str(exc)) from exc

    def assess(self, cid, *, auto=False):
        # Serialize automatic/manual attempts so concurrent requests cannot double-charge.
        with self.lock:
            self._check_stopped()
            self._model_takeover_guard(cid)
            p, doc, config, digest = self.assessment_input(cid)
            details = {"input_hash": digest, "document_id": doc["id"]}
            with self.store.task('assessment:' + digest):
                existing = self.store.rows("assessments", "WHERE input_hash=?", (digest,))
                if existing:
                    self.resume_status(cid, "completed", "已按当前岗位 JD 自动评估，缺失证据保留待确认", **details)
                    return json.loads(existing[0]["result"])
                self.resume_status(cid, "scoring", "正在按岗位 JD 评分", **details)
                try:
                    self._check_stopped()
                    if auto:
                        self._auto_assessment_guard(cid)
                    self._model_takeover_guard(cid)
                    result = agent.assess(p, doc, config)
                    self._check_stopped()
                    self._model_takeover_guard(cid)
                    if auto:
                        self._auto_assessment_guard(cid)
                        if self.assessment_input(cid)[3] != digest:
                            raise TaskCancelled('评分期间岗位或简历依据已变化，请按最新资料重新评分')
                except TaskCancelled:
                    self.resume_status(cid, 'paused', '任务已停止，未继续处理评分', **details)
                    raise
                except Exception:
                    self.resume_status(cid, "failed", "评分未完成，简历已保留；请检查模型配置后重试", **details)
                    raise
                result.update({"document_id": doc["id"], "position_id": p['id'], "position_version": p["version"], "model": config.get("ai", {}).get("model")})
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
        doc = self.store.current_document(cid)
        assessment = None
        digest = self.assessment_input(cid)[3] if doc else None
        for item in self.store.rows("assessments", "WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC", (cid,)):
            result = json.loads(item["result"])
            if doc and item['input_hash'] == digest and result.get('position_id') == p['id'] and result.get("document_id") == doc["id"] and result.get("position_version") == p["version"]:
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
        if c["do_not_contact"] or (not text and (c['taken_over'] or not p['enabled'])):
            raise ValueError("当前会话已人工接管、停止联系或岗位暂停")
        refs = {"position_id": p["id"], "position_version": p["version"], "knowledge": {}}
        if text:
            if len(text) > 500:
                raise ValueError("测试回复最多500字")
            refs["source"] = "human_draft"
        else:
            with self.lock, self.store.task('reply:' + cid):
                self._reply_stage('sync_before_generation', '生成前同步会话')
                self.sync(cid)
                context = self.reply_context(cid)
                current = self.store.row('conversations', cid)
                position = self.store.row('positions', current['position_id'])
                if current['taken_over'] or current['do_not_contact'] or not position['enabled']:
                    raise TaskCancelled('已人工接管、停止联系或岗位暂停，回复生成已停止')
                refs.update(position_id=position['id'], position_version=position['version'])
                messages = context["conversation"]["messages"]
                target = candidate_question(context['conversation'])
                if not target:
                    raise ValueError("最新消息不是候选人发来的消息，请核对会话后处理")
                if question and question != target['text']:
                    raise ValueError("问题与实际会话不一致，不使用脱离上下文的问题生成回复")
                config = deepcopy(self.config_provider())
                signature = self.reply_context_signature(context, self._reply_model_revision(config))
                for draft in self.store.rows('outbox', "WHERE conversation_id=? AND kind='reply' AND status='draft' ORDER BY created_at DESC,rowid DESC", (cid,)):
                    saved = json.loads(draft['refs'])
                    if saved.get('source') == 'ai_draft' and saved.get('context_signature') == signature:
                        return draft
                self._check_stopped()
                self._reply_stage('generating', '模型正在生成回复')
                self._model_takeover_guard(cid)
                result = agent.reply(context, config)
                self._check_stopped()
                self._model_takeover_guard(cid)
                # Human messages can change the live browser even while our queue is locked.
                self._reply_stage('sync_after_generation', '回复已生成，重新核对消息和资料')
                self.sync(cid)
                fresh = self.reply_context(cid)
                if self.reply_context_signature(fresh) != signature:
                    raise ValueError("生成期间会话或资料发生变化，本次回复未保存，请基于新上下文重新生成")
                text = result["text"]
                refs.update({"source": "ai_draft", "needs_human": result["needs_human"],
                    "company_version": context["company"]["version"],
                    "document_id": context["resume"]["id"] if context["resume"] else None,
                    "assessment_id": context["assessment"]["id"] if context["assessment"] else None,
                    "message_count": len(messages), "context_signature": signature,
                    "context_coverage": context["conversation"]["coverage"],
                    "basis": result["basis"], "missing": result.get("missing", [])})
                check_reply(text)
                self._model_takeover_guard(cid)
                return self.store.draft(cid, 'reply', text.strip(), refs)
        check_reply(text)
        return self.store.draft(cid, "reply", text.strip(), refs)

    def _reply_model_revision(self, config=None):
        config = self.config_provider() if config is None else config
        digest = fingerprint([config.get('ai', {}), agent.get_ai_api_key(config),
                              get_ai_base_url(config), get_ai_service(config), 'reply-model-v1'])
        # Keep credential-derived identity private. Only a random revision contributes
        # to the public draft signature; raw credentials and this digest stay server-side.
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM settings WHERE key='reply_model_identity'").fetchone()
            identity = json.loads(row['value']) if row else {}
            if identity.get('digest') != digest:
                identity = {'digest': digest, 'revision': uuid4().hex}
                db.execute("INSERT OR REPLACE INTO settings VALUES ('reply_model_identity',?)", (encode(identity),))
            return identity['revision']

    def _model_takeover_guard(self, cid):
        if self.store.row('conversations', cid)['taken_over']:
            raise TaskCancelled('人工接管中，停止模型评分和回复生成')

    def reply_context_signature(self, context, model_revision=None):
        revision = self._reply_model_revision() if model_revision is None else model_revision
        return fingerprint([context["conversation"]["context_hash"], context["company"]["version"],
                            context['job']['id'], context["job"]["version"], context["resume"], context["assessment"], revision])

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

    def execute(self, ident, auto=False):
        """执行一条待发草稿；auto=True 表示由自动外发触发，受每日自动回复上限约束。"""
        with self.lock, self.store.task('outbound-browser'):
            draft = self.store.row("outbox", ident)
            if draft["kind"] == "invitation":
                raise PermissionError("本次测试禁止发送面试邀约；仅支持保存本地草稿")
            if draft["kind"] == "reply":
                check_reply(draft["content"])
            c = self.store.row("conversations", draft["conversation_id"])
            p = self.store.row("positions", c["position_id"])
            if auto:
                self.jobs.check_position(p['id'])
            if c["do_not_contact"] or (auto and (c['taken_over'] or not p['enabled'])):
                raise ValueError("已人工接管、停止联系或岗位暂停")
            if draft["status"] != "draft":
                raise ValueError("动作不在待执行状态")
            refs = json.loads(draft["refs"])
            if refs.get("needs_human") or refs.get("position_version") != p["version"]:
                raise ValueError("草稿需要人工处理或岗位版本变化，请重新准备")
            if auto and draft["kind"] == "reply" and self._reply_quota_exhausted():
                raise ValueError("今日自动回复已达上限，请人工处理")
            for kid, version in refs.get("knowledge", {}).items():
                fact = self.store.row("knowledge", kid)
                if fact["version"] != version or not fact["approved"] or not fact["public"] or (fact["valid_until"] and fact["valid_until"] < date.today().isoformat()):
                    raise ValueError("知识已变更／过期，请重新生成回复")
            if refs.get("source") == "ai_draft" and refs.get("context_signature") != self.reply_context_signature(self.reply_context(c["id"])):
                raise ValueError("回复依据已变化，请使用最新公司说明、JD、简历和会话重新生成")
            self._check_stopped()
            self._reply_stage('checking_browser', '检查 BOSS 当前对话和编辑器')
            before = self.browser.open_conversation(c["id"])
            if not before["editor_empty"]:
                raise ValueError("编辑器中有人工输入，已停止自动操作")
            if self.use_local_session:
                self._reply_stage('preflight', '发送前重新核对最新消息')
                # 本地模式：草稿来自 HTTP 快照，而浏览器 DOM 读到的消息结构不同（无 id/timestamp），
                # 直接比对 context_hash 会误判。这里改用与草稿同源（HTTP）重读判断是否有新消息，
                # 浏览器快照只用于发送前的编辑框/会话守卫，不写入数据库覆盖 HTTP 快照。
                source = self.local_session.read_conversation(
                    c["id"], c["name"], p["title"],
                    json.loads(c["snapshot"]).get("account_uid"), throttle=False,
                    since_mid=self._last_message_id(json.loads(c['snapshot'])))
                if source is not None:
                    previous = json.loads(c['snapshot'])
                    source['position_platform_id'] = source.get('position_platform_id') or previous.get('position_platform_id')
                    source['received_resume_message_id'] = source.get('received_resume_message_id') or previous.get('received_resume_message_id')
            else:
                source = before
            updated = self.store.import_conversation(source, append=self.use_local_session) if source is not None else self.store.row('conversations', c['id'])
            if updated["context_hash"] != draft["context_hash"]:
                raise ValueError("会话有新消息，已停止发送")
            if isinstance(self.browser, BossBrowser):
                self._reply_stage('verifying_account', '核实发送账号')
                self._count_request('identity')
                self.browser.verify_account(c['id'], json.loads(c['snapshot']).get('account_uid'), on_refusal=self._pause_platform_requests)
            self._send_guard(ident, auto)
            self.store.claim(ident, auto=auto, daily_limit=self._recruiting_cfg().get('auto_reply_daily_limit', 5))
            try:
                self._send_guard(ident, auto, claimed=True)
                self._reply_stage('sending', '正在提交发送，请勿重复发送')
                if isinstance(self.browser, BossBrowser):
                    self.browser.execute(draft['kind'], before, draft['content'], preflight=lambda: self._send_guard(ident, auto, claimed=True))
                else:
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
                self._reply_stage(status, '已确认发送成功' if status == 'sent' else '发送结果待核实，请先检查 BOSS')
                if not self.use_local_session:
                    self.store.import_conversation(after)
            except TaskCancelled:
                self.store.finish(ident, 'cancelled', '发送前任务已停止，未提交后续点击')
                raise
            except Exception:
                self.store.finish(ident, "uncertain", "操作结果待核实；请查看 Chrome，禁止盲目重试")
                self.store.event("outbound_uncertain", ident, "执行中断，未自动重试")
                raise BrowserError("操作结果待核实；请检查 Chrome，已阻止再次发送")
            self.store.event("outbound_" + status, ident, message)
            return self.store.row("outbox", ident)

    def _send_guard(self, ident, auto=False, *, claimed=False):
        self._check_stopped()
        self._check_platform_cooldown()
        draft = self.store.row('outbox', ident)
        expected = 'sending' if claimed else 'draft'
        if draft['status'] != expected or (claimed and draft['owner'] != self.store.owner):
            raise TaskCancelled('动作已取消、状态已变化或发送任务不属于当前进程，停止发送')
        c = self.store.row('conversations', draft['conversation_id'])
        p = self.store.row('positions', c['position_id'])
        if auto and not c['binding_confirmed']:
            raise TaskCancelled('会话尚未确认绑定，停止自动发送')
        if c['do_not_contact'] or (auto and (c['taken_over'] or not p['enabled'])):
            raise TaskCancelled('已人工接管、停止联系或岗位暂停')
        if auto:
            try:
                self.jobs.check_position(p['id'])
            except ValueError as exc:
                raise TaskCancelled(str(exc)) from exc
            if not c['auto_send']:
                raise TaskCancelled('自动外发已关闭，停止当前发送')
        refs = json.loads(draft['refs'])
        if refs.get('needs_human'):
            raise TaskCancelled('草稿需要人工处理，停止当前发送')
        if c['context_hash'] != draft['context_hash'] or refs.get('position_version') != p['version']:
            raise TaskCancelled('会话或岗位已变化，停止当前发送')
        if refs.get('source') == 'ai_draft' and refs.get('context_signature') != self.reply_context_signature(self.reply_context(c['id'])):
            raise TaskCancelled('公司说明、简历或评分依据已变化，停止当前发送')

    def _send_channel_status(self):
        """探测发送通道（Browser Runtime + Chrome 招聘页标签）是否可用。

        只读探测，不启动 Runtime、不切换标签；发送通道断了不代表监测要停，
        这里只把状态暴露给前端，让「发不出去」显式可见（草稿会留在 outbox）。
        """
        try:
            health = self.browser.runtime.health()
        except Exception as exc:
            return {"available": False, "message": f"Browser Runtime 未连接：{exc}"}
        if not isinstance(health, dict) or health.get("runtime") != "bosshunter":
            return {"available": False, "message": "BossHunter Browser Runtime 未连接"}
        try:
            targets = [t for t in self.browser.runtime.targets() if self.browser.recruiter_target(t)]
        except Exception as exc:
            return {"available": False, "message": f"读取 Chrome 标签失败：{exc}"}
        if not targets:
            return {"available": False, "message": "未找到 BOSS 招聘端标签页，发送需在 Chrome 打开招聘页"}
        target_id = getattr(self.browser, 'target_id', None)
        if target_id and not any(t.get('targetId') == target_id for t in targets):
            return {"available": False, "message": "绑定的招聘标签页已关闭或离开招聘端"}
        return {"available": True, "message": "发送通道正常"}

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

    def _check_stopped(self):
        """收到停止信号时抛异常，让当前自动任务统一退出（不再发请求/调模型/发送）。"""
        if self._stop_event is not None and self._stop_event.is_set():
            raise TaskCancelled("已请求停止，中断当前任务")
        self._check_monitor_enabled()

    def _check_platform_cooldown(self):
        if self.store.setting('request_paused_until', 0) > datetime.now(timezone.utc).timestamp():
            raise TaskCancelled('BOSS 拒绝或限流后的共享冷却尚未结束，停止发送；请核实账号后手动恢复')

    def _check_monitor_enabled(self):
        """worker 监测流程中检查「关闭监测」标志，及时退出当前任务而非只阻止下一轮。

        仅 worker（设置了 stop_event）时检查；Web 的「手动检查一次」不受监测开关约束。
        """
        if self._stop_event is not None and not self.store.setting('monitor_enabled', True):
            raise TaskCancelled("监测已停止，中断当前任务")

    def worker_loop(self, stop_event):
        """独立进程主循环：独立心跳线程 + 轮询 monitor_enabled 标志、定期跑 monitor_once。"""
        self.store.event('worker_started', '', '独立监测进程已启动')
        self._stop_event = stop_event
        if hasattr(self.local_session, 'set_stop_event'):
            self.local_session.set_stop_event(stop_event)
        if hasattr(self.local_session, 'set_cancel_check'):
            self.local_session.set_cancel_check(self._check_stopped)

        def heartbeat():
            # monitor_once 会被节流睡眠阻塞 1–2 分钟以上，若只在主循环写心跳，
            # 前端 30 秒阈值会把运行中的 worker 误判为已停止。独立线程保证心跳始终新鲜。
            while not stop_event.wait(10):
                try:
                    self._write_heartbeat()
                except Exception:
                    pass  # 心跳失败不致命，下个周期重试

        Thread(target=heartbeat, daemon=True, name='recruiting-heartbeat').start()
        last_run = 0.0
        consecutive_errors = 0
        previous_enabled = False
        previous_revision = None
        try:
            while not stop_event.is_set():
                if self.store.setting('monitor_enabled', False):
                    revision = self.store.setting('monitor_run_id')
                    if not previous_enabled or revision != previous_revision:
                        consecutive_errors = 0
                        last_run = 0.0
                    previous_enabled = True
                    previous_revision = revision
                    if time.time() - last_run >= self.monitor['interval_seconds']:
                        try:
                            self.monitor_once()
                            consecutive_errors = 0
                        except AccountPauseError as exc:
                            # 验证码/登录失效/身份不一致：暂停等人工，不自动重试
                            self.set_monitor_enabled(False)
                            self.store.event('monitor_paused', '', f"已暂停，需人工处理：{exc}")
                            self.monitor['error'] = str(exc)
                            self._persist_monitor_state()
                        except (TaskCancelled, TaskBusy):
                            pass
                        except Exception as exc:
                            if self.store.setting('monitor_run_id') != revision:
                                # A late failure belongs to the old run. Do not
                                # charge or stop the run just enabled by the user.
                                consecutive_errors = 0
                            else:
                                consecutive_errors += 1
                                self.store.event('monitor_paused', '', str(exc)[:250])
                                if consecutive_errors >= 3 and self.store.pause_monitor_run(revision):
                                    self.store.event('monitor_paused', '', f"连续失败 {consecutive_errors} 次，已暂停，请人工处理")
                        finally:
                            # 失败也推进 last_run：持续出错时仍按 interval 重试，而不是每 10 秒紧循环。
                            last_run = time.time()
                else:
                    previous_enabled = False
                stop_event.wait(10)  # 每 10 秒轮询一次 monitor_enabled 标志
        finally:
            self.store.event('worker_stopped', '', '独立监测进程已停止')

    def _persist_monitor_state(self):
        """把监测状态写回 DB，供服务重启后恢复显示。"""
        self.store.set_setting('monitor_state', {
            'last_success': self.monitor['last_success'],
            'error': self.monitor['error'],
            'processing_conversation_id': self.monitor.get('processing_conversation_id'),
        })

    def _auto_send_enabled(self, cid):
        """该会话的自动外发开关开启时才允许自动外发。"""
        c = self.store.row("conversations", cid)
        return bool(c.get('binding_confirmed') and c.get("auto_send"))

    def _auto_reply_sent_today(self):
        """今日已自动外发的回复数（只计自动，不计人工手动发送）。"""
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        data = self.store.setting('auto_reply_sent', {})
        return data.get('count', 0) if data.get('date') == day else 0

    def _mark_auto_reply_sent(self):
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        data = self.store.setting('auto_reply_sent', {})
        if data.get('date') != day:
            data = {'date': day, 'count': 0}
        data['count'] = data.get('count', 0) + 1
        self.store.set_setting('auto_reply_sent', data)

    def _reply_quota_exhausted(self):
        limit = self.store.setting('auto_reply_daily_limit', self._recruiting_cfg().get("auto_reply_daily_limit", 5))
        if not limit:
            return False
        return self._auto_reply_sent_today() >= limit

    def _auto_send_if_allowed(self, draft):
        """按条件自动发送一条回复草稿；不满足则留草稿给人工。"""
        if draft['status'] != 'draft':
            return False
        with self.store.db() as db:
            if db.execute("SELECT 1 FROM outbox WHERE status IN ('sending','uncertain') LIMIT 1").fetchone() or db.execute("SELECT 1 FROM greeting_attempts WHERE status IN ('sending','uncertain') LIMIT 1").fetchone():
                self._reply_stage('blocked', '存在发送结果待核实，请先人工核实')
                return False
        refs = json.loads(draft["refs"])
        if refs.get("needs_human"):
            self._reply_stage('needs_attention', '草稿需要人工处理，不自动发送')
            return False
        cid = draft["conversation_id"]
        if not self._auto_send_enabled(cid):
            self._reply_stage('drafted', '回复草稿已保存，自动外发未开启')
            return False
        if self._reply_quota_exhausted():
            self._reply_stage('blocked', '今日自动回复已达上限')
            self.store.event("auto_send_skipped", cid, "今日自动回复已达上限")
            return False
        try:
            result = self.execute(draft["id"], auto=True)
            if result['status'] == 'sent':
                self.store.event("auto_sent", cid, "已确认自动发送回复")
                return True
            self.store.event('auto_send_uncertain', cid, '自动回复结果待核实，已暂停外发')
            return False
        except (AccountPauseError, TaskCancelled):
            raise
        except ConversationNotSelected as exc:
            self._reply_stage('waiting_browser', '请在 BOSS 手动打开该候选人的对话；草稿已保留，后续轮询重试')
            self.store.event('auto_send_skipped', cid, str(exc))
            return False
        except (ValueError, BrowserError) as exc:
            self._reply_stage('blocked', str(exc)[:250])
            self.store.event("auto_send_skipped", cid, str(exc))
            return False

    def _allowed_conversations(self):
        """返回允许同步的会话；人工接管只暂停模型与自动外发。"""
        allowed = []
        for c in self.store.rows("conversations"):
            if not c['binding_confirmed'] or c["do_not_contact"]:
                continue
            p = self.store.row("positions", c["position_id"])
            if not p["enabled"]:
                continue
            try:
                self.jobs.check_position(p['id'])
            except ValueError:
                continue
            allowed.append(c["id"])
        return allowed

    def _next_monitor_cid(self, allowed):
        """按轮询游标从允许处理的会话里选下一个，分散到多轮而非每轮读全部。"""
        cursor = self.store.setting("monitor_cursor")
        if cursor and cursor in allowed:
            return allowed[(allowed.index(cursor) + 1) % len(allowed)]
        return allowed[0]

    def monitor_once(self):
        with self.store.task('monitor-cycle'):
            return self._monitor_once()

    def _monitor_once(self):
        """轮询式监测：每轮只处理一个允许的会话（round-robin），而非每轮读全部。

        候选人越多越不能每轮把所有历史各拉一遍；用 monitor_cursor 记录上次处理到
        谁，下一轮处理下一个。人工接管继续同步；停止联系/岗位暂停的会话跳过。
        """
        allowed = self._allowed_conversations()
        if not allowed:
            self._persist_monitor_state()
            return {"changed": False, "last_success": self.monitor["last_success"]}
        cid = self._next_monitor_cid(allowed)
        self._check_stopped()
        self._check_monitor_enabled()
        self.store.set_setting("monitor_cursor", cid)
        self.monitor['processing_conversation_id'] = cid
        self._persist_monitor_state()
        self._reply_progress_local.cid = cid
        self._reply_stage('syncing', '同步会话，检查新消息与简历')
        try:
            before = self.store.row("conversations", cid)
            after = self.sync(cid, process=True)
            changed = before['context_hash'] != after['context_hash']
            if changed:
                self._check_monitor_enabled()
                self.store.event("conversation_changed", cid, "会话发生变化，旧草稿已过期")
            messages = json.loads(after['snapshot'])['messages']
            target = candidate_question({'messages': messages})
            incoming = target is not None
            pending = self.store.setting('reply_work:' + cid, {})
            previous_target = candidate_question(json.loads(before['snapshot']))
            if changed and incoming and fingerprint(target) != fingerprint(previous_target):
                pending = {'status': 'waiting', 'context_hash': after['context_hash']}
                self.store.set_setting('reply_work:' + cid, pending)
            pending_blocked = False
            if pending.get('draft_id'):
                original = self.store.row('outbox', pending['draft_id'])
                if original['status'] in {'sent', 'cancelled'}:
                    pending = {}
                    self.store.set_setting('reply_work:' + cid, {})
                elif original['status'] in {'sending', 'uncertain'}:
                    pending_blocked = True
            current = self.store.row('conversations', cid)
            if current['taken_over']:
                self._reply_stage('taken_over', '人工接管中：继续同步消息和读取简历，模型评分、回复生成与自动外发暂停')
            elif incoming and pending and pending.get('status') != 'needs_attention' and not pending_blocked and agent.get_ai_api_key(self.config_provider()):
                self._check_stopped()
                try:
                    drafts = self.store.rows('outbox', "WHERE conversation_id=? AND kind='reply' AND status='draft' AND context_hash=? ORDER BY created_at DESC", (cid, after['context_hash']))
                    reusable = next((d for d in drafts if json.loads(d['refs']).get('source') == 'ai_draft'), None)
                    if reusable and json.loads(reusable['refs']).get('context_signature') != self.reply_context_signature(self.reply_context(cid)):
                        reusable = None
                    draft = reusable or self.prepare_reply(cid)
                    self.store.set_setting('reply_work:' + cid, {'status': 'drafted', 'draft_id': draft['id'], 'context_hash': after['context_hash']})
                    if self._auto_send_if_allowed(draft):
                        self.store.set_setting('reply_work:' + cid, {})
                except (PermissionError, agent.ModelOutputError) as exc:
                    self._reply_stage('needs_attention', str(exc)[:250])
                    self.store.set_setting('reply_work:' + cid, {'status': 'needs_attention',
                                                               'context_hash': after['context_hash'], 'reason': str(exc)})
                    self.store.event('reply_needs_attention', cid, str(exc))
                except ValueError as exc:
                    self._reply_stage('blocked', str(exc)[:250])
                    self.store.event('reply_needs_attention', cid, str(exc))
            elif pending.get('status') == 'needs_attention':
                self._reply_stage('needs_attention', pending.get('reason', '回复需要人工处理'))
            elif incoming and pending and not agent.get_ai_api_key(self.config_provider()):
                self._reply_stage('waiting_model', '新消息等待配置模型')
            else:
                self._reply_stage('idle', '本轮检查完成，等待下一次轮询')
            return {"changed": before["context_hash"] != after["context_hash"],
                    "conversation_id": cid, "last_success": self.monitor["last_success"]}
        except (TaskCancelled, AccountPauseError) as exc:
            self._reply_stage('paused', str(exc)[:250])
            raise
        except Exception as exc:
            self._reply_stage('failed', str(exc)[:250])
            raise
        finally:
            progress = self.store.setting('reply_progress:' + cid, {})
            progress.update(active=False, updated_at=now(), next_check_at=time.time() + self.monitor['interval_seconds'])
            progress.pop('wait_until', None)
            self.store.set_setting('reply_progress:' + cid, progress)
            self._reply_progress_local.cid = None
            self.monitor['processing_conversation_id'] = None
            self._persist_monitor_state()
