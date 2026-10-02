import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("APP_DB", os.path.join(_tmp_dir, "corrections.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

TENANT = "tcor"


def _accept(order_id: str, amount: int = 1000, currency: str = "CNY", tenant: str = TENANT, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": currency},
        headers=headers,
    )


def _correct(order_id: str, new_id: str, amount: int, currency: str, key: str | None = None, tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/orders/{order_id}/correction",
        json={"order_id": new_id, "amount_cents": amount, "currency": currency},
        headers=headers,
    )


def _corrections(order_id: str, tenant: str = TENANT):
    return client.get(f"/orders/{order_id}/corrections", headers={"X-Tenant": tenant})


def _get(order_id: str, tenant: str = TENANT):
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant})


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


def test_correction_changes_content_and_order_becomes_readable_by_new_id() -> None:
    _accept("c1", 500, "CNY")
    resp = _correct("c1", "c1-new", 800, "USD")
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == "c1-new"
    assert body["amount_cents"] == 800 and body["currency"] == "USD"
    # 尚未发生收款：已收/未收按新金额闭合
    assert body["paid_cents"] == 0 and body["outstanding_cents"] == 800
    assert body["status"] == "accepted"

    # 按新标识可读，按旧标识不再存在
    assert _get("c1-new").status_code == 200
    assert _get("c1").status_code == 404


def test_correction_can_keep_order_id_and_only_change_content() -> None:
    _accept("c2", 500, "CNY")
    resp = _correct("c2", "c2", 640, "EUR")
    assert resp.status_code == 200
    got = _get("c2").json()
    assert got["amount_cents"] == 640 and got["currency"] == "EUR"
    assert got["outstanding_cents"] == 640


def test_order_with_payment_cannot_be_corrected() -> None:
    _accept("c3", 500)
    _pay("c3", 200)
    resp = _correct("c3", "c3-x", 999, "USD")
    assert resp.status_code == 409
    assert "cannot be corrected" in resp.json()["detail"]

    # 金额、币种、标识、状态与留痕保持不变
    got = _get("c3").json()
    assert got["amount_cents"] == 500 and got["currency"] == "CNY"
    assert got["paid_cents"] == 200 and got["status"] == "accepted"
    assert _get("c3-x").status_code == 404


def test_order_with_reversal_trace_even_after_paid_back_to_zero_cannot_be_corrected() -> None:
    _accept("c4", 500)
    _pay("c4", 200)
    _reverse("c4", 200)  # paid_cents 回到 0，但存在收款与冲正留痕
    got = _get("c4").json()
    assert got["paid_cents"] == 0

    resp = _correct("c4", "c4-x", 100, "CNY")
    assert resp.status_code == 409
    assert "cannot be corrected" in resp.json()["detail"]
    assert _get("c4").json()["amount_cents"] == 500


def test_order_with_reconciliation_cannot_be_corrected() -> None:
    _accept("c5", 500)
    _pay("c5", 300)
    _reconcile("c5", 300)
    resp = _correct("c5", "c5-x", 100, "CNY")
    assert resp.status_code == 409
    assert "cannot be corrected" in resp.json()["detail"]
    assert _get("c5").json()["reconciled_cents"] == 300


def test_invalid_correction_inputs_are_409_with_distinct_reasons() -> None:
    _accept("c6", 500)

    zero = _correct("c6", "c6", 0, "CNY")
    assert zero.status_code == 409 and zero.json()["detail"] == "amount must be a positive integer in minor units"

    negative = _correct("c6", "c6", -5, "CNY")
    assert negative.status_code == 409 and "positive integer" in negative.json()["detail"]

    empty_id = _correct("c6", "   ", 100, "CNY")
    assert empty_id.status_code == 409 and empty_id.json()["detail"] == "order id must not be empty"

    bad_ccy = _correct("c6", "c6", 100, "XYZ")
    assert bad_ccy.status_code == 409 and bad_ccy.json()["detail"] == "unsupported currency: XYZ"

    # 非法输入不改变订单本身的金额、币种与状态
    got = _get("c6").json()
    assert got["amount_cents"] == 500 and got["currency"] == "CNY"

    # 每次非法结论同样作为 rejected 留痕，按发生顺序可读出具体原因
    records = _corrections("c6").json()["corrections"]
    assert [r["result"] for r in records] == ["rejected", "rejected", "rejected", "rejected"]
    assert [r["reject_reason"] for r in records] == [
        "amount must be a positive integer in minor units",
        "amount must be a positive integer in minor units",
        "order id must not be empty",
        "unsupported currency: XYZ",
    ]
    # 留痕记录了提交的更正后内容，便于解释被拒绝的请求
    assert records[3]["after_currency"] == "XYZ"


