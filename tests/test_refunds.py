import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("APP_DB", os.path.join(_tmp_dir, "refunds.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

TENANT = "trfd"


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


def _refund(order_id: str, amount: int, reason: str = "customer request",
            key: str | None = None, tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/orders/{order_id}/refunds",
        json={"amount_cents": amount, "reason": reason},
        headers=headers,
    )


def _get(order_id: str, tenant: str = TENANT) -> dict:
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant}).json()


def _refunds(order_id: str, tenant: str = TENANT):
    return client.get(f"/orders/{order_id}/refunds", headers={"X-Tenant": tenant})


def test_refund_decreases_paid_and_increases_outstanding() -> None:
    _accept("rf1", 500)
    assert _pay("rf1", 300).status_code == 200
    resp = _refund("rf1", 200, "defective goods")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paid_cents"] == 100
    assert body["outstanding_cents"] == 400
    assert body["status"] == "accepted"
    # 任意时刻已收 + 未收 = 订单金额
    assert body["paid_cents"] + body["outstanding_cents"] == 500


def test_pay_refund_pay_conservation_and_resettle() -> None:
    _accept("rf2", 500)
    _pay("rf2", 500)
    assert _get("rf2")["status"] == "settled"

    refunded = _refund("rf2", 500, "full return").json()
    assert refunded["paid_cents"] == 0
    assert refunded["outstanding_cents"] == 500
    assert refunded["status"] == "accepted"

    again = _pay("rf2", 500).json()
    assert again["paid_cents"] == 500 and again["outstanding_cents"] == 0
    assert again["status"] == "settled"


def test_refund_rejects_non_positive_and_overpaid_amounts() -> None:
    _accept("rf3", 500)
    _pay("rf3", 200)

    over = _refund("rf3", 201, "too much")
    assert over.status_code == 409
    assert over.json()["detail"] == "refund would make paid amount negative"

    zero = _refund("rf3", 0, "zero")
    assert zero.status_code == 409
    assert zero.json()["detail"] == "refund amount must be greater than zero"

    negative = _refund("rf3", -10, "negative")
    assert negative.status_code == 409
    assert negative.json()["detail"] == "refund amount must be greater than zero"

    # 被拒绝后金额、状态不变；拒绝结论本身作为退款留痕可读
    got = _get("rf3")
    assert got["paid_cents"] == 200 and got["outstanding_cents"] == 300
    ledger = _refunds("rf3").json()["refunds"]
    assert [r["result"] for r in ledger] == ["rejected", "rejected", "rejected"]
    assert ledger[0]["reject_reason"] == "refund would make paid amount negative"
    assert ledger[0]["paid_after"] is None
    assert [r["reason"] for r in ledger] == ["too much", "zero", "negative"]


def test_refund_cannot_go_below_reconciled_amount() -> None:
    _accept("rf4", 500)
    _pay("rf4", 400)
    # 直接在库中登记已核销金额 300
    conn = connect()
    try:
        conn.execute(
            "UPDATE orders SET reconciled_cents=300 WHERE tenant=? AND order_id=?",
            (TENANT, "rf4"),
        )
    finally:
        conn.close()

    blocked = _refund("rf4", 200, "would dip below reconciled")  # 退款后已收 200 < 已核销 300
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "refund would make paid amount less than reconciled amount"
    assert _get("rf4")["paid_cents"] == 400

    ok = _refund("rf4", 100, "down to reconciled")  # 退款后已收 300 == 已核销 300，允许
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 300


def test_refund_errors_distinguishable_from_missing_and_cross_tenant() -> None:
    # 订单不存在：404，原因不同于金额非法的 409
    assert _refund("rf-missing", 10, "no order").status_code == 404
    # 跨租户退款：按不存在处理
    _accept("rf5", 500)
    _pay("rf5", 100)
    assert _refund("rf5", 10, "cross tenant", tenant="other-tenant").status_code == 404
    # 跨租户读取留痕同样 404
    assert _refunds("rf5", tenant="other-tenant").status_code == 404
    # 金额、状态、留痕不受跨租户尝试影响
    assert _get("rf5")["paid_cents"] == 100
    assert _refunds("rf5").json()["refunds"] == []


