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

# --- 收款凭据 ---------------------------------------------------------------

H1 = {"X-Tenant": "t1"}

def _confirm(oid: str, rid: str, tenant: str = "t1"):
    return client.post(f"/orders/{oid}/receipts/{rid}/confirm", headers={"X-Tenant": tenant})

def _revoke(oid: str, rid: str, tenant: str = "t1"):
    return client.post(f"/orders/{oid}/receipts/{rid}/revoke", headers={"X-Tenant": tenant})

def _refund_receipt(oid: str, rid: str, payload: dict, tenant: str = "t1"):
    return client.post(f"/orders/{oid}/receipts/{rid}/refunds", json=payload, headers={"X-Tenant": tenant})

def test_pending_receipt_counts_in_paid_but_blocks_settlement() -> None:
    _make_order("rc1", 1000)
    # 待核销收款立即计入 paid_cents 与流水，但订单不结清。
    resp = client.post("/orders/rc1/payments", json={"amount_cents": 400, "receipt_id": "rcpt-1"}, headers=H1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paid_cents"] == 400 and body["outstanding_cents"] == 600 and body["status"] == "accepted"
    entry = body["ledger"][-1]
    assert entry["type"] == "payment" and entry["receipt_id"] == "rcpt-1"
    assert entry["receipt_status"] == "pending" and entry["balance_after_cents"] == 400

    # 待核销收款可继续用于收款与冲正的上限计算：再收 200、冲正 100 都成立。
    assert client.post("/orders/rc1/payments", json={"amount_cents": 200}, headers=H1).status_code == 200
    rf = client.post("/orders/rc1/refunds", json={"refund_id": "rf-p", "amount_cents": 100}, headers=H1)
    assert rf.status_code == 200 and rf.json()["paid_cents"] == 500
    # 即便收足也不结清：再收 500 使 paid=1000，仍有一笔待核销。
    full = client.post("/orders/rc1/payments", json={"amount_cents": 500}, headers=H1)
    assert full.status_code == 200 and full.json()["paid_cents"] == 1000
    assert full.json()["status"] == "accepted"
    # 超过未收金额仍按既有规则 409。
    assert client.post("/orders/rc1/payments", json={"amount_cents": 1}, headers=H1).status_code == 409

def test_confirm_settles_order() -> None:
    _make_order("rc2", 500)
    client.post("/orders/rc2/payments", json={"amount_cents": 500, "receipt_id": "rcpt-2"}, headers=H1)
    resp = _confirm("rc2", "rcpt-2")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "receipt_id": "rcpt-2",
        "receipt_status": "confirmed",
        "paid_cents": 500,
        "outstanding_cents": 0,
        "status": "settled",
        "idempotent_replay": False,
    }
    got = client.get("/orders/rc2", headers=H1).json()
    assert got["status"] == "settled"
    assert got["ledger"][-1]["receipt_status"] == "confirmed"

def test_revoke_releases_outstanding_and_allows_reregister() -> None:
    _make_order("rc3", 500)
    client.post("/orders/rc3/payments", json={"amount_cents": 300, "receipt_id": "rcpt-3"}, headers=H1)
    resp = _revoke("rc3", "rcpt-3")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "receipt_id": "rcpt-3",
        "receipt_status": "revoked",
        "paid_cents": 0,
        "outstanding_cents": 500,
        "status": "accepted",
        "idempotent_replay": False,
    }
    got = client.get("/orders/rc3", headers=H1).json()
    # 补撤销流水：含凭据标识与余额快照；收款行保留并标记 revoked。
    assert [(e["type"], e["amount_cents"], e["balance_after_cents"]) for e in got["ledger"]] == [
        ("payment", 300, 300),
        ("revocation", 300, 0),
    ]
    assert got["ledger"][0]["receipt_status"] == "revoked"
    assert got["ledger"][1]["receipt_id"] == "rcpt-3"
    # 撤销后同名凭据可再次登记，且可以核销。
    again = client.post("/orders/rc3/payments", json={"amount_cents": 500, "receipt_id": "rcpt-3"}, headers=H1)
    assert again.status_code == 200 and again.json()["paid_cents"] == 500
    assert _confirm("rc3", "rcpt-3").json()["status"] == "settled"

