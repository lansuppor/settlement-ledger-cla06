import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("APP_DB", os.path.join(_tmp_dir, "reconciliation.sqlite"))

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
    # 核销只改变认定口径：已收、未收、订单金额不变
    assert body["reconciled_cents"] == 300
    assert body["paid_cents"] == 400
    assert body["outstanding_cents"] == 100
    assert body["paid_cents"] + body["outstanding_cents"] == 500

    again = _reconcile("rc1", 100)
    assert again.status_code == 200
    assert again.json()["reconciled_cents"] == 400  # 累计核销


def test_reconcile_invalid_amount_rejected_without_change() -> None:
    _accept("rc2", 500)
    _pay("rc2", 200)

    zero = _reconcile("rc2", 0)
    assert zero.status_code == 409
    assert zero.json()["detail"] == "reconciliation amount must be greater than zero"

    negative = _reconcile("rc2", -5)
    assert negative.status_code == 409
    assert negative.json()["detail"] == "reconciliation amount must be greater than zero"

    over = _reconcile("rc2", 201)  # 累计核销超过当前已收
    assert over.status_code == 409
    assert over.json()["detail"] == "reconciliation amount exceeds paid amount"

    # 拒绝不改变任何金额、状态与留痕
    got = _get("rc2")
    assert got["reconciled_cents"] == 0
    assert got["paid_cents"] == 200
    assert _reconciliations("rc2").json()["reconciliations"] == []


def test_reconcile_cumulative_cannot_exceed_paid() -> None:
    _accept("rc3", 500)
    _pay("rc3", 400)
    assert _reconcile("rc3", 300).status_code == 200

    over = _reconcile("rc3", 101)  # 300 + 101 > 已收 400
    assert over.status_code == 409
    assert over.json()["detail"] == "reconciliation amount exceeds paid amount"
    assert _get("rc3")["reconciled_cents"] == 300

    ok = _reconcile("rc3", 100)  # 累计 400 == 已收 400，允许
    assert ok.status_code == 200 and ok.json()["reconciled_cents"] == 400


def test_reconcile_errors_distinguishable_from_missing_and_cross_tenant() -> None:
    # 订单不存在：404，原因不同于金额非法的 409
    assert _reconcile("rc-missing", 10).status_code == 404
    # 跨租户核销：按不存在处理
    _accept("rc4", 500)
    _pay("rc4", 100)
    assert _reconcile("rc4", 10, tenant="other-tenant").status_code == 404
    # 跨租户读取核销留痕同样 404
    assert _reconciliations("rc4", tenant="other-tenant").status_code == 404
    assert _get("rc4")["reconciled_cents"] == 0


def test_reconciled_amount_is_floor_for_reversal() -> None:
    _accept("rc5", 500)
    _pay("rc5", 400)
    assert _reconcile("rc5", 300).status_code == 200

    # 核销生效后，任何使已收降到 300 以下的冲正都必须被拒绝
    blocked = _reverse("rc5", 200)  # 冲正后已收 200 < 已核销 300
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "reversal would make paid amount less than reconciled amount"
    # 与「超过当前已收」的拒绝原因可区分
    over = _reverse("rc5", 500)
    assert over.status_code == 409
    assert over.json()["detail"] == "reversal amount exceeds paid amount"

    # 拒绝不改变已有金额、状态与留痕
    got = _get("rc5")
    assert got["paid_cents"] == 400 and got["reconciled_cents"] == 300

    ok = _reverse("rc5", 100)  # 冲正后已收 300 == 已核销 300，允许
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 300


def test_reconciliation_ledger_reads_each_amount_and_result_in_order() -> None:
    _accept("rc6", 1000)
    _pay("rc6", 800)
    assert _reconcile("rc6", 500, key="rec-rc6-1").status_code == 200
    assert _reconcile("rc6", 200, key="rec-rc6-2").status_code == 200

    resp = _reconciliations("rc6")
    assert resp.status_code == 200
    records = resp.json()["reconciliations"]
    assert [r["amount_cents"] for r in records] == [500, 200]
    assert all(r["result"] == "applied" for r in records)
    assert [r["reconciled_after"] for r in records] == [500, 700]
    assert [r["request_id"] for r in records] == ["rec-rc6-1", "rec-rc6-2"]
    # 留痕顺序与金额合计解释了当前已核销：500 + 200 = 700
    assert _get("rc6")["reconciled_cents"] == 700


def test_idempotent_reconcile_applies_once_and_replays() -> None:
    _accept("rc7", 500)
    _pay("rc7", 400)
    first = _reconcile("rc7", 150, key="rec-key-1")
    assert first.status_code == 200 and first.json()["reconciled_cents"] == 150

    replay = _reconcile("rc7", 150, key="rec-key-1")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert _get("rc7")["reconciled_cents"] == 150  # 没有重复累加
    assert len(_reconciliations("rc7").json()["reconciliations"]) == 1


