import sqlite3

from app.store.db import connect


class RefundExceedsPaid(Exception):
    """冲正金额超过当前已收金额。"""

class RefundIdMismatch(Exception):
    """同一冲正标识被重试，但冲正金额与首次请求不一致。"""

class ReceiptNotFound(Exception):
    """凭据在（租户，订单）作用域内不存在（含已撤销且未再登记、跨租户）。"""

class ReceiptConflict(Exception):
    """凭据当前状态不允许该操作（重复登记、已核销撤销、待核销冲正等）。"""

def _ledger(conn: sqlite3.Connection, tenant: str, order_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT entry_type, amount_cents, ref_id, balance_after_cents, created_at, entry_seq, "
        "receipt_id, receipt_status "
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
        if row["receipt_id"] is not None:
            # 凭据收款行带标识与状态；以凭据冲正/撤销流水带凭据标识。
            entry["receipt_id"] = row["receipt_id"]
            if row["receipt_status"] is not None:
                entry["receipt_status"] = row["receipt_status"]
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

def _has_pending(conn: sqlite3.Connection, tenant: str, order_id: str) -> bool:
    # 待核销收款虽计入 paid_cents，但订单在其核销确认前不得结清。
    return conn.execute(
        "SELECT 1 FROM payment_receipts WHERE tenant=? AND order_id=? AND status='pending' LIMIT 1",
        (tenant, order_id),
    ).fetchone() is not None

def _status_for(conn: sqlite3.Connection, tenant: str, order_id: str,
                amount_cents: int, paid_cents: int) -> str:
    return "settled" if paid_cents >= amount_cents and not _has_pending(conn, tenant, order_id) else "accepted"

def _receipt_result(receipt_id: str, receipt_status: str, amount_cents: int, paid_cents: int,
                    order_status: str, idempotent_replay: bool) -> dict:
    return {
        "receipt_id": receipt_id,
        "receipt_status": receipt_status,
        "paid_cents": paid_cents,
        "outstanding_cents": amount_cents - paid_cents,
        "status": order_status,
        "idempotent_replay": idempotent_replay,
    }

_NEXT_SEQ = "COALESCE((SELECT MAX(entry_seq)+1 FROM ledger_entries WHERE tenant=? AND order_id=?),1)"

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

def add_payment(tenant: str, order_id: str, amount_cents: int,
                receipt_id: str | None = None) -> dict | None:
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
        if receipt_id is not None:
            clash = conn.execute(
                "SELECT 1 FROM payment_receipts WHERE tenant=? AND order_id=? AND receipt_id=? "
                "AND status IN ('pending','confirmed','refunded') LIMIT 1",
                (tenant, order_id, receipt_id),
            ).fetchone()
            if clash is not None:
                conn.execute("ROLLBACK")
                raise ReceiptConflict("receipt identifier already in use")
        new_paid = row["paid_cents"] + amount_cents
        try:
            if receipt_id is None:
                seq_row = conn.execute(
                    "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, "
                    "balance_after_cents, order_status_after) "
                    f"VALUES(?,?,{_NEXT_SEQ},'payment',?,NULL,?,NULL) RETURNING entry_seq",
                    (tenant, order_id, tenant, order_id, amount_cents, new_paid),
                ).fetchone()
            else:
                seq_row = conn.execute(
                    "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, "
                    "balance_after_cents, order_status_after, receipt_id, receipt_status) "
                    f"VALUES(?,?,{_NEXT_SEQ},'payment',?,NULL,?,NULL,?,'pending') RETURNING entry_seq",
                    (tenant, order_id, tenant, order_id, amount_cents, new_paid, receipt_id),
                ).fetchone()
                conn.execute(
                    "INSERT INTO payment_receipts(tenant, order_id, receipt_id, amount_cents, status, payment_seq) "
                    "VALUES(?,?,?,?,'pending',?)",
                    (tenant, order_id, receipt_id, amount_cents, seq_row["entry_seq"]),
                )
        except sqlite3.IntegrityError:
            # 并发登记同一生效凭据：唯一索引兜底，整体回滚不留痕。
            conn.execute("ROLLBACK")
            raise ReceiptConflict("receipt identifier already in use")
        # 状态在凭据落库后判定：新登记的待核销收款必然阻止结清。
        new_status = _status_for(conn, tenant, order_id, row["amount_cents"], new_paid)
        conn.execute(
            "UPDATE ledger_entries SET order_status_after=? WHERE tenant=? AND order_id=? AND entry_seq=?",
            (new_status, tenant, order_id, seq_row["entry_seq"]),
        )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        conn.execute("COMMIT")
        return get(tenant, order_id)
    finally:
        conn.close()

def _current_receipt(conn: sqlite3.Connection, tenant: str, order_id: str,
                     receipt_id: str) -> sqlite3.Row | None:
    """同名凭据可能有多个代次（撤销后再登记），操作始终针对最新代次。"""
    return conn.execute(
        "SELECT * FROM payment_receipts WHERE tenant=? AND order_id=? AND receipt_id=? ORDER BY id DESC LIMIT 1",
        (tenant, order_id, receipt_id),
    ).fetchone()

def _get_order_or_rollback(conn: sqlite3.Connection, tenant: str, order_id: str) -> sqlite3.Row:
    order = conn.execute(
        "SELECT amount_cents, paid_cents, status FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if order is None:
        conn.execute("ROLLBACK")
        raise _OrderMissing
    return order

class _OrderMissing(Exception):
    pass

def confirm_receipt(tenant: str, order_id: str, receipt_id: str) -> dict | None:
    """核销确认：待核销 → 已核销，订单按现有规则判定结清。重复确认回放首次结果。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = _get_order_or_rollback(conn, tenant, order_id)
        except _OrderMissing:
            return None
        receipt = _current_receipt(conn, tenant, order_id, receipt_id)
        if receipt is None or receipt["status"] == "revoked":
            conn.execute("ROLLBACK")
            raise ReceiptNotFound("receipt not found")
        if receipt["status"] == "confirmed":
            # 幂等重试：不重复留痕、不改余额，回放首次结果。
            conn.execute("COMMIT")
            return _receipt_result(receipt_id, "confirmed", order["amount_cents"],
                                   receipt["confirmed_paid_cents"], receipt["confirmed_status"], True)
        if receipt["status"] == "refunded":
            conn.execute("ROLLBACK")
            raise ReceiptConflict("receipt already refunded")

        # 先把凭据移出 pending，再判定订单结清（结清以“无待核销收款”为前提）。
        conn.execute(
            "UPDATE payment_receipts SET status='confirmed', confirmed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (receipt["id"],),
        )
        new_status = _status_for(conn, tenant, order_id, order["amount_cents"], order["paid_cents"])
        conn.execute(
            "UPDATE payment_receipts SET confirmed_paid_cents=?, confirmed_status=? WHERE id=?",
            (order["paid_cents"], new_status, receipt["id"]),
        )
        conn.execute(
            "UPDATE ledger_entries SET receipt_status='confirmed', order_status_after=? "
            "WHERE tenant=? AND order_id=? AND entry_seq=?",
            (new_status, tenant, order_id, receipt["payment_seq"]),
        )
        conn.execute(
            "UPDATE orders SET status=? WHERE tenant=? AND order_id=?",
            (new_status, tenant, order_id),
        )
        conn.execute("COMMIT")
        return _receipt_result(receipt_id, "confirmed", order["amount_cents"],
                               order["paid_cents"], new_status, False)
    finally:
        conn.close()

def revoke_receipt(tenant: str, order_id: str, receipt_id: str) -> dict | None:
    """撤销：整笔移除待核销收款，补撤销流水；已核销拒绝；重复撤销回放首次结果。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = _get_order_or_rollback(conn, tenant, order_id)
        except _OrderMissing:
            return None
        receipt = _current_receipt(conn, tenant, order_id, receipt_id)
        if receipt is None:
            conn.execute("ROLLBACK")
            raise ReceiptNotFound("receipt not found")
        if receipt["status"] == "revoked":
            # 幂等重试：返回首次撤销结果（撤销流水上的快照）。
            first = conn.execute(
                "SELECT balance_after_cents, order_status_after FROM ledger_entries "
                "WHERE tenant=? AND order_id=? AND entry_seq=?",
                (tenant, order_id, receipt["revocation_seq"]),
            ).fetchone()
            conn.execute("COMMIT")
            return _receipt_result(receipt_id, "revoked", order["amount_cents"],
                                   first["balance_after_cents"], first["order_status_after"], True)
        if receipt["status"] == "confirmed":
            conn.execute("ROLLBACK")
            raise ReceiptConflict("confirmed receipt cannot be revoked")
        if receipt["status"] == "refunded":
            conn.execute("ROLLBACK")
            raise ReceiptConflict("refunded receipt cannot be revoked")
        if receipt["amount_cents"] > order["paid_cents"]:
            # 待核销收款对应的余额已被普通冲正占用，整笔移除会破坏守恒：拒绝且数据不变。
            conn.execute("ROLLBACK")
            raise ReceiptConflict("receipt amount exceeds effective paid balance")

        new_paid = order["paid_cents"] - receipt["amount_cents"]
        seq_row = conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, "
            "balance_after_cents, order_status_after, receipt_id) "
            f"VALUES(?,?,{_NEXT_SEQ},'revocation',?,NULL,?,?,?) RETURNING entry_seq",
            (tenant, order_id, tenant, order_id, receipt["amount_cents"], new_paid, None, receipt_id),
        ).fetchone()
        # 先把凭据移出 pending，再判定订单状态并回填流水快照。
        conn.execute(
            "UPDATE payment_receipts SET status='revoked', revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "revocation_seq=? WHERE id=?",
            (seq_row["entry_seq"], receipt["id"]),
        )
        conn.execute(
            "UPDATE ledger_entries SET receipt_status='revoked' "
            "WHERE tenant=? AND order_id=? AND entry_seq=?",
            (tenant, order_id, receipt["payment_seq"]),
        )
        new_status = _status_for(conn, tenant, order_id, order["amount_cents"], new_paid)
        conn.execute(
            "UPDATE ledger_entries SET order_status_after=? WHERE tenant=? AND order_id=? AND entry_seq=?",
            (new_status, tenant, order_id, seq_row["entry_seq"]),
        )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        conn.execute("COMMIT")
        return _receipt_result(receipt_id, "revoked", order["amount_cents"],
                               new_paid, new_status, False)
    finally:
        conn.close()

def refund_receipt(tenant: str, order_id: str, receipt_id: str,
                   refund_id: str, amount_cents: int) -> dict | None:
    """以凭据冲正：沿用冲正幂等/上限/原子性规则；只有已核销收款可冲正。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = _get_order_or_rollback(conn, tenant, order_id)
        except _OrderMissing:
            return None

        existing = conn.execute(
            "SELECT amount_cents, balance_after_cents, order_status_after, created_at, receipt_id FROM ledger_entries "
            "WHERE tenant=? AND order_id=? AND ref_id=? AND entry_type='refund'",
            (tenant, order_id, refund_id),
        ).fetchone()
        if existing is not None:
            # 冲正标识幂等优先：重试原样回放首次结果，不重复留痕。
            if existing["amount_cents"] != amount_cents:
                conn.execute("ROLLBACK")
                raise RefundIdMismatch("refund identifier reused with a different amount")
            replayed_receipt = _current_receipt(conn, tenant, order_id, existing["receipt_id"])
            receipt_status = replayed_receipt["status"] if replayed_receipt is not None else "refunded"
            conn.execute("COMMIT")
            return {
                "receipt_id": existing["receipt_id"],
                "receipt_status": receipt_status,
                "refund_id": refund_id,
                "refunded_cents": existing["amount_cents"],
                "paid_cents": existing["balance_after_cents"],
                "outstanding_cents": order["amount_cents"] - existing["balance_after_cents"],
                "status": existing["order_status_after"],
                "created_at": existing["created_at"],
                "idempotent_replay": True,
            }

        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise ValueError("refund amount must be positive")

        receipt = _current_receipt(conn, tenant, order_id, receipt_id)
        if receipt is None:
            conn.execute("ROLLBACK")
            raise ReceiptNotFound("receipt not found")
        if receipt["status"] in ("pending", "revoked"):
            # 只有已核销收款可冲正：待核销或已撤销均 409 且数据不变。
            conn.execute("ROLLBACK")
            raise ReceiptConflict("only confirmed receipts can be refunded")
        if receipt["status"] == "refunded":
            conn.execute("ROLLBACK")
            raise ReceiptConflict("receipt already refunded")
        if amount_cents > order["paid_cents"]:
            conn.execute("ROLLBACK")
            raise RefundExceedsPaid("refund exceeds paid amount")

        new_paid = order["paid_cents"] - amount_cents
        new_status = _status_for(conn, tenant, order_id, order["amount_cents"], new_paid)
        created_row = conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, "
            "balance_after_cents, order_status_after, receipt_id) "
            f"VALUES(?,?,{_NEXT_SEQ},'refund',?,?,?,?,?) RETURNING created_at",
            (tenant, order_id, tenant, order_id, amount_cents, refund_id, new_paid, new_status, receipt_id),
        ).fetchone()
        conn.execute(
            "UPDATE payment_receipts SET status='refunded' WHERE id=?",
            (receipt["id"],),
        )
        conn.execute(
            "UPDATE ledger_entries SET receipt_status='refunded' "
            "WHERE tenant=? AND order_id=? AND entry_seq=?",
            (tenant, order_id, receipt["payment_seq"]),
        )
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
        )
        conn.execute("COMMIT")
        return {
            "receipt_id": receipt_id,
            "receipt_status": "refunded",
            "refund_id": refund_id,
            "refunded_cents": amount_cents,
            "paid_cents": new_paid,
            "outstanding_cents": order["amount_cents"] - new_paid,
            "status": new_status,
            "created_at": created_row["created_at"],
            "idempotent_replay": False,
        }
    finally:
        conn.close()

def add_refund(tenant: str, order_id: str, refund_id: str, amount_cents: int) -> dict | None:
    """对一笔订单冲正已收金额。

    订单不存在返回 None；冲正与余额减少在同一事务内提交；
    同一（租户，订单，冲正标识）重试返回首次结果。
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
        new_status = _status_for(conn, tenant, order_id, row["amount_cents"], new_paid)
        created_row = conn.execute(
            "INSERT INTO ledger_entries(tenant, order_id, entry_seq, entry_type, amount_cents, ref_id, "
            "balance_after_cents, order_status_after) "
            f"VALUES(?,?,{_NEXT_SEQ},'refund',?,?,?,?) RETURNING created_at",
            (tenant, order_id, tenant, order_id, amount_cents, refund_id, new_paid, new_status),
        ).fetchone()
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, new_status, tenant, order_id),
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
