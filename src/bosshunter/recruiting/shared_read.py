"""Coalesce identical in-flight reads across Web/worker; never cache later reads."""
import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .browser import AccountPauseError, BrowserError, TaskCancelled
from .store import encode, now


def shared_read(store, key, reader, check_cancelled):
    check_cancelled()
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec='seconds')
        db.execute("DELETE FROM read_flights WHERE status!='running' AND created_at<?", (cutoff,))
        row = db.execute("SELECT * FROM read_flights WHERE request_key=? AND status='running'", (key,)).fetchone()
        if row and not store.owner_alive(row['owner']):
            db.execute("UPDATE read_flights SET status='failed',error=? WHERE token=?",
                       (encode({'type': 'BrowserError', 'message': '共享读取进程已退出；请重新同步'}), row['token']))
            row = None
        leader = row is None
        token = uuid4().hex if leader else row['token']
        if leader:
            db.execute('INSERT INTO read_flights VALUES (?,?,?,?,?,?,?)',
                       (token, key, store.owner, 'running', None, None, now()))
    if leader:
        try:
            result = reader()
            with store.db() as db:
                db.execute("UPDATE read_flights SET status='done',result=? WHERE token=?", (encode(result), token))
            return result
        except BaseException as exc:
            with store.db() as db:
                db.execute("UPDATE read_flights SET status='failed',error=? WHERE token=?",
                           (encode({'type': type(exc).__name__, 'message': str(exc)}), token))
            raise
    errors = {'AccountPauseError': AccountPauseError, 'TaskCancelled': TaskCancelled,
              'BrowserError': BrowserError, 'ValueError': ValueError, 'PermissionError': PermissionError}
    while True:
        check_cancelled()
        with store.db() as db:
            row = db.execute('SELECT * FROM read_flights WHERE token=?', (token,)).fetchone()
        if not row:
            raise BrowserError('共享读取结果已失效；请重新同步')
        if row['status'] == 'done':
            return json.loads(row['result'])
        if row['status'] == 'failed':
            error = json.loads(row['error'])
            raise errors.get(error['type'], BrowserError)(error['message'])
        if not store.owner_alive(row['owner']):
            raise BrowserError('共享读取进程已退出；未自动重试')
        time.sleep(.1)