def test_duplicate_confirm_is_idempotent() -> None:
    _make_order("rc4", 500)
    client.post("/orders/rc4/payments", json={"amount_cents": 300, "receipt_id": "rcpt-4"}, headers=H1)
    first = _confirm("rc4", "rcpt-4")
    second = _confirm("rc4", "rcpt-4")
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["receipt_status"] == b["receipt_status"] == "confirmed"
    assert a["paid_cents"] == b["paid_cents"] == 300
    assert a["idempotent_replay"] is False and b["idempotent_replay"] is True
    got = client.get("/orders/rc4", headers=H1).json()
    # 不重复留痕、不改余额：仍只有一条收款流水。
    assert len(got["ledger"]) == 1 and got["paid_cents"] == 300

def test_duplicate_revoke_is_idempotent() -> None:
    _make_order("rc5", 500)
    client.post("/orders/rc5/payments", json={"amount_cents": 300, "receipt_id": "rcpt-5"}, headers=H1)
    first = _revoke("rc5", "rcpt-5")
    second = _revoke("rc5", "rcpt-5")
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["receipt_status"] == b["receipt_status"] == "revoked"
    assert a["paid_cents"] == b["paid_cents"] == 0
    assert a["idempotent_replay"] is False and b["idempotent_replay"] is True
    got = client.get("/orders/rc5", headers=H1).json()
    # 只补一笔撤销流水。
    assert len(got["ledger"]) == 2 and got["paid_cents"] == 0

def test_revoke_confirmed_receipt_is_conflict() -> None:
    _make_order("rc6", 500)
    client.post("/orders/rc6/payments", json={"amount_cents": 500, "receipt_id": "rcpt-6"}, headers=H1)
    assert _confirm("rc6", "rcpt-6").status_code == 200
    resp = _revoke("rc6", "rcpt-6")
    assert resp.status_code == 409
    got = client.get("/orders/rc6", headers=H1).json()
    # 数据不变：余额、状态、流水都保持已核销。
    assert got["paid_cents"] == 500 and got["status"] == "settled"
    assert len(got["ledger"]) == 1

def test_refund_pending_or_revoked_receipt_is_conflict() -> None:
    _make_order("rc7", 500)
    client.post("/orders/rc7/payments", json={"amount_cents": 300, "receipt_id": "rcpt-7"}, headers=H1)
    # 待核销直接冲正：409 且数据不变。
    resp = _refund_receipt("rc7", "rcpt-7", {"refund_id": "rf-r1", "amount_cents": 100})
    assert resp.status_code == 409
    got = client.get("/orders/rc7", headers=H1).json()
    assert got["paid_cents"] == 300 and len(got["ledger"]) == 1

    # 撤销后以凭据冲正：同样 409 且数据不变。
    assert _revoke("rc7", "rcpt-7").status_code == 200
    resp2 = _refund_receipt("rc7", "rcpt-7", {"refund_id": "rf-r2", "amount_cents": 100})
    assert resp2.status_code == 409
    got2 = client.get("/orders/rc7", headers=H1).json()
    assert got2["paid_cents"] == 0 and [e["type"] for e in got2["ledger"]] == ["payment", "revocation"]

