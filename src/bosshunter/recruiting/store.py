"""Small, isolated recruiting store; no jobseeker tables or credentials."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path: Path):
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
            """)
            cols = [r[1] for r in db.execute("PRAGMA table_info(conversations)").fetchall()]
            if 'auto_send' not in cols:
                db.execute("ALTER TABLE conversations ADD COLUMN auto_send INTEGER NOT NULL DEFAULT 0")
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

    def set_setting(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, encode(value)))

    def request_budget(self):
        """读取当日请求预算；跨日返回全新预算。"""
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        budget = self.setting('request_budget', {})
        if budget.get('date') != day:
            return {'date': day, 'count': 0, 'by_kind': {}}
        return budget

    def count_request(self, kind, daily_limit):
        """计数一次后台 HTTP 请求；超限抛 ValueError。

        计数存 DB（settings.request_budget），重启不重置、Web 与 worker 两进程共用；
        并按 kind 记录每类请求的数量，便于看出会话/岗位/附件/额度各自占用。
        """
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='request_budget'").fetchone()
            budget = json.loads(row[0]) if row else {'date': day, 'count': 0, 'by_kind': {}}
            if budget.get('date') != day:
                budget = {'date': day, 'count': 0, 'by_kind': {}}
            if budget['count'] >= daily_limit:
                raise ValueError(f'后台请求达到单日上限 {daily_limit} 次，请明日再试')
            budget['count'] += 1
            budget['by_kind'][kind] = budget['by_kind'].get(kind, 0) + 1
            db.execute("INSERT OR REPLACE INTO settings VALUES ('request_budget', ?)", (encode(budget),))
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

    def import_conversation(self, snapshot):
        ident = snapshot["id"]
        if not snapshot.get("position_title") or not ident:
            raise ValueError("缺少会话标识或沟通职位")
        with self.db() as db:
            old = db.execute("SELECT position_id FROM conversations WHERE id=?", (ident,)).fetchone()
            if old:
                pid = old[0]
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
            context_hash = fingerprint({k: snapshot[k] for k in ("id", "position_title", "messages")})
            db.execute("""INSERT INTO conversations(id,name,position_id,snapshot,context_hash,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                snapshot=excluded.snapshot,context_hash=excluded.context_hash,updated_at=excluded.updated_at""",
                       (ident, snapshot["name"], pid, encode(snapshot), context_hash, now()))
            db.execute("UPDATE outbox SET status='expired',updated_at=? WHERE conversation_id=? AND context_hash<>? AND status='draft'", (now(), ident, context_hash))
            # 首个导入的会话自动设为当前（保持向后兼容）；后续导入不改变当前，由 select_conversation 显式切换
            if not db.execute("SELECT 1 FROM settings WHERE key='pilot_conversation'").fetchone():
                db.execute("INSERT OR REPLACE INTO settings VALUES ('pilot_conversation',?)", (encode(ident),))
        return self.row("conversations", ident)

    def select_conversation(self, cid):
        """切换当前选中的会话（回复/评分/读简历等操作都针对它）。"""
        self.row("conversations", cid)  # 校验会话存在
        self.set_setting("pilot_conversation", cid)
        return self.row("conversations", cid)

    def save_document(self, cid, source, text, complete, meta):
        self.row("conversations", cid)
        digest = fingerprint([text, complete, meta])
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,?)",
                       (uuid4().hex, cid, source, text[:100000], int(complete), encode(meta), digest, now()))
            return dict(db.execute("SELECT * FROM documents WHERE conversation_id=? AND source=? AND content_hash=?", (cid, source, digest)).fetchone())

    def draft(self, cid, kind, content, refs):
        if kind not in {"reply", "request_resume", "accept_resume", "invitation"}:
            raise ValueError("不支持的动作")
        c = self.row("conversations", cid)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO outbox(id,conversation_id,kind,content,context_hash,refs,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                       (uuid4().hex, cid, kind, content, c["context_hash"], encode(refs), now(), now()))
            row = db.execute("SELECT * FROM outbox WHERE conversation_id=? AND kind=? AND context_hash=? AND content=?", (cid, kind, c["context_hash"], content)).fetchone()
            # Explicit regeneration may yield identical wording with updated facts.
            # Only revive an unattempted expired draft, never uncertain/sent actions.
            if row["status"] == "expired" and row["refs"] != encode(refs):
                db.execute("UPDATE outbox SET refs=?,status='draft',result='依据更新后重新生成，请重新核对',updated_at=? WHERE id=?", (encode(refs), now(), row["id"]))
                row = db.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone()
            return dict(row)

    def recover_outbox(self):
        with self.db() as db:
            db.execute("UPDATE outbox SET status='uncertain',result='进程中断，请人工核实平台结果',updated_at=? WHERE status='sending'", (now(),))

    def claim(self, ident):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] != "draft":
                raise ValueError("动作已处理或结果待核实，不能重复执行")
            if row["kind"] == "invitation":
                raise ValueError("本次测试禁止发送面试邀约（执行层锁定）")
            db.execute("UPDATE outbox SET status='sending',updated_at=? WHERE id=?", (now(), ident))
            return dict(row)

    def finish(self, ident, status, result):
        with self.db() as db:
            db.execute("UPDATE outbox SET status=?,result=?,updated_at=? WHERE id=?", (status, result[:1000], now(), ident))
