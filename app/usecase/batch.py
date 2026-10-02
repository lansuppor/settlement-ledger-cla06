"""批量导入订单：逗号分隔文本按行受理，部分成功、逐行结论、可安全重试。

每一行在独立的写事务中处理：整行合法且受理成功才落库，任一行的问题
不影响其他行；导入中途失败时已受理的行保持生效。导入不携带请求标识、
不占用幂等记录；已受理订单的重复导入按「跳过」处理，金额或币种不一致
按「失败」处理，已有数据均不被改变，因此同一份输入重试任意多次都不会
产生重复订单，每行结论由当前库内状态唯一确定。
"""
import re
import sqlite3

from app.rules import order_rules
from app.store import orders
from app.store.db import connect

# 失败/跳过原因码：调用方可据此区分并定位修正
REASON_INVALID_FIELDS = "invalid_fields"            # 字段缺失或格式错误
REASON_INVALID_AMOUNT = "invalid_amount"            # 金额不是正整数
REASON_UNSUPPORTED_CURRENCY = "unsupported_currency"  # 币种不受支持
REASON_ORDER_CONFLICT = "order_conflict"            # 订单标识与已有订单的业务内容不一致
REASON_ORDER_EXISTS = "order_exists"                # 订单标识已存在（同租户重复受理，按跳过处理）

HEADER_LINE = "tenant,order_id,amount_cents,currency"
_AMOUNT_RE = re.compile(r"[0-9]+")
_MAX_INT64 = 2**63 - 1

OUTCOME_ACCEPTED = "accepted"
OUTCOME_SKIPPED = "skipped"
OUTCOME_CONFLICT = "conflict"


def _parse_line(raw: str) -> tuple[tuple[str, str, int, str] | None, str | None, str | None, str | None]:
    """解析并校验一行。返回 (合法字段, 可解析出的订单标识, 失败原因码, 失败说明)。"""
    parts = [part.strip() for part in raw.split(",")]
    order_id = parts[1] if len(parts) >= 2 and parts[1] else None
    if len(parts) != 4 or any(not part for part in parts):
        return None, order_id, REASON_INVALID_FIELDS, (
            "line must contain exactly 4 non-empty fields: tenant,order_id,amount_cents,currency"
        )
    tenant, order_id, amount_raw, currency = parts
    if not _AMOUNT_RE.fullmatch(amount_raw):
        return None, order_id, REASON_INVALID_AMOUNT, "amount must be a positive integer in minor units"
    amount = int(amount_raw)
    if amount <= 0 or amount > _MAX_INT64:
        return None, order_id, REASON_INVALID_AMOUNT, "amount must be a positive integer in minor units"
    if currency not in order_rules.ALLOWED_CURRENCIES:
        return None, order_id, REASON_UNSUPPORTED_CURRENCY, f"unsupported currency: {currency}"
    return (tenant, order_id, amount, currency), order_id, None, None


def _accept_line(conn: sqlite3.Connection, tenant: str, order_id: str, amount: int, currency: str) -> str:
    """在独立写事务内受理一行：不存在则落库，一致则跳过，不一致则冲突。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = orders.get_conn(conn, tenant, order_id)
        if existing is None:
            orders.insert_conn(conn, tenant, order_id, amount, currency)
            outcome = OUTCOME_ACCEPTED
        elif existing["amount_cents"] == amount and existing["currency"] == currency:
            outcome = OUTCOME_SKIPPED
        else:
            outcome = OUTCOME_CONFLICT
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return outcome


def import_orders(text: str) -> dict:
    """逐行受理逗号分隔的订单文本，返回计数闭合的导入结果。

    计数闭合：submitted = succeeded + skipped + failed。全空白行与首个
    内容行之前的表头行（tenant,order_id,amount_cents,currency）不计入提交行数；
    行号按输入文本的物理行从 1 计，便于调用方定位修正后重试。
    """
    failures: list[dict] = []
    skips: list[dict] = []
    succeeded = 0
    conn = connect()
    try:
        content_seen = False
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if not raw.strip():
                continue
            if not content_seen:
                content_seen = True
                if raw.strip().lower() == HEADER_LINE:
                    continue
            parsed, order_id, reason, detail = _parse_line(raw)
            if parsed is None:
                failures.append({"line": lineno, "order_id": order_id, "reason": reason, "detail": detail})
                continue
            tenant, order_id, amount, currency = parsed
            outcome = _accept_line(conn, tenant, order_id, amount, currency)
            if outcome == OUTCOME_ACCEPTED:
                succeeded += 1
            elif outcome == OUTCOME_SKIPPED:
                skips.append({
                    "line": lineno,
                    "order_id": order_id,
                    "reason": REASON_ORDER_EXISTS,
                    "detail": "order already accepted with identical content",
                })
            else:
                failures.append({
                    "line": lineno,
                    "order_id": order_id,
                    "reason": REASON_ORDER_CONFLICT,
                    "detail": "order id already exists with different amount or currency",
                })
    finally:
        conn.close()
    skipped = len(skips)
    failed = len(failures)
    return {
        "submitted": succeeded + skipped + failed,
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "failures": failures,
        "skips": skips,
    }
