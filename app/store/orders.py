import sqlite3

from app.rules import order_rules
from app.store.db import connect

ORDER_COLUMNS = "tenant, order_id, amount_cents, paid_cents, reconciled_cents, currency, status"

RESULT_APPLIED = "applied"
RESULT_REJECTED = "rejected"

# 更正被业务规则拒绝（订单已有业务事实，或同租户新标识已存在）：
# 拒绝结论已写入更正留痕，调用方据此返回 409。
class CorrectionRejected(ValueError):
    pass

# 退款被业务规则拒绝（金额非正整数、退款后已收为负或低于已核销）：
# 拒绝结论已写入退款留痕，调用方据此返回 409。
class RefundRejected(ValueError):
    pass

# 更正前置校验所查看的留痕表：任一表存在该订单的记录即视为已产生业务事实
_FACT_TABLES = ("payment_records", "reversal_records", "reconciliation_records", "refund_records")

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

def _insert_refund_record(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    amount_cents: int,
    reason: str,
    result: str,
    reject_reason: str | None,
    paid_after: int | None,
    request_id: str | None,
) -> None:
    conn.execute(
        "INSERT INTO refund_records"
        "(tenant, order_id, amount_cents, reason, result, reject_reason, paid_after, request_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (tenant, order_id, amount_cents, reason, result, reject_reason, paid_after, request_id),
    )


def refund_conn(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    amount_cents: int,
    reason: str,
    request_id: str | None = None,
) -> dict:
    """在调用方给定的写事务内对已登记收款的订单按笔退款，并写入退款流水。

    退款是真实出账退钱（区别于纠正录错登记的冲正）：生效后已收减少、未收增加，
    二者之和仍等于订单金额；订单金额与已核销金额不变。

    - 订单不存在抛 LookupError（不写任何留痕，跨租户同样按不存在处理）；
    - 金额 <= 0、退款后已收为负、退款后已收低于已核销金额时抛 RefundRejected，
      并把该结论（含退款原因与具体拒绝原因）写入退款留痕，订单本身保持不变。
    成功则写 applied 留痕，返回更新后的订单。
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
        reject = "refund amount must be greater than zero"
        _insert_refund_record(
            conn, tenant, order_id, amount_cents, reason, RESULT_REJECTED, reject, None, request_id
        )
        raise RefundRejected(reject)
    paid_after = paid_before - amount_cents
    if paid_after < 0:
        reject = "refund would make paid amount negative"
        _insert_refund_record(
            conn, tenant, order_id, amount_cents, reason, RESULT_REJECTED, reject, None, request_id
        )
        raise RefundRejected(reject)
    if paid_after < row["reconciled_cents"]:
        reject = "refund would make paid amount less than reconciled amount"
        _insert_refund_record(
            conn, tenant, order_id, amount_cents, reason, RESULT_REJECTED, reject, None, request_id
        )
        raise RefundRejected(reject)

    conn.execute(
        "UPDATE orders SET paid_cents = ?, "
        "status = CASE WHEN ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
        "WHERE tenant=? AND order_id=?",
        (paid_after, paid_after, tenant, order_id),
    )
    _insert_refund_record(
        conn, tenant, order_id, amount_cents, reason, RESULT_APPLIED, None, paid_after, request_id
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
    - 核销金额 <= 0 或使累计核销超过当前已收金额抛 ValueError。
    校验失败时不写任何流水、不改变订单金额与状态。
    核销只改变认定口径：已收、未收与订单金额均不变。成功返回更新后的订单。
    """
    row = conn.execute(
        "SELECT amount_cents, paid_cents, reconciled_cents FROM orders "
        "WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        raise LookupError("order not found")
    if amount_cents <= 0:
        raise ValueError("reconciliation amount must be greater than zero")
    reconciled_after = row["reconciled_cents"] + amount_cents
    if reconciled_after > row["paid_cents"]:
        raise ValueError("reconciled amount would exceed paid amount")

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

def _has_business_facts(conn: sqlite3.Connection, tenant: str, order_id: str) -> bool:
    """订单是否已产生任何收款、冲正或核销留痕。"""
    for table in _FACT_TABLES:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE tenant=? AND order_id=? LIMIT 1",
            (tenant, order_id),
        ).fetchone()
        if row is not None:
            return True
    return False


def _insert_correction_record(
    conn: sqlite3.Connection,
    tenant: str,
    record_order_id: str,
    before: dict,
    after: dict,
    result: str,
    reject_reason: str | None,
    request_id: str | None,
) -> None:
    conn.execute(
        "INSERT INTO correction_records"
        "(tenant, order_id, before_order_id, before_amount_cents, before_currency, "
        "after_order_id, after_amount_cents, after_currency, result, reject_reason, request_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            tenant, record_order_id,
            before["order_id"], before["amount_cents"], before["currency"],
            after["order_id"], after["amount_cents"], after["currency"],
            result, reject_reason, request_id,
        ),
    )


