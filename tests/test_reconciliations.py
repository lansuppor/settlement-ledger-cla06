import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("APP_DB", os.path.join(_tmp_dir, "reconciliations.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

TENANT = "trec"


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


def _reconcile(order_id: str, amount: int, key: str | None = None, tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/orders/{order_id}/reconciliations", json={"amount_cents": amount}, headers=headers)


def _get(order_id: str, tenant: str = TENANT) -> dict:
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant}).json()


def _reconciliations(order_id: str, tenant: str = TENANT):
    return client.get(f"/orders/{order_id}/reconciliations", headers={"X-Tenant": tenant})


def test_reconcile_accumulates_without_changing_paid_or_outstanding() -> None:
    _accept("rc1", 500)
    _pay("rc1", 400)
    resp = _reconcile("rc1", 300)
    assert resp.status_code == 200
    body = resp.json()
    # 核销只改变认定口径：已收、未收、订单金额均不变
    assert body["reconciled_cents"] == 300
    assert body["paid_cents"] == 400
    assert body["outstanding_cents"] == 100
    assert body["paid_cents"] + body["outstanding_cents"] == 500

    again = _reconcile("rc1", 100)
    assert again.status_code == 200
    assert again.json()["reconciled_cents"] == 400
    assert again.json()["paid_cents"] == 400


def test_reconcile_rejects_nonpositive_and_overpaid_without_change() -> None:
    _accept("rc2", 500)
    _pay("rc2", 400)
    _reconcile("rc2", 300)

    zero = _reconcile("rc2", 0)
    assert zero.status_code == 409
    assert zero.json()["detail"] == "reconciliation amount must be greater than zero"

    negative = _reconcile("rc2", -10)
    assert negative.status_code == 409
    assert negative.json()["detail"] == "reconciliation amount must be greater than zero"

    over = _reconcile("rc2", 101)  # 累计 300+101 > 已收 400
    assert over.status_code == 409
    assert over.json()["detail"] == "reconciled amount would exceed paid amount"

    # 被拒绝后金额、状态与核销留痕均不变
    got = _get("rc2")
    assert got["reconciled_cents"] == 300
    assert got["paid_cents"] == 400 and got["outstanding_cents"] == 100
    ledger = _reconciliations("rc2").json()["reconciliations"]
    assert len(ledger) == 1 and ledger[0]["amount_cents"] == 300


def test_reconcile_errors_distinguishable_from_missing_and_cross_tenant() -> None:
    # 订单不存在：404，原因不同于金额非法的 409
    assert _reconcile("rc-missing", 10).status_code == 404
    # 跨租户核销：按不存在处理
    _accept("rc3", 500)
    _pay("rc3", 100)
    assert _reconcile("rc3", 10, tenant="other-tenant").status_code == 404
    # 跨租户读取核销留痕同样 404
    assert _reconciliations("rc3", tenant="other-tenant").status_code == 404
    # 金额与留痕不受跨租户尝试影响
    assert _get("rc3")["reconciled_cents"] == 0
    assert _reconciliations("rc3").json()["reconciliations"] == []


def test_reconciled_amount_is_floor_for_reversal() -> None:
    _accept("rc4", 500)
    _pay("rc4", 400)
    assert _reconcile("rc4", 300).status_code == 200

    # 核销生效后立即可约束冲正：冲正后已收 200 < 已核销 300，拒绝
    blocked = _reverse("rc4", 200)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "reversal would make paid amount less than reconciled amount"
    # 与「超过当前已收」的拒绝原因可区分
    over = _reverse("rc4", 500)
    assert over.status_code == 409
    assert over.json()["detail"] == "reversal amount exceeds paid amount"

    # 拒绝不改变任何金额、状态与留痕
    got = _get("rc4")
    assert got["paid_cents"] == 400 and got["reconciled_cents"] == 300

    # 冲正后已收 300 == 已核销 300，允许
    ok = _reverse("rc4", 100)
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 300
    # 此后累计核销仍不得超过（已被冲正降低的）已收
    still_blocked = _reconcile("rc4", 1)
    assert still_blocked.status_code == 409
    assert still_blocked.json()["detail"] == "reconciled amount would exceed paid amount"


def test_reconciliation_ledger_reads_each_amount_and_result_in_order() -> None:
    _accept("rc5", 1000)
    _pay("rc5", 800)
    assert _reconcile("rc5", 500, key="rec-rc5-1").status_code == 200
    assert _reconcile("rc5", 100, key="rec-rc5-2").status_code == 200

    resp = _reconciliations("rc5")
    assert resp.status_code == 200
    records = resp.json()["reconciliations"]
    assert [r["amount_cents"] for r in records] == [500, 100]
    assert all(r["result"] == "applied" for r in records)
    assert [r["reconciled_after"] for r in records] == [500, 600]
    assert [r["request_id"] for r in records] == ["rec-rc5-1", "rec-rc5-2"]
    # 留痕合计解释了当前已核销金额：500 + 100 = 600
    assert _get("rc5")["reconciled_cents"] == 600


def test_idempotent_reconcile_applies_once_and_replays() -> None:
    _accept("rc6", 500)
    _pay("rc6", 400)
    first = _reconcile("rc6", 150, key="rec-key-1")
    assert first.status_code == 200 and first.json()["reconciled_cents"] == 150

    replay = _reconcile("rc6", 150, key="rec-key-1")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    got = _get("rc6")
    assert got["reconciled_cents"] == 150  # 没有重复累加
    assert len(_reconciliations("rc6").json()["reconciliations"]) == 1


def test_reconcile_key_conflicts_rejected_without_change() -> None:
    _accept("rc7", 500)
    _pay("rc7", 400)
    assert _reconcile("rc7", 100, key="rec-key-x").status_code == 200

    # 同标识不同金额
    assert _reconcile("rc7", 200, key="rec-key-x").status_code == 422
    # 同标识用于另一张订单
    _accept("rc7b", 500)
    _pay("rc7b", 400)
    assert _reconcile("rc7b", 100, key="rec-key-x").status_code == 422
    # 同标识用于不同操作类型（收款/冲正）
    assert _pay("rc7", 100, key="rec-key-x").status_code == 422
    assert _reverse("rc7", 100, key="rec-key-x").status_code == 422

    assert _get("rc7")["reconciled_cents"] == 100
    assert _get("rc7b")["reconciled_cents"] == 0
    assert len(_reconciliations("rc7").json()["reconciliations"]) == 1


def test_first_rejected_reconcile_replays_same_conclusion() -> None:
    _accept("rc8", 300)
    _pay("rc8", 100)
    first = _reconcile("rc8", 200, key="rec-key-bad")  # 超过已收
    assert first.status_code == 409
    replay = _reconcile("rc8", 200, key="rec-key-bad")
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert _get("rc8")["reconciled_cents"] == 0
    assert _reconciliations("rc8").json()["reconciliations"] == []


def test_concurrent_same_reconcile_key_applies_once() -> None:
    _accept("rc9", 1000)
    _pay("rc9", 500)
    headers = {"X-Tenant": TENANT, "Idempotency-Key": "rec-conc-1"}

    def submit(_: int):
        return TestClient(app).post("/orders/rc9/reconciliations", json={"amount_cents": 200}, headers=headers)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["reconciled_cents"] == 200 for r in responses)
    assert len(_reconciliations("rc9").json()["reconciliations"]) == 1