def test_correction_of_missing_or_cross_tenant_order_is_404() -> None:
    assert _correct("c-missing", "c-missing-x", 100, "CNY").status_code == 404

    _accept("c7", 500)
    # 跨租户更正按不存在处理，不泄漏订单是否存在
    assert _correct("c7", "c7-x", 100, "CNY", tenant="other-tenant").status_code == 404
    # 跨租户读取留痕同样 404
    assert _corrections("c7", tenant="other-tenant").status_code == 404
    # 原订单不受跨租户尝试影响
    assert _get("c7").json()["amount_cents"] == 500


def test_new_order_id_taken_in_same_tenant_is_rejected_cross_tenant_name_allowed() -> None:
    _accept("c8", 500)
    _accept("c8-taken", 300)
    clash = _correct("c8", "c8-taken", 500, "CNY")
    assert clash.status_code == 409
    assert clash.json()["detail"] == "target order id already exists for tenant"
    # 原订单保持不变，被占用订单不受影响
    assert _get("c8").json()["amount_cents"] == 500
    assert _get("c8-taken").json()["amount_cents"] == 300

    # 跨租户同名订单不构成冲突：tb 下可把订单改名为 ta 已占用的 c8-taken
    _accept("d8", 500, tenant="tb")
    ok = _correct("d8", "c8-taken", 700, "CNY", tenant="tb")
    assert ok.status_code == 200
    assert _get("c8-taken", tenant="tb").json()["amount_cents"] == 700
    # ta 的同名订单未受影响
    assert _get("c8-taken", tenant=TENANT).json()["amount_cents"] == 300


def test_correction_ledger_records_before_after_and_result_in_order() -> None:
    _accept("c10", 500, "CNY")
    assert _correct("c10", "c10", 700, "CNY", key="corr-c10-1").status_code == 200
    assert _correct("c10", "c10-final", 900, "USD", key="corr-c10-2").status_code == 200

    resp = _corrections("c10-final")
    assert resp.status_code == 200
    records = resp.json()["corrections"]
    assert len(records) == 2  # 历史随改名一起迁移，始终可按当前标识读出完整更正链
    first, second = records
    assert first["result"] == "applied" and first["reject_reason"] is None
    assert (first["before_order_id"], first["before_amount_cents"], first["before_currency"]) == ("c10", 500, "CNY")
    assert (first["after_order_id"], first["after_amount_cents"], first["after_currency"]) == ("c10", 700, "CNY")
    assert first["request_id"] == "corr-c10-1"
    assert (second["before_order_id"], second["before_amount_cents"]) == ("c10", 700)
    assert (second["after_order_id"], second["after_amount_cents"], second["after_currency"]) == ("c10-final", 900, "USD")
    assert second["request_id"] == "corr-c10-2"


def test_rejected_correction_is_recorded_on_original_order() -> None:
    _accept("c11", 500)
    _pay("c11", 100)
    assert _correct("c11", "c11-x", 999, "USD", key="corr-c11-bad").status_code == 409

    records = _corrections("c11").json()["corrections"]
    assert len(records) == 1
    rec = records[0]
    assert rec["result"] == "rejected"
    assert rec["before_order_id"] == "c11"
    assert rec["after_order_id"] == "c11-x" and rec["after_amount_cents"] == 999
    assert rec["after_currency"] == "USD"
    assert "cannot be corrected" in rec["reject_reason"]
    assert rec["request_id"] == "corr-c11-bad"


def test_idempotent_correction_applies_once_and_replays() -> None:
    _accept("c12", 500)
    first = _correct("c12", "c12-new", 800, "USD", key="corr-key-1")
    assert first.status_code == 200 and first.json()["order_id"] == "c12-new"

    replay = _correct("c12", "c12-new", 800, "USD", key="corr-key-1")
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 回放首次响应，不重复更正
    assert _get("c12").status_code == 404
    assert len(_corrections("c12-new").json()["corrections"]) == 1