def test_refund_confirmed_receipt_happy_path_and_replay() -> None:
    _make_order("rc8", 500)
    client.post("/orders/rc8/payments", json={"amount_cents": 400, "receipt_id": "rcpt-8"}, headers=H1)
    client.post("/orders/rc8/payments", json={"amount_cents": 100}, headers=H1)
    assert _confirm("rc8", "rcpt-8").status_code == 200
    payload = {"refund_id": "rf-rc8", "amount_cents": 300}
    first = _refund_receipt("rc8", "rcpt-8", payload)
    second = _refund_receipt("rc8", "rcpt-8", payload)
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["receipt_id"] == "rcpt-8" and a["receipt_status"] == "refunded"
    assert a["paid_cents"] == 200 and a["outstanding_cents"] == 300 and a["status"] == "accepted"
    assert a["idempotent_replay"] is False and b["idempotent_replay"] is True
    assert a["created_at"] == b["created_at"]
    # 已冲正凭据不可再次冲正。
    assert _refund_receipt("rc8", "rcpt-8", {"refund_id": "rf-rc9", "amount_cents": 1}).status_code == 409
    # 冲正标识携带不同金额仍按 409；超额同样 409，且均不留痕。
    assert _refund_receipt("rc8", "rcpt-8", {"refund_id": "rf-rc8", "amount_cents": 5}).status_code == 409
    assert _refund_receipt("rc8", "rcpt-8", {"refund_id": "rf-big", "amount_cents": 201}).status_code == 409
    got = client.get("/orders/rc8", headers=H1).json()
    assert got["paid_cents"] == 200 and len(got["ledger"]) == 3
    # 冲正后凭据收款行标记 refunded，冲正流水带凭据标识。
    assert got["ledger"][0]["receipt_status"] == "refunded"
    assert got["ledger"][-1]["type"] == "refund" and got["ledger"][-1]["receipt_id"] == "rcpt-8"

def test_duplicate_active_receipt_id_is_conflict() -> None:
    _make_order("rc9", 500)
    client.post("/orders/rc9/payments", json={"amount_cents": 100, "receipt_id": "dup"}, headers=H1)
    # 待核销/已核销期间同名再登记均 409；冲正标识与凭据标识作用域相同但互不替代。
    assert client.post("/orders/rc9/payments", json={"amount_cents": 100, "receipt_id": "dup"}, headers=H1).status_code == 409
    assert _confirm("rc9", "dup").status_code == 200
    assert client.post("/orders/rc9/payments", json={"amount_cents": 100, "receipt_id": "dup"}, headers=H1).status_code == 409

def test_receipt_bad_params_are_400() -> None:
    _make_order("rc10", 500)
    h = H1
    assert client.post("/orders/rc10/payments", json={"amount_cents": 100, "receipt_id": "  "}, headers=h).status_code == 400
    assert client.post("/orders/rc10/payments", json={"amount_cents": 100, "receipt_id": 123}, headers=h).status_code == 422
    assert _refund_receipt("rc10", "rc-x", {"amount_cents": 100}, ).status_code == 400
    assert _refund_receipt("rc10", "rc-x", {"refund_id": "rf", "amount_cents": 0}).status_code == 400
    assert _refund_receipt("rc10", "rc-x", {"refund_id": "", "amount_cents": 100}).status_code == 400
    assert _refund_receipt("rc10", "rc-x", {"refund_id": "rf", "amount_cents": "10"}).status_code == 400

def test_receipt_unknown_order_or_id_is_404() -> None:
    assert _confirm("missing", "rc-x").status_code == 404
    assert _revoke("missing", "rc-x").status_code == 404
    assert _refund_receipt("missing", "rc-x", {"refund_id": "rf", "amount_cents": 1}).status_code == 404
    _make_order("rc11", 500)
    assert _confirm("rc11", "no-such-receipt").status_code == 404
    assert _revoke("rc11", "no-such-receipt").status_code == 404

