import os, tempfile
os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)
LIVE_PORT = 8765

def test_accept_and_read_order() -> None:
    body = {"tenant": "t1", "order_id": "o1", "amount_cents": 500, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    got = client.get("/orders/o1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500

def test_duplicate_is_refused() -> None:
    body = {"tenant": "t1", "order_id": "o2", "amount_cents": 100, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    assert client.post("/orders", json=body).status_code == 409

def test_cross_tenant_read_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o3", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.get("/orders/o3", headers={"X-Tenant": "t2"}).status_code == 404

def test_payment_cannot_exceed_outstanding() -> None:
    body = {"tenant": "t1", "order_id": "o4", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409

# ---------- 冲正 ----------

def _create_order(tenant: str, order_id: str, amount: int) -> None:
    assert client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    ).status_code == 201

def _pay(order_id: str, amount: int, tenant: str = "t1"):
    return client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})

def _refund(order_id: str, refund_id: str, amount: int, tenant: str = "t1"):
    return client.post(
        f"/orders/{order_id}/refunds",
        json={"refund_id": refund_id, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )

def test_refund_full_then_reopen_order() -> None:
    _create_order("t1", "r1", 1000)
    assert _pay("r1", 1000).json()["status"] == "settled"
    resp = _refund("r1", "rf-1", 400)
    assert resp.status_code == 200
    body = resp.json()
    assert body["refund_id"] == "rf-1" and body["amount_cents"] == 400
    assert body["paid_cents"] == 600 and body["outstanding_cents"] == 400
    assert body["idempotent_replay"] is False
    # 已收低于订单金额：回到未结清。
    got = client.get("/orders/r1", headers={"X-Tenant": "t1"}).json()
    assert got["status"] == "accepted"
    # 流水按发生顺序解释 paid_cents：1000 − 400 = 600。
    assert got["entries"] == [
        {"kind": "payment", "amount_cents": 1000, "created_at": got["entries"][0]["created_at"]},
        {"kind": "refund", "amount_cents": 400, "refund_id": "rf-1", "created_at": got["entries"][1]["created_at"]},
    ]

def test_partial_refund_then_payment() -> None:
    _create_order("t1", "r2", 1000)
    _pay("r2", 500)
    assert _refund("r2", "rf-1", 200).json()["paid_cents"] == 300
    resp = _pay("r2", 700)
    assert resp.status_code == 200 and resp.json()["paid_cents"] == 1000
    assert resp.json()["status"] == "settled" and resp.json()["outstanding_cents"] == 0
    got = client.get("/orders/r2", headers={"X-Tenant": "t1"}).json()
    kinds = [(e["kind"], e["amount_cents"]) for e in got["entries"]]
    assert kinds == [("payment", 500), ("refund", 200), ("payment", 700)]

def test_refund_cannot_exceed_paid() -> None:
    _create_order("t1", "r3", 1000)
    _pay("r3", 300)
    resp = _refund("r3", "rf-big", 400)
    assert resp.status_code == 409
    # 409 原因可与“订单不存在”区分。
    assert "paid" in resp.json()["detail"]
    # 拒绝不留任何痕迹，余额不变。
    got = client.get("/orders/r3", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 300 and len(got["entries"]) == 1
    # 未收过款的订单冲正同样 409，而非 404。
    _create_order("t1", "r3b", 100)
    assert _refund("r3b", "rf-x", 10).status_code == 409

def test_duplicate_refund_id_is_idempotent() -> None:
    _create_order("t1", "r4", 1000)
    _pay("r4", 1000)
    first = _refund("r4", "rf-dup", 300)
    assert first.status_code == 200
    # 重复请求即使携带不同金额，也回放首次结果，不再次减少余额。
    second = _refund("r4", "rf-dup", 999)
    assert second.status_code == 200
    a, b = first.json(), second.json()
    assert b["idempotent_replay"] is True
    assert a["amount_cents"] == b["amount_cents"] == 300
    assert a["paid_cents"] == b["paid_cents"] == 700
    assert a["created_at"] == b["created_at"]
    got = client.get("/orders/r4", headers={"X-Tenant": "t1"}).json()
    refunds = [e for e in got["entries"] if e["kind"] == "refund"]
    assert len(refunds) == 1
    # 冲正标识作用域是（租户, 订单）：不同标识各自生效；同标识在另一订单独立。
    assert _refund("r4", "rf-other", 100).json()["paid_cents"] == 600
    _create_order("t1", "r4b", 500)
    _pay("r4b", 500)
    assert _refund("r4b", "rf-dup", 500).status_code == 200

def test_refund_invalid_params_are_400() -> None:
    _create_order("t1", "r5", 1000)
    _pay("r5", 100)
    assert _refund("r5", "rf", 0).status_code == 400
    assert _refund("r5", "rf", -1).status_code == 400
    resp = client.post("/orders/r5/refunds", json={"amount_cents": 10}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400
    resp = client.post("/orders/r5/refunds", json={"refund_id": "rf", "amount_cents": 10})
    assert resp.status_code == 400

def test_refund_order_not_found_or_cross_tenant() -> None:
    _create_order("t1", "r6", 1000)
    _pay("r6", 100)
    assert client.post(
        "/orders/missing/refunds",
        json={"refund_id": "rf", "amount_cents": 10},
        headers={"X-Tenant": "t1"},
    ).status_code == 404
    # 跨租户冲正按不存在处理，且不留痕：t1 随后用同一标识仍可首次生效。
    resp = _refund("r6", "rf-secret", 50, tenant="t2")
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"
    assert _refund("r6", "rf-secret", 50, tenant="t1").status_code == 200

@pytest.fixture(scope="module")
def live_server():
    # 起真实 HTTP 服务支撑并发测试（TestClient 为串行门户，不适合多线程压测）。
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=LIVE_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{LIVE_PORT}/health", timeout=1)
            break
        except httpx.TransportError:
            time.sleep(0.1)
    yield
    server.should_exit = True

def test_concurrent_refunds_and_payments_conserve(live_server) -> None:
    _create_order("t1", "r7", 1000)
    _pay("r7", 600)

    def call(kind: str, idx: int) -> int:
        path = "payments" if kind == "pay" else "refunds"
        payload = {"amount_cents": 100} if kind == "pay" else {"refund_id": f"rf-{idx}", "amount_cents": 100}
        with httpx.Client(base_url=f"http://127.0.0.1:{LIVE_PORT}", timeout=10) as http:
            return http.post(f"/orders/r7/{path}", json=payload, headers={"X-Tenant": "t1"}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        # 10 笔冲正共 1000，4 笔补款共 400，交叠发生。
        futures = [pool.submit(call, "refund", i) for i in range(10)]
        futures += [pool.submit(call, "pay", i) for i in range(4)]
        statuses = [f.result() for f in futures]
    assert set(statuses) <= {200, 409}

    got = client.get("/orders/r7", headers={"X-Tenant": "t1"}).json()
    paid_sum = sum(e["amount_cents"] for e in got["entries"] if e["kind"] == "payment")
    refund_sum = sum(e["amount_cents"] for e in got["entries"] if e["kind"] == "refund")
    # 守恒：账面余额 = 生效收款之和 − 生效冲正之和，且恒在 [0, 订单金额]。
    assert got["paid_cents"] == paid_sum - refund_sum
    assert 0 <= got["paid_cents"] <= 1000
    assert got["paid_cents"] + got["outstanding_cents"] == 1000
    # 被拒请求未留痕：生效冲正总额不超过生效收款总额。
    assert refund_sum <= paid_sum
