import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("FDE_DATA_DIR", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "fde.sqlite"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def uid():
    return uuid.uuid4().hex[:16]


@contextmanager
def connection():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init():
    if (DATA / "RESTORE_INCOMPLETE").exists():
        raise RuntimeError("恢复未完成的数据目录不可启动；请使用原库或重新恢复到新目录")
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, pages TEXT NOT NULL, result TEXT,
          review TEXT, parcel_id TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS occurrences(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
          batch TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, UNIQUE(document_id,batch));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
          state TEXT NOT NULL, strategy TEXT NOT NULL, created REAL NOT NULL, error TEXT, run_path TEXT);
        CREATE TABLE IF NOT EXISTS parcels(id TEXT PRIMARY KEY, business_key TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS publications(id INTEGER PRIMARY KEY AUTOINCREMENT, parcel_id TEXT NOT NULL,
          payload TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL, actor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL,
          action TEXT NOT NULL, target TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,label TEXT NOT NULL,received REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rulesets(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY, publication_id INTEGER NOT NULL REFERENCES publications(id),
          payload TEXT NOT NULL, created REAL NOT NULL);
        """)
        for table in ("audit", "publications", "notices", "rulesets"):
            for operation in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS no_{operation.lower()}_{table} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable history'); END"
                )
        batch_columns = {r[1] for r in db.execute("PRAGMA table_info(batches)")}
        for column in ("expected", "failures"):
            if column not in batch_columns:
                db.execute(f"ALTER TABLE batches ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        if "merged_into" not in {r[1] for r in db.execute("PRAGMA table_info(parcels)")}:
            db.execute("ALTER TABLE parcels ADD COLUMN merged_into TEXT")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS tax_pushes(id TEXT PRIMARY KEY, period TEXT NOT NULL, sha256 TEXT NOT NULL,
          rows INTEGER NOT NULL, created REAL NOT NULL, actor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notice_events(id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id TEXT NOT NULL
          REFERENCES notices(id), status TEXT NOT NULL, note TEXT NOT NULL, created REAL NOT NULL, actor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS generated_contracts(id TEXT PRIMARY KEY, parcel_id TEXT NOT NULL, kind TEXT NOT NULL,
          payload TEXT NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL, actor TEXT NOT NULL);
        """)
        for table in ("tax_pushes", "notice_events", "generated_contracts"):
            for operation in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS no_{operation.lower()}_{table} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable history'); END"
                )
        columns = {r[1] for r in db.execute("PRAGMA table_info(documents)")}
        if "intake" not in columns:
            db.execute("ALTER TABLE documents ADD COLUMN intake TEXT")
        db.execute(
            "CREATE TABLE IF NOT EXISTS review_drafts(document_id TEXT PRIMARY KEY,version INTEGER NOT NULL,payload TEXT NOT NULL,actor TEXT NOT NULL,at REAL NOT NULL)"
        )
        db.execute("PRAGMA user_version=6")
        # Interrupted work is visible and resumable; never silently marked successful.
        db.execute(
            "UPDATE jobs SET state='interrupted',error='进程中断，可显式重试或人工接管' WHERE state='running'"
        )
        db.execute("UPDATE documents SET state='needs_human' WHERE state='recognizing'")


def audit(db, actor, action, target, payload):
    db.execute(
        "INSERT INTO audit(at,actor,action,target,payload) VALUES(?,?,?,?,?)",
        (time.time(), actor, action, target, dumps(payload)),
    )


def doc_row(row):
    if row is None:
        return None
    item = dict(row)
    for key in ("pages", "result", "review", "intake"):
        item[key] = json.loads(item[key]) if item.get(key) else None
    return item


def get_doc(doc_id):
    with connection() as db:
        return doc_row(db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


def list_docs():
    with connection() as db:
        return [doc_row(r) for r in db.execute("SELECT * FROM documents ORDER BY created DESC")]


def latest(db, parcel_id):
    row = db.execute(
        "SELECT * FROM publications WHERE parcel_id=? ORDER BY id DESC LIMIT 1", (parcel_id,)
    ).fetchone()
    if not row:
        return None
    r = dict(row)
    r["payload"] = json.loads(r["payload"])
    return r


def publish(db, parcel_id, payload, actor):
    digest = hashlib.sha256(dumps(payload).encode()).hexdigest()
    previous = latest(db, parcel_id)
    if previous and previous["digest"] == digest:
        return previous
    cursor = db.execute(
        "INSERT INTO publications(parcel_id,payload,digest,created,actor) VALUES(?,?,?,?,?)",
        (parcel_id, dumps(payload), digest, time.time(), actor),
    )
    audit(
        db,
        actor,
        "publish",
        parcel_id,
        {
            "before": previous["digest"] if previous else None,
            "after": digest,
            "publication": cursor.lastrowid,
        },
    )
    return latest(db, parcel_id)
