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
    return client.post(
        f"/orders/{order_id}/reconciliations", json={"amount_cents": amount}, headers=headers
    )


def _correct(order_id: str, new_id: str, amount: int, key: str | None = None, tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/orders/{order_id}/correction",
        json={"order_id": new_id, "amount_cents": amount, "currency": "CNY"},
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
    _accept("tl1", 1000, key="tl1-accept")
    assert _pay("tl1", 600, key="tl1-pay-1").status_code == 200
    assert _pay("tl1", 400, key="tl1-pay-2").status_code == 200
    assert _reverse("tl1", 200, key="tl1-rev-1").status_code == 200
    assert _reconcile("tl1", 300, key="tl1-rec-1").status_code == 200
    assert _refund("tl1", 100, "return", key="tl1-ref-1").status_code == 200

    resp = _timeline("tl1")
    assert resp.status_code == 200
    assert resp.json()["order_id"] == "tl1"
    entries = resp.json()["timeline"]
    # 受理、收款、收款、冲正、核销、退款按发生顺序合并呈现
    assert [e["stage"] for e in entries] == [
        "acceptance", "payment", "payment", "reversal", "reconciliation", "refund",
    ]
    assert [e["result"] for e in entries] == ["applied"] * 6
    assert [e["amount_cents"] for e in entries] == [1000, 600, 400, 200, 300, 100]
    # 结果相关字段：收款后累计已收、冲正/退款后累计已收、核销后累计已核销
    assert [e["paid_after"] for e in entries] == [None, 600, 1000, 800, None, 700]
    assert entries[4]["reconciled_after"] == 300
    assert entries[0]["currency"] == "CNY"
    assert entries[5]["reason"] == "return"
    assert [e["request_id"] for e in entries] == [
        "tl1-accept", "tl1-pay-1", "tl1-pay-2", "tl1-rev-1", "tl1-rec-1", "tl1-ref-1",
    ]
    assert all(e["created_at"] for e in entries)
    # 序号全库单调，同一订单内严格递增
    assert [e["id"] for e in entries] == sorted(e["id"] for e in entries)


def test_timeline_records_rejected_conclusions_with_reasons() -> None:
    _accept("tl2", 500)
    assert _pay("tl2", 200).status_code == 200
    assert _reconcile("tl2", 150).status_code == 200

    # 被拒绝的冲正：冲正后已收 50 < 已核销 150
    blocked_reversal = _reverse("tl2", 150)
    assert blocked_reversal.status_code == 409
    # 被拒绝的核销：累计 150+100 > 已收 200
    assert _reconcile("tl2", 100).status_code == 409
    # 被拒绝的退款：退款后已收为负
    assert _refund("tl2", 300, "too much").status_code == 409

    entries = _timeline("tl2").json()["timeline"]
    assert [e["stage"] for e in entries] == [
        "acceptance", "payment", "reconciliation", "reversal", "reconciliation", "refund",
    ]
    assert [e["result"] for e in entries] == [
        "applied", "applied", "applied", "rejected", "rejected", "rejected",
    ]
    rejected = entries[3:]
    assert [e["reject_reason"] for e in rejected] == [
        "reversal would make paid amount less than reconciled amount",
        "reconciled amount would exceed paid amount",
        "refund would make paid amount negative",
    ]
    assert all(e["paid_after"] is None for e in rejected)
    # 拒绝不改变金额：已收仍为 200、已核销仍为 150
    got = client.get("/orders/tl2", headers={"X-Tenant": TENANT}).json()
    assert got["paid_cents"] == 200 and got["reconciled_cents"] == 150


def test_timeline_correction_entries_and_rename_migration() -> None:
    _accept("tl3", 500, key="tl3-accept")
    # 被拒绝的更正：金额非正
    assert _correct("tl3", "tl3-x", 0, key="tl3-corr-1").status_code == 409
    # 生效的更正：改名并改金额
    assert _correct("tl3", "tl3-renamed", 480, key="tl3-corr-2").status_code == 200

    # 旧标识不再存在
    assert _timeline("tl3").status_code == 404
    entries = _timeline("tl3-renamed").json()["timeline"]
    # 受理与全部更正结论（含改名前的）随订单迁移到新标识，按发生顺序排列
    assert [e["stage"] for e in entries] == ["acceptance", "correction", "correction"]
    assert [e["result"] for e in entries] == ["applied", "rejected", "applied"]
    rejected, applied = entries[1], entries[2]
    assert rejected["reject_reason"] == "amount must be a positive integer in minor units"
    assert (rejected["before_order_id"], rejected["after_order_id"]) == ("tl3", "tl3-x")
    assert (applied["before_order_id"], applied["after_order_id"]) == ("tl3", "tl3-renamed")
    assert (applied["before_amount_cents"], applied["after_amount_cents"]) == (500, 480)
    assert [e["request_id"] for e in entries] == ["tl3-accept", "tl3-corr-1", "tl3-corr-2"]


def test_timeline_correction_rejected_when_order_has_facts() -> None:
    _accept("tl4", 500)
    assert _pay("tl4", 100).status_code == 200
    resp = _correct("tl4", "tl4-x", 500)
    assert resp.status_code == 409

    entries = _timeline("tl4").json()["timeline"]
    assert [e["stage"] for e in entries] == ["acceptance", "payment", "correction"]
    last = entries[-1]
    assert last["result"] == "rejected"
    assert last["reject_reason"] == (
        "order has payments, reversals or reconciliations and cannot be corrected"
    )


def test_timeline_idempotent_replay_adds_no_entries() -> None:
    _accept("tl5", 500)
    assert _pay("tl5", 200, key="tl5-pay").status_code == 200
    assert _refund("tl5", 300, "too much", key="tl5-ref-bad").status_code == 409

    before = _timeline("tl5").json()["timeline"]
    # 重复提交同标识请求：回放首次结论，不新增时间线条目
    assert _pay("tl5", 200, key="tl5-pay").status_code == 200
    assert _refund("tl5", 300, "too much", key="tl5-ref-bad").status_code == 409
    after = _timeline("tl5").json()["timeline"]
    assert after == before
    # 首次被拒绝的退款只占一条
    assert [e["stage"] for e in after] == ["acceptance", "payment", "refund"]
    assert after[-1]["result"] == "rejected"


def test_timeline_missing_and_cross_tenant_return_404() -> None:
    assert _timeline("tl-missing").status_code == 404
    _accept("tl6", 500)
    assert _timeline("tl6").status_code == 200
    # 跨租户读取按不存在处理，不泄漏订单是否存在
    assert _timeline("tl6", tenant="other-tenant").status_code == 404
    # 缺少租户请求头
    assert client.get("/orders/tl6/timeline").status_code == 400


def test_timeline_batch_import_order_then_first_payment() -> None:
    body = "tenant,order_id,amount_cents,currency\nttl,tl7,1200,CNY\n"
    resp = client.post("/orders/import", content=body, headers={"Content-Type": "text/plain"})
    assert resp.json()["succeeded"] == 1
    assert _pay("tl7", 300, key="tl7-pay-1").status_code == 200

    entries = _timeline("tl7").json()["timeline"]
    # 批量导入的受理与其后第一笔收款的先后关系与各自留痕一致
    assert [e["stage"] for e in entries] == ["acceptance", "payment"]
    assert entries[0]["amount_cents"] == 1200 and entries[0]["request_id"] is None
    assert entries[1]["paid_after"] == 300


def test_timeline_repeated_reads_and_restart_are_stable() -> None:
    _accept("tl8", 800, key="tl8-accept")
    assert _pay("tl8", 500, key="tl8-pay").status_code == 200
    assert _reverse("tl8", 100, key="tl8-rev").status_code == 200

    first = _timeline("tl8").json()
    # 重复读取结果一致
    assert _timeline("tl8").json() == first

    # 模拟重启：丢弃进程内状态后用全新客户端访问同一数据库文件
    client2 = TestClient(app)
    restarted = client2.get("/orders/tl8/timeline", headers={"X-Tenant": TENANT}).json()
    assert restarted == first
    assert [e["stage"] for e in restarted["timeline"]] == ["acceptance", "payment", "reversal"]
