import os
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as store
from app.store.db import migrate
from app.store.orders import RefundExceedsPaid

migrate()
client = TestClient(app)

def _make_order(oid: str, amount: int = 1000, tenant: str = "t1") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"})
    assert resp.status_code == 201, resp.text

# --- 既有行为 ---------------------------------------------------------------

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
    _make_order("o4", 300)
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409

# --- 冲正：正常冲正 ----------------------------------------------------------

def test_full_refund_after_settlement() -> None:
    _make_order("r1", 500)
    assert client.post("/orders/r1/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 200
    resp = client.post("/orders/r1/refunds", json={"refund_id": "rf-1", "amount_cents": 500}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "refund_id": "rf-1",
        "refunded_cents": 500,
        "paid_cents": 0,
        "outstanding_cents": 500,
        "created_at": body["created_at"],
        "idempotent_replay": False,
    }
    got = client.get("/orders/r1", headers={"X-Tenant": "t1"}).json()
    # 全额冲正后回到未结清；语义字段含义不变。
    assert got["paid_cents"] == 0 and got["outstanding_cents"] == 500 and got["status"] == "accepted"
    # 流水按发生顺序留痕，能解释“已收金额为何是这个数”。
    ledger = got["ledger"]
    assert [(e["type"], e["amount_cents"], e["balance_after_cents"]) for e in ledger] == [
        ("payment", 500, 500),
        ("refund", 500, 0),
    ]
    assert ledger[1]["refund_id"] == "rf-1" and ledger[1]["created_at"]

def test_partial_refund_then_payment_resettles() -> None:
    _make_order("r2", 300)
    client.post("/orders/r2/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    resp = client.post("/orders/r2/refunds", json={"refund_id": "rf-1", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    assert resp.json()["paid_cents"] == 200 and resp.json()["outstanding_cents"] == 100
    got = client.get("/orders/r2", headers={"X-Tenant": "t1"}).json()
    assert got["status"] == "accepted"
    # 部分冲正后再收款，重新结清。
    again = client.post("/orders/r2/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert again.status_code == 200 and again.json()["paid_cents"] == 300
    assert again.json()["status"] == "settled"
    types = [(e["type"], e["amount_cents"]) for e in again.json()["ledger"]]
    assert types == [("payment", 300), ("refund", 100), ("payment", 100)]

# --- 冲正：超额拒绝 ----------------------------------------------------------

def test_refund_over_paid_is_conflict_and_unchanged() -> None:
    _make_order("r3", 300)
    client.post("/orders/r3/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    resp = client.post("/orders/r3/refunds", json={"refund_id": "rf-big", "amount_cents": 150}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 409 and "refund exceeds paid amount" in resp.json()["detail"]
    got = client.get("/orders/r3", headers={"X-Tenant": "t1"}).json()
    # 失败不留任何痕迹：余额与流水都不变。
    assert got["paid_cents"] == 100 and len(got["ledger"]) == 1

def test_refund_unknown_order_is_not_found() -> None:
    resp = client.post("/orders/missing/refunds", json={"refund_id": "x", "amount_cents": 1}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 404

def test_refund_bad_params_are_400() -> None:
    _make_order("r3b", 100)
    client.post("/orders/r3b/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    h = {"X-Tenant": "t1"}
    assert client.post("/orders/r3b/refunds", json={"amount_cents": 100}, headers=h).status_code == 400
    assert client.post("/orders/r3b/refunds", json={"refund_id": "rf", "amount_cents": 0}, headers=h).status_code == 400
    assert client.post("/orders/r3b/refunds", json={"refund_id": "rf", "amount_cents": -1}, headers=h).status_code == 400
    assert client.post("/orders/r3b/refunds", json={"refund_id": "rf", "amount_cents": "10"}, headers=h).status_code == 400
    assert client.post("/orders/r3b/refunds", json={"refund_id": "", "amount_cents": 10}, headers=h).status_code == 400
    assert client.post("/orders/r3b/refunds", json={"refund_id": "rf", "amount_cents": 10}).status_code == 400

# --- 冲正：幂等 --------------------------------------------------------------

def test_duplicate_refund_id_is_idempotent() -> None:
    _make_order("r4", 500)
    client.post("/orders/r4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    payload = {"refund_id": "rf-dup", "amount_cents": 200}
    first = client.post("/orders/r4/refunds", json=payload, headers={"X-Tenant": "t1"})
    second = client.post("/orders/r4/refunds", json=payload, headers={"X-Tenant": "t1"})
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["paid_cents"] == b["paid_cents"] == 300
    assert a["outstanding_cents"] == b["outstanding_cents"] == 200
    assert a["created_at"] == b["created_at"]
    assert a["idempotent_replay"] is False and b["idempotent_replay"] is True
    got = client.get("/orders/r4", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 300
    assert [e for e in got["ledger"] if e["type"] == "refund"] == [got["ledger"][1]]

def test_same_refund_id_with_different_amount_is_conflict() -> None:
    _make_order("r4b", 500)
    client.post("/orders/r4b/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r4b/refunds", json={"refund_id": "rf", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    resp = client.post("/orders/r4b/refunds", json={"refund_id": "rf", "amount_cents": 200}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 409
    assert client.get("/orders/r4b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400

# --- 冲正：并发守恒 ----------------------------------------------------------

def test_concurrent_payments_and_refunds_conserve() -> None:
    oid = "r5"
    amount = 1000
    _make_order(oid, amount)
    # 先收足，冲正才有余额；随后并发冲正与再收款交错。
    assert store.add_payment("t1", oid, amount) is not None

    errors: list[Exception] = []
    start = threading.Barrier(24)

    def refund(i: int) -> None:
        try:
            start.wait()
            store.add_refund("t1", oid, f"rf-{i}", 50)
        except RefundExceedsPaid:
            pass  # 竞争中余额暂时不足，属于允许的 409 路径
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def pay(i: int) -> None:
        try:
            start.wait()
            store.add_payment("t1", oid, 50)
        except ValueError:
            pass  # 收款同样可能竞争超额
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=refund, args=(i,)) for i in range(16)]
    threads += [threading.Thread(target=pay, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    order = store.get("t1", oid)
    assert order is not None
    paid_sum = sum(e["amount_cents"] for e in order["ledger"] if e["type"] == "payment")
    refund_sum = sum(e["amount_cents"] for e in order["ledger"] if e["type"] == "refund")
    # 守恒式：paid = 生效收款之和 − 生效冲正之和。
    assert order["paid_cents"] == paid_sum - refund_sum
    assert 0 <= order["paid_cents"] <= amount
    # 流水余额快照任意时刻都在合法区间，且前后衔接（含起点 0）。
    prev = 0
    for e in order["ledger"]:
        delta = e["amount_cents"] if e["type"] == "payment" else -e["amount_cents"]
        assert e["balance_after_cents"] == prev + delta
        assert 0 <= e["balance_after_cents"] <= amount
        prev = e["balance_after_cents"]

def test_concurrent_duplicate_refund_ids_apply_once() -> None:
    oid = "r6"
    _make_order(oid, 500)
    store.add_payment("t1", oid, 500)
    results: list[int | str] = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def call() -> None:
        start.wait()
        try:
            r = store.add_refund("t1", oid, "rf-same", 200)
            with lock:
                results.append("ok" if r and not r["idempotent_replay"] else "replay")
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(str(exc))

    threads = [threading.Thread(target=call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("replay") == 9
    assert store.get("t1", oid)["paid_cents"] == 300

# --- 冲正：跨租户不可见 ------------------------------------------------------

def test_cross_tenant_refund_is_not_found() -> None:
    _make_order("r7", 100, tenant="t1")
    client.post("/orders/r7/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    # 跨租户冲正按不存在处理，且不改变 t1 的任何数据。
    resp = client.post("/orders/r7/refunds", json={"refund_id": "rf-x", "amount_cents": 100}, headers={"X-Tenant": "t2"})
    assert resp.status_code == 404
    got = client.get("/orders/r7", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 100 and len(got["ledger"]) == 1

def test_refund_id_scope_is_per_tenant_order() -> None:
    # 冲正标识作用域是（租户，订单）：不同租户可各自独立使用同一标识，
    # 也不能用冲正标识替代订单标识。
    _make_order("r8", 100, tenant="t1")
    _make_order("r8", 100, tenant="t2")
    client.post("/orders/r8/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/r8/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t2"})
    payload = {"refund_id": "shared-rf", "amount_cents": 40}
    r1 = client.post("/orders/r8/refunds", json=payload, headers={"X-Tenant": "t1"})
    r2 = client.post("/orders/r8/refunds", json=payload, headers={"X-Tenant": "t2"})
    assert r1.status_code == r2.status_code == 200
    assert client.get("/orders/r8", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 60
    assert client.get("/orders/r8", headers={"X-Tenant": "t2"}).json()["paid_cents"] == 60
