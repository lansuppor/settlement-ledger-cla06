import sqlite3

from app.store.db import connect


class RefundExceedsPaid(Exception):
    """冲正金额超过当前已收金额。"""

class RefundIdMismatch(Exception):
    """同一冲正标识被重试，但冲正金额与首次请求不一致，或标识被用于别的冲正。"""

class ReceiptNotPending(Exception):
    """凭据当前不是待核销状态：只有待核销凭据可确认/撤销。"""

class ReceiptNotRefundable(Exception):
    """只有已核销凭据可以凭据冲正；待核销凭据需先核销或撤销。"""

class ReceiptAlreadyRefunded(Exception):
    """凭据已冲正，不能重复冲正。"""

class ReceiptAmountMismatch(Exception):
    """按凭据冲正的金额与凭据金额不一致。"""

class RevokeExceedsPaid(Exception):
    """撤销金额超过当前已收：待核销资金可能已被其他冲正占用，撤销会使余额为负。"""

def _next_seq(conn: sqlite3.Connection, tenant: str, order_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(entry_seq),0) FROM ledger_entries WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    return row[0] + 1

def _ledger(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT entry_type, amount_cents, ref_id, balance_after_cents, created_at, entry_seq "
        "FROM ledger_entries WHERE tenant=? AND order_id=? ORDER BY entry_seq",
        (tenant, order_id),
    ).fetchall()
    # 活凭据按 payment_seq 关联收款流水；被撤销的历史代次在事件表中查得，标记 revoked。
    live_status = {
        row["payment_seq"]: row["status"]
        for row in conn.execute(
            "SELECT payment_seq, status FROM payment_receipts WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchall()
    }
    revoked_seqs = {
        row["payment_seq"]
        for row in conn.execute(
            "SELECT payment_seq FROM payment_receipt_events "
            "WHERE tenant=? AND order_id=? AND event='revoked'",
            (tenant, order_id),
        ).fetchall()
    }
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
        elif row["entry_type"] == "payment" and row["ref_id"] is not None:
            entry["receipt_id"] = row["ref_id"]
            entry["receipt_status"] = live_status.get(row["entry_seq"], "revoked" if row["entry_seq"] in revoked_seqs else None)
        elif row["entry_type"] == "receipt_revocation":
            entry["receipt_id"] = row["ref_id"]
        entries.append(entry)
    return entries

def _order_dict(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    return {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "paid_cents": row["paid_cents"],
        "currency": row["currency"],
        "status": row["status"],
        "outstanding_cents": row["amount_cents"] - row["paid_cents"],
        "ledger": _ledger(conn, row["tenant"], row["order_id"]),
    }

def _pending_count(conn: sqlite3.Connection, tenant: str, order_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM payment_receipts WHERE tenant=? AND order_id=? AND status='pending'",
        (tenant, order_id),
    ).fetchone()[0]

def _settle_status(conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int, new_paid: int) -> str:
    """结清裁决：待核销收款不计入结清条件；其余沿用 paid==amount 即 settled。"""
    if new_paid >= amount_cents and _pending_count(conn, tenant, order_id) == 0:
        return "settled"
    return "accepted"

def _apply_balance(
    conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int, new_paid: int
) -> str:
    status = _settle_status(conn, tenant, order_id, amount_cents, new_paid)
    conn.execute(
        "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
        (new_paid, status, tenant, order_id),
    )
    return status

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

def add_payment(tenant: str, order_id: str, amount_cents: int, receipt_id: str | None = None) -> dict | None:
    """登记收款；receipt_id 非空时收款进入待核销：立即计入余额与流水，但订单不因此结清。"""
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
        next_seq = _next_seq(conn, tenant, order_id)
        conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, balance_after_cents) "
            "VALUES(?,?,?,'payment',?,?,?)",
            (tenant, order_id, next_seq, amount_cents, receipt_id, new_paid),
        )
        if receipt_id is not None:
            try:
                conn.execute(
                    "INSERT INTO payment_receipts(tenant, order_id, receipt_id, amount_cents, status, payment_seq) "
                    "VALUES(?,?,?,?,'pending',?)",
                    (tenant, order_id, receipt_id, amount_cents, next_seq),
                )
            except sqlite3.IntegrityError as error:
                conn.execute("ROLLBACK")
                if "UNIQUE" in str(error):
                    raise ValueError("receipt identifier already in use") from error
                raise
        _apply_balance(conn, tenant, order_id, row["amount_cents"], new_paid)
        conn.execute("COMMIT")
        return get(tenant, order_id)
    finally:
        conn.close()

