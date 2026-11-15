import hashlib, json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from backend.config import DATABASE_URL, DEFAULT_CRITERIA

DB = DATABASE_URL.removeprefix("sqlite:///")
Path(DB).parent.mkdir(parents=True, exist_ok=True)

def now(): return datetime.now(timezone.utc)
def stamp(t): return t.isoformat()
def payload_hash(d): return hashlib.sha256(json.dumps(d,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally: conn.close()

def init():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS emails (id TEXT PRIMARY KEY, sender TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, fingerprint TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS deals (id TEXT PRIMARY KEY, email_id TEXT NOT NULL, property TEXT NOT NULL, evaluation TEXT NOT NULL, draft TEXT NOT NULL, extraction TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, deadline TEXT NOT NULL, decided_at TEXT, sender TEXT, comments TEXT, notification TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, approval_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, executed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL);''')
        c.execute('CREATE TABLE IF NOT EXISTS mailbox_messages (id TEXT PRIMARY KEY, received_at TEXT NOT NULL, result TEXT NOT NULL)')
        c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',('criteria',json.dumps(DEFAULT_CRITERIA)))
init()

from fastapi import HTTPException

def audit(c,event,details): c.execute('INSERT INTO audit(at,event,details) VALUES (?,?,?)',(stamp(now()),event,json.dumps(details)))
def row(c,table,id):
    r=c.execute(f'SELECT * FROM {table} WHERE id=?',(id,)).fetchone()
    if not r: raise HTTPException(404, f'{table} record not found')
    return dict(r)
def unpack(r, fields):
    for f in fields: r[f]=json.loads(r[f])
    return r
