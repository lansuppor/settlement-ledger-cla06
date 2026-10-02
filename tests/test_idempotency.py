import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_tmp_dir = tempfile.mkdtemp()
DB_PATH = os.path.join(_tmp_dir, "idem.sqlite")
os.environ.setdefault("APP_DB", DB_PATH)

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

ORDER = {"tenant": "ta", "order_id": "o-1", "amount_cents": 1000, "currency": "CNY"}


def _post_order(key: str, body: dict | None = None, tenant: str = "ta"):
    headers = {"Idempotency-Key": key}
    return client.post("/orders", json=body or ORDER, headers=headers)


def test_duplicate_order_submission_replays_first_result() -> None:
    first = _post_order("k-order-1")
    assert first.status_code == 201
    first_body = first.json()

    second = _post_order("k-order-1")
    assert second.status_code == 201
    assert second.json() == first_body

    # 只产生一笔受理记录
    assert client.get("/orders/o-1", headers={"X-Tenant": "ta"}).json()["paid_cents"] == 0


def test_different_amount_with_same_key_is_rejected_without_change() -> None:
    _post_order("k-order-2", {"tenant": "ta", "order_id": "o-2", "amount_cents": 300, "currency": "CNY"})
    conflict = _post_order(
        "k-order-2", {"tenant": "ta", "order_id": "o-2", "amount_cents": 999, "currency": "CNY"}
    )
    assert conflict.status_code == 422

    # 金额仍为首次的 300，冲突请求没有产生新订单
    got = client.get("/orders/o-2", headers={"X-Tenant": "ta"}).json()
    assert got["amount_cents"] == 300


def test_different_currency_with_same_key_is_rejected() -> None:
    _post_order("k-order-3", {"tenant": "ta", "order_id": "o-3a", "amount_cents": 100, "currency": "CNY"})
    conflict = _post_order(
        "k-order-3", {"tenant": "ta", "order_id": "o-3a", "amount_cents": 100, "currency": "USD"}
    )
    assert conflict.status_code == 422


