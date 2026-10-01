import sqlite3

from app.store.db import connect


class RefundExceedsPaid(Exception):
    """冲正金额超过当前已收金额。"""

class RefundIdMismatch(Exception):
    """同一冲正标识被重试，但冲正金额与首次请求不一致。"""

def _ledger(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT entry_type, amount_cents, ref_id, balance_after_cents, created_at, entry_seq "
        "FROM ledger_entries WHERE tenant=? AND order_id=? ORDER BY entry_seq",
        (tenant, order_id),
    ).fetchall()
    entries = []
    for row in rows:
        entry = {
            "seq": row["entry_seq"],
            "type": row["entry_type"],
            "amount_cents": row["amount_cents"],
            "balance_after_cents": row["balance_after_cents"],
            "created_at": row["created_at"],
        }
        if row["entry_type"] == "refund":
            entry["refund_id"] = row["ref_id"]
        entries.append(entry)
    return entries

def _order_dict(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    result = {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "paid_cents": row["paid_cents"],
        "currency": row["currency"],
        "status": row["status"],
        "outstanding_cents": row["amount_cents"] - row["paid_cents"],
        "ledger": _ledger(conn, row["tenant"], row["order_id"]),
    }
    return result

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            return None
        return _order_dict(row, conn)
    finally:
        conn.close()

def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        new_paid = row["paid_cents"] + amount_cents
        conn.execute(
            "UPDATE orders SET paid_cents = ?, status = CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (new_paid, new_paid, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, balance_after_cents) "
            "VALUES(?,?,COALESCE((SELECT MAX(entry_seq)+1 FROM ledger_entries WHERE tenant=? AND order_id=?),1),'payment',?,NULL,?)",
            (tenant, order_id, tenant, order_id, amount_cents, new_paid),
        )
        conn.execute("COMMIT")
        return get(tenant, order_id)
    finally:
        conn.close()

def add_refund(tenant: str, order_id: str, refund_id: str, amount_cents: int) -> dict | None:
    """对一笔订单冲正已收金额。

    返回 (冲正流水结果, 订单视图)；订单不存在返回 None。
    冲正与余额减少在同一事务内提交；同一（租户，订单，冲正标识）重试返回首次结果。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None

        existing = conn.execute(
            "SELECT amount_cents, balance_after_cents, created_at FROM ledger_entries "
            "WHERE tenant=? AND order_id=? AND ref_id=? AND entry_type='refund'",
            (tenant, order_id, refund_id),
        ).fetchone()
        if existing is not None:
            # 幂等重试：不重复减少已收金额，原样返回首次执行结果。
            if existing["amount_cents"] != amount_cents:
                conn.execute("ROLLBACK")
                raise RefundIdMismatch("refund identifier reused with a different amount")
            paid_after = existing["balance_after_cents"]
            conn.execute("COMMIT")
            return {
                "refund_id": refund_id,
                "refunded_cents": existing["amount_cents"],
                "paid_cents": paid_after,
                "outstanding_cents": row["amount_cents"] - paid_after,
                "created_at": existing["created_at"],
                "idempotent_replay": True,
            }

        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise ValueError("refund amount must be positive")
        if amount_cents > row["paid_cents"]:
            conn.execute("ROLLBACK")
            raise RefundExceedsPaid("refund exceeds paid amount")

        new_paid = row["paid_cents"] - amount_cents
        created_row = conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, balance_after_cents) "
            "VALUES(?,?,COALESCE((SELECT MAX(entry_seq)+1 FROM ledger_entries WHERE tenant=? AND order_id=?),1),'refund',?,?,?) "
            "RETURNING created_at",
            (tenant, order_id, tenant, order_id, amount_cents, refund_id, new_paid),
        ).fetchone()
        conn.execute(
            "UPDATE orders SET paid_cents = ?, status = CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (new_paid, new_paid, tenant, order_id),
        )
        conn.execute("COMMIT")
        return {
            "refund_id": refund_id,
            "refunded_cents": amount_cents,
            "paid_cents": new_paid,
            "outstanding_cents": row["amount_cents"] - new_paid,
            "created_at": created_row["created_at"],
            "idempotent_replay": False,
        }
    finally:
        conn.close()