def test_refund_ledger_reads_each_amount_reason_and_result_in_order() -> None:
    _accept("rf6", 1000)
    _pay("rf6", 800)
    assert _refund("rf6", 500, "return", key="rfd-rf6-1").status_code == 200
    assert _refund("rf6", 100, "goodwill", key="rfd-rf6-2").status_code == 200

    resp = _refunds("rf6")
    assert resp.status_code == 200
    records = resp.json()["refunds"]
    assert [r["amount_cents"] for r in records] == [500, 100]
    assert [r["reason"] for r in records] == ["return", "goodwill"]
    assert all(r["result"] == "applied" for r in records)
    assert [r["paid_after"] for r in records] == [300, 200]
    assert [r["request_id"] for r in records] == ["rfd-rf6-1", "rfd-rf6-2"]
    assert all(r["created_at"] for r in records)
    # 留痕顺序与金额合计解释了当前已收：800 - 500 - 100 = 200
    assert _get("rf6")["paid_cents"] == 200


def test_idempotent_refund_applies_once_and_replays() -> None:
    _accept("rf7", 500)
    _pay("rf7", 400)
    first = _refund("rf7", 150, "return", key="rfd-key-1")
    assert first.status_code == 200 and first.json()["paid_cents"] == 250

    replay = _refund("rf7", 150, "return", key="rfd-key-1")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    got = _get("rf7")
    assert got["paid_cents"] == 250  # 没有重复退款
    assert len(_refunds("rf7").json()["refunds"]) == 1


def test_refund_key_conflicts_rejected_without_change() -> None:
    _accept("rf8", 500)
    _pay("rf8", 400)
    assert _refund("rf8", 100, "return", key="rfd-key-x").status_code == 200

    # 同标识不同金额
    assert _refund("rf8", 200, "return", key="rfd-key-x").status_code == 422
    # 同标识不同退款原因
    assert _refund("rf8", 100, "other reason", key="rfd-key-x").status_code == 422
    # 同标识用于另一张订单
    _accept("rf8b", 500)
    _pay("rf8b", 400)
    assert _refund("rf8b", 100, "return", key="rfd-key-x").status_code == 422
    # 同标识用于不同操作类型（收款）
    assert _pay("rf8", 100, key="rfd-key-x").status_code == 422

    assert _get("rf8")["paid_cents"] == 300
    assert _get("rf8b")["paid_cents"] == 400
    assert len(_refunds("rf8").json()["refunds"]) == 1


def test_first_rejected_refund_replays_same_conclusion() -> None:
    _accept("rf9", 300)
    _pay("rf9", 100)
    first = _refund("rf9", 200, "too much", key="rfd-key-bad")  # 退款后已收为负
    assert first.status_code == 409
    replay = _refund("rf9", 200, "too much", key="rfd-key-bad")
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert _get("rf9")["paid_cents"] == 100
    # 首次拒绝结论只留痕一次，重复提交不重复写入
    records = _refunds("rf9").json()["refunds"]
    assert len(records) == 1
    assert records[0]["result"] == "rejected"
    assert records[0]["reject_reason"] == "refund would make paid amount negative"
    assert records[0]["reason"] == "too much"
    assert records[0]["request_id"] == "rfd-key-bad"


def test_concurrent_same_refund_key_applies_once() -> None:
    _accept("rf10", 1000)
    _pay("rf10", 500)
    headers = {"X-Tenant": TENANT, "Idempotency-Key": "rfd-conc-1"}

    def submit(_: int):
        return TestClient(app).post(
            "/orders/rf10/refunds",
            json={"amount_cents": 200, "reason": "return"},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["paid_cents"] == 300 for r in responses)
    assert len(_refunds("rf10").json()["refunds"]) == 1


