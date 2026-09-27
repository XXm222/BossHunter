"""Published-position selection and account-wide greeting budget.

Discovery imports never grant permission to contact. Platform job identifiers are required for synchronization.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from .store import encode, now


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
                    status TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')
            db.execute("UPDATE greeting_attempts SET status='uncertain' WHERE status='sending'")

    def config(self):
        return self.store.setting('greeting_budget', {'mode': 'platform', 'limit': 100})

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

    def state(self):
        day = day_key()
        with self.store.db() as db:
            jobs = [dict(r) for r in db.execute("SELECT * FROM published_jobs ORDER BY CASE status WHEN '开放中' THEN 0 ELSE 1 END,title")]
            counts = dict(db.execute('SELECT status,count(*) FROM greeting_attempts WHERE day=? GROUP BY status', (day,)).fetchall())
            attempts = [dict(r) for r in db.execute('SELECT id,job_id,candidate_id,status,created_at FROM greeting_attempts WHERE day=? ORDER BY id DESC', (day,))]
            unresolved = db.execute("SELECT count(*) FROM greeting_attempts WHERE status IN ('sending','uncertain')").fetchone()[0]
        for job in jobs:
            job['details'] = json.loads(job['details'])
        config = self.config()
        used = sum(counts.values())
        selected = [j for j in jobs if j['selected']]
        blockers = []
        if not selected:
            blockers.append('请先勾选允许自动处理的开放岗位')
        if any(not j['platform_id'] for j in selected):
            blockers.append('已选岗位的平台唯一标识尚未核实')
        blockers.append('平台每日剩余额度与主动招呼执行尚未完成实测接通')
        if unresolved:
            blockers.append('存在发送结果待核实的招呼，暂停继续外发')
        return {'jobs': jobs, 'sync': self.store.setting('jobs_sync', {}), 'budget': config,
                'daily': {'date': day, 'timezone': 'Asia/Shanghai', 'attempted': used,
                          'sent': counts.get('sent', 0), 'uncertain': counts.get('uncertain', 0),
                          'platform_remaining': None,
                          'custom_remaining': max(0, config['limit'] - used) if config['mode'] == 'custom' else None},
                'attempts': attempts,
                'selected_count': len(selected), 'running': False, 'blockers': blockers}