def test_first_rejected_correction_replays_same_conclusion() -> None:
    _accept("c13", 500)
    _pay("c13", 100)
    first = _correct("c13", "c13-x", 999, "USD", key="corr-key-bad")
    assert first.status_code == 409
    replay = _correct("c13", "c13-x", 999, "USD", key="corr-key-bad")
    assert replay.status_code == 409
    assert replay.json() == first.json()
    # 只固化一次拒绝结论
    assert len(_corrections("c13").json()["corrections"]) == 1
    assert _get("c13").json()["amount_cents"] == 500


def test_correction_key_conflicts_return_422_without_change() -> None:
    _accept("c14", 500)
    assert _correct("c14", "c14a", 100, "CNY", key="corr-key-x").status_code == 200

    # 同标识不同更正后金额
    assert _correct("c14", "c14a", 200, "CNY", key="corr-key-x").status_code == 422
    # 同标识不同更正后币种
    assert _correct("c14", "c14a", 100, "USD", key="corr-key-x").status_code == 422
    # 同标识不同更正后订单标识
    assert _correct("c14", "c14-other", 100, "CNY", key="corr-key-x").status_code == 422
    # 同标识不同目标订单
    _accept("c14b", 500)
    assert _correct("c14b", "c14b", 100, "CNY", key="corr-key-x").status_code == 422
    # 同标识用于不同操作类型（受理/收款/冲正/核销）
    assert _pay("c14a", 50, key="corr-key-x").status_code == 422
    assert _reverse("c14a", 50, key="corr-key-x").status_code == 422
    assert _reconcile("c14a", 50, key="corr-key-x").status_code == 422
    assert _accept("c14c", 100, key="corr-key-x").status_code == 422

    # 首次更正结论保持不变
    assert _get("c14a").json()["amount_cents"] == 100
    assert len(_corrections("c14a").json()["corrections"]) == 1


def test_same_correction_key_across_tenants_is_independent() -> None:
    _accept("c15", 500, tenant="ta")
    _accept("c15", 700, tenant="tb")
    ra = _correct("c15", "c15", 600, "CNY", key="shared-corr-key", tenant="ta")
    rb = _correct("c15", "c15", 800, "CNY", key="shared-corr-key", tenant="tb")
    assert ra.status_code == 200 and rb.status_code == 200
    assert _get("c15", tenant="ta").json()["amount_cents"] == 600
    assert _get("c15", tenant="tb").json()["amount_cents"] == 800


def test_concurrent_same_correction_key_applies_once() -> None:
    _accept("c16", 500)
    headers = {"X-Tenant": TENANT, "Idempotency-Key": "corr-conc-1"}

    def submit(_: int):
        return TestClient(app).post(
            "/orders/c16/correction",
            json={"order_id": "c16-new", "amount_cents": 800, "currency": "USD"},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["order_id"] == "c16-new" for r in responses)
    assert len({r.text for r in responses}) == 1
    assert len(_corrections("c16-new").json()["corrections"]) == 1
    assert _get("c16").status_code == 404


def test_corrections_survive_restart() -> None:
    _accept("c17", 500)
    assert _correct("c17", "c17-new", 650, "EUR", key="corr-c17").status_code == 200

    client2 = TestClient(app)
    # 重启后更正结果仍有效
    got = client2.get("/orders/c17-new", headers={"X-Tenant": TENANT})
    assert got.status_code == 200
    assert got.json()["amount_cents"] == 650 and got.json()["currency"] == "EUR"
    assert client2.get("/orders/c17", headers={"X-Tenant": TENANT}).status_code == 404

    # 重启后重复更正：回放首次结果，不再次更正
    replay = client2.post(
        "/orders/c17/correction",
        json={"order_id": "c17-new", "amount_cents": 650, "currency": "EUR"},
        headers={"X-Tenant": TENANT, "Idempotency-Key": "corr-c17"},
    )
    assert replay.status_code == 200 and replay.json()["amount_cents"] == 650
    records = client2.get("/orders/c17-new/corrections", headers={"X-Tenant": TENANT}).json()["corrections"]
    assert len(records) == 1 and records[0]["result"] == "applied"


def test_corrected_order_keeps_normal_payment_reversal_reconciliation_rules() -> None:
    _accept("c18", 500)
    corrected = _correct("c18", "c18-new", 500, "CNY").json()
    assert corrected["outstanding_cents"] == 500

    assert _pay("c18-new", 500, key="pay-c18").status_code == 200
    assert _get("c18-new").json()["status"] == "settled"
    # 已有收款后不再允许更正
    assert _correct("c18-new", "c18-again", 100, "CNY").status_code == 409
