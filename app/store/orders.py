import sqlite3
import uuid

from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store.db import connect

ORDER_COLUMNS = "tenant, order_id, amount_cents, paid_cents, reconciled_cents, currency, status"

RESULT_APPLIED = "applied"
RESULT_REJECTED = "rejected"

def _row_to_order(row: sqlite3.Row) -> dict:
    return {**dict(row), "outstanding_cents": row["amount_cents"] - row["paid_cents"]}

def insert_conn(conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    """在调用方给定的连接/事务内受理订单。"""
    conn.execute(
        "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status, ledger_id) "
        "VALUES(?,?,?,0,?,'accepted',?)",
        (tenant, order_id, amount_cents, currency, uuid.uuid4().hex),
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


# 更正被拒绝（409）的可区分原因
CORRECTION_AMOUNT_INVALID = "correction amount must be greater than zero"
CORRECTION_ORDER_ID_EMPTY = "correction order id must not be empty"
CORRECTION_HAS_FACTS = "order already has payment, reversal or reconciliation facts and cannot be corrected"
CORRECTION_TARGET_EXISTS = "correction target order id already exists"


def _correction_currency_invalid(currency: str) -> str:
    return f"unsupported currency: {currency}"


def correct_conn(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    new_order_id: str,
    new_amount_cents: int,
    new_currency: str,
    request_id: str | None = None,
) -> dict:
    """在调用方给定的写事务内更正已受理订单，并写入更正留痕。

    - 订单不存在抛 LookupError（跨租户同样查不到）；
    - 更正金额 <= 0、目标标识为空、币种不受支持抛 ValueError（409 输入非法）；
    - 订单已产生任何收款/冲正/核销业务事实抛 ValueError；
    - 同租户下目标标识已被其他订单占用抛 ValueError。
    无论生效还是被业务拒绝，都写一条更正留痕（applied/rejected）后返回或抛出；
    LookupError 不写任何留痕。成功返回按新标识读取的订单。
    """
    row = conn.execute(
        "SELECT order_id, amount_cents, currency, paid_cents, reconciled_cents, ledger_id "
        "FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        raise LookupError("order not found")

    before_order_id = row["order_id"]
    before_amount = row["amount_cents"]
    before_currency = row["currency"]
    ledger_id = row["ledger_id"]

    # 1) 输入合法性（409，原因可区分）
    if new_amount_cents <= 0:
        reason = CORRECTION_AMOUNT_INVALID
    elif not new_order_id:
        reason = CORRECTION_ORDER_ID_EMPTY
    elif new_currency not in ALLOWED_CURRENCIES:
        reason = _correction_currency_invalid(new_currency)
    elif row["paid_cents"] != 0 or row["reconciled_cents"] != 0 or _has_business_facts(
        conn, tenant, before_order_id
    ):
        # 2) 已产生收款/冲正/核销任一业务事实：金额非零或存在任一留痕即拒绝。
        #    收款被全额冲正后 paid_cents 虽归零，但收款/冲正留痕仍在，仍须拒绝。
        reason = CORRECTION_HAS_FACTS
    elif new_order_id != before_order_id:
        # 3) 同租户下新标识若已属于其他订单则冲突；跨租户同名不构成冲突
        clash = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=? AND ledger_id<>?",
            (tenant, new_order_id, ledger_id),
        ).fetchone()
        reason = CORRECTION_TARGET_EXISTS if clash is not None else None
    else:
        reason = None

    if reason is not None:
        #被拒绝：留痕记录更正前后的内容与拒绝原因，订单本身保持不变
        conn.execute(
            "INSERT INTO correction_records"
            "(tenant, order_id, ledger_id, before_order_id, before_amount_cents, before_currency, "
            "after_order_id, after_amount_cents, after_currency, result, reject_reason, request_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant, before_order_id, ledger_id,
                before_order_id, before_amount, before_currency,
                new_order_id, new_amount_cents, new_currency,
                RESULT_REJECTED, reason, request_id,
            ),
        )
        raise ValueError(reason)

    conn.execute(
        "UPDATE orders SET order_id=?, amount_cents=?, currency=? "
        "WHERE tenant=? AND ledger_id=?",
        (new_order_id, new_amount_cents, new_currency, tenant, ledger_id),
    )
    conn.execute(
        "INSERT INTO correction_records"
        "(tenant, order_id, ledger_id, before_order_id, before_amount_cents, before_currency, "
        "after_order_id, after_amount_cents, after_currency, result, reject_reason, request_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)",
        (
            tenant, before_order_id, ledger_id,
            before_order_id, before_amount, before_currency,
            new_order_id, new_amount_cents, new_currency,
            RESULT_APPLIED, request_id,
        ),
    )
    updated = get_conn(conn, tenant, new_order_id)
    assert updated is not None
    return updated


def _has_business_facts(conn: sqlite3.Connection, tenant: str, order_id: str) -> bool:
    """该订单（按当前标识）是否已留下任何收款/冲正/核销业务事实。

    只有尚未有任何业务事实的订单才允许更正，因此一旦更正生效，该订单名下
    必然仍无任何留痕；收款/冲正/核销都在更正之前被拦截，故留痕只会记在订单
    更正前的当前标识下，直接按当前标识判定即可。
    """
    for table in ("payment_records", "reversal_records", "reconciliation_records"):
        hit = conn.execute(
            f"SELECT 1 FROM {table} WHERE tenant=? AND order_id=? LIMIT 1",
            (tenant, order_id),
        ).fetchone()
        if hit is not None:
            return True
    return False


def list_corrections_conn(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    """按发生顺序读出订单的每笔更正留痕（含生效与拒绝）；订单不存在抛 LookupError。

    经由不可变链路键 ledger_id 关联，订单被改名后按新标识仍能读出其全部更正留痕，
    且复用了旧标识的新订单不会读到前者的留痕。
    """
    row = conn.execute(
        "SELECT ledger_id FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        raise LookupError("order not found")
    rows = conn.execute(
        "SELECT id, before_order_id, before_amount_cents, before_currency, "
        "after_order_id, after_amount_cents, after_currency, result, reject_reason, request_id, created_at "
        "FROM correction_records WHERE tenant=? AND ledger_id=? ORDER BY id",
        (tenant, row["ledger_id"]),
    ).fetchall()
    return [dict(r) for r in rows]

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
    """无请求标识的更正入口：订单不存在返回 None；输入非法或被业务拒绝抛 ValueError。

    与冲正/核销不同，被业务拒绝的更正也要留痕，故 ValueError 时提交事务以固化
    rejected 留痕，再向上抛出；LookupError（不存在/跨租户）回滚、不留痕。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = correct_conn(conn, tenant, order_id, new_order_id, new_amount_cents, new_currency)
        except LookupError:
            conn.execute("ROLLBACK")
            return None
        except Exception:
            # ValueError（含 rejected 留痕）在此提交，保证拒绝结论可按订单读出
            conn.execute("COMMIT")
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
