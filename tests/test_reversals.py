import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("APP_DB", os.path.join(_tmp_dir, "reversals.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

TENANT = "trev"


def _accept(order_id: str, amount: int = 1000, key: str | None = None) -> None:
    headers = {"Idempotency-Key": key} if key else {}
    resp = client.post(
        "/orders",
        json={"tenant": TENANT, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
        headers=headers,
    )
    assert resp.status_code in (201, 409)


def _pay(order_id: str, amount: int, key: str | None = None, tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers=headers)


def _reverse(order_id: str, amount: int, key: str | None = None, tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/orders/{order_id}/reversals", json={"amount_cents": amount}, headers=headers)


def _get(order_id: str, tenant: str = TENANT) -> dict:
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant}).json()


def _reversals(order_id: str, tenant: str = TENANT):
    return client.get(f"/orders/{order_id}/reversals", headers={"X-Tenant": tenant})


def test_reversal_decreases_paid_and_increases_outstanding() -> None:
    _accept("rv1", 500)
    assert _pay("rv1", 300).status_code == 200
    resp = _reverse("rv1", 200)
    assert resp.status_code == 200
    body = resp.json()
    assert body["paid_cents"] == 100
    assert body["outstanding_cents"] == 400
    assert body["status"] == "accepted"
    # 任意时刻已收 + 未收 = 订单金额
    assert body["paid_cents"] + body["outstanding_cents"] == 500


def test_pay_reverse_pay_conservation_and_resettle() -> None:
    _accept("rv2", 500)
    _pay("rv2", 500)
    assert _get("rv2")["status"] == "settled"

    reversed_order = _reverse("rv2", 500).json()
    assert reversed_order["paid_cents"] == 0
    assert reversed_order["outstanding_cents"] == 500
    assert reversed_order["status"] == "accepted"

    again = _pay("rv2", 500).json()
    assert again["paid_cents"] == 500 and again["outstanding_cents"] == 0
    assert again["status"] == "settled"


def test_reversal_below_zero_or_above_paid_is_rejected() -> None:
    _accept("rv3", 500)
    _pay("rv3", 200)

    over = _reverse("rv3", 201)
    assert over.status_code == 409
    assert over.json()["detail"] == "reversal amount exceeds paid amount"

    zero = _reverse("rv3", 0)
    assert zero.status_code == 409
    assert zero.json()["detail"] == "reversal amount must be greater than zero"

    negative = _reverse("rv3", -10)
    assert negative.status_code == 409
    assert negative.json()["detail"] == "reversal amount must be greater than zero"

    # 被拒绝后金额、状态与留痕均不变
    got = _get("rv3")
    assert got["paid_cents"] == 200 and got["outstanding_cents"] == 300
    ledger = _reversals("rv3").json()["reversals"]
    assert ledger == []


def test_reversal_cannot_go_below_reconciled_amount() -> None:
    _accept("rv4", 500)
    _pay("rv4", 400)
    # 直接在库中登记已核销金额 300（当前尚无核销业务接口）
    conn = connect()
    try:
        conn.execute("UPDATE orders SET reconciled_cents=300 WHERE tenant=? AND order_id=?", (TENANT, "rv4"))
    finally:
        conn.close()

    blocked = _reverse("rv4", 200)  # 冲正后已收 200 < 已核销 300
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "reversal would make paid amount less than reconciled amount"
    assert _get("rv4")["paid_cents"] == 400

    ok = _reverse("rv4", 100)  # 冲正后已收 300 == 已核销 300，允许
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 300


def test_reversal_errors_distinguishable_from_missing_and_cross_tenant() -> None:
    # 订单不存在：404，原因不同于金额非法的 409
    assert _reverse("rv-missing", 10).status_code == 404
    # 跨租户冲正：按不存在处理
    _accept("rv5", 500)
    _pay("rv5", 100)
    assert _reverse("rv5", 10, tenant="other-tenant").status_code == 404
    # 跨租户读取留痕同样 404
    assert _reversals("rv5", tenant="other-tenant").status_code == 404
    # 金额、状态、留痕不受跨租户尝试影响
    assert _get("rv5")["paid_cents"] == 100


def test_reversal_ledger_reads_each_amount_and_result_in_order() -> None:
    _accept("rv6", 1000)
    _pay("rv6", 800)
    assert _reverse("rv6", 500, key="rev-rv6-1").status_code == 200
    assert _reverse("rv6", 100, key="rev-rv6-2").status_code == 200

    resp = _reversals("rv6")
    assert resp.status_code == 200
    records = resp.json()["reversals"]
    assert [r["amount_cents"] for r in records] == [500, 100]
    assert all(r["result"] == "applied" for r in records)
    assert [r["paid_after"] for r in records] == [300, 200]
    assert [r["request_id"] for r in records] == ["rev-rv6-1", "rev-rv6-2"]
    # 留痕顺序与金额合计解释了当前已收：800 - 500 - 100 = 200
    assert _get("rv6")["paid_cents"] == 200


def test_idempotent_reversal_applies_once_and_replays() -> None:
    _accept("rv7", 500)
    _pay("rv7", 400)
    first = _reverse("rv7", 150, key="rev-key-1")
    assert first.status_code == 200 and first.json()["paid_cents"] == 250

    replay = _reverse("rv7", 150, key="rev-key-1")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    got = _get("rv7")
    assert got["paid_cents"] == 250  # 没有重复冲正
    assert len(_reversals("rv7").json()["reversals"]) == 1


def test_reversal_key_conflicts_rejected_without_change() -> None:
    _accept("rv8", 500)
    _pay("rv8", 400)
    assert _reverse("rv8", 100, key="rev-key-x").status_code == 200

    # 同标识不同金额
    conflict_amount = _reverse("rv8", 200, key="rev-key-x")
    assert conflict_amount.status_code == 422
    # 同标识用于另一张订单
    _accept("rv8b", 500)
    _pay("rv8b", 400)
    conflict_order = _reverse("rv8b", 100, key="rev-key-x")
    assert conflict_order.status_code == 422
    # 同标识用于不同操作类型（收款）
    conflict_scope = _pay("rv8", 100, key="rev-key-x")
    assert conflict_scope.status_code == 422

    assert _get("rv8")["paid_cents"] == 300
    assert _get("rv8b")["paid_cents"] == 400
    assert len(_reversals("rv8").json()["reversals"]) == 1


def test_first_rejected_reversal_replays_same_conclusion() -> None:
    _accept("rv9", 300)
    _pay("rv9", 100)
    first = _reverse("rv9", 200, key="rev-key-bad")  # 超过已收
    assert first.status_code == 409
    replay = _reverse("rv9", 200, key="rev-key-bad")
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert _get("rv9")["paid_cents"] == 100
    assert _reversals("rv9").json()["reversals"] == []


def test_concurrent_same_reversal_key_applies_once() -> None:
    _accept("rv10", 1000)
    _pay("rv10", 500)
    headers = {"X-Tenant": TENANT, "Idempotency-Key": "rev-conc-1"}

    def submit(_: int):
        return TestClient(app).post("/orders/rv10/reversals", json={"amount_cents": 200}, headers=headers)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["paid_cents"] == 300 for r in responses)
    assert len(_reversals("rv10").json()["reversals"]) == 1


def test_concurrent_payments_and_reversals_stay_consistent() -> None:
    _accept("rv11", 1000)
    _pay("rv11", 500)  # 预留已收，保证任意调度顺序下冲正都不会超额

    def submit(spec):
        kind, amount, key = spec
        client_local = TestClient(app)
        headers = {"X-Tenant": TENANT, "Idempotency-Key": key}
        path = f"/orders/rv11/{'payments' if kind == 'pay' else 'reversals'}"
        return client_local.post(path, json={"amount_cents": amount}, headers=headers)

    specs = [("pay", 100, f"rv11-p-{i}") for i in range(4)]
    specs += [("rev", 50, f"rv11-r-{i}") for i in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, specs))
    assert all(r.status_code == 200 for r in responses)

    got = _get("rv11")
    # 500(初始) + 4*100(收款) - 4*50(冲正) = 700
    assert got["paid_cents"] == 700
    assert got["outstanding_cents"] == 300
    assert got["paid_cents"] >= 0
    assert got["paid_cents"] + got["outstanding_cents"] == 1000

    conn = connect()
    try:
        paid_sum = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM payment_records WHERE tenant=? AND order_id=?",
            (TENANT, "rv11"),
        ).fetchone()["s"]
        rev_count = conn.execute(
            "SELECT COUNT(*) AS c FROM reversal_records WHERE tenant=? AND order_id=? AND result='applied'",
            (TENANT, "rv11"),
        ).fetchone()["c"]
        rev_sum = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM reversal_records "
            "WHERE tenant=? AND order_id=? AND result='applied'",
            (TENANT, "rv11"),
        ).fetchone()["s"]
    finally:
        conn.close()
    assert paid_sum == 900 and rev_count == 4 and rev_sum == 200
    assert paid_sum - rev_sum == got["paid_cents"]


