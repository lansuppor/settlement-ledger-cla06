import os
import tempfile

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("APP_DB", os.path.join(_tmp_dir, "timeline.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

TENANT = "ttl"


def _accept(order_id: str, amount: int = 1000, key: str | None = None, tenant: str = TENANT):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
        headers=headers,
    )


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


def _correct(order_id: str, new_id: str, amount: int, currency: str = "CNY",
             key: str | None = None, tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/orders/{order_id}/correction",
        json={"order_id": new_id, "amount_cents": amount, "currency": currency},
        headers=headers,
    )


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


def _timeline(order_id: str, tenant: str = TENANT):
    return client.get(f"/orders/{order_id}/timeline", headers={"X-Tenant": tenant})


def test_timeline_merges_all_stages_in_business_order() -> None:
    _accept("tl1", 500, key="tl1-accept")
    assert _pay("tl1", 200, key="tl1-pay-1").status_code == 200
    assert _pay("tl1", 300, key="tl1-pay-2").status_code == 200
    assert _reverse("tl1", 150, key="tl1-rev-1").status_code == 200
    assert _reconcile("tl1", 300, key="tl1-rec-1").status_code == 200
    assert _refund("tl1", 50, "return", key="tl1-ref-1").status_code == 200

    resp = _timeline("tl1")
    assert resp.status_code == 200
    events = resp.json()["timeline"]
    assert resp.json()["order_id"] == "tl1"
    assert [e["stage"] for e in events] == [
        "acceptance", "payment", "payment", "reversal", "reconciliation", "refund",
    ]
    assert [e["result"] for e in events] == ["applied"] * 6
    assert [e["amount_cents"] for e in events] == [500, 200, 300, 150, 300, 50]
    # 各环节结论后的累计已收：受理 0，收款累加，冲正/退款减少；核销不改变已收
    assert [e["paid_after"] for e in events] == [0, 200, 500, 350, None, 300]
    # 核销后的累计已核销只在核销事件上呈现；受理为 0
    assert [e["reconciled_after"] for e in events] == [0, None, None, None, 300, None]
    assert [e["request_id"] for e in events] == [
        "tl1-accept", "tl1-pay-1", "tl1-pay-2", "tl1-rev-1", "tl1-rec-1", "tl1-ref-1",
    ]
    assert all(e["created_at"] for e in events)
    # 全局自增 id 严格递增：排序可判定且可重放
    ids = [e["id"] for e in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_timeline_records_rejected_conclusions_with_reasons() -> None:
    _accept("tl2", 500)
    _pay("tl2", 200)
    assert _reverse("tl2", 999).status_code == 409      # 超过当前已收
    assert _reverse("tl2", 0).status_code == 409        # 金额 <= 0
    assert _reconcile("tl2", 300).status_code == 409    # 累计核销超过已收
    assert _refund("tl2", 999, "too much").status_code == 409  # 退款后已收为负
    # 已有业务事实的订单不可更正
    assert _correct("tl2", "tl2-x", 100).status_code == 409

    events = _timeline("tl2").json()["timeline"]
    assert [e["stage"] for e in events] == [
        "acceptance", "payment", "reversal", "reversal", "reconciliation", "refund", "correction",
    ]
    assert [e["result"] for e in events] == [
        "applied", "applied", "rejected", "rejected", "rejected", "rejected", "rejected",
    ]
    assert [e["reject_reason"] for e in events[2:]] == [
        "reversal amount exceeds paid amount",
        "reversal amount must be greater than zero",
        "reconciled amount would exceed paid amount",
        "refund would make paid amount negative",
        "order has payments, reversals or reconciliations and cannot be corrected",
    ]
    # 被拒绝的结论不呈现结论后金额
    assert all(e["paid_after"] is None for e in events[2:])
    # 更正事件携带更正前后内容
    correction = events[-1]
    assert correction["before_order_id"] == "tl2" and correction["before_amount_cents"] == 500
    assert correction["after_order_id"] == "tl2-x" and correction["after_amount_cents"] == 100
    # 既有留痕读取入口不受时间线影响：被拒绝的冲正/核销仍不出现在各自留痕中
    reversals = client.get("/orders/tl2/reversals", headers={"X-Tenant": TENANT}).json()["reversals"]
    assert reversals == []
    reconciliations = client.get(
        "/orders/tl2/reconciliations", headers={"X-Tenant": TENANT}
    ).json()["reconciliations"]
    assert reconciliations == []


def test_timeline_idempotent_replay_adds_no_entries() -> None:
    _accept("tl3", 500, key="tl3-accept")
    _accept("tl3", 500, key="tl3-accept")  # 受理回放
    _pay("tl3", 200, key="tl3-pay")
    _pay("tl3", 200, key="tl3-pay")        # 收款回放
    assert _refund("tl3", 300, "return", key="tl3-ref").status_code == 409
    assert _refund("tl3", 300, "return", key="tl3-ref").status_code == 409  # 拒绝结论回放

    events = _timeline("tl3").json()["timeline"]
    # 同一次业务请求至多产生一条时间线条目
    assert [e["stage"] for e in events] == ["acceptance", "payment", "refund"]
    assert [e["result"] for e in events] == ["applied", "applied", "rejected"]


def test_timeline_correction_rename_moves_history_to_new_order_id() -> None:
    _accept("tl4", 500, key="tl4-accept")
    assert _correct("tl4", "", 500).status_code == 409          # 新标识为空，拒绝
    assert _correct("tl4", "tl4-fixed", 480).status_code == 200  # 生效并改名

    # 旧标识不再存在
    assert _timeline("tl4").status_code == 404
    events = _timeline("tl4-fixed").json()["timeline"]
    # 受理与历史更正结论随订单迁移到新标识，按发生顺序完整可读
    assert [e["stage"] for e in events] == ["acceptance", "correction", "correction"]
    assert [e["result"] for e in events] == ["applied", "rejected", "applied"]
    applied = events[-1]
    assert applied["before_order_id"] == "tl4" and applied["after_order_id"] == "tl4-fixed"
    assert applied["before_amount_cents"] == 500 and applied["after_amount_cents"] == 480


def test_timeline_imported_order_then_first_payment_keeps_order() -> None:
    body = "tenant,order_id,amount_cents,currency\nttl,tl5,1200,CNY\n"
    resp = client.post("/orders/import", content=body, headers={"Content-Type": "text/plain"})
    assert resp.status_code == 200 and resp.json()["succeeded"] == 1
    assert _pay("tl5", 200, key="tl5-pay-1").status_code == 200

    events = _timeline("tl5").json()["timeline"]
    # 批量导入的受理与其后第一笔收款的先后关系与各自留痕一致
    assert [e["stage"] for e in events] == ["acceptance", "payment"]
    assert events[0]["amount_cents"] == 1200 and events[0]["request_id"] is None
    assert events[1]["paid_after"] == 200


def test_timeline_missing_and_cross_tenant_return_404() -> None:
    assert _timeline("tl-missing").status_code == 404
    _accept("tl6", 500)
    # 跨租户读取按不存在处理，不泄漏订单是否存在
    assert _timeline("tl6", tenant="other-tenant").status_code == 404
    # 缺少租户请求头
    assert client.get("/orders/tl6/timeline").status_code == 400


def test_timeline_stable_across_repeated_reads_and_restart() -> None:
    _accept("tl7", 500, key="tl7-accept")
    _pay("tl7", 500, key="tl7-pay")
    _refund("tl7", 100, "return", key="tl7-ref")

    first = _timeline("tl7").json()
    again = _timeline("tl7").json()
    assert again == first  # 重复读取不改变顺序与内容

    # 模拟重启：丢弃进程内状态后用全新客户端访问同一数据库文件
    client2 = TestClient(app)
    after_restart = client2.get("/orders/tl7/timeline", headers={"X-Tenant": TENANT}).json()
    assert after_restart == first
