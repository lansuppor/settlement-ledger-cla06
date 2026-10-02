import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发写入同一库时，等待持锁方而不是立即报 SQLITE_BUSY
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def migrate() -> None:
    conn = connect()
    try:
        # 按文件名记录已执行的迁移：含 ALTER TABLE 的脚本不可重复执行
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY)")
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(name) VALUES(?)", (path.name,))
    finally:
        conn.close()
