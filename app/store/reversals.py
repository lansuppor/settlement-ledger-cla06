import sqlite3

from app.store.db import connect

REVERSAL_COLUMNS = (
    "tenant, order_id, reversal_seq, amount_cents, "
    "paid_after_cents, outstanding_after_cents, request_id, created_at"
)


def insert_conn(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    amount_cents: int,
    paid_after_cents: int,
    outstanding_after_cents: int,
    request_id: str | None,
) -> dict:
    """在调用方给定的写事务内追加一条冲正留痕，返回该记录。"""
    seq = conn.execute(
        "SELECT COALESCE(MAX(reversal_seq), 0) + 1 AS seq FROM reversals WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()["seq"]
    conn.execute(
        f"INSERT INTO reversals({REVERSAL_COLUMNS}) VALUES(?,?,?,?,?,?,?,datetime('now'))",
        (tenant, order_id, seq, amount_cents, paid_after_cents, outstanding_after_cents, request_id),
    )
    row = conn.execute(
        f"SELECT {REVERSAL_COLUMNS} FROM reversals WHERE tenant=? AND order_id=? AND reversal_seq=?",
        (tenant, order_id, seq),
    ).fetchone()
    return dict(row)


def list_conn(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    rows = conn.execute(
        f"SELECT {REVERSAL_COLUMNS} FROM reversals WHERE tenant=? AND order_id=? ORDER BY reversal_seq",
        (tenant, order_id),
    ).fetchall()
    return [dict(row) for row in rows]


def list(tenant: str, order_id: str) -> list[dict]:
    conn = connect()
    try:
        return list_conn(conn, tenant, order_id)
    finally:
        conn.close()
