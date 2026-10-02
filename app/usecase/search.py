"""订单条件检索用例：租户内按状态/币种/金额区间筛选，按受理顺序稳定分页。

游标为服务端不透明令牌：以库内随机密钥对（租户、筛选条件、分页位置）计算 HMAC，
摘要本身不暴露 accept_seq 等内部位置；映射持久化在 order_search_cursors，
令牌对同一（租户、筛选、位置）确定性生成，故同租户同条件同游标的多次查询
（含服务重启后）返回完全相同的订单集合与顺序。

分页正确性：位置只认 orders.accept_seq —— 受理顺序的全库单调序号，一经写入
不再改变。翻页过程中其他订单的收款/冲正/核销/更正/退款只改变其当前状态与命中
与否，不会移动任何 accept_seq，因此已翻过的页不重复、未翻到的页不因状态变化
被跳过：继续命中的订单位置固定，状态/金额/币种变化导致掉出结果集的订单只是
不再出现，新受理订单的序号恒大于所有已发游标位置，只会在后续页末出现。
"""
import hashlib
import hmac
import json

from app.rules import order_rules
from app.store import search as search_store
from app.store.db import connect

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# 当前订单状态：accepted=未收清，settled=已结清
STATUS_ACCEPTED = "accepted"
STATUS_SETTLED = "settled"
_ALLOWED_STATUS = (STATUS_ACCEPTED, STATUS_SETTLED)


class InvalidQuery(ValueError):
    """检索参数非法（条数越界、状态/币种不支持、金额区间非法或游标无效）：返回 400。"""


def _parse_optional_int(raw: str | None, field: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidQuery(f"{field} must be an integer in minor units")
    if value < 0:
        raise InvalidQuery(f"{field} must be a non-negative integer in minor units")
    return value


def _parse_limit(raw: str | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidQuery(f"limit must be an integer between 1 and {MAX_LIMIT}")
    if value < 1 or value > MAX_LIMIT:
        raise InvalidQuery(f"limit must be an integer between 1 and {MAX_LIMIT}")
    return value


def _validate_filters(
    status: str | None, currency: str | None, min_amount: int | None, max_amount: int | None
) -> tuple[str, str, int | None, int | None]:
    status = (status or "").strip()
    currency = (currency or "").strip()
    if status and status not in _ALLOWED_STATUS:
        raise InvalidQuery("status must be one of: accepted, settled")
    if currency:
        try:
            order_rules.assert_currency(currency)
        except ValueError:
            raise InvalidQuery(f"unsupported currency: {currency}")
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise InvalidQuery("min_amount must not be greater than max_amount")
    return status, currency, min_amount, max_amount


def _make_token(
    secret: str,
    tenant: str,
    status_filter: str,
    currency_filter: str,
    min_amount: int | None,
    max_amount: int | None,
    after_accept_seq: int,
) -> str:
    # 令牌为带库内密钥的 HMAC 摘要，载荷为规范化 JSON（与项目幂等指纹同一编码约定），
    # 不含租户地址或 accept_seq 等内部位置；v1 版本前缀便于将来调整令牌形态。
    canonical = json.dumps(
        [
            "v1",
            tenant,
            status_filter,
            currency_filter,
            min_amount,
            max_amount,
            after_accept_seq,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def search_orders(
    tenant: str,
    status_raw: str | None = None,
    currency_raw: str | None = None,
    min_amount_raw: str | None = None,
    max_amount_raw: str | None = None,
    limit_raw: str | None = None,
    cursor: str | None = None,
) -> dict:
    """执行一次条件检索，返回 {"orders": [...], "next_cursor": "..."}。"""
    limit = _parse_limit(limit_raw)
    min_amount = _parse_optional_int(min_amount_raw, "min_amount")
    max_amount = _parse_optional_int(max_amount_raw, "max_amount")
    status, currency, min_amount, max_amount = _validate_filters(
        status_raw, currency_raw, min_amount, max_amount
    )

    secret = search_store.get_cursor_secret()
    conn = connect()
    try:
        if cursor:
            row = search_store.load_cursor_conn(conn, cursor)
            # 游标无效或不属于本租户一律按非法游标拒绝，不据其读取任何数据。
            if row is None or row["tenant"] != tenant:
                raise InvalidQuery("invalid cursor")
            bound_status = row["status_filter"]
            bound_currency = row["currency_filter"]
            bound_min = row["min_amount"]
            bound_max = row["max_amount"]
            # 本次显式给出的筛选必须与游标绑定的筛选一致，避免同一游标跨条件混用。
            if (
                (status and status != bound_status)
                or (currency and currency != bound_currency)
                or (min_amount is not None and min_amount != bound_min)
                or (max_amount is not None and max_amount != bound_max)
            ):
                raise InvalidQuery("filter does not match the cursor")
            status, currency, min_amount, max_amount = bound_status, bound_currency, bound_min, bound_max
            after_accept_seq = row["after_accept_seq"]
        else:
            after_accept_seq = 0

        page = search_store.search_orders_conn(
            conn,
            tenant,
            status or None,
            currency or None,
            min_amount,
            max_amount,
            after_accept_seq,
            limit,
        )

        next_cursor = ""
        if len(page) == limit:
            # 本页取满：下一页从本页最后一条的受理序号之后继续（可能其后已无命中，
            # 届时下一次返回空列表与空游标，仍满足不重不漏）。
            last_seq = page[-1][1]
            token = _make_token(secret, tenant, status, currency, min_amount, max_amount, last_seq)
            search_store.save_cursor_conn(
                conn, token, tenant, status, currency, min_amount, max_amount, last_seq
            )
            conn.commit()
            next_cursor = token
        return {"orders": [item for item, _ in page], "next_cursor": next_cursor}
    finally:
        conn.close()
