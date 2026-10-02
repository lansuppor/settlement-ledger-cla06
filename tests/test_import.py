import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

def _import(text: str):
    return client.post("/orders/import", content=text, headers={"Content-Type": "text/plain"})

def test_import_valid_lines_all_accepted() -> None:
    resp = _import("b1,imp-1,500,CNY\nb1,imp-2,800,USD\nb2,imp-3,100,EUR\n")
    assert resp.status_code == 200
    result = resp.json()
    assert result["submitted"] == 3
    assert result["succeeded"] == 3
    assert result["skipped"] == 0
    assert result["failed"] == 0
    assert result["failures"] == []
    # 导入的订单可按标识读取
    got = client.get("/orders/imp-1", headers={"X-Tenant": "b1"})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500

def test_import_partial_success_with_distinct_reasons() -> None:
    text = (
        "b1,imp-10,100,CNY\n"      # 行1 成功
        "b1,imp-11\n"              # 行2 字段缺失
        "b1,imp-12,abc,CNY\n"      # 行3 金额非正整数
        "b1,imp-13,-5,CNY\n"       # 行4 金额非正整数
        "b1,imp-14,100,GBP\n"      # 行5 币种不受支持
        "b1,imp-15,200,CNY\n"      # 行6 成功
    )
    result = _import(text).json()
    assert result["submitted"] == 6
    assert result["succeeded"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 4
    reasons = {f["line"]: f["reason"] for f in result["failures"]}
    assert reasons[2] == "invalid_fields"
    assert reasons[3] == "invalid_amount"
    assert reasons[4] == "invalid_amount"
    assert reasons[5] == "unsupported_currency"
    # 失败清单带订单标识（能解析出时）
    assert {f["line"]: f["order_id"] for f in result["failures"]}[3] == "imp-12"
    # 失败行未落库，成功行已落库
    assert client.get("/orders/imp-11", headers={"X-Tenant": "b1"}).status_code == 404
    assert client.get("/orders/imp-15", headers={"X-Tenant": "b1"}).status_code == 200

def test_reimport_same_input_skips_accepted() -> None:
    text = "b1,imp-20,300,CNY\nb1,imp-21,400,CNY\n"
    first = _import(text).json()
    assert first["succeeded"] == 2
    second = _import(text).json()
    assert second["submitted"] == 2
    assert second["succeeded"] == 0
    assert second["skipped"] == 2
    assert second["failed"] == 0
    assert {s["reason"] for s in second["skips"]} == {"order_exists"}
    # 金额未被重复累加或改变
    order = client.get("/orders/imp-20", headers={"X-Tenant": "b1"}).json()
    assert order["amount_cents"] == 300 and order["paid_cents"] == 0

def test_conflicting_line_fails_without_changing_existing() -> None:
    _import("b1,imp-30,500,CNY\n")
    result = _import("b1,imp-30,999,CNY\nb1,imp-31,100,CNY\n").json()
    assert result["submitted"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    failure = result["failures"][0]
    assert failure["line"] == 1
    assert failure["order_id"] == "imp-30"
    assert failure["reason"] == "order_conflict"
    # 已有订单数据不变
    order = client.get("/orders/imp-30", headers={"X-Tenant": "b1"}).json()
    assert order["amount_cents"] == 500 and order["currency"] == "CNY"

def test_conflicting_currency_also_fails() -> None:
    _import("b1,imp-32,500,CNY\n")
    result = _import("b1,imp-32,500,USD\n").json()
    assert result["failed"] == 1
    assert result["failures"][0]["reason"] == "order_conflict"

def test_same_order_id_different_tenants_are_isolated() -> None:
    result = _import("b3,imp-40,100,CNY\nb4,imp-40,200,USD\n").json()
    assert result["succeeded"] == 2
    assert client.get("/orders/imp-40", headers={"X-Tenant": "b3"}).json()["amount_cents"] == 100
    assert client.get("/orders/imp-40", headers={"X-Tenant": "b4"}).json()["amount_cents"] == 200

def test_header_and_blank_lines_not_counted() -> None:
    result = _import("tenant,order_id,amount_cents,currency\n\nb1,imp-50,100,CNY\n").json()
    assert result["submitted"] == 1
    assert result["succeeded"] == 1

def test_imported_orders_support_payments_and_idempotency() -> None:
    _import("b1,imp-60,500,CNY\n")
    headers = {"X-Tenant": "b1", "Idempotency-Key": "imp-pay-1"}
    first = client.post("/orders/imp-60/payments", json={"amount_cents": 200}, headers=headers)
    assert first.status_code == 200
    replay = client.post("/orders/imp-60/payments", json={"amount_cents": 200}, headers=headers)
    assert replay.json() == first.json()
    assert client.get("/orders/imp-60", headers={"X-Tenant": "b1"}).json()["paid_cents"] == 200

def test_retry_after_mixed_batch_converges() -> None:
    text = "b1,imp-70,100,CNY\nb1,imp-71,bad,CNY\nb1,imp-72,200,CNY\n"
    first = _import(text).json()
    assert (first["succeeded"], first["skipped"], first["failed"]) == (2, 0, 1)
    # 模拟中断后重试同一份输入：成功行跳过、失败行重新判定、计数仍闭合
    second = _import(text).json()
    assert (second["succeeded"], second["skipped"], second["failed"]) == (0, 2, 1)
    assert second["submitted"] == first["submitted"] == 3
    assert second["failures"] == first["failures"]
