"""批量导入订单：逗号分隔的文本按行受理，部分成功、逐行定论、可安全续跑。

每行格式：tenant,order_id,amount_cents,currency（四个字段，逗号分隔）。
- 空行不计入提交行数；首个非空行若与表头同名（tenant,order_id,amount_cents,currency）
  视为表头跳过，不计入提交行数；行号始终按物理行（从 1 开始）给出，便于定位修正。
- 每行独立校验、独立事务受理：任一行的问题不影响其他行；进程中途退出时，
  已提交的行保持生效，重试同一份输入时成功过的行按跳过处理，不产生重复订单。
- 同租户同订单标识且金额币种完全一致 -> 跳过；内容不一致 -> 失败；
  批量导入写入的订单不携带请求标识，后续收款/冲正/核销及其幂等规则照常适用。
"""
import sqlite3

from app.rules import order_rules
from app.store import orders
from app.store.db import connect

# 失败/跳过原因码（可区分，供调用方逐行定位修正）
REASON_INVALID_LINE = "invalid_line"  # 字段缺失或格式错误
REASON_INVALID_AMOUNT = "invalid_amount"  # 金额不是正整数
REASON_UNSUPPORTED_CURRENCY = "unsupported_currency"  # 币种不受支持
REASON_ORDER_ALREADY_EXISTS = "order_already_exists"  # 订单标识已存在（同租户重复受理，内容一致，跳过）
REASON_ORDER_CONTENT_MISMATCH = "order_content_mismatch"  # 订单标识与已有订单的业务内容不一致

HEADER_FIELDS = ("tenant", "order_id", "amount_cents", "currency")


class _LineError(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _parse_line(fields: list[str]) -> tuple[str, str, int, str]:
    """校验一行的四个字段，返回 (tenant, order_id, amount_cents, currency)。

    校验顺序：字段数量与缺失 -> 金额正整数 -> 币种受支持；
    一行同时有多个问题时报告按此顺序的第一个原因。
    """
    if len(fields) != 4:
        raise _LineError(
            REASON_INVALID_LINE,
            "expected 4 comma-separated fields: tenant,order_id,amount_cents,currency",
        )
    tenant, order_id, amount_raw, currency = fields
    missing = [
        name
        for name, value in zip(HEADER_FIELDS, fields, strict=True)
        if not value
    ]
    if missing:
        raise _LineError(REASON_INVALID_LINE, "missing required field(s): " + ", ".join(missing))
    if not (amount_raw.isascii() and amount_raw.isdigit()):
        raise _LineError(REASON_INVALID_AMOUNT, "amount_cents must be a positive integer")
    try:
        amount_cents = int(amount_raw)
    except ValueError:
        # 超出 int 转换上限的超长数字串同样按金额非法处理
        raise _LineError(REASON_INVALID_AMOUNT, "amount_cents must be a positive integer") from None
    if amount_cents <= 0:
        raise _LineError(REASON_INVALID_AMOUNT, "amount_cents must be a positive integer")
    try:
        order_rules.assert_currency(currency)
    except ValueError as error:
        raise _LineError(REASON_UNSUPPORTED_CURRENCY, str(error)) from None
    return tenant, order_id, amount_cents, currency


def _accept_line(
    conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int, currency: str
) -> str:
    """在独立写事务内受理一行，返回 "applied" / "skipped" / "mismatch"。

    已存在且内容一致时不重写任何数据；内容不一致时不改变已有订单。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = orders.get_conn(conn, tenant, order_id)
        if existing is None:
            orders.insert_conn(conn, tenant, order_id, amount_cents, currency)
            outcome = "applied"
        elif existing["amount_cents"] == amount_cents and existing["currency"] == currency:
            outcome = "skipped"
        else:
            outcome = "mismatch"
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return outcome


def import_orders_text(text: str) -> dict:
    """逐行受理文本中的订单，返回计数闭合的导入结果。

    保证：submitted == succeeded + skipped + failed；每行结论唯一确定；
    每行在独立事务中提交，中途失败后重试同一份输入安全（成功行跳过、
    失败行重新判定、不产生重复订单）。
    """
    submitted = succeeded = skipped = failed = 0
    failures: list[dict] = []
    skipped_lines: list[dict] = []

    conn = connect()
    try:
        first_content_line = True
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            fields = [part.strip() for part in line.split(",")]
            if first_content_line:
                first_content_line = False
                if tuple(field.lower() for field in fields) == HEADER_FIELDS:
                    # 表头行：不计入提交行数
                    continue
            submitted += 1
            order_id = fields[1] if len(fields) >= 2 and fields[1] else None
            try:
                tenant, order_id, amount_cents, currency = _parse_line(fields)
            except _LineError as error:
                failed += 1
                failures.append(
                    {"line": lineno, "order_id": order_id, "reason": error.reason, "detail": error.detail}
                )
                continue

            outcome = _accept_line(conn, tenant, order_id, amount_cents, currency)
            if outcome == "applied":
                succeeded += 1
            elif outcome == "skipped":
                skipped += 1
                skipped_lines.append(
                    {
                        "line": lineno,
                        "order_id": order_id,
                        "reason": REASON_ORDER_ALREADY_EXISTS,
                        "detail": "order already accepted with identical amount and currency",
                    }
                )
            else:
                failed += 1
                failures.append(
                    {
                        "line": lineno,
                        "order_id": order_id,
                        "reason": REASON_ORDER_CONTENT_MISMATCH,
                        "detail": "order_id already exists with different amount_cents or currency",
                    }
                )
    finally:
        conn.close()

    return {
        "submitted": submitted,
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "failures": failures,
        "skipped_lines": skipped_lines,
    }