def test_concurrent_receipt_lifecycle_conserves() -> None:
    amount = 1000
    oid = "rc12"
    _make_order(oid, amount)
    # 20 笔待核销收款各 50，全部计入 paid 但不结清。
    for i in range(20):
        r = store.add_payment("t1", oid, 50, receipt_id=f"rcpt-{i}")
        assert r is not None
    assert store.get("t1", oid)["paid_cents"] == amount
    assert store.get("t1", oid)["status"] == "accepted"

    errors: list[Exception] = []
    start = threading.Barrier(20)

    def lifecycle(i: int) -> None:
        try:
            start.wait()
            # 偶数核销确认，奇数撤销；全部成功，互不影响。
            if i % 2 == 0:
                store.confirm_receipt("t1", oid, f"rcpt-{i}")
            else:
                store.revoke_receipt("t1", oid, f"rcpt-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=lifecycle, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    order = store.get("t1", oid)
    assert order is not None
    paid_sum = sum(e["amount_cents"] for e in order["ledger"] if e["type"] == "payment")
    refund_sum = sum(e["amount_cents"] for e in order["ledger"] if e["type"] == "refund")
    revoked_sum = sum(e["amount_cents"] for e in order["ledger"] if e["type"] == "revocation")
    # 守恒式：paid = 生效收款 − 生效冲正 − 生效撤销。
    assert order["paid_cents"] == paid_sum - refund_sum - revoked_sum == 500
    assert 0 <= order["paid_cents"] <= amount
    # 待核销全部离开 pending（10 confirmed、10 revoked），订单按余额判定。
    assert order["status"] == "accepted"
    # 流水余额快照前后衔接（含起点 0）。
    prev = 0
    for e in order["ledger"]:
        delta = e["amount_cents"] if e["type"] == "payment" else -e["amount_cents"]
        assert e["balance_after_cents"] == prev + delta
        assert 0 <= e["balance_after_cents"] <= amount
        prev = e["balance_after_cents"]

def test_concurrent_duplicate_confirm_applies_once() -> None:
    oid = "rc13"
    _make_order(oid, 500)
    store.add_payment("t1", oid, 500, receipt_id="rcpt-same")
    results: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def call() -> None:
        start.wait()
        r = store.confirm_receipt("t1", oid, "rcpt-same")
        with lock:
            results.append(bool(r and not r["idempotent_replay"]))

    threads = [threading.Thread(target=call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and results.count(False) == 9
    order = store.get("t1", oid)
    assert order["status"] == "settled" and len(order["ledger"]) == 1

def test_cross_tenant_receipt_not_visible() -> None:
    _make_order("rc14", 500, tenant="t1")
    client.post("/orders/rc14/payments", json={"amount_cents": 300, "receipt_id": "rcpt-x"}, headers={"X-Tenant": "t1"})
    # 跨租户的三个凭据操作一律 404，且不改变 t1 数据。
    assert _confirm("rc14", "rcpt-x", tenant="t2").status_code == 404
    assert _revoke("rc14", "rcpt-x", tenant="t2").status_code == 404
    assert _refund_receipt("rc14", "rcpt-x", {"refund_id": "rf-x", "amount_cents": 100}, tenant="t2").status_code == 404
    got = client.get("/orders/rc14", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 300 and len(got["ledger"]) == 1
    # 凭据标识作用域为（租户，订单）：t2 同名订单可独立登记同名凭据。
    _make_order("rc14", 500, tenant="t2")
    other = client.post("/orders/rc14/payments", json={"amount_cents": 200, "receipt_id": "rcpt-x"},
                        headers={"X-Tenant": "t2"})
    assert other.status_code == 200

def test_revoke_blocked_when_balance_consumed_by_refund() -> None:
    _make_order("rc15", 500)
    client.post("/orders/rc15/payments", json={"amount_cents": 300, "receipt_id": "rcpt-15"}, headers=H1)
    # 普通冲正先消耗掉这笔待核销收款对应的余额。
    assert client.post("/orders/rc15/refunds", json={"refund_id": "rf-15", "amount_cents": 300}, headers=H1).status_code == 200
    # 再撤销会使余额为负：409 且数据不变。
    resp = _revoke("rc15", "rcpt-15")
    assert resp.status_code == 409
    got = client.get("/orders/rc15", headers=H1).json()
    assert got["paid_cents"] == 0 and [e["type"] for e in got["ledger"]] == ["payment", "refund"]