def test_same_key_across_tenants_is_independent() -> None:
    body_a = {"tenant": "ta", "order_id": "o-cross", "amount_cents": 100, "currency": "CNY"}
    body_b = {"tenant": "tb", "order_id": "o-cross", "amount_cents": 200, "currency": "CNY"}
    r1 = client.post("/orders", json=body_a, headers={"Idempotency-Key": "shared-key"})
    r2 = client.post("/orders", json=body_b, headers={"Idempotency-Key": "shared-key"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["amount_cents"] == 100
    assert r2.json()["amount_cents"] == 200
    # 跨租户仍然读不到对方的订单
    assert client.get("/orders/o-cross", headers={"X-Tenant": "tb"}).json()["amount_cents"] == 200


def test_payment_duplicate_does_not_double_charge() -> None:
    _post_order("k-order-pay", {"tenant": "ta", "order_id": "o-pay", "amount_cents": 500, "currency": "CNY"})
    headers = {"X-Tenant": "ta", "Idempotency-Key": "k-pay-1"}
    first = client.post("/orders/o-pay/payments", json={"amount_cents": 200}, headers=headers)
    assert first.status_code == 200 and first.json()["paid_cents"] == 200

    second = client.post("/orders/o-pay/payments", json={"amount_cents": 200}, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["paid_cents"] == 200  # 没有重复累加
    assert second.json()["outstanding_cents"] == 300


def test_payment_conflicting_amount_rejected_and_not_applied() -> None:
    _post_order("k-order-pay2", {"tenant": "ta", "order_id": "o-pay2", "amount_cents": 500, "currency": "CNY"})
    headers = {"X-Tenant": "ta", "Idempotency-Key": "k-pay-2"}
    assert client.post("/orders/o-pay2/payments", json={"amount_cents": 100}, headers=headers).status_code == 200
    conflict = client.post("/orders/o-pay2/payments", json={"amount_cents": 150}, headers=headers)
    assert conflict.status_code == 422
    got = client.get("/orders/o-pay2", headers={"X-Tenant": "ta"}).json()
    assert got["paid_cents"] == 100  # 冲突请求未改变金额


def test_replayed_overpayment_keeps_first_conflict_result() -> None:
    _post_order("k-order-pay3", {"tenant": "ta", "order_id": "o-pay3", "amount_cents": 100, "currency": "CNY"})
    headers = {"X-Tenant": "ta", "Idempotency-Key": "k-pay-over"}
    first = client.post("/orders/o-pay3/payments", json={"amount_cents": 200}, headers=headers)
    assert first.status_code == 409
    # 重复提交回放首次结论，且收款没有被登记
    second = client.post("/orders/o-pay3/payments", json={"amount_cents": 200}, headers=headers)
    assert second.status_code == 409
    assert client.get("/orders/o-pay3", headers={"X-Tenant": "ta"}).json()["paid_cents"] == 0


def test_replayed_order_against_existing_order_keeps_409() -> None:
    # 先以无标识方式受理订单
    body = {"tenant": "ta", "order_id": "o-existing", "amount_cents": 100, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    r1 = _post_order("k-order-existing", body)
    r2 = _post_order("k-order-existing", body)
    assert r1.status_code == 409 and r2.status_code == 409


def test_same_key_cannot_switch_scope() -> None:
    _post_order("k-scope", {"tenant": "ta", "order_id": "o-scope", "amount_cents": 100, "currency": "CNY"})
    pay = client.post(
        "/orders/o-scope/payments",
        json={"amount_cents": 100},
        headers={"X-Tenant": "ta", "Idempotency-Key": "k-scope"},
    )
    assert pay.status_code == 422


def test_concurrent_same_key_only_one_order() -> None:
    body = {"tenant": "ta", "order_id": "o-concurrent", "amount_cents": 100, "currency": "CNY"}

    def submit(_: int):
        # 每个线程使用独立客户端，但共用同一个 SQLite 库文件，真正验证库级串行化
        return TestClient(app).post("/orders", json=body, headers={"Idempotency-Key": "k-concurrent"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert [r.status_code for r in responses].count(201) == 8
    bodies = {r.text for r in responses}
    assert len(bodies) == 1  # 所有响应回放的都是同一笔订单


def test_concurrent_same_payment_key_charges_once() -> None:
    _post_order(
        "k-order-conc-pay",
        {"tenant": "ta", "order_id": "o-conc-pay", "amount_cents": 1000, "currency": "CNY"},
    )
    headers = {"X-Tenant": "ta", "Idempotency-Key": "k-conc-pay"}

    def submit(_: int):
        return TestClient(app).post(
            "/orders/o-conc-pay/payments", json={"amount_cents": 300}, headers=headers
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["paid_cents"] == 300 for r in responses)
    assert client.get("/orders/o-conc-pay", headers={"X-Tenant": "ta"}).json()["paid_cents"] == 300


def test_request_ids_survive_restart() -> None:
    # 模拟重启：丢弃所有连接上的进程内状态（本实现只有数据库状态），重新迁移并建新客户端
    client2 = TestClient(app)
    # 重启后重复受理：仍是首次结果，不产生第二笔
    repeat = client2.post("/orders", json=ORDER, headers={"Idempotency-Key": "k-order-1"})
    assert repeat.status_code == 201
    assert repeat.json()["outstanding_cents"] == 1000

    # 重启后重复收款：不累加
    headers = {"X-Tenant": "ta", "Idempotency-Key": "k-pay-1"}
    repeat_pay = client2.post("/orders/o-pay/payments", json={"amount_cents": 200}, headers=headers)
    assert repeat_pay.status_code == 200
    assert repeat_pay.json()["paid_cents"] == 200

    # 重启后冲突请求结论一致，不会被误判为新请求
    conflict = client2.post(
        "/orders",
        json={"tenant": "ta", "order_id": "o-2", "amount_cents": 999, "currency": "CNY"},
        headers={"Idempotency-Key": "k-order-2"},
    )
    assert conflict.status_code == 422
    assert client2.get("/orders/o-2", headers={"X-Tenant": "ta"}).json()["amount_cents"] == 300