def test_reversals_survive_restart() -> None:
    # 模拟重启：丢弃进程内状态后用全新客户端访问同一数据库文件
    _accept("rv12", 500, key="rev-rv12-order")
    _pay("rv12", 400, key="rev-rv12-pay")
    assert _reverse("rv12", 300, key="rev-rv12-rev").status_code == 200

    client2 = TestClient(app)
    got = client2.get("/orders/rv12", headers={"X-Tenant": TENANT}).json()
    assert got["paid_cents"] == 100 and got["outstanding_cents"] == 400

    # 重启后重复冲正：回放首次结果，不再次冲正
    replay = client2.post(
        "/orders/rv12/reversals", json={"amount_cents": 300},
        headers={"X-Tenant": TENANT, "Idempotency-Key": "rev-rv12-rev"},
    )
    assert replay.status_code == 200 and replay.json()["paid_cents"] == 100

    # 重启后留痕仍可按订单读出
    records = client2.get("/orders/rv12/reversals", headers={"X-Tenant": TENANT}).json()["reversals"]
    assert len(records) == 1 and records[0]["amount_cents"] == 300 and records[0]["result"] == "applied"


def test_migration_is_versioned_and_runs_once() -> None:
    # 旧库（只有 001/002 的结构）升级到 003，且重复迁移不会因 ALTER TABLE 报错
    from app.store.db import MIGRATIONS_DIR
    original_db = os.environ["APP_DB"]
    legacy = os.path.join(_tmp_dir, "legacy.sqlite")
    os.environ["APP_DB"] = legacy
    try:
        conn = connect()
        try:
            for name in ("001_init.sql", "002_idempotency.sql"):
                conn.executescript((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        finally:
            conn.close()

        migrate()
        migrate()  # 第二次不应重复执行 ALTER TABLE ADD COLUMN

        conn = connect()
        try:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
            assert "reconciled_cents" in cols
            conn.execute(
                "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
                "VALUES('t','o',10,0,'CNY','accepted')"
            )
        finally:
            conn.close()
    finally:
        # 恢复默认测试库路径，避免影响其他用例
        os.environ["APP_DB"] = original_db
    assert order_store.get(TENANT, "rv1") is not None