def add_refund(tenant: str, order_id: str, refund_id: str, amount_cents: int) -> dict | None:
    """对一笔订单冲正已收金额。

    返回冲正结果；订单不存在返回 None。
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
        _apply_balance(conn, tenant, order_id, row["amount_cents"], new_paid)
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

def _receipt_view(
    receipt_id: str, receipt_status: str, amount_cents: int, paid_cents: int, status: str,
    at: str, idempotent_replay: bool,
) -> dict:
    """三个凭据操作的统一返回：凭据标识、凭据状态与订单 paid/outstanding/status。"""
    return {
        "receipt_id": receipt_id,
        "receipt_status": receipt_status,
        "paid_cents": paid_cents,
        "outstanding_cents": amount_cents - paid_cents,
        "status": status,
        "created_at": at,
        "idempotent_replay": idempotent_replay,
    }

def confirm_receipt(tenant: str, order_id: str, receipt_id: str) -> dict | None:
    """核销确认：待核销 → 已核销，订单按现有规则重新判定结清与否。重复确认回放首次结果。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        receipt = conn.execute(
            "SELECT status, confirmed_at, confirmed_paid_cents, confirmed_status "
            "FROM payment_receipts WHERE tenant=? AND order_id=? AND receipt_id=?",
            (tenant, order_id, receipt_id),
        ).fetchone()
        if receipt is None:
            conn.execute("ROLLBACK")
            return None
        if receipt["status"] == "confirmed":
            # 幂等回放：返回首次确认结果，不重复留痕、不改余额。
            conn.execute("COMMIT")
            return _receipt_view(
                receipt_id, "confirmed", order["amount_cents"],
                receipt["confirmed_paid_cents"], receipt["confirmed_status"],
                receipt["confirmed_at"], True,
            )
        if receipt["status"] != "pending":
            conn.execute("ROLLBACK")
            raise ReceiptNotPending("only pending receipts can be confirmed")

        # 余额不变；先落确认状态，再按“无待核销”规则重新判定结清，最后固化首次结果快照。
        stamped = conn.execute(
            "UPDATE payment_receipts SET status='confirmed', confirmed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE tenant=? AND order_id=? AND receipt_id=? RETURNING confirmed_at",
            (tenant, order_id, receipt_id),
        ).fetchone()
        status = _apply_balance(conn, tenant, order_id, order["amount_cents"], order["paid_cents"])
        conn.execute(
            "UPDATE payment_receipts SET confirmed_paid_cents=?, confirmed_status=? "
            "WHERE tenant=? AND order_id=? AND receipt_id=?",
            (order["paid_cents"], status, tenant, order_id, receipt_id),
        )
        conn.execute("COMMIT")
        return _receipt_view(
            receipt_id, "confirmed", order["amount_cents"], order["paid_cents"],
            status, stamped["confirmed_at"], False,
        )
    finally:
        conn.close()

