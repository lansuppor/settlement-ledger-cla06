"""订单条件检索存储层：按租户、状态/币种/金额区间筛选，并按受理顺序做 keyset 分页。

排序位置为 orders.accept_seq（受理时间线条目的全库单调序号）。它只在受理时写入、
之后永不改变（更正只在原行改金额/标识，不产生新订单），因此翻页过程中其他订单的
收款、冲正、核销、更正、退款只改变命中与否，绝不移动已翻过的游标位置。
"""
import sqlite3

from app.store.db import connect
from app.store.orders import ORDER_COLUMNS, _row_to_order

CURSOR_SECRET_KEY = "order_search_cursor_secret"


def get_cursor_secret() -> str:
    """读取游标 HMAC 密钥（随迁移生成并持久化，重启后不变）。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (CURSOR_SECRET_KEY,)
        ).fetchone()
        if row is not None:
            return row["value"]
        # 兼容未跑迁移的极端情况：补一份并持久化。
        secret = conn.execute("SELECT lower(hex(randomblob(32))) AS value").fetchone()["value"]
        conn.execute(
            "INSERT OR IGNORE INTO app_meta(key, value) VALUES(?,?)",
            (CURSOR_SECRET_KEY, secret),
        )
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (CURSOR_SECRET_KEY,)
        ).fetchone()
        return row["value"]
    finally:
        conn.close()


def load_cursor_conn(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT token, tenant, status_filter, currency_filter, min_amount, max_amount, after_accept_seq "
        "FROM order_search_cursors WHERE token=?",
        (token,),
    ).fetchone()


def save_cursor_conn(
    conn: sqlite3.Connection,
    token: str,
    tenant: str,
    status_filter: str,
    currency_filter: str,
    min_amount: int | None,
    max_amount: int | None,
    after_accept_seq: int,
) -> None:
    # 同一（租户、筛选、位置）复用同一令牌行：重复查询、服务重启后均可重放。
    conn.execute(
        "INSERT OR IGNORE INTO order_search_cursors"
        "(token, tenant, status_filter, currency_filter, min_amount, max_amount, after_accept_seq) "
        "VALUES(?,?,?,?,?,?,?)",
        (token, tenant, status_filter, currency_filter, min_amount, max_amount, after_accept_seq),
    )


def search_orders_conn(
    conn: sqlite3.Connection,
    tenant: str,
    status: str | None,
    currency: str | None,
    min_amount: int | None,
    max_amount: int | None,
    after_accept_seq: int,
    limit: int,
) -> list[tuple[dict, int]]:
    """按受理顺序返回命中的一页 (订单对象, accept_seq)，不含 after_accept_seq 位置。

    严格租户过滤，游标位置只增、只认 accept_seq；返回条数不足 limit 即最后一页。
    """
    where = ["tenant = ?", "accept_seq > ?"]
    params: list = [tenant, after_accept_seq]
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if currency is not None:
        where.append("currency = ?")
        params.append(currency)
    if min_amount is not None:
        where.append("amount_cents >= ?")
        params.append(min_amount)
    if max_amount is not None:
        where.append("amount_cents <= ?")
        params.append(max_amount)
    sql = (
        f"SELECT {ORDER_COLUMNS}, accept_seq FROM orders "
        f"WHERE {' AND '.join(where)} ORDER BY accept_seq LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [(_row_to_order_without_seq(row), row["accept_seq"]) for row in rows]


def _row_to_order_without_seq(row: sqlite3.Row) -> dict:
    order = _row_to_order(row)
    order.pop("accept_seq", None)
    return order
