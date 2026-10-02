import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)


def import_text(text: str) -> dict:
    resp = client.post("/orders/import", content=text.encode("utf-8"),
                       headers={"Content-Type": "text/plain"})
    assert resp.status_code == 200
    return resp.json()


def test_mixed_batch_partial_success_and_counts_close() -> None:
    # 行 1 为表头（不计入提交行数）；行 2/8 成功；行 3/4 金额非法；行 5 币种不支持；
    # 行 6 字段缺失；行 7 订单标识缺失；末尾空行不计入提交行数
    text = (
        "tenant,order_id,amount_cents,currency\n"
        "t1,imp-1,500,CNY\n"
        "t1,imp-2,abc,CNY\n"
        "t1,imp-3,0,CNY\n"
        "t1,imp-4,100,GBP\n"
        "t1,imp-5,100\n"
        "t1,,100,CNY\n"
        "t2,imp-6,4500,USD\n"
        "\n"
    )
    result = import_text(text)
    assert result["submitted"] == 7
    assert result["succeeded"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 5
    assert result["submitted"] == result["succeeded"] + result["skipped"] + result["failed"]

    failures = {f["line"]: f for f in result["failures"]}
    assert failures[3]["reason"] == "invalid_amount" and failures[3]["order_id"] == "imp-2"
    assert failures[4]["reason"] == "invalid_amount"
    assert failures[5]["reason"] == "unsupported_currency" and failures[5]["order_id"] == "imp-4"
    assert failures[6]["reason"] == "invalid_line" and failures[6]["order_id"] == "imp-5"
    assert failures[7]["reason"] == "invalid_line" and failures[7]["order_id"] is None

    # 成功的订单可按标识读取，且租户隔离
    got = client.get("/orders/imp-1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["amount_cents"] == 500
    assert client.get("/orders/imp-1", headers={"X-Tenant": "t2"}).status_code == 404
    # 失败的行没有落库
    assert client.get("/orders/imp-4", headers={"X-Tenant": "t1"}).status_code == 404


def test_reimport_is_safe_and_idempotent() -> None:
    text = "t1,imp-10,700,CNY\nt1,imp-11,900,CNY\n"
    first = import_text(text)
    assert (first["succeeded"], first["skipped"], first["failed"]) == (2, 0, 0)

    # 重试同一份输入：成功过的行按跳过处理，不产生重复订单
    second = import_text(text)
    assert (second["succeeded"], second["skipped"], second["failed"]) == (0, 2, 0)
    assert second["submitted"] == 2
    assert {s["line"] for s in second["skipped_lines"]} == {1, 2}
    assert all(s["reason"] == "order_already_exists" for s in second["skipped_lines"])

    order = client.get("/orders/imp-10", headers={"X-Tenant": "t1"}).json()
    assert order["amount_cents"] == 700 and order["paid_cents"] == 0


def test_same_order_id_with_different_content_fails_and_keeps_original() -> None:
    assert import_text("t1,imp-20,100,CNY")["succeeded"] == 1

    result = import_text("t1,imp-20,200,CNY\nt1,imp-20,100,USD")
    assert result["succeeded"] == 0 and result["skipped"] == 0 and result["failed"] == 2
    assert all(f["reason"] == "order_content_mismatch" for f in result["failures"])
    assert [f["line"] for f in result["failures"]] == [1, 2]

    # 已有订单的金额与状态不被改变
    order = client.get("/orders/imp-20", headers={"X-Tenant": "t1"}).json()
    assert order["amount_cents"] == 100 and order["currency"] == "CNY"
    assert order["paid_cents"] == 0 and order["status"] == "accepted"


def test_failed_line_can_be_fixed_and_retried() -> None:
    bad = import_text("t1,imp-30,-50,CNY")
    assert bad["failed"] == 1 and bad["failures"][0]["reason"] == "invalid_amount"

    # 修正后重试：原先失败的行按其结果重新判定并成功受理
    fixed = import_text("t1,imp-30,50,CNY")
    assert fixed["succeeded"] == 1
    assert client.get("/orders/imp-30", headers={"X-Tenant": "t1"}).json()["amount_cents"] == 50


def test_imported_orders_follow_normal_payment_and_idempotency_rules() -> None:
    assert import_text("t1,imp-40,300,CNY")["succeeded"] == 1

    headers = {"X-Tenant": "t1", "Idempotency-Key": "pay-imp-40"}
    resp = client.post("/orders/imp-40/payments", json={"amount_cents": 120}, headers=headers)
    assert resp.status_code == 200 and resp.json()["paid_cents"] == 120
    # 同标识重复收款回放首次结果，不重复累加
    replay = client.post("/orders/imp-40/payments", json={"amount_cents": 120}, headers=headers)
    assert replay.status_code == 200 and replay.json()["paid_cents"] == 120
    # 超额收款仍按原有规则拒绝
    assert client.post("/orders/imp-40/payments", json={"amount_cents": 500},
                       headers={"X-Tenant": "t1"}).status_code == 409


def test_tenant_isolation_in_import() -> None:
    result = import_text("t1,imp-50,100,CNY\nt2,imp-50,100,CNY")
    assert result["succeeded"] == 2
    assert client.get("/orders/imp-50", headers={"X-Tenant": "t1"}).status_code == 200
    assert client.get("/orders/imp-50", headers={"X-Tenant": "t2"}).status_code == 200


def test_import_does_not_conflict_with_single_accept() -> None:
    # 逐张受理的订单再出现在导入中：内容一致跳过、不一致失败
    body = {"tenant": "t1", "order_id": "imp-60", "amount_cents": 800, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    result = import_text("t1,imp-60,800,CNY\nt1,imp-60,801,CNY")
    assert (result["succeeded"], result["skipped"], result["failed"]) == (0, 1, 1)
    assert result["failures"][0]["reason"] == "order_content_mismatch"
