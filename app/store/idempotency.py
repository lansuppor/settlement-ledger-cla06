import sqlite3


def get_conn(conn: sqlite3.Connection, tenant: str, request_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT tenant, request_id, scope, request_hash, order_id, status_code, response_json "
        "FROM idempotent_requests WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()

def insert_conn(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    scope: str,
    request_hash: str,
    order_id: str | None,
    status_code: int,
    response_json: str,
) -> None:
    conn.execute(
        "INSERT INTO idempotent_requests(tenant, request_id, scope, request_hash, order_id, status_code, response_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (tenant, request_id, scope, request_hash, order_id, status_code, response_json),
    )
