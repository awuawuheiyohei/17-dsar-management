"""DB helper —— 统一用 context manager 避免 SQLite WAL lock"""
import os
import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# 支持用环境变量指向独立 DB（测试隔离 / 多环境）
DB_PATH = Path(os.environ.get("DSAR_DB", DATA_DIR / "dsar.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


@contextmanager
def db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()


def init_db(verbose=False):
    with db_connection() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    if verbose:
        print(f"[+] DB at {DB_PATH}")


def row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row else {}


def rows_to_dicts(rows) -> list:
    return [row_to_dict(r) for r in rows]


def write_audit(conn, actor: str, action: str, entity_type: str = None,
                entity_id: str = None, details: dict = None, actor_role: str = None):
    conn.execute(
        """INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id, details)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (actor, actor_role, action, entity_type, entity_id,
         json.dumps(details or {}, ensure_ascii=False))
    )
    conn.commit()
