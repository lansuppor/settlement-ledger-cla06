import sqlite3

from app.store.db import connect

ORDER_COLUMNS = "tenant, order_id, amount_cents, paid_cents, reconciled_cents, currency, status"

RESULT_APPLIED = "applied"

def _row_to_order(row: sqlite3.Row) -> dict:
    return {**dict(row), "outstanding_cents": row["amount_cents"] - row["paid_cents"]}

def insert_conn(conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    """在调用方给定的连接/事务内受理订单。"""
    conn.execute(
        "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
        "VALUES(?,?,?,0,?,'accepted')",
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

def pay_conn(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    amount_cents: int,
    request_id: str | None = None,
) -> dict:
    """在调用方给定的写事务内登记收款，并写入收款流水。

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
    paid_after = row["paid_cents"] + amount_cents
    conn.execute(
        "UPDATE orders SET paid_cents = ?, "
        "status = CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
        "WHERE tenant=? AND order_id=?",
        (paid_after, paid_after, tenant, order_id),
    )
    conn.execute(
        "INSERT INTO payment_records(tenant, order_id, amount_cents, paid_after, request_id) "
        "VALUES(?,?,?,?,?)",
        (tenant, order_id, amount_cents, paid_after, request_id),
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
) -> dict:
    """在调用方给定的写事务内冲正已登记收款，并写入冲正流水。

    - 订单不存在抛 LookupError；
    - 冲正金额 <= 0 或超过当前已收金额抛 ValueError；
    - 冲正会使已收金额低于已核销金额时抛 ValueError。
    校验失败时不写任何流水、不改变订单金额与状态。成功返回更新后的订单。
    """
    row = conn.execute(
        "SELECT amount_cents, paid_cents, reconciled_cents FROM orders "
        "WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        raise LookupError("order not found")
    paid_before = row["paid_cents"]
    if amount_cents <= 0:
        raise ValueError("reversal amount must be greater than zero")
    if amount_cents > paid_before:
        raise ValueError("reversal amount exceeds paid amount")
    paid_after = paid_before - amount_cents
    if paid_after < row["reconciled_cents"]:
        raise ValueError("reversal would make paid amount less than reconciled amount")

    conn.execute(
        "UPDATE orders SET paid_cents = ?, "
        "status = CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
        "WHERE tenant=? AND order_id=?",
        (paid_after, paid_after, tenant, order_id),
    )
    conn.execute(
        "INSERT INTO reversal_records"
        "(tenant, order_id, amount_cents, result, reject_reason, paid_after, request_id) "
        "VALUES(?,?,?,?,NULL,?,?)",
        (tenant, order_id, amount_cents, RESULT_APPLIED, paid_after, request_id),
    )
    updated = get_conn(conn, tenant, order_id)
    assert updated is not None
    return updated

def reconcile_conn(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    amount_cents: int,
    request_id: str | None = None,
) -> dict:
    """在调用方给定的写事务内核销已确认收款，并写入核销流水。

    - 订单不存在抛 LookupError；
    - 核销金额 <= 0 抛 ValueError；
    - 累计核销会超过当前已收金额时抛 ValueError。
    校验失败时不写任何流水、不改变订单金额与状态。成功返回更新后的订单。
    核销只改变认定口径（reconciled_cents），不改变已收、未收与订单金额。
    """
    row = conn.execute(
        "SELECT paid_cents, reconciled_cents FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        raise LookupError("order not found")
    if amount_cents <= 0:
        raise ValueError("reconciliation amount must be greater than zero")
    reconciled_after = row["reconciled_cents"] + amount_cents
    if reconciled_after > row["paid_cents"]:
        raise ValueError("reconciliation amount exceeds paid amount")

    conn.execute(
        "UPDATE orders SET reconciled_cents = ? WHERE tenant=? AND order_id=?",
        (reconciled_after, tenant, order_id),
    )
    conn.execute(
        "INSERT INTO reconciliation_records"
        "(tenant, order_id, amount_cents, result, reconciled_after, request_id) "
        "VALUES(?,?,?,?,?,?)",
        (tenant, order_id, amount_cents, RESULT_APPLIED, reconciled_after, request_id),
    )
    updated = get_conn(conn, tenant, order_id)
    assert updated is not None
    return updated

def list_reconciliations_conn(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    """按发生顺序读出订单的每笔核销留痕；订单不存在抛 LookupError。"""
    if get_conn(conn, tenant, order_id) is None:
        raise LookupError("order not found")
    rows = conn.execute(
        "SELECT id, amount_cents, result, reconciled_after, request_id, created_at "
        "FROM reconciliation_records WHERE tenant=? AND order_id=? ORDER BY id",
        (tenant, order_id),
    ).fetchall()
    return [dict(row) for row in rows]

def list_reversals_conn(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    """按发生顺序读出订单的每笔冲正留痕；订单不存在抛 LookupError。"""
    if get_conn(conn, tenant, order_id) is None:
        raise LookupError("order not found")
    rows = conn.execute(
        "SELECT id, amount_cents, result, paid_after, request_id, created_at "
        "FROM reversal_records WHERE tenant=? AND order_id=? ORDER BY id",
        (tenant, order_id),
    ).fetchall()
    return [dict(row) for row in rows]

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

def reverse_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    """无请求标识的冲正入口：订单不存在返回 None，金额非法抛 ValueError。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = reverse_conn(conn, tenant, order_id, amount_cents)
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

def reconcile(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    """无请求标识的核销入口：订单不存在返回 None，金额非法抛 ValueError。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = reconcile_conn(conn, tenant, order_id, amount_cents)
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

def list_reversals(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        try:
            return list_reversals_conn(conn, tenant, order_id)
        except LookupError:
            return None
    finally:
        conn.close()

def list_reconciliations(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        try:
            return list_reconciliations_conn(conn, tenant, order_id)
        except LookupError:
            return None
    finally:
        conn.close()

