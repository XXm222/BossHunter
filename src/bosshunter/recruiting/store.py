"""Small, isolated recruiting store; no jobseeker tables or credentials."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def candidate_question(snapshot):
    """Latest candidate text still awaiting a text reply."""
    for message in reversed(snapshot.get('messages', [])):
        if message['direction'] == 'out' and message['kind'] == 'text':
            return None
        if message['direction'] == 'in' and message['kind'] == 'text':
            return message
    return None


def conversation_hash(snapshot):
    """会话内容指纹：只取会话标识、关联岗位与消息数组，读取源无关。

    import_conversation 与发送前的变更检测共用这一口径，避免浏览器 DOM 与
    HTTP 接口消息结构不同导致 context_hash 无法直接比对。
    """
    return fingerprint({k: snapshot[k] for k in ("id", "position_title", "messages")})


def merge_messages(previous, snapshot):
    """Preserve saved history; platform IDs identify messages, timestamps order them."""
    snapshot = dict(snapshot)
    merged, unidentified = {}, {}
    for m in (previous.get('messages') or []) + (snapshot.get('messages') or []):
        if str(m.get('id', '')).isdigit():
            merged[str(m['id'])] = m
        else:
            unidentified[json.dumps(m, ensure_ascii=False, sort_keys=True)] = m
    ordered = sorted(merged.values(), key=lambda m: (m.get('timestamp', 0), int(m['id'])))
    snapshot['messages'] = list(unidentified.values()) + ordered
    snapshot['unidentified_history_count'] = len(unidentified)
    for key in ('position_platform_id', 'account_uid'):
        snapshot[key] = snapshot.get(key) or previous.get(key)
    attachment_ids = {previous.get('received_resume_message_id'), snapshot.get('received_resume_message_id')} - {None}
    attachment_messages = [m for m in ordered if m['id'] in attachment_ids]
    snapshot['received_resume_message_id'] = (attachment_messages[-1]['id'] if attachment_messages else
                                              snapshot.get('received_resume_message_id') or previous.get('received_resume_message_id'))
    return snapshot


class TaskBusy(ValueError):
    """Another live task owns this resource; do not convert it into a failure."""


class RequestThrottled(ValueError):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__('招聘端请求仍在冷却')


class RequestPaused(ValueError):
    """A platform refusal established a shared cooldown."""


class Store:
    def __init__(self, path: Path):
        self.owner = f'{os.getpid()}:{uuid4().hex}'
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, jd TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, position_id TEXT NOT NULL,
                snapshot TEXT NOT NULL, context_hash TEXT NOT NULL,
                taken_over INTEGER NOT NULL DEFAULT 0, do_not_contact INTEGER NOT NULL DEFAULT 0,
                auto_send INTEGER NOT NULL DEFAULT 0,
                binding_confirmed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, source TEXT NOT NULL,
                text TEXT NOT NULL, complete INTEGER NOT NULL, meta TEXT NOT NULL,
                content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(conversation_id, source, content_hash)
            );
            CREATE TABLE IF NOT EXISTS knowledge (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
                position_id TEXT NOT NULL DEFAULT '', keywords TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL, public INTEGER NOT NULL DEFAULT 0,
                approved INTEGER NOT NULL DEFAULT 0, valid_until TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, input_hash TEXT NOT NULL UNIQUE,
                result TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, kind TEXT NOT NULL,
                content TEXT NOT NULL, context_hash TEXT NOT NULL, refs TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(conversation_id, kind, context_hash, content)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                object_id TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_claims (
                key TEXT PRIMARY KEY, owner TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """)
            db.execute('BEGIN IMMEDIATE')
            cols = [r[1] for r in db.execute("PRAGMA table_info(conversations)").fetchall()]
            if 'auto_send' not in cols:
                db.execute("ALTER TABLE conversations ADD COLUMN auto_send INTEGER NOT NULL DEFAULT 0")
            if 'binding_confirmed' not in cols:
                db.execute("ALTER TABLE conversations ADD COLUMN binding_confirmed INTEGER NOT NULL DEFAULT 0")
                # Only explicit binding events prove that an old record was
                # confirmed. Contact discovery or job linking is not binding.
                db.execute("UPDATE conversations SET binding_confirmed=1 WHERE id IN (SELECT object_id FROM events WHERE kind='conversation_bound')")
            if 'owner' not in [r[1] for r in db.execute('PRAGMA table_info(outbox)')]:
                db.execute("ALTER TABLE outbox ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def request_daily_limit(self, default):
        from zoneinfo import ZoneInfo
        override = self.setting('request_daily_override', {})
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        if (isinstance(override, dict) and override.get('date') == day
                and type(override.get('limit')) is int and 1 <= override['limit'] <= 10000):
            return override['limit']
        return default

    def set_setting(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, encode(value)))

    @staticmethod
    def owner_alive(owner):
        try:
            pid = int(str(owner).split(':', 1)[0])
            if pid <= 0:
                return False
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (ValueError, TypeError, ProcessLookupError):
            return False

    @classmethod
    def task_active(cls, db, key):
        row = db.execute('SELECT owner FROM task_claims WHERE key=?', (key,)).fetchone()
        return bool(row and cls.owner_alive(row['owner']))

    @contextmanager
    def task(self, key):
        # Process identity prevents another Web/worker instance stealing live work.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.task_active(db, key):
                raise TaskBusy('该任务正在其他请求或进程中处理，请稍后重试')
            db.execute('INSERT OR REPLACE INTO task_claims VALUES (?,?,?)', (key, self.owner, now()))
        try:
            yield
        finally:
            with self.db() as db:
                db.execute('DELETE FROM task_claims WHERE key=? AND owner=?', (key, self.owner))

    def request_budget(self):
        """读取当日请求预算；跨日返回全新预算。"""
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        budget = self.setting('request_budget', {})
        if budget.get('date') != day:
            return {'date': day, 'count': 0, 'by_kind': {}}
        return budget

    def pause_requests(self, seconds):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM settings WHERE key='request_paused_until'").fetchone()
            previous = json.loads(row[0]) if row else 0
            until = max(previous, datetime.now(timezone.utc).timestamp() + seconds)
            db.execute("INSERT OR REPLACE INTO settings VALUES ('request_paused_until',?)", (encode(until),))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('monitor_enabled','false')")
        return until

    def count_request(self, kind, daily_limit, *, min_interval=0, pace=True, page_interval=0):
        """计数一次后台 HTTP 请求；超限抛 ValueError。

        计数存 DB（settings.request_budget），重启不重置、Web 与 worker 两进程共用；
        并按 kind 记录每类请求的数量，便于看出会话/岗位/附件/额度各自占用。
        BEGIN IMMEDIATE 让「读计数 → 加一 → 写回」原子，避免两个进程同时计数时丢一次递增。
        """
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            paused = db.execute("SELECT value FROM settings WHERE key='request_paused_until'").fetchone()
            if paused and json.loads(paused[0]) > datetime.now(timezone.utc).timestamp():
                raise RequestPaused('BOSS 请求拒绝或限流后的冷却尚未结束；请核实账号，冷却结束后再手动开启任务')
            row = db.execute("SELECT value FROM settings WHERE key='request_budget'").fetchone()
            budget = json.loads(row[0]) if row else {'date': day, 'count': 0, 'by_kind': {}}
            if budget.get('date') != day:
                budget = {'date': day, 'count': 0, 'by_kind': {}}
            if budget['count'] >= daily_limit:
                raise ValueError(f'后台请求达到单日上限 {daily_limit} 次，请明日再试')
            stamp = datetime.now(timezone.utc).timestamp()
            remaining = 0
            operation_key = 'request_greeting_next_at' if kind == 'greeting' else 'request_next_at'
            if pace and min_interval > 0:
                slot = db.execute("SELECT value FROM settings WHERE key=?", (operation_key,)).fetchone()
                remaining = max(remaining, (json.loads(slot[0]) if slot else 0) - stamp)
            if page_interval > 0:
                page_slot = db.execute("SELECT value FROM settings WHERE key='request_page_next_at'").fetchone()
                remaining = max(remaining, (json.loads(page_slot[0]) if page_slot else 0) - stamp)
            if remaining > 0:
                raise RequestThrottled(remaining)
            budget['count'] += 1
            budget['by_kind'][kind] = budget['by_kind'].get(kind, 0) + 1
            db.execute("INSERT OR REPLACE INTO settings VALUES ('request_budget', ?)", (encode(budget),))
            if pace and min_interval > 0:
                db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (operation_key, encode(stamp + min_interval)))
            if page_interval > 0:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('request_page_next_at', ?)", (encode(stamp + page_interval),))
        return budget

    def event(self, kind, object_id="", detail=""):
        with self.db() as db:
            db.execute("INSERT INTO events(kind,object_id,detail,created_at) VALUES (?,?,?,?)",
                       (kind, object_id, detail[:1000], now()))

    def row(self, table, ident):
        if table not in {"positions", "conversations", "documents", "knowledge", "assessments", "outbox"}:
            raise ValueError("未知数据类型")
        with self.db() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ValueError("记录不存在")
            return dict(row)

    def rows(self, table, where="", args=()):
        if table not in {"positions", "conversations", "documents", "knowledge", "assessments", "outbox", "events"}:
            raise ValueError("未知数据类型")
        with self.db() as db:
            return [dict(r) for r in db.execute(f"SELECT * FROM {table} {where}", args)]

    def state_snapshot(self):
        """Read dashboard records and document selections in one read transaction."""
        ordering = {'documents': 'ORDER BY created_at DESC,rowid DESC',
                    'assessments': 'ORDER BY created_at DESC,rowid DESC',
                    'outbox': 'ORDER BY created_at DESC,rowid DESC',
                    'events': 'ORDER BY id DESC LIMIT 30'}
        with self.db() as db:
            db.execute('BEGIN')
            result = {table: [dict(row) for row in db.execute(f'SELECT * FROM {table} {ordering.get(table, "")}')]
                      for table in ('positions', 'conversations', 'documents', 'assessments', 'outbox', 'events')}
            result['settings'] = {row['key']: json.loads(row['value']) for row in db.execute('SELECT * FROM settings')}
            return result

    def pause_monitor_run(self, revision):
        """Only a failure from the current run may pause that run."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM settings WHERE key='monitor_run_id'").fetchone()
            if (json.loads(row[0]) if row else None) != revision:
                return False
            db.execute("INSERT OR REPLACE INTO settings VALUES ('monitor_enabled','false')")
            return True

    def save_position(self, ident, title, jd, source="manual", enabled=True):
        if not title.strip() or len(jd) > 30000:
            raise ValueError("请填写岗位名称，JD 最多 30000 字")
        with self.db() as db:
            old = db.execute("SELECT * FROM positions WHERE id=?", (ident,)).fetchone()
            version = old["version"] + 1 if old and (old["jd"] != jd or old["title"] != title or bool(old["enabled"]) != enabled) else (old["version"] if old else 1)
            db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?)",
                       (ident, title.strip(), jd.strip(), source, int(enabled), version, now()))
            if old and version != old["version"]:
                db.execute("UPDATE outbox SET status='expired',updated_at=? WHERE status='draft' AND conversation_id IN (SELECT id FROM conversations WHERE position_id=?)", (now(), ident))
        return self.row("positions", ident)

    def import_conversation(self, snapshot, *, append=False, confirmed=False, track_reply=False):
        ident = snapshot["id"]
        if not snapshot.get("position_title") or not ident:
            raise ValueError("缺少会话标识或沟通职位")
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute("SELECT position_id,snapshot,binding_confirmed FROM conversations WHERE id=?", (ident,)).fetchone()
            if append and old:
                snapshot = merge_messages(json.loads(old['snapshot']), snapshot)
            if old:
                pid = old[0]
                platform_id = snapshot.get('position_platform_id')
                if platform_id and pid != 'boss-' + str(platform_id):
                    if pid.startswith('context-') and not json.loads(old['snapshot']).get('position_platform_id'):
                        raise ValueError('该会话尚未关联平台岗位，请先人工确认并关联岗位，再绑定会话')
                    raise ValueError('会话平台岗位身份已变化，请先人工重新关联岗位')
            else:
                platform_id = snapshot.get("position_platform_id")
                if platform_id:
                    # 按平台岗位唯一 ID 关联，同名但不同职责/地点的岗位不会共用 JD
                    pid = "boss-" + str(platform_id)
                else:
                    # 读不到平台岗位 ID：每个会话独立岗位，不按名称复用，避免同名岗位误共用
                    pid = "context-" + fingerprint([ident])[:16]
            pos = db.execute("SELECT title FROM positions WHERE id=?", (pid,)).fetchone()
            if pos and pos[0] != snapshot["position_title"]:
                raise ValueError("会话关联职位已变化，请先人工核对")
            if not pos:
                db.execute("INSERT INTO positions(id,title,source,updated_at) VALUES (?,?,?,?)",
                           (pid, snapshot["position_title"], "conversation_context", now()))
            context_hash = conversation_hash(snapshot)
            db.execute("""INSERT INTO conversations(id,name,position_id,snapshot,context_hash,updated_at,binding_confirmed)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                snapshot=excluded.snapshot,context_hash=excluded.context_hash,updated_at=excluded.updated_at,
                binding_confirmed=MAX(conversations.binding_confirmed,excluded.binding_confirmed)""",
                       (ident, snapshot["name"], pid, encode(snapshot), context_hash, now(), int(confirmed)))
            db.execute("UPDATE outbox SET status='expired',updated_at=? WHERE conversation_id=? AND context_hash<>? AND status='draft'", (now(), ident, context_hash))
            if track_reply and old and old['binding_confirmed']:
                target = candidate_question(snapshot)
                if target and fingerprint(target) != fingerprint(candidate_question(json.loads(old['snapshot']))):
                    # Persist the work in the same transaction as the messages:
                    # a manual sync cannot consume the worker's change signal.
                    pending = {'status': 'waiting', 'context_hash': context_hash}
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('reply_work:' + ident, encode(pending)))
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('reply_progress:' + ident, encode(
                        {'stage': 'queued', 'message': '已发现新消息，等待轮询处理', 'active': False, 'updated_at': now()})))
            # 首个导入的会话自动设为当前（保持向后兼容）；后续导入不改变当前，由 select_conversation 显式切换
            if not db.execute("SELECT 1 FROM settings WHERE key='pilot_conversation'").fetchone():
                db.execute("INSERT OR REPLACE INTO settings VALUES ('pilot_conversation',?)", (encode(ident),))
        return self.row("conversations", ident)

    def link_position(self, cid, platform_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            c = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
            job = db.execute('SELECT * FROM published_jobs WHERE platform_id=?', (platform_id,)).fetchone()
            if not c or not job:
                raise ValueError('会话或平台岗位不存在，请先同步岗位')
            if db.execute("SELECT 1 FROM outbox WHERE conversation_id=? AND status IN ('sending','uncertain')", (cid,)).fetchone():
                raise ValueError('存在发送结果待核实，请先核实再重新关联')
            db.execute("INSERT OR IGNORE INTO positions(id,title,source,updated_at) VALUES (?,?,?,?)",
                       (job['id'], job['title'], 'published_job_binding', now()))
            position = db.execute('SELECT title FROM positions WHERE id=?', (job['id'],)).fetchone()
            if position['title'] != job['title']:
                db.execute('UPDATE positions SET title=?,version=version+1,updated_at=? WHERE id=?', (job['title'], now(), job['id']))
                db.execute("UPDATE outbox SET status='expired',updated_at=? WHERE status='draft' AND conversation_id IN (SELECT id FROM conversations WHERE position_id=?)", (now(), job['id']))
            snapshot = json.loads(c['snapshot'])
            snapshot.update(position_title=job['title'], position_platform_id=platform_id)
            db.execute('UPDATE conversations SET position_id=?,snapshot=?,context_hash=?,auto_send=0,updated_at=? WHERE id=?',
                       (job['id'], encode(snapshot), conversation_hash(snapshot), now(), cid))
            db.execute("UPDATE outbox SET status='expired',updated_at=? WHERE conversation_id=? AND status='draft'", (now(), cid))
        self.event('position_reassociated', cid, '人工核实平台岗位后重新关联；未复制旧 JD，自动外发已关闭')
        return self.row('conversations', cid)

    def select_conversation(self, cid):
        """切换当前选中的会话（回复/评分/读简历等操作都针对它）。"""
        self.row("conversations", cid)  # 校验会话存在
        self.set_setting("pilot_conversation", cid)
        return self.row("conversations", cid)

    def save_document(self, cid, source, text, complete, meta):
        self.row("conversations", cid)
        digest = fingerprint([text, complete, meta])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,?)",
                       (uuid4().hex, cid, source, text[:100000], int(complete), encode(meta), digest, now()))
            row = db.execute("SELECT * FROM documents WHERE conversation_id=? AND source=? AND content_hash=?", (cid, source, digest)).fetchone()
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('current_document:' + cid, encode(row['id'])))
            return dict(row)

    def current_document(self, cid):
        with self.db() as db:
            db.execute('BEGIN')
            selected = db.execute('SELECT value FROM settings WHERE key=?', ('current_document:' + cid,)).fetchone()
            row = None
            if selected:
                row = db.execute('SELECT * FROM documents WHERE conversation_id=? AND id=?', (cid, json.loads(selected['value']))).fetchone()
            # Existing databases used the latest created document before explicit selection.
            if row is None:
                row = db.execute('SELECT * FROM documents WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (cid,)).fetchone()
            return dict(row) if row else None

    def draft(self, cid, kind, content, refs):
        if kind not in {"reply", "request_resume", "accept_resume", "invitation"}:
            raise ValueError("不支持的动作")
        c = self.row("conversations", cid)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO outbox(id,conversation_id,kind,content,context_hash,refs,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                       (uuid4().hex, cid, kind, content, c["context_hash"], encode(refs), now(), now()))
            row = db.execute("SELECT * FROM outbox WHERE conversation_id=? AND kind=? AND context_hash=? AND content=?", (cid, kind, c["context_hash"], content)).fetchone()
            # Identical wording can have different authority and evidence. Update
            # unsubmitted drafts atomically; never revive submitted/cancelled actions.
            changed_basis = row['refs'] != encode(refs)
            # Re-preparation can produce the same valid inputs and text after a
            # takeover. Expired is unsubmitted, unlike cancelled/uncertain/sent.
            if row['status'] == 'expired' or (row['status'] == 'draft' and changed_basis):
                db.execute("UPDATE outbox SET refs=?,status='draft',result='已重新准备，请重新核对',updated_at=? WHERE id=?", (encode(refs), now(), row["id"]))
                row = db.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone()
            return dict(row)

    def recover_outbox(self):
        with self.db() as db:
            for row in db.execute("SELECT id,owner FROM outbox WHERE status='sending'").fetchall():
                if not self.owner_alive(row['owner']):
                    db.execute("UPDATE outbox SET status='uncertain',result='进程中断，请人工核实平台结果',updated_at=? WHERE id=? AND status='sending'", (now(), row['id']))

    def claim(self, ident, *, auto=False, daily_limit=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] != "draft":
                raise ValueError("动作已处理或结果待核实，不能重复执行")
            if row["kind"] == "invitation":
                raise ValueError("本次测试禁止发送面试邀约（执行层锁定）")
            if db.execute("SELECT 1 FROM outbox WHERE status IN ('sending','uncertain')").fetchone():
                raise ValueError('存在发送结果待核实的动作，暂停继续外发')
            if db.execute("SELECT 1 FROM greeting_attempts WHERE status IN ('sending','uncertain')").fetchone():
                raise ValueError('存在招呼结果待核实，暂停继续外发')
            c = db.execute('SELECT * FROM conversations WHERE id=?', (row['conversation_id'],)).fetchone()
            p = db.execute('SELECT * FROM positions WHERE id=?', (c['position_id'],)).fetchone()
            if c['do_not_contact'] or (auto and (c['taken_over'] or not p['enabled'])):
                raise ValueError('已人工接管、停止联系或岗位暂停')
            refs = json.loads(row['refs'])
            if refs.get('needs_human'):
                raise ValueError('草稿需要人工处理，请先编辑核实后重新保存')
            if c['context_hash'] != row['context_hash'] or (refs.get('position_version') is not None and refs['position_version'] != p['version']):
                raise ValueError('会话或岗位依据已变化，请重新生成')
            if auto:
                from .jobs import RecruitingJobs, day_key
                RecruitingJobs.require_selected(db, c['position_id'])
                if not c['auto_send']:
                    raise ValueError('该会话已关闭自动外发')
                if row['kind'] == 'reply':
                    day = day_key()
                    value = db.execute("SELECT value FROM settings WHERE key='auto_reply_sent'").fetchone()
                    count = json.loads(value[0]) if value else {}
                    if count.get('date') != day:
                        count = {'date': day, 'count': 0}
                    configured = db.execute("SELECT value FROM settings WHERE key='auto_reply_daily_limit'").fetchone()
                    limit = json.loads(configured[0]) if configured else daily_limit
                    if limit and count['count'] >= limit:
                        raise ValueError('今日自动回复已达上限，请人工处理')
                    count['count'] += 1
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('auto_reply_sent',?)", (encode(count),))
            db.execute("UPDATE outbox SET status='sending',owner=?,updated_at=? WHERE id=?", (self.owner, now(), ident))
            return dict(row)

    def cancel_draft(self, ident):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE outbox SET status='cancelled',result='本地用户取消草稿',updated_at=? WHERE id=? AND status='draft'", (now(), ident))
            if changed.rowcount != 1:
                raise ValueError('只能取消未执行草稿；动作已被领取或处理，请核对状态')
            return dict(db.execute('SELECT * FROM outbox WHERE id=?', (ident,)).fetchone())

    def finish(self, ident, status, result):
        with self.db() as db:
            db.execute("UPDATE outbox SET status=?,result=?,updated_at=? WHERE id=?", (status, result[:1000], now(), ident))

    def resolve_outbound(self, kind, ident, outcome, evidence):
        if kind not in {'reply', 'greeting'} or outcome not in {'sent', 'not_sent'}:
            raise ValueError('请选择有效的核实对象与结果')
        if not isinstance(evidence, str) or not 5 <= len(evidence.strip()) <= 1000:
            raise ValueError('请填写 5–1000 字的平台核实依据')
        table = 'outbox' if kind == 'reply' else 'greeting_attempts'
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
            if not row or row['status'] not in {'uncertain', 'sending'}:
                raise ValueError('记录不在待核实状态')
            if row['status'] == 'sending' and self.owner_alive(row['owner']):
                raise ValueError('原发送任务仍在运行，不能覆盖执行结果')
            status = 'sent' if outcome == 'sent' else 'cancelled'
            if kind == 'reply':
                db.execute('UPDATE outbox SET status=?,result=?,updated_at=? WHERE id=?',
                           (status, '人工核实：' + evidence.strip(), now(), ident))
            else:
                db.execute('UPDATE greeting_attempts SET status=? WHERE id=?', (status, ident))
            db.execute('INSERT INTO events(kind,object_id,detail,created_at) VALUES (?,?,?,?)',
                       ('outbound_resolved', str(ident), f'{kind} 人工核实为 {outcome}：{evidence.strip()}', now()))
        return {'status': status, 'retried': False}
