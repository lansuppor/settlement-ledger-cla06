"""幂等用例：同一租户内同一请求标识只产生一次业务效果。

并发语义依赖 SQLite 的 BEGIN IMMEDIATE：同一时刻只有一个请求能进入写事务，
后来的同标识请求在锁释放后读到的必然是已提交的首次记录，从而回放首次结果。
"""
import hashlib
import json
import sqlite3

from app.store import idempotency, orders
from app.store.db import connect

SCOPE_ORDER_CREATE = "order_create"
SCOPE_PAYMENT = "payment"
SCOPE_REVERSAL = "reversal"
SCOPE_RECONCILIATION = "reconciliation"
SCOPE_CORRECTION = "order_correction"
SCOPE_REFUND = "refund"


class IdempotentConflict(Exception):
    """同一请求标识对应了与首次不同的业务内容，或被用于不同类型的操作。"""


class IdemResult:
    def __init__(self, status_code: int, body: dict, replay: bool):
        self.status_code = status_code
        self.body = body
        self.replay = replay


def _fingerprint(parts: tuple) -> str:
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _finish(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def _abort(conn: sqlite3.Connection) -> None:
    conn.execute("ROLLBACK")


def accept_order(tenant: str, request_id: str, order_id: str, amount_cents: int, currency: str) -> IdemResult:
    request_hash = _fingerprint((SCOPE_ORDER_CREATE, order_id, amount_cents, currency))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = idempotency.get_conn(conn, tenant, request_id)
        if existing is not None:
            _finish(conn)
            if existing["scope"] != SCOPE_ORDER_CREATE or existing["request_hash"] != request_hash:
                raise IdempotentConflict("request id was already used with different request content")
            return IdemResult(existing["status_code"], json.loads(existing["response_json"]), replay=True)

        try:
            try:
                orders.insert_conn(conn, tenant, order_id, amount_cents, currency)
                status_code, body = 201, orders.get_conn(conn, tenant, order_id)
            except sqlite3.IntegrityError:
                # 同一租户重复受理（未携带本请求标识的既有订单/或首次结果即冲突）
                status_code, body = 409, {"detail": "order already accepted"}
            idempotency.insert_conn(
                conn, tenant, request_id, SCOPE_ORDER_CREATE, request_hash, order_id,
                status_code, json.dumps(body, ensure_ascii=False),
            )
            _finish(conn)
        except Exception:
            _abort(conn)
            raise
    finally:
        conn.close()
    return IdemResult(status_code, body, replay=False)


def register_payment(tenant: str, request_id: str, order_id: str, amount_cents: int) -> IdemResult:
    request_hash = _fingerprint((SCOPE_PAYMENT, order_id, amount_cents))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = idempotency.get_conn(conn, tenant, request_id)
        if existing is not None:
            _finish(conn)
            if existing["scope"] != SCOPE_PAYMENT or existing["request_hash"] != request_hash:
                raise IdempotentConflict("request id was already used with different request content")
            return IdemResult(existing["status_code"], json.loads(existing["response_json"]), replay=True)

        try:
            try:
                body = orders.pay_conn(conn, tenant, order_id, amount_cents, request_id)
                status_code = 200
            except LookupError:
                status_code, body = 404, {"detail": "order not found"}
            except ValueError as error:
                status_code, body = 409, {"detail": str(error)}
            idempotency.insert_conn(
                conn, tenant, request_id, SCOPE_PAYMENT, request_hash, order_id,
                status_code, json.dumps(body, ensure_ascii=False),
            )
            _finish(conn)
        except Exception:
            _abort(conn)
            raise
    finally:
        conn.close()
    return IdemResult(status_code, body, replay=False)


def reverse_payment(tenant: str, request_id: str, order_id: str, amount_cents: int) -> IdemResult:
    request_hash = _fingerprint((SCOPE_REVERSAL, order_id, amount_cents))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = idempotency.get_conn(conn, tenant, request_id)
        if existing is not None:
            _finish(conn)
            if existing["scope"] != SCOPE_REVERSAL or existing["request_hash"] != request_hash:
                raise IdempotentConflict("request id was already used with different request content")
            return IdemResult(existing["status_code"], json.loads(existing["response_json"]), replay=True)

        try:
            try:
                body = orders.reverse_conn(conn, tenant, order_id, amount_cents, request_id)
                status_code = 200
            except LookupError:
                status_code, body = 404, {"detail": "order not found"}
            except ValueError as error:
                status_code, body = 409, {"detail": str(error)}
            idempotency.insert_conn(
                conn, tenant, request_id, SCOPE_REVERSAL, request_hash, order_id,
                status_code, json.dumps(body, ensure_ascii=False),
            )
            _finish(conn)
        except Exception:
            _abort(conn)
            raise
    finally:
        conn.close()
    return IdemResult(status_code, body, replay=False)


def reconcile_order(tenant: str, request_id: str, order_id: str, amount_cents: int) -> IdemResult:
    request_hash = _fingerprint((SCOPE_RECONCILIATION, order_id, amount_cents))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = idempotency.get_conn(conn, tenant, request_id)
        if existing is not None:
            _finish(conn)
            if existing["scope"] != SCOPE_RECONCILIATION or existing["request_hash"] != request_hash:
                raise IdempotentConflict("request id was already used with different request content")
            return IdemResult(existing["status_code"], json.loads(existing["response_json"]), replay=True)

        try:
            try:
                body = orders.reconcile_conn(conn, tenant, order_id, amount_cents, request_id)
                status_code = 200
            except LookupError:
                status_code, body = 404, {"detail": "order not found"}
            except ValueError as error:
                status_code, body = 409, {"detail": str(error)}
            idempotency.insert_conn(
                conn, tenant, request_id, SCOPE_RECONCILIATION, request_hash, order_id,
                status_code, json.dumps(body, ensure_ascii=False),
            )
            _finish(conn)
        except Exception:
            _abort(conn)
            raise
    finally:
        conn.close()
    return IdemResult(status_code, body, replay=False)


def refund_order(
    tenant: str,
    request_id: str,
    order_id: str,
    amount_cents: int,
    reason: str,
) -> IdemResult:
    request_hash = _fingerprint((SCOPE_REFUND, order_id, amount_cents, reason))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = idempotency.get_conn(conn, tenant, request_id)
        if existing is not None:
            _finish(conn)
            if existing["scope"] != SCOPE_REFUND or existing["request_hash"] != request_hash:
                raise IdempotentConflict("request id was already used with different request content")
            return IdemResult(existing["status_code"], json.loads(existing["response_json"]), replay=True)

        try:
            try:
                body = orders.refund_conn(conn, tenant, order_id, amount_cents, reason, request_id)
                status_code = 200
            except LookupError:
                status_code, body = 404, {"detail": "order not found"}
            except ValueError as error:
                # 含 RefundRejected：业务拒绝结论已随退款留痕落库，此处只固化响应
                status_code, body = 409, {"detail": str(error)}
            idempotency.insert_conn(
                conn, tenant, request_id, SCOPE_REFUND, request_hash, order_id,
                status_code, json.dumps(body, ensure_ascii=False),
            )
            _finish(conn)
        except Exception:
            _abort(conn)
            raise
    finally:
        conn.close()
    return IdemResult(status_code, body, replay=False)


def correct_order(
    tenant: str,
    request_id: str,
    order_id: str,
    new_order_id: str,
    new_amount_cents: int,
    new_currency: str,
) -> IdemResult:
    request_hash = _fingerprint((SCOPE_CORRECTION, order_id, new_order_id, new_amount_cents, new_currency))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = idempotency.get_conn(conn, tenant, request_id)
        if existing is not None:
            _finish(conn)
            if existing["scope"] != SCOPE_CORRECTION or existing["request_hash"] != request_hash:
                raise IdempotentConflict("request id was already used with different request content")
            return IdemResult(existing["status_code"], json.loads(existing["response_json"]), replay=True)

        try:
            try:
                body = orders.correct_conn(
                    conn, tenant, order_id, new_order_id, new_amount_cents, new_currency, request_id
                )
                status_code = 200
            except LookupError:
                status_code, body = 404, {"detail": "order not found"}
            except ValueError as error:
                # 含 CorrectionRejected：业务拒绝结论已随更正留痕落库，此处只固化响应
                status_code, body = 409, {"detail": str(error)}
            idempotency.insert_conn(
                conn, tenant, request_id, SCOPE_CORRECTION, request_hash, order_id,
                status_code, json.dumps(body, ensure_ascii=False),
            )
            _finish(conn)
        except Exception:
            _abort(conn)
            raise
    finally:
        conn.close()
    return IdemResult(status_code, body, replay=False)
