import sqlite3
from app.store.db import connect

class RefundExceedsPaid(Exception):
    """冲正金额超过当前已收金额。"""

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def _hydrate(row: sqlite3.Row) -> dict:
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            return None
        order = _hydrate(row)
        order["entries"] = _entries(conn, tenant, order_id)
        return order
    finally:
        conn.close()

def _entries(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    # id 自增即发生顺序；收款与冲正统一流水，paid_cents 可由 entries 重算解释。
    rows = conn.execute(
        "SELECT kind, amount_cents, ref_id, created_at FROM order_ledger "
        "WHERE tenant=? AND order_id=? ORDER BY id",
        (tenant, order_id),
    ).fetchall()
    entries = []
    for row in rows:
        entry = {"kind": row["kind"], "amount_cents": row["amount_cents"], "created_at": row["created_at"]}
        if row["kind"] == "refund":
            entry["refund_id"] = row["ref_id"]
        entries.append(entry)
    return entries

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
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        # 余额变动与流水留痕同一事务：流水之和始终可以解释 paid_cents。
        conn.execute(
            "INSERT INTO order_ledger(tenant, order_id, kind, amount_cents) VALUES(?,?, 'payment', ?)",
            (tenant, order_id, amount_cents),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)

def add_refund(tenant: str, order_id: str, refund_id: str, amount_cents: int) -> dict | None:
    """登记冲正。订单不存在返回 None；超过当前已收金额抛 RefundExceedsPaid。

    同一（租户, 订单, 冲正标识）重复请求不重复生效，返回首次执行结果，
    并以 idempotent_replay 标识是否为重放。
    """
    if amount_cents <= 0:
        raise ValueError("refund amount must be positive")
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
        # 幂等放最前：重复请求直接回放首次结果，不再做余额判断。
        existing = conn.execute(
            "SELECT amount_cents, created_at FROM order_ledger "
            "WHERE tenant=? AND order_id=? AND kind='refund' AND ref_id=?",
            (tenant, order_id, refund_id),
        ).fetchone()
        replay = existing is not None
        if existing is None:
            if amount_cents > row["paid_cents"]:
                conn.execute("ROLLBACK")
                raise RefundExceedsPaid("refund exceeds paid amount")
            conn.execute(
                "UPDATE orders SET paid_cents = paid_cents - ?, status = CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
                (amount_cents, amount_cents, tenant, order_id),
            )
            conn.execute(
                "INSERT INTO order_ledger(tenant, order_id, kind, amount_cents, ref_id) VALUES(?,?, 'refund', ?,?)",
                (tenant, order_id, amount_cents, refund_id),
            )
        conn.execute("COMMIT")
        result = _load_refund_result(conn, tenant, order_id, refund_id)
        result["idempotent_replay"] = replay
        return result
    finally:
        conn.close()

def _load_refund_result(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> dict:
    order = conn.execute(
        "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    entry = conn.execute(
        "SELECT amount_cents, created_at FROM order_ledger WHERE tenant=? AND order_id=? AND kind='refund' AND ref_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()
    return {
        "order_id": order_id,
        "refund_id": refund_id,
        "amount_cents": entry["amount_cents"],
        "paid_cents": order["paid_cents"],
        "outstanding_cents": order["amount_cents"] - order["paid_cents"],
        "created_at": entry["created_at"],
    }
