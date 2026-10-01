import sqlite3
from pathlib import Path
from app.config import db_path

SCHEMA = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 写事务串行时等待锁而非立即 SQLITE_BUSY，支撑收款/冲正并发。
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def migrate() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    finally:
        conn.close()