def correct_conn(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    new_order_id: str,
    new_amount_cents: int,
    new_currency: str,
    request_id: str | None = None,
) -> dict:
    """在调用方给定的写事务内更正已受理订单。

    订单不存在抛 LookupError（不写任何留痕，跨租户同样按不存在处理）。
    其余任何拒绝（非法输入、订单已有业务事实、同租户新标识被占用）都抛
    CorrectionRejected，并把该结论（含更正前后内容与具体原因）写入更正留痕，
    订单本身保持不变。成功则订单按新内容生效并写 applied 留痕，返回更正后的订单。
    """
    row = conn.execute(
        "SELECT order_id, amount_cents, paid_cents, reconciled_cents, currency "
        "FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        raise LookupError("order not found")

    target_id = new_order_id.strip() if isinstance(new_order_id, str) else ""
    before = {"order_id": row["order_id"], "amount_cents": row["amount_cents"], "currency": row["currency"]}
    after = {"order_id": target_id, "amount_cents": new_amount_cents, "currency": new_currency}

    # 输入合法性：金额须为大于零的整数、币种须受支持、新标识不能为空。
    # 拒绝结论同样留痕（不改变任何已有数据），与生效结论一样可按订单读出。
    if not target_id:
        reason = "order id must not be empty"
        _insert_correction_record(conn, tenant, order_id, before, after, RESULT_REJECTED, reason, request_id)
        raise CorrectionRejected(reason)
    if new_amount_cents <= 0:
        reason = "amount must be a positive integer in minor units"
        _insert_correction_record(conn, tenant, order_id, before, after, RESULT_REJECTED, reason, request_id)
        raise CorrectionRejected(reason)
    try:
        order_rules.assert_currency(new_currency)
    except ValueError:
        reason = f"unsupported currency: {new_currency}"
        _insert_correction_record(conn, tenant, order_id, before, after, RESULT_REJECTED, reason, request_id)
        raise CorrectionRejected(reason)

    # 业务前置：已有任何业务事实则不允许更正。金额/标识/币种/状态与全部留痕保持不变。
    if row["paid_cents"] != 0 or row["reconciled_cents"] != 0 or _has_business_facts(conn, tenant, order_id):
        reason = "order has payments, reversals, reconciliations or refunds and cannot be corrected"
        _insert_correction_record(conn, tenant, order_id, before, after, RESULT_REJECTED, reason, request_id)
        raise CorrectionRejected(reason)

    # 同租户下新标识若已被其他订单占用则拒绝（跨租户同名不冲突，由主键天然区分）
    if target_id != order_id:
        clash = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, target_id),
        ).fetchone()
        if clash is not None:
            reason = "target order id already exists for tenant"
            _insert_correction_record(conn, tenant, order_id, before, after, RESULT_REJECTED, reason, request_id)
            raise CorrectionRejected(reason)

    # 生效：金额与币种更新；订单标识可能改变（主键变更用删除+插入，同事务内完成）。
    # 订单尚未产生任何收款/冲正/核销留痕，故改名不会使这些记录悬挂；
    # 该订单既有的更正留痕随订单一起迁移到新标识，保证完整更正历史始终可按当前标识读出。
    conn.execute(
        "UPDATE orders SET order_id=?, amount_cents=?, currency=? "
        "WHERE tenant=? AND order_id=?",
        (target_id, new_amount_cents, new_currency, tenant, order_id),
    )
    if target_id != order_id:
        conn.execute(
            "UPDATE correction_records SET order_id=? WHERE tenant=? AND order_id=?",
            (target_id, tenant, order_id),
        )
    _insert_correction_record(
        conn, tenant, target_id, before, after, RESULT_APPLIED, None, request_id
    )
    updated = get_conn(conn, tenant, target_id)
    assert updated is not None
    return updated


def list_corrections_conn(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    """按发生顺序读出订单的每笔更正留痕；订单不存在抛 LookupError。

    生效的更正挂在更正后的订单标识上；被拒绝的更正挂在原目标订单标识上。
    """
    if get_conn(conn, tenant, order_id) is None:
        raise LookupError("order not found")
    rows = conn.execute(
        "SELECT id, before_order_id, before_amount_cents, before_currency, "
        "after_order_id, after_amount_cents, after_currency, result, reject_reason, "
        "request_id, created_at "
        "FROM correction_records WHERE tenant=? AND order_id=? ORDER BY id",
        (tenant, order_id),
    ).fetchall()
    return [dict(row) for row in rows]


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

def list_refunds_conn(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    """按发生顺序读出订单的每笔退款留痕（含被拒绝结论）；订单不存在抛 LookupError。"""
    if get_conn(conn, tenant, order_id) is None:
        raise LookupError("order not found")
    rows = conn.execute(
        "SELECT id, amount_cents, reason, result, reject_reason, paid_after, request_id, created_at "
        "FROM refund_records WHERE tenant=? AND order_id=? ORDER BY id",
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

def refund(tenant: str, order_id: str, amount_cents: int, reason: str) -> dict | None:
    """无请求标识的退款入口：订单不存在返回 None，业务拒绝抛 RefundRejected。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = refund_conn(conn, tenant, order_id, amount_cents, reason)
        except LookupError:
            conn.execute("ROLLBACK")
            return None
        except RefundRejected:
            # 业务拒绝结论已写入退款留痕，需提交保留（订单本身保持不变）
            conn.execute("COMMIT")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
    return order

def list_refunds(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        try:
            return list_refunds_conn(conn, tenant, order_id)
        except LookupError:
            return None
    finally:
        conn.close()

def list_reversals(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        try:
            return list_reversals_conn(conn, tenant, order_id)
        except LookupError:
            return None
    finally:
        conn.close()

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

def list_reconciliations(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        try:
            return list_reconciliations_conn(conn, tenant, order_id)
        except LookupError:
            return None
    finally:
        conn.close()


def correct(
    tenant: str,
    order_id: str,
    new_order_id: str,
    new_amount_cents: int,
    new_currency: str,
) -> dict | None:
    """无请求标识的更正入口：订单不存在返回 None，非法输入/业务拒绝抛 ValueError。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = correct_conn(
                conn, tenant, order_id, new_order_id, new_amount_cents, new_currency
            )
        except LookupError:
            conn.execute("ROLLBACK")
            return None
        except CorrectionRejected:
            # 业务拒绝结论已写入更正留痕，需提交保留（订单本身保持不变）
            conn.execute("COMMIT")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
    return order


def list_corrections(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        try:
            return list_corrections_conn(conn, tenant, order_id)
        except LookupError:
            return None
    finally:
        conn.close()