def test_reconcile_key_conflicts_rejected_without_change() -> None:
    _accept("rc8", 500)
    _pay("rc8", 400)
    assert _reconcile("rc8", 100, key="rec-key-x").status_code == 200

    # 同标识不同金额
    assert _reconcile("rc8", 200, key="rec-key-x").status_code == 422
    # 同标识用于另一张订单
    _accept("rc8b", 500)
    _pay("rc8b", 400)
    assert _reconcile("rc8b", 100, key="rec-key-x").status_code == 422
    # 同标识用于不同操作类型（收款/冲正）
    assert _pay("rc8", 100, key="rec-key-x").status_code == 422
    assert _reverse("rc8", 100, key="rec-key-x").status_code == 422

    assert _get("rc8")["reconciled_cents"] == 100
    assert _get("rc8")["paid_cents"] == 400
    assert _get("rc8b")["reconciled_cents"] == 0
    assert len(_reconciliations("rc8").json()["reconciliations"]) == 1


def test_first_rejected_reconcile_replays_same_conclusion() -> None:
    _accept("rc9", 300)
    _pay("rc9", 100)
    first = _reconcile("rc9", 200, key="rec-key-bad")  # 超过已收
    assert first.status_code == 409
    replay = _reconcile("rc9", 200, key="rec-key-bad")
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert _get("rc9")["reconciled_cents"] == 0
    assert _reconciliations("rc9").json()["reconciliations"] == []


def test_concurrent_same_reconcile_key_applies_once() -> None:
    _accept("rc10", 1000)
    _pay("rc10", 500)
    headers = {"X-Tenant": TENANT, "Idempotency-Key": "rec-conc-1"}

    def submit(_: int):
        return TestClient(app).post("/orders/rc10/reconciliations", json={"amount_cents": 200}, headers=headers)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["reconciled_cents"] == 200 for r in responses)
    assert len(_reconciliations("rc10").json()["reconciliations"]) == 1


def test_concurrent_payments_reversals_reconciles_stay_consistent() -> None:
    _accept("rc11", 1000)
    _pay("rc11", 500)  # 预留已收，保证任意调度顺序下核销/冲正都有余量

    def submit(spec):
        kind, amount, key = spec
        client_local = TestClient(app)
        headers = {"X-Tenant": TENANT, "Idempotency-Key": key}
        path = {"pay": "payments", "rev": "reversals", "rec": "reconciliations"}[kind]
        return client_local.post(f"/orders/rc11/{path}", json={"amount_cents": amount}, headers=headers)

    specs = [("pay", 100, f"rc11-p-{i}") for i in range(4)]
    specs += [("rev", 50, f"rc11-r-{i}") for i in range(4)]
    specs += [("rec", 80, f"rc11-c-{i}") for i in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, specs))
    assert all(r.status_code == 200 for r in responses)

    got = _get("rc11")
    # 500(初始) + 4*100(收款) - 4*50(冲正) = 700
    assert got["paid_cents"] == 700
    assert got["outstanding_cents"] == 300
    assert got["paid_cents"] + got["outstanding_cents"] == 1000
    # 累计核销 4*80 = 320，不超过累计已收 700
    assert got["reconciled_cents"] == 320
    assert got["reconciled_cents"] <= got["paid_cents"]

    conn = connect()
    try:
        rec_count = conn.execute(
            "SELECT COUNT(*) AS c FROM reconciliation_records WHERE tenant=? AND order_id=?",
            (TENANT, "rc11"),
        ).fetchone()["c"]
        rec_sum = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM reconciliation_records "
            "WHERE tenant=? AND order_id=?",
            (TENANT, "rc11"),
        ).fetchone()["s"]
    finally:
        conn.close()
    # 账面合计与留痕对得上
    assert rec_count == 4 and rec_sum == got["reconciled_cents"]


def test_reconciliations_survive_restart() -> None:
    # 模拟重启：丢弃进程内状态后用全新客户端访问同一数据库文件
    _accept("rc12", 500, key="rec-rc12-order")
    _pay("rc12", 400, key="rec-rc12-pay")
    assert _reconcile("rc12", 300, key="rec-rc12-rec").status_code == 200

    client2 = TestClient(app)
    got = client2.get("/orders/rc12", headers={"X-Tenant": TENANT}).json()
    assert got["reconciled_cents"] == 300 and got["paid_cents"] == 400

    # 重启后重复核销：回放首次结果，不再次累加
    replay = client2.post(
        "/orders/rc12/reconciliations", json={"amount_cents": 300},
        headers={"X-Tenant": TENANT, "Idempotency-Key": "rec-rc12-rec"},
    )
    assert replay.status_code == 200 and replay.json()["reconciled_cents"] == 300

    # 重启后核销留痕仍可按订单读出，且冲正下限继续生效
    records = client2.get("/orders/rc12/reconciliations", headers={"X-Tenant": TENANT}).json()["reconciliations"]
    assert len(records) == 1 and records[0]["amount_cents"] == 300 and records[0]["result"] == "applied"
    blocked = client2.post(
        "/orders/rc12/reversals", json={"amount_cents": 200},
        headers={"X-Tenant": TENANT},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "reversal would make paid amount less than reconciled amount"