def test_concurrent_payments_and_refunds_stay_consistent() -> None:
    _accept("rf11", 1000)
    _pay("rf11", 500)  # 预留已收，保证任意调度顺序下退款都不会超额

    def submit(spec):
        kind, amount, key = spec
        client_local = TestClient(app)
        headers = {"X-Tenant": TENANT, "Idempotency-Key": key}
        if kind == "pay":
            return client_local.post(
                "/orders/rf11/payments", json={"amount_cents": amount}, headers=headers
            )
        return client_local.post(
            "/orders/rf11/refunds",
            json={"amount_cents": amount, "reason": "return"},
            headers=headers,
        )

    specs = [("pay", 100, f"rf11-p-{i}") for i in range(4)]
    specs += [("refund", 50, f"rf11-r-{i}") for i in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, specs))
    assert all(r.status_code == 200 for r in responses)

    got = _get("rf11")
    # 500(初始) + 4*100(收款) - 4*50(退款) = 700
    assert got["paid_cents"] == 700
    assert got["outstanding_cents"] == 300
    assert got["paid_cents"] >= 0
    assert got["paid_cents"] + got["outstanding_cents"] == 1000

    conn = connect()
    try:
        paid_sum = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM payment_records WHERE tenant=? AND order_id=?",
            (TENANT, "rf11"),
        ).fetchone()["s"]
        refund_count = conn.execute(
            "SELECT COUNT(*) AS c FROM refund_records WHERE tenant=? AND order_id=? AND result='applied'",
            (TENANT, "rf11"),
        ).fetchone()["c"]
        refund_sum = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM refund_records "
            "WHERE tenant=? AND order_id=? AND result='applied'",
            (TENANT, "rf11"),
        ).fetchone()["s"]
    finally:
        conn.close()
    assert paid_sum == 900 and refund_count == 4 and refund_sum == 200
    assert paid_sum - refund_sum == got["paid_cents"]


def test_refund_interleaves_with_reversal_and_reconciliation() -> None:
    _accept("rf12", 1000)
    _pay("rf12", 600)
    # 冲正 100 → 已收 500；核销 300；退款 200 → 已收 300 == 已核销下限
    assert client.post(
        "/orders/rf12/reversals", json={"amount_cents": 100}, headers={"X-Tenant": TENANT}
    ).status_code == 200
    assert client.post(
        "/orders/rf12/reconciliations", json={"amount_cents": 300}, headers={"X-Tenant": TENANT}
    ).status_code == 200
    ok = _refund("rf12", 200, "partial return")
    assert ok.status_code == 200
    body = ok.json()
    assert body["paid_cents"] == 300
    assert body["reconciled_cents"] == 300
    assert body["outstanding_cents"] == 700
    # 再退 1 分都会跌破已核销下限
    blocked = _refund("rf12", 1, "below reconciled")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "refund would make paid amount less than reconciled amount"


def test_order_with_refunds_cannot_be_corrected() -> None:
    _accept("rf13", 500)
    _pay("rf13", 500)
    assert _refund("rf13", 500, "full return").status_code == 200
    # 退款后已收回到 0，但退款留痕已构成业务事实，不允许更正
    resp = client.post(
        "/orders/rf13/correction",
        json={"order_id": "rf13", "amount_cents": 400, "currency": "CNY"},
        headers={"X-Tenant": TENANT},
    )
    assert resp.status_code == 409
    assert "cannot be corrected" in resp.json()["detail"]


def test_refunds_survive_restart() -> None:
    # 模拟重启：丢弃进程内状态后用全新客户端访问同一数据库文件
    _accept("rf14", 500, key="rfd-rf14-order")
    _pay("rf14", 400, key="rfd-rf14-pay")
    assert _refund("rf14", 300, "return", key="rfd-rf14-refund").status_code == 200

    client2 = TestClient(app)
    got = client2.get("/orders/rf14", headers={"X-Tenant": TENANT}).json()
    assert got["paid_cents"] == 100 and got["outstanding_cents"] == 400

    # 重启后重复退款：回放首次结果，不再次退款
    replay = client2.post(
        "/orders/rf14/refunds", json={"amount_cents": 300, "reason": "return"},
        headers={"X-Tenant": TENANT, "Idempotency-Key": "rfd-rf14-refund"},
    )
    assert replay.status_code == 200 and replay.json()["paid_cents"] == 100

    # 重启后留痕仍可按订单读出
    records = client2.get("/orders/rf14/refunds", headers={"X-Tenant": TENANT}).json()["refunds"]
    assert len(records) == 1
    assert records[0]["amount_cents"] == 300
    assert records[0]["reason"] == "return"
    assert records[0]["result"] == "applied"
