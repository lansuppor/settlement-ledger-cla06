import sqlite3

from app.store import reversals
from app.store.db import connect

ORDER_COLUMNS = "tenant, order_id, amount_cents, paid_cents, currency, status"
READ_COLUMNS = ORDER_COLUMNS + ", written_off_cents"

def _row_to_order(row: sqlite3.Row) -> dict:
    return {**dict(row), "outstanding_cents": row["amount_cents"] - row["paid_cents"]}

def insert_conn(conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    """在调用方给定的连接/事务内受理订单。"""
    conn.execute(
        f"INSERT INTO orders({ORDER_COLUMNS}) VALUES(?,?,?,0,?,'accepted')",
        (tenant, order_id, amount_cents, currency),
    )

def get_conn(conn: sqlite3.Connection, tenant: str, order_id: str) -> dict | None:
    row = conn.execute(
        f"SELECT {READ_COLUMNS} FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_order(row)

def pay_conn(conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int) -> dict:
    """在调用方给定的写事务内登记收款。

    订单不存在抛 LookupError；超过未收金额抛 ValueError；成功返回更新后的订单。
    """
    row = conn.execute(
        "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        raise LookupError("order not found")
    if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
        raise ValueError("payment exceeds outstanding amount")
    conn.execute(
        "UPDATE orders SET paid_cents = paid_cents + ?, "
        "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
        "WHERE tenant=? AND order_id=?",
        (amount_cents, amount_cents, tenant, order_id),
    )
    updated = get_conn(conn, tenant, order_id)
    assert updated is not None
    return updated

def reverse_conn(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    amount_cents: int,
    request_id: str | None = None,
) -> tuple[dict, dict]:
    """在调用方给定的写事务内冲正收款。

    订单不存在抛 LookupError；冲正金额不合法（小于等于零、或使已收金额低于
    已核销金额/零）抛 ValueError；成功返回 (更新后的订单, 冲正留痕记录)。
    """
    row = conn.execute(
        "SELECT amount_cents, paid_cents, written_off_cents FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        raise LookupError("order not found")
    if amount_cents <= 0:
        raise ValueError("reversal amount must be positive")
    if row["paid_cents"] - amount_cents < row["written_off_cents"]:
        raise ValueError("reversal exceeds paid amount available for reversal")
    conn.execute(
        "UPDATE orders SET paid_cents = paid_cents - ?, "
        "status = CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
        "WHERE tenant=? AND order_id=?",
        (amount_cents, amount_cents, tenant, order_id),
    )
    paid_after = row["paid_cents"] - amount_cents
    reversal = reversals.insert_conn(
        conn, tenant, order_id, amount_cents, paid_after, row["amount_cents"] - paid_after, request_id
    )
    updated = get_conn(conn, tenant, order_id)
    assert updated is not None
    return updated, reversal

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        insert_conn(conn, tenant, order_id, amount_cents, currency)
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        return get_conn(conn, tenant, order_id)
    finally:
        conn.close()

def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = pay_conn(conn, tenant, order_id, amount_cents)
        except LookupError:
            conn.execute("ROLLBACK")
            return None
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
    return order

def reverse(tenant: str, order_id: str, amount_cents: int) -> tuple[dict, dict] | None:
    """未携带请求标识的冲正入口：订单不存在返回 None，其余语义同 reverse_conn。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = reverse_conn(conn, tenant, order_id, amount_cents)
        except LookupError:
            conn.execute("ROLLBACK")
            return None
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
    return result
