import sqlite3, json, os
from datetime import datetime, timezone
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    # Published snapshots are the only chain shown by the workbench and the
    # archive download.  They are written transactionally so a failed publish
    # leaves the previously published chain untouched.
    db.execute('CREATE TABLE IF NOT EXISTS published (run_id TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL, published_at TEXT NOT NULL)')
    return db

def save(run_id, body):
    with connection() as db:
        # Same input bytes + parameters + rule version keep the same batch id:
        # a rerun refreshes the preview body but never adds a second batch or
        # touches the published chain.
        db.execute('INSERT INTO runs(id,body) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body

def published_version(run_id):
    with connection() as db:r=db.execute('SELECT version FROM published WHERE run_id=?',(run_id,)).fetchone()
    return r['version'] if r else 0

def get_published(run_id):
    with connection() as db:r=db.execute('SELECT * FROM published WHERE run_id=?',(run_id,)).fetchone()
    if not r:return None
    body=json.loads(r['body']);body.update(run_id=r['run_id'],published_version=r['version'])
    return body

def publish(run_id, expected_version, build):
    """Atomically publish the next chain version.

    ``build(version)`` is evaluated inside the write transaction; raising from
    it (validation failure) rolls back and keeps the existing chain.  A stale
    expected version is rejected with 409, matching the review flow.
    """
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT version FROM published WHERE run_id=?',(run_id,)).fetchone()
        current=row['version'] if row else 0
        if expected_version is not None:
            # First publish accepts 0 or 1 as "the chain as it stands".
            allowed={current} if current else {0,1}
            if expected_version not in allowed:raise DomainError('STALE','保障链版本已更新，请刷新',409)
        version=current+1
        body=build(version)
        stamp=datetime.now(timezone.utc).isoformat()
        if row:
            cur=db.execute('UPDATE published SET version=?,body=?,published_at=? WHERE run_id=? AND version=?',(version,json.dumps(body,ensure_ascii=False),stamp,run_id,current))
            if cur.rowcount!=1:raise DomainError('STALE','保障链版本已更新，请刷新',409)
        else:
            try:db.execute('INSERT INTO published(run_id,version,body,published_at) VALUES (?,?,?,?)',(run_id,version,json.dumps(body,ensure_ascii=False),stamp))
            except sqlite3.IntegrityError:raise DomainError('STALE','保障链版本已更新，请刷新',409)
    return get_published(run_id)

def history():
    with connection() as db:rows=db.execute("SELECT r.id,r.version,r.decision,p.version AS publish_version FROM runs r LEFT JOIN published p ON p.run_id=r.id ORDER BY CASE WHEN r.id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,r.rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)
