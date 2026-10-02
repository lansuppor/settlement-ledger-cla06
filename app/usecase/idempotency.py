"""携带请求标识的幂等用例。

同一 (tenant, request_id) 只允许一次成功的业务执行：
- 首次请求：在单个 BEGIN IMMEDIATE 事务内完成 查重 -> 业务写入 -> 登记结果快照；
- 重复请求：业务内容一致时直接返回首次响应快照，不再产生任何业务效果；
- 冲突请求：同一标识携带不同业务内容（或用于不同业务端点）时拒绝，不写入任何数据。

登记表与业务表在同一事务内提交，SQLite 的写锁串行化保证并发提交下
最终只有一次请求真正产生业务效果；登记行持久化，服务重启后结论不变。
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from app.store.db import connect

ENDPOINT_CREATE_ORDER = "POST /orders"
ENDPOINT_ADD_PAYMENT = "POST /orders/{order_id}/payments"


class IdempotencyConflict(Exception):
    """请求标识已被用于不同的业务内容。"""


class PaymentExceedsOutstanding(Exception):
    """收款金额超过订单未收金额。"""


class OrderNotFound(Exception):
    """租户内不存在该订单。"""


@dataclass
class IdempotentResult:
    status_code: int
    body: dict
    replayed: bool = False


def _fingerprint(endpoint: str, content: dict) -> str:
    raw = json.dumps({"endpoint": endpoint, "content": content}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _order_from_row(row: sqlite3.Row) -> dict:
    return {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "paid_cents": row["paid_cents"],
        "currency": row["currency"],
        "status": row["status"],
        "outstanding_cents": row["amount_cents"] - row["paid_cents"],
    }


def _replay_or_none(conn: sqlite3.Connection, tenant: str, request_id: str, endpoint: str,
                    fingerprint: str) -> IdempotentResult | None:
    row = conn.execute(
        "SELECT endpoint, fingerprint, response_code, response_body FROM idempotency_keys "
        "WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["endpoint"] != endpoint or row["fingerprint"] != fingerprint:
        raise IdempotencyConflict("request id was already used with a different request")
    return IdempotentResult(row["response_code"], json.loads(row["response_body"]), replayed=True)


def create_order(tenant: str, order_id: str, amount_cents: int, currency: str,
                 request_id: str) -> IdempotentResult:
    endpoint = ENDPOINT_CREATE_ORDER
    fingerprint = _fingerprint(endpoint, {
        "order_id": order_id,
        "amount_cents": amount_cents,
        "currency": currency,
    })
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replayed = _replay_or_none(conn, tenant, request_id, endpoint, fingerprint)
        if replayed is not None:
            conn.execute("ROLLBACK")
            return replayed
        try:
            conn.execute(
                "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
                "VALUES(?,?,?,0,?,'accepted')",
                (tenant, order_id, amount_cents, currency),
            )
        except sqlite3.IntegrityError:
            # 同一租户重复受理：保持既有 409 语义，且不登记幂等键。
            conn.execute("ROLLBACK")
            raise
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        body = _order_from_row(row)
        conn.execute(
            "INSERT INTO idempotency_keys(tenant, request_id, endpoint, fingerprint, order_id, "
            "response_code, response_body) VALUES(?,?,?,?,?,?,?)",
            (tenant, request_id, endpoint, fingerprint, order_id, 201,
             json.dumps(body, ensure_ascii=False)),
        )
        conn.execute("COMMIT")
        return IdempotentResult(201, body)
    finally:
        conn.close()


def add_payment(tenant: str, order_id: str, amount_cents: int,
                request_id: str) -> IdempotentResult:
    endpoint = ENDPOINT_ADD_PAYMENT
    fingerprint = _fingerprint(endpoint, {
        "order_id": order_id,
        "amount_cents": amount_cents,
    })
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replayed = _replay_or_none(conn, tenant, request_id, endpoint, fingerprint)
        if replayed is not None:
            conn.execute("ROLLBACK")
            return replayed
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise OrderNotFound("order not found")
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise PaymentExceedsOutstanding("payment exceeds outstanding amount")
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        body = _order_from_row(row)
        conn.execute(
            "INSERT INTO idempotency_keys(tenant, request_id, endpoint, fingerprint, order_id, "
            "response_code, response_body) VALUES(?,?,?,?,?,?,?)",
            (tenant, request_id, endpoint, fingerprint, order_id, 200,
             json.dumps(body, ensure_ascii=False)),
        )
        conn.execute("COMMIT")
        return IdempotentResult(200, body)
    finally:
        conn.close()