def revoke_receipt(tenant: str, order_id: str, receipt_id: str) -> dict | None:
    """撤销：整笔移除待核销收款，余额减少并补撤销流水；已核销/已冲正凭据拒绝。

    活凭据行删除（释放同名凭据作用域），撤销事件只追加保留；重复撤销回放首次结果。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        receipt = conn.execute(
            "SELECT amount_cents, status, payment_seq FROM payment_receipts "
            "WHERE tenant=? AND order_id=? AND receipt_id=?",
            (tenant, order_id, receipt_id),
        ).fetchone()
        if receipt is None:
            history = conn.execute(
                "SELECT paid_after_cents, status_after, created_at FROM payment_receipt_events "
                "WHERE tenant=? AND order_id=? AND receipt_id=? AND event='revoked' ORDER BY rowid DESC LIMIT 1",
                (tenant, order_id, receipt_id),
            ).fetchone()
            if history is None:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
            return _receipt_view(
                receipt_id, "revoked", order["amount_cents"],
                history["paid_after_cents"], history["status_after"],
                history["created_at"], True,
            )
        if receipt["status"] != "pending":
            conn.execute("ROLLBACK")
            raise ReceiptNotPending("only pending receipts can be revoked")

        amount = receipt["amount_cents"]
        if amount > order["paid_cents"]:
            # 待核销资金已被其他冲正占用：整笔撤销会使余额为负，按冲突拒绝且数据不变。
            conn.execute("ROLLBACK")
            raise RevokeExceedsPaid("revoke exceeds paid amount")
        new_paid = order["paid_cents"] - amount
        next_seq = _next_seq(conn, tenant, order_id)
        created_row = conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, balance_after_cents) "
            "VALUES(?,?,?,'receipt_revocation',?,?,?) RETURNING created_at",
            (tenant, order_id, next_seq, amount, receipt_id, new_paid),
        ).fetchone()
        status = _apply_balance(conn, tenant, order_id, order["amount_cents"], new_paid)
        conn.execute(
            "INSERT INTO payment_receipt_events(tenant, order_id, receipt_id, payment_seq, event, "
            "amount_cents, paid_after_cents, status_after, created_at) VALUES(?,?,?,?,'revoked',?,?,?,?)",
            (tenant, order_id, receipt_id, receipt["payment_seq"], amount, new_paid, status, created_row["created_at"]),
        )
        conn.execute(
            "DELETE FROM payment_receipts WHERE tenant=? AND order_id=? AND receipt_id=?",
            (tenant, order_id, receipt_id),
        )
        conn.execute("COMMIT")
        return _receipt_view(
            receipt_id, "revoked", order["amount_cents"], new_paid, status,
            created_row["created_at"], False,
        )
    finally:
        conn.close()

def refund_receipt(
    tenant: str, order_id: str, receipt_id: str, refund_id: str, amount_cents: int
) -> dict | None:
    """以凭据冲正：沿用冲正的幂等/上限/原子性；只有已核销凭据可冲正，冲正后凭据标记已冲正。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None

        existing = conn.execute(
            "SELECT amount_cents, balance_after_cents, created_at FROM ledger_entries "
            "WHERE tenant=? AND order_id=? AND ref_id=? AND entry_type='refund'",
            (tenant, order_id, refund_id),
        ).fetchone()
        if existing is not None:
            linked = conn.execute(
                "SELECT receipt_id, payment_seq FROM payment_receipts "
                "WHERE tenant=? AND order_id=? AND refund_id=? AND status='refunded'",
                (tenant, order_id, refund_id),
            ).fetchone()
            if existing["amount_cents"] != amount_cents or linked is None or linked["receipt_id"] != receipt_id:
                conn.execute("ROLLBACK")
                raise RefundIdMismatch("refund identifier reused with a different amount or target")
            # 幂等回放：余额、订单状态、时间均取首次冲正结果快照。
            snap = conn.execute(
                "SELECT paid_after_cents, status_after, created_at FROM payment_receipt_events "
                "WHERE tenant=? AND order_id=? AND payment_seq=? AND event='refunded'",
                (tenant, order_id, linked["payment_seq"]),
            ).fetchone()
            conn.execute("COMMIT")
            return {
                "receipt_id": receipt_id,
                "receipt_status": "refunded",
                "refund_id": refund_id,
                "refunded_cents": existing["amount_cents"],
                "paid_cents": snap["paid_after_cents"],
                "outstanding_cents": order["amount_cents"] - snap["paid_after_cents"],
                "status": snap["status_after"],
                "created_at": snap["created_at"],
                "idempotent_replay": True,
            }

        receipt = conn.execute(
            "SELECT amount_cents, status, payment_seq FROM payment_receipts "
            "WHERE tenant=? AND order_id=? AND receipt_id=?",
            (tenant, order_id, receipt_id),
        ).fetchone()
        if receipt is None:
            # 已撤销凭据冲正按冲突拒绝且数据不变；从未存在过才是 404。
            revoked = conn.execute(
                "SELECT 1 FROM payment_receipt_events "
                "WHERE tenant=? AND order_id=? AND receipt_id=? AND event='revoked' LIMIT 1",
                (tenant, order_id, receipt_id),
            ).fetchone()
            conn.execute("ROLLBACK")
            if revoked is not None:
                raise ReceiptNotRefundable("receipt revoked and cannot be refunded")
            return None
        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise ValueError("refund amount must be positive")
        if receipt["status"] == "pending":
            conn.execute("ROLLBACK")
            raise ReceiptNotRefundable("receipt must be confirmed before refund")
        if receipt["status"] == "refunded":
            conn.execute("ROLLBACK")
            raise ReceiptAlreadyRefunded("receipt already refunded")
        if amount_cents != receipt["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ReceiptAmountMismatch("refund amount does not match receipt amount")
        if amount_cents > order["paid_cents"]:
            conn.execute("ROLLBACK")
            raise RefundExceedsPaid("refund exceeds paid amount")

        new_paid = order["paid_cents"] - amount_cents
        next_seq = _next_seq(conn, tenant, order_id)
        created_row = conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, balance_after_cents) "
            "VALUES(?,?,?,'refund',?,?,?) RETURNING created_at",
            (tenant, order_id, next_seq, amount_cents, refund_id, new_paid),
        ).fetchone()
        status = _apply_balance(conn, tenant, order_id, order["amount_cents"], new_paid)
        conn.execute(
            "UPDATE payment_receipts SET status='refunded', refund_id=? "
            "WHERE tenant=? AND order_id=? AND receipt_id=?",
            (refund_id, tenant, order_id, receipt_id),
        )
        conn.execute(
            "INSERT INTO payment_receipt_events(tenant, order_id, receipt_id, payment_seq, event, "
            "amount_cents, paid_after_cents, status_after, created_at) VALUES(?,?,?,?,'refunded',?,?,?,?)",
            (tenant, order_id, receipt_id, receipt["payment_seq"], amount_cents, new_paid, status, created_row["created_at"]),
        )
        conn.execute("COMMIT")
        return {
            "receipt_id": receipt_id,
            "receipt_status": "refunded",
            "refund_id": refund_id,
            "refunded_cents": amount_cents,
            "paid_cents": new_paid,
            "outstanding_cents": order["amount_cents"] - new_paid,
            "status": status,
            "created_at": created_row["created_at"],
            "idempotent_replay": False,
        }
    finally:
        conn.close()