def test_concurrent_payments_reconciles_and_reversals_stay_consistent() -> None:
    _accept("rc10", 2000)
    _pay("rc10", 1000)  # 预留已收，保证任意调度顺序下核销/冲正都不会越界

    def submit(spec):
        kind, amount, key = spec
        client_local = TestClient(app)
        headers = {"X-Tenant": TENANT, "Idempotency-Key": key}
        path = {"pay": "payments", "rev": "reversals", "rec": "reconciliations"}[kind]
        return client_local.post(f"/orders/rc10/{path}", json={"amount_cents": amount}, headers=headers)

    specs = [("pay", 100, f"rc10-p-{i}") for i in range(4)]
    specs += [("rev", 50, f"rc10-r-{i}") for i in range(4)]
    specs += [("rec", 100, f"rc10-c-{i}") for i in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, specs))
    assert all(r.status_code == 200 for r in responses)

    got = _get("rc10")
    # 已收：1000 + 4*100 - 4*50 = 1200；已核销：4*100 = 400
    assert got["paid_cents"] == 1200
    assert got["reconciled_cents"] == 400
    assert got["outstanding_cents"] == 800
    assert got["paid_cents"] + got["outstanding_cents"] == 2000
    # 累计核销不超过累计已收
    assert got["reconciled_cents"] <= got["paid_cents"]

    conn = connect()
    try:
        rec_sum = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM reconciliation_records "
            "WHERE tenant=? AND order_id=? AND result='applied'",
            (TENANT, "rc10"),
        ).fetchone()["s"]
        rec_count = conn.execute(
            "SELECT COUNT(*) AS c FROM reconciliation_records WHERE tenant=? AND order_id=?",
            (TENANT, "rc10"),
        ).fetchone()["c"]
    finally:
        conn.close()
    # 账面合计与留痕对得上
    assert rec_count == 4 and rec_sum == got["reconciled_cents"]


def test_reconciliations_survive_restart() -> None:
    # 模拟重启：丢弃进程内状态后用全新客户端访问同一数据库文件
    _accept("rc11", 500, key="rec-rc11-order")
    _pay("rc11", 400, key="rec-rc11-pay")
    assert _reconcile("rc11", 300, key="rec-rc11-rec").status_code == 200

    client2 = TestClient(app)
    got = client2.get("/orders/rc11", headers={"X-Tenant": TENANT}).json()
    assert got["reconciled_cents"] == 300
    assert got["paid_cents"] == 400 and got["outstanding_cents"] == 100

    # 重启后重复核销：回放首次结果，不重复核销
    replay = client2.post(
        "/orders/rc11/reconciliations", json={"amount_cents": 300},
        headers={"X-Tenant": TENANT, "Idempotency-Key": "rec-rc11-rec"},
    )
    assert replay.status_code == 200 and replay.json()["reconciled_cents"] == 300

    # 重启后核销留痕仍可按订单读出，且冲正下限继续生效
    records = client2.get("/orders/rc11/reconciliations", headers={"X-Tenant": TENANT}).json()["reconciliations"]
    assert len(records) == 1 and records[0]["amount_cents"] == 300 and records[0]["result"] == "applied"
    blocked = client2.post(
        "/orders/rc11/reversals", json={"amount_cents": 200},
        headers={"X-Tenant": TENANT},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "reversal would make paid amount less than reconciled amount"
