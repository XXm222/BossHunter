"""Published-position selection and account-wide greeting budget.

Discovery imports never grant permission to contact. Platform job identifiers are required for synchronization.
"""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from .store import encode, now

QUOTA_TTL_SECONDS = 600


def day_key():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


class RecruitingJobs:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS published_jobs (
                    id TEXT PRIMARY KEY, platform_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS greeting_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                    job_id TEXT NOT NULL, candidate_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')
            db.execute('BEGIN IMMEDIATE')
            cols = [r[1] for r in db.execute("PRAGMA table_info(greeting_attempts)").fetchall()]
            if 'name' not in cols:
                db.execute("ALTER TABLE greeting_attempts ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            if 'owner' not in cols:
                db.execute("ALTER TABLE greeting_attempts ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            for row in db.execute("SELECT id,owner FROM greeting_attempts WHERE status='sending'").fetchall():
                if not store.owner_alive(row['owner']):
                    db.execute("UPDATE greeting_attempts SET status='uncertain' WHERE id=? AND status='sending'", (row['id'],))

    def config(self):
        return self.store.setting('greeting_budget', {'mode': 'platform', 'limit': 100})

    @staticmethod
    def quota_fresh(quota, day):
        if quota.get('date') != day:
            return False
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(quota['updated_at'])).total_seconds()
            return 0 <= age <= QUOTA_TTL_SECONDS
        except (KeyError, ValueError, TypeError):
            return False

    @staticmethod
    def _platform_remaining(quota, day, used):
        """根据额度读取时间戳和之后的本地发送数，计算平台剩余额度。

        unlimited 时返回 None（不限）；remaining 缺失或跨日过期返回 None（需重新读取）；
        否则返回 remaining 减去「读取之后新增的本地发送」。
        """
        if not RecruitingJobs.quota_fresh(quota, day) or quota.get('unlimited'):
            return None
        remaining = quota.get('remaining')
        if remaining is None or quota.get('date') != day:
            return None
        since_read = max(0, used - quota.get('local_used_at_read', 0))
        return max(0, remaining - since_read)

    def _quota_blocked(self, config, quota, used, day):
        """返回额度阻止招呼的原因（字符串），不阻止返回 None。与 state() 的 blockers 口径一致。"""
        if config['mode'] == 'custom':
            if used >= config['limit']:
                return '今日招呼额度已用完'
            if not self.quota_fresh(quota, day):
                return '平台剩余额度未读取或已过期，请先读取额度'
            if quota.get('date') == day and not quota.get('unlimited'):
                remaining = self._platform_remaining(quota, day, used)
                if remaining is None:
                    return '平台剩余额度未知，请先读取额度'
                if remaining is not None and remaining <= 0:
                    return '平台剩余额度已用完，暂停主动招呼'
            return None
        # platform 模式
        if not self.quota_fresh(quota, day):
            return '平台剩余额度未读取或已过期，请先读取额度'
        if quota.get('unlimited'):
            return None
        remaining = self._platform_remaining(quota, day, used)
        if remaining is None:
            return '平台剩余额度未读取或已过期，请先读取额度'
        if remaining <= 0:
            return '今日招呼额度已用完'
        return None

    def save_budget(self, mode, limit):
        if mode not in {'platform', 'custom'}:
            raise ValueError('额度模式必须是平台额度或自定义')
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('每日自定义额度需为 1–10000 的整数')
        value = {'mode': mode, 'limit': limit}
        self.store.set_setting('greeting_budget', value)
        self.store.event('greeting_budget_saved', '', '每日招呼额度设置已保存；没有启动外发')
        return value

    def import_snapshot(self, snapshot):
        rows = snapshot.get('jobs', [])
        if snapshot.get('complete') is not True or len(rows) != snapshot.get('total'):
            raise ValueError('职位列表未完整读取，保留上次结果，停止更新勾选范围')
        prepared = []
        for job in rows:
            if not job.get('title') or not job.get('status') or not isinstance(job.get('details'), list):
                raise ValueError('职位信息不完整')
            platform_id = job.get('platform_id')
            if not isinstance(platform_id, str) or not platform_id:
                raise ValueError('缺少平台岗位唯一标识')
            ident = 'boss-' + platform_id
            prepared.append((ident, platform_id, job['title'], encode(job['details']), job['status']))
        if len({p[0] for p in prepared}) != len(prepared):
            raise ValueError('存在无法区分的同名同条件岗位，需核实平台标识后再同步')
        stamp = now()
        with self.store.db() as db:
            for ident, platform_id, title, details, status in prepared:
                db.execute('''INSERT INTO published_jobs(id,platform_id,title,details,status,last_seen)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                    details=excluded.details,status=excluded.status,last_seen=excluded.last_seen,
                    selected=CASE WHEN excluded.status='开放中' THEN published_jobs.selected ELSE 0 END''',
                    (ident, platform_id, title, details, status, stamp))
            present = {p[0] for p in prepared}
            for old in db.execute('SELECT id FROM published_jobs').fetchall():
                if old['id'] not in present:
                    db.execute("UPDATE published_jobs SET status='已不在列表',selected=0 WHERE id=?", (old['id'],))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('jobs_sync',?)", (encode({'synced_at': stamp, 'attempted_at': stamp, 'total': len(rows), 'error': ''}),))
        self.store.event('jobs_synced', '', f'完整读取 {len(rows)} 个岗位；新增岗位默认未勾选')
        return self.state()

    def select(self, identifiers):
        if not isinstance(identifiers, list) or any(not isinstance(i, str) for i in identifiers) or len(identifiers) != len(set(identifiers)):
            raise ValueError('请提交不重复的岗位标识列表')
        with self.store.db() as db:
            available = {r['id'] for r in db.execute("SELECT id FROM published_jobs WHERE status='开放中'")}
            if not set(identifiers) <= available:
                raise ValueError('只能勾选已同步且处于开放中的岗位，请重新同步')
            db.execute('UPDATE published_jobs SET selected=0')
            db.executemany('UPDATE published_jobs SET selected=1 WHERE id=?', [(i,) for i in identifiers])
        self.store.event('jobs_selected', '', f'人工选择 {len(identifiers)} 个岗位；未勾选岗位不进入自动任务')
        return self.state()

    def reserve_greeting(self, job_id, candidate_id, name=""):
        """点击前记录 sending 占用：同一事务里先检查额度与待核实，再占用候选人。

        BEGIN IMMEDIATE 让「检查额度 + 插入占用」原子，避免两个任务同时通过检查、
        并发超发；候选人唯一约束则防止同一候选人被并发重复占用。
        """
        day = day_key()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_selected(db, job_id)
            if self.store.task_active(db, 'greeting-quota'):
                raise ValueError('正在核对平台额度，请稍后重试招呼')
            config_raw = db.execute("SELECT value FROM settings WHERE key='greeting_budget'").fetchone()
            config = json.loads(config_raw[0]) if config_raw else {'mode': 'platform', 'limit': 100}
            quota_raw = db.execute("SELECT value FROM settings WHERE key='greeting_quota'").fetchone()
            quota = json.loads(quota_raw[0]) if quota_raw else {}
            used = db.execute("SELECT count(*) FROM greeting_attempts WHERE day=?", (day,)).fetchone()[0]
            unresolved = db.execute("SELECT count(*) FROM greeting_attempts WHERE status IN ('sending','uncertain')").fetchone()[0]
            if unresolved:
                raise ValueError('存在发送结果待核实的招呼，暂停继续外发')
            if db.execute("SELECT 1 FROM outbox WHERE status IN ('sending','uncertain')").fetchone():
                raise ValueError('存在消息发送结果待核实，暂停继续外发')
            blocked = self._quota_blocked(config, quota, used, day)
            if blocked:
                raise ValueError(blocked)
            cur = db.execute('INSERT OR IGNORE INTO greeting_attempts(day, job_id, candidate_id, name, status, created_at, owner) VALUES (?,?,?,?,?,?,?)',
                             (day, job_id, candidate_id, name, 'sending', now(), self.store.owner))
            if cur.rowcount == 0:
                raise ValueError('该候选人已被占用，请核实后重试')
        self.store.event('greeting_reserved', candidate_id, f'岗位 {job_id} 已占用，准备打招呼')
        return self.state()

    @staticmethod
    def require_selected(db, job_id):
        job = db.execute('SELECT * FROM published_jobs WHERE id=?', (job_id,)).fetchone()
        if not job or not job['platform_id']:
            raise ValueError('岗位平台身份尚未关联，请先核实岗位')
        if not job['selected']:
            raise ValueError('该岗位未人工勾选，不能自动处理')
        if job['status'] != '开放中':
            raise ValueError('该岗位当前不是开放中，不能自动处理')

    def check_position(self, job_id):
        with self.store.db() as db:
            self.require_selected(db, job_id)

    def finish_greeting(self, candidate_id, status):
        """结束发送占用；cancelled 仅用于已确定未提交点击的任务。"""
        if status not in {'sent', 'uncertain', 'cancelled'}:
            raise ValueError('招呼结果必须是 sent、uncertain 或 cancelled')
        with self.store.db() as db:
            db.execute("UPDATE greeting_attempts SET status=? WHERE candidate_id=? AND status='sending'",
                       (status, candidate_id))
        self.store.event('greeting_recorded', candidate_id, f'招呼结果：{status}')
        return self.state()

    def state(self):
        day = day_key()
        with self.store.db() as db:
            jobs = [dict(r) for r in db.execute("SELECT * FROM published_jobs ORDER BY CASE status WHEN '开放中' THEN 0 ELSE 1 END,title")]
            counts = dict(db.execute('SELECT status,count(*) FROM greeting_attempts WHERE day=? GROUP BY status', (day,)).fetchall())
            attempts = [dict(r) for r in db.execute("SELECT id,day,job_id,candidate_id,name,status,created_at FROM greeting_attempts WHERE day=? OR status IN ('sending','uncertain') ORDER BY id DESC", (day,))]
            unresolved = db.execute("SELECT count(*) FROM greeting_attempts WHERE status IN ('sending','uncertain')").fetchone()[0]
        for job in jobs:
            job['details'] = json.loads(job['details'])
        config = self.config()
        quota = self.store.setting('greeting_quota', {})
        used = sum(counts.values())
        selected = [j for j in jobs if j['selected']]
        platform_remaining = self._platform_remaining(quota, day, used)
        quota_status = ('unread' if not quota else 'expired' if not self.quota_fresh(quota, day)
                        else 'fresh' if quota.get('unlimited') or platform_remaining is not None else 'unknown')
        blockers = []
        if not selected:
            blockers.append('请先勾选允许自动处理的开放岗位')
        if any(not j['platform_id'] for j in selected):
            blockers.append('已选岗位的平台唯一标识尚未核实')
        if unresolved:
            blockers.append('存在发送结果待核实的招呼，暂停继续外发')
        can_start = not blockers
        if config['mode'] == 'custom' and used >= config['limit']:
            can_start = False
        if selected:
            blocked = self._quota_blocked(config, quota, used, day)
            if blocked:
                blockers.append(blocked)
                if self.quota_fresh(quota, day):
                    can_start = False
        return {'jobs': jobs, 'sync': self.store.setting('jobs_sync', {}), 'budget': config,
                'daily': {'date': day, 'timezone': 'Asia/Shanghai', 'attempted': used,
                          'sent': counts.get('sent', 0), 'uncertain': counts.get('uncertain', 0),
                          'platform_remaining': platform_remaining,
                          'custom_remaining': max(0, config['limit'] - used) if config['mode'] == 'custom' else None,
                          'quota_unlimited': bool(quota.get('unlimited')),
                          'quota_date': quota.get('date'), 'quota_status': quota_status,
                          'quota_updated_at': quota.get('updated_at'),
                          'quota_last_remaining': quota.get('remaining')},
                'attempts': attempts,
                'selected_count': len(selected), 'running': False, 'can_start': can_start, 'blockers': blockers}
