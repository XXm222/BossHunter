"""WS notification scheduling; HTTP remains the authoritative message reader.

All mutation uses a separate short lock: observation continues while the worker
waits for HTTP pacing or the model. Persist queue BEFORE advancing the cursor.
"""
import json
import re
import time
from threading import RLock


class HybridMonitor:
    def __init__(self, store):
        self.store = store
        self.lock = RLock()
        self.saved = store.setting('ws_monitor_queue', {})
        self.queue = self.saved.get('queue', {})
        self.seen = self.saved.get('seen', [])[-512:]
        self.cursor = 0
        self.epoch = None
        self.generation = None
        self._connected = False
        self.last_observed = 0
        self.allowed = set()
        self.serial = max([int(v) for v in self.queue.values()] + [0])
        self.state = {'transport': 'http', 'reason': '等待 WS 连接检查，使用 HTTP 轮询'}
        self.retry_at = {}

    @property
    def connected(self):
        # A hung/local observer thread must not silently disable HTTP forever.
        return self._connected and time.time() - self.last_observed < 35

    def _enqueue(self, cid):
        self.serial += 1
        self.queue[cid] = self.serial
        self.retry_at.pop(cid, None)

    def _save(self):
        self.store.set_setting('ws_monitor_queue', {'queue': self.queue, 'seen': self.seen[-512:]})
        self.state.update(updated_at=self.last_observed, pending_count=len(self.queue))
        self.store.set_setting('ws_monitor_state', self.state)

    def observe(self, result, rows):
        with self.lock:
            current = {r['id']: r for r in rows}
            self.queue = {cid: n for cid, n in self.queue.items() if cid in current}
            result = result or {'connected': False, 'reason': '浏览器或监听服务未连接，使用 HTTP 轮询'}
            if result.get('connected') and any(
                    not re.fullmatch(r'[1-9][0-9]*-[01]', cid)
                    or not json.loads(row['snapshot']).get('account_uid') for cid, row in current.items()):
                result = {**result, 'connected': False,
                          'reason': '部分授权会话缺少可核实的账号身份，保留 HTTP 轮询'}
            connected = result.get('connected') is True
            changed = (result.get('epoch'), result.get('generation')) != (self.epoch, self.generation)
            if connected and (not self.connected or changed or result.get('gap')):
                for cid in current:
                    self._enqueue(cid)  # Initial/reconnected/lost notification backfill.
            if connected:
                for cid in current.keys() - self.allowed:
                    self._enqueue(cid)
            # A fresh runtime can have lower sequence numbers; its retained events
            # are returned from zero on the next local poll as well as backfilled.
            for event in result.get('events', []):
                sender, recipient = event.get('from') or {}, event.get('to') or {}
                if not event.get('mid'):
                    continue
                # Match either direction using the bound account and the peer's
                # explicit source. Never infer candidate identity from a name.
                match = None
                for peer, owner in ((sender, recipient), (recipient, sender)):
                    source = str(peer.get('source'))
                    cid = f"{peer.get('uid')}-{source}"
                    row = current.get(cid)
                    if not row or source not in {'0', '1'}:
                        continue
                    account = str(json.loads(row['snapshot']).get('account_uid') or '')
                    if account and str(owner.get('uid')) == account and str(peer.get('uid')) != account:
                        match = cid, account
                        break
                if match is None:
                    continue
                cid, account = match
                key = f"{account}:{cid}:{event['mid']}"
                if key in self.seen:
                    continue
                self.seen = (self.seen + [key])[-512:]
                self._enqueue(cid)
            self._connected = connected
            self.last_observed = time.time()
            self.epoch, self.generation = result.get('epoch'), result.get('generation')
            self.allowed = set(current)
            self.state = {'transport': 'ws' if connected else 'http',
                          'reason': result.get('reason') or 'WS 不可用，使用 HTTP 轮询',
                          'connected': connected}
            self._save()
            self.cursor = int(result.get('cursor') or 0)

    def next_job(self, allowed, interval):
        with self.lock:
            if not self.connected:
                return None
            now = time.time()
            for cid, version in self.queue.items():
                if cid in allowed and now >= self.retry_at.get(cid, 0):
                    return cid, version
            # Drafts waiting for the manually selected browser conversation and
            # unfinished generation must keep progressing even without a new WS.
            for cid in allowed:
                row = self.store.row('conversations', cid)
                work = self.store.setting('reply_work:' + cid, {})
                if (not row['taken_over'] and work.get('status') in {'waiting', 'drafted'}
                        and now >= self.retry_at.get(cid, 0)):
                    return cid, None
            return None

    def finish(self, job, success, interval):
        if job is None:
            return
        cid, version = job
        with self.lock:
            # A notification arriving DURING sync must remain queued.
            if success and version is not None and self.queue.get(cid) == version:
                self.queue.pop(cid, None)
            if not success or self.queue.get(cid) in {None, version}:
                self.retry_at[cid] = time.time() + interval
            self._save()
