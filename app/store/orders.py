import sqlite3

from app.store.db import connect

ORDER_COLUMNS = "tenant, order_id, amount_cents, paid_cents, currency, status"

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
        f"SELECT {ORDER_COLUMNS} FROM orders WHERE tenant=? AND order_id=?",
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
