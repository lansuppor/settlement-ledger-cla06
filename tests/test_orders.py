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
H = {"X-Tenant": "t1"}

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

# --- 收款凭据：待核销登记 ----------------------------------------------------

def test_receipt_payment_is_pending_and_counts_toward_balance_but_not_settled() -> None:
    _make_order("c1", 1000)
    resp = client.post("/orders/c1/payments", json={"amount_cents": 400, "receipt_id": "rc-1"}, headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 立即计入 paid_cents，但存在待核销收款时订单不结清。
    assert body["paid_cents"] == 400 and body["outstanding_cents"] == 600 and body["status"] == "accepted"
    got = client.get("/orders/c1", headers=H).json()
    pay = got["ledger"][-1]
    assert pay["type"] == "payment" and pay["receipt_id"] == "rc-1" and pay["receipt_status"] == "pending"
    assert pay["balance_after_cents"] == 400

def test_pending_receipt_still_allows_more_payments_and_refunds() -> None:
    _make_order("c2", 1000)
    client.post("/orders/c2/payments", json={"amount_cents": 400, "receipt_id": "rc-1"}, headers=H)
    # 待核销资金计入上限：继续收款只能收未收部分，超额仍 409。
    assert client.post("/orders/c2/payments", json={"amount_cents": 600}, headers=H).status_code == 200
    again = client.post("/orders/c2/payments", json={"amount_cents": 1}, headers=H)
    assert again.status_code == 409
    # 待核销资金同样可被普通冲正占用。
    rf = client.post("/orders/c2/refunds", json={"refund_id": "rf-1", "amount_cents": 300}, headers=H)
    assert rf.status_code == 200 and rf.json()["paid_cents"] == 700
    # 仍有待核销凭据，即便收足过也保持 accepted。
    assert client.get("/orders/c2", headers=H).json()["status"] == "accepted"

def test_duplicate_receipt_id_on_payment_is_conflict() -> None:
    _make_order("c2b", 500)
    first = client.post("/orders/c2b/payments", json={"amount_cents": 100, "receipt_id": "dup"}, headers=H)
    assert first.status_code == 200
    second = client.post("/orders/c2b/payments", json={"amount_cents": 100, "receipt_id": "dup"}, headers=H)
    assert second.status_code == 409
    got = client.get("/orders/c2b", headers=H).json()
    assert got["paid_cents"] == 100 and len(got["ledger"]) == 1

def test_payment_bad_receipt_id_is_400() -> None:
    _make_order("c2c", 500)
    assert client.post("/orders/c2c/payments", json={"amount_cents": 100, "receipt_id": ""}, headers=H).status_code == 400
    assert client.post("/orders/c2c/payments", json={"amount_cents": 100, "receipt_id": "   "}, headers=H).status_code == 400

# --- 收款凭据：核销确认 ------------------------------------------------------

def test_confirm_settles_order_when_fully_paid() -> None:
    _make_order("c3", 500)
    client.post("/orders/c3/payments", json={"amount_cents": 500, "receipt_id": "rc-1"}, headers=H)
    assert client.get("/orders/c3", headers=H).json()["status"] == "accepted"
    resp = client.post("/orders/c3/receipts/rc-1/confirm", headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["receipt_id"] == "rc-1" and body["receipt_status"] == "confirmed"
    assert body["paid_cents"] == 500 and body["outstanding_cents"] == 0 and body["status"] == "settled"
    assert body["idempotent_replay"] is False and body["created_at"]
    got = client.get("/orders/c3", headers=H).json()
    assert got["status"] == "settled"
    assert got["ledger"][-1]["receipt_status"] == "confirmed"

def test_confirm_partial_receipt_keeps_accepted() -> None:
    _make_order("c3b", 500)
    client.post("/orders/c3b/payments", json={"amount_cents": 200, "receipt_id": "rc-1"}, headers=H)
    resp = client.post("/orders/c3b/receipts/rc-1/confirm", headers=H)
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted" and resp.json()["outstanding_cents"] == 300
    # 确认不新增流水、不改余额。
    got = client.get("/orders/c3b", headers=H).json()
    assert len(got["ledger"]) == 1 and got["paid_cents"] == 200

def test_duplicate_confirm_is_idempotent() -> None:
    _make_order("c4", 500)
    client.post("/orders/c4/payments", json={"amount_cents": 500, "receipt_id": "rc-1"}, headers=H)
    first = client.post("/orders/c4/receipts/rc-1/confirm", headers=H)
    second = client.post("/orders/c4/receipts/rc-1/confirm", headers=H)
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["receipt_status"] == b["receipt_status"] == "confirmed"
    assert a["paid_cents"] == b["paid_cents"] == 500
    assert a["status"] == b["status"] == "settled"
    assert a["created_at"] == b["created_at"]
    assert a["idempotent_replay"] is False and b["idempotent_replay"] is True
    got = client.get("/orders/c4", headers=H).json()
    # 不重复留痕：仍只有一条收款流水。
    assert len(got["ledger"]) == 1

# --- 收款凭据：撤销 ----------------------------------------------------------

def test_revoke_releases_pending_money_and_appends_ledger_entry() -> None:
    _make_order("c5", 1000)
    client.post("/orders/c5/payments", json={"amount_cents": 400, "receipt_id": "rc-1"}, headers=H)
    resp = client.post("/orders/c5/receipts/rc-1/revoke", headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["receipt_id"] == "rc-1" and body["receipt_status"] == "revoked"
    assert body["paid_cents"] == 0 and body["outstanding_cents"] == 1000 and body["status"] == "accepted"
    assert body["idempotent_replay"] is False
    got = client.get("/orders/c5", headers=H).json()
    ledger = got["ledger"]
    assert [(e["type"], e["amount_cents"], e["balance_after_cents"]) for e in ledger] == [
        ("payment", 400, 400),
        ("receipt_revocation", 400, 0),
    ]
    assert ledger[0]["receipt_status"] == "revoked" and ledger[1]["receipt_id"] == "rc-1"
    # 撤销释放未收：原金额可以重新收齐。
    again = client.post("/orders/c5/payments", json={"amount_cents": 1000}, headers=H)
    assert again.status_code == 200 and again.json()["paid_cents"] == 1000

def test_same_receipt_id_can_register_again_after_revoke() -> None:
    _make_order("c5b", 500)
    client.post("/orders/c5b/payments", json={"amount_cents": 200, "receipt_id": "rc-x"}, headers=H)
    assert client.post("/orders/c5b/receipts/rc-x/revoke", headers=H).status_code == 200
    resp = client.post("/orders/c5b/payments", json={"amount_cents": 300, "receipt_id": "rc-x"}, headers=H)
    assert resp.status_code == 200 and resp.json()["paid_cents"] == 300
    assert client.post("/orders/c5b/receipts/rc-x/revoke", headers=H).json()["paid_cents"] == 0
    got = client.get("/orders/c5b", headers=H).json()
    assert [(e["type"], e["amount_cents"]) for e in got["ledger"]] == [
        ("payment", 200),
        ("receipt_revocation", 200),
        ("payment", 300),
        ("receipt_revocation", 300),
    ]

def test_duplicate_revoke_is_idempotent() -> None:
    _make_order("c6", 500)
    client.post("/orders/c6/payments", json={"amount_cents": 200, "receipt_id": "rc-1"}, headers=H)
    first = client.post("/orders/c6/receipts/rc-1/revoke", headers=H)
    second = client.post("/orders/c6/receipts/rc-1/revoke", headers=H)
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["receipt_status"] == b["receipt_status"] == "revoked"
    assert a["paid_cents"] == b["paid_cents"] == 0
    assert a["created_at"] == b["created_at"]
    assert a["idempotent_replay"] is False and b["idempotent_replay"] is True
    got = client.get("/orders/c6", headers=H).json()
    # 只补一条撤销流水。
    assert [e["type"] for e in got["ledger"]] == ["payment", "receipt_revocation"]

def test_revoke_confirmed_receipt_is_conflict_and_unchanged() -> None:
    _make_order("c7", 500)
    client.post("/orders/c7/payments", json={"amount_cents": 500, "receipt_id": "rc-1"}, headers=H)
    client.post("/orders/c7/receipts/rc-1/confirm", headers=H)
    resp = client.post("/orders/c7/receipts/rc-1/revoke", headers=H)
    assert resp.status_code == 409
    got = client.get("/orders/c7", headers=H).json()
    assert got["paid_cents"] == 500 and got["status"] == "settled" and len(got["ledger"]) == 1

def test_revoke_unknown_receipt_is_not_found() -> None:
    _make_order("c7b", 100)
    assert client.post("/orders/c7b/receipts/nope/revoke", headers=H).status_code == 404

# --- 收款凭据：以凭据冲正 ----------------------------------------------------

def test_refund_pending_receipt_is_conflict_and_unchanged() -> None:
    _make_order("c8", 500)
    client.post("/orders/c8/payments", json={"amount_cents": 300, "receipt_id": "rc-1"}, headers=H)
    resp = client.post("/orders/c8/receipts/rc-1/refund", json={"refund_id": "rf-1", "amount_cents": 300}, headers=H)
    assert resp.status_code == 409
    got = client.get("/orders/c8", headers=H).json()
    assert got["paid_cents"] == 300 and len(got["ledger"]) == 1
    assert got["ledger"][0]["receipt_status"] == "pending"

def test_refund_confirmed_receipt_succeeds_and_marks_refunded() -> None:
    _make_order("c9", 500)
    client.post("/orders/c9/payments", json={"amount_cents": 300, "receipt_id": "rc-1"}, headers=H)
    assert client.post("/orders/c9/receipts/rc-1/confirm", headers=H).status_code == 200
    resp = client.post("/orders/c9/receipts/rc-1/refund", json={"refund_id": "rf-1", "amount_cents": 300}, headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["receipt_id"] == "rc-1" and body["receipt_status"] == "refunded"
    assert body["refund_id"] == "rf-1" and body["refunded_cents"] == 300
    assert body["paid_cents"] == 0 and body["outstanding_cents"] == 500 and body["status"] == "accepted"
    assert body["idempotent_replay"] is False
    got = client.get("/orders/c9", headers=H).json()
    assert got["ledger"][0]["receipt_status"] == "refunded"
    assert got["ledger"][1]["type"] == "refund" and got["ledger"][1]["refund_id"] == "rf-1"

def test_receipt_refund_replays_on_same_refund_id() -> None:
    _make_order("c9b", 500)
    client.post("/orders/c9b/payments", json={"amount_cents": 300, "receipt_id": "rc-1"}, headers=H)
    client.post("/orders/c9b/receipts/rc-1/confirm", headers=H)
    payload = {"refund_id": "rf-dup", "amount_cents": 300}
    first = client.post("/orders/c9b/receipts/rc-1/refund", json=payload, headers=H)
    second = client.post("/orders/c9b/receipts/rc-1/refund", json=payload, headers=H)
    assert first.status_code == second.status_code == 200
    assert first.json()["idempotent_replay"] is False and second.json()["idempotent_replay"] is True
    assert first.json()["created_at"] == second.json()["created_at"]
    got = client.get("/orders/c9b", headers=H).json()
    assert len([e for e in got["ledger"] if e["type"] == "refund"]) == 1

def test_receipt_refund_rules_409_and_unchanged() -> None:
    _make_order("c9c", 500)
    client.post("/orders/c9c/payments", json={"amount_cents": 300, "receipt_id": "rc-1"}, headers=H)
    client.post("/orders/c9c/receipts/rc-1/confirm", headers=H)
    # 金额必须与凭据金额一致。
    bad = client.post("/orders/c9c/receipts/rc-1/refund", json={"refund_id": "rf-x", "amount_cents": 200}, headers=H)
    assert bad.status_code == 409
    # 已冲正凭据不可再次冲正。
    client.post("/orders/c9c/receipts/rc-1/refund", json={"refund_id": "rf-1", "amount_cents": 300}, headers=H)
    again = client.post("/orders/c9c/receipts/rc-1/refund", json={"refund_id": "rf-2", "amount_cents": 300}, headers=H)
    assert again.status_code == 409
    got = client.get("/orders/c9c", headers=H).json()
    assert got["paid_cents"] == 0 and len([e for e in got["ledger"] if e["type"] == "refund"]) == 1

def test_receipt_refund_bad_params_are_400() -> None:
    _make_order("c9d", 500)
    client.post("/orders/c9d/payments", json={"amount_cents": 300, "receipt_id": "rc-1"}, headers=H)
    client.post("/orders/c9d/receipts/rc-1/confirm", headers=H)
    assert client.post("/orders/c9d/receipts/rc-1/refund", json={"amount_cents": 300}, headers=H).status_code == 400
    assert client.post("/orders/c9d/receipts/rc-1/refund", json={"refund_id": "rf", "amount_cents": 0}, headers=H).status_code == 400
    assert client.post("/orders/c9d/receipts/rc-1/refund", json={"refund_id": "rf", "amount_cents": "300"}, headers=H).status_code == 400

def test_refund_revoked_receipt_is_conflict() -> None:
    _make_order("c9e", 500)
    client.post("/orders/c9e/payments", json={"amount_cents": 200, "receipt_id": "rc-1"}, headers=H)
    client.post("/orders/c9e/receipts/rc-1/revoke", headers=H)
    resp = client.post("/orders/c9e/receipts/rc-1/refund", json={"refund_id": "rf-1", "amount_cents": 200}, headers=H)
    assert resp.status_code == 409
    assert client.get("/orders/c9e", headers=H).json()["paid_cents"] == 0

def test_receipt_refund_unknown_is_404() -> None:
    _make_order("c9f", 100)
    resp = client.post("/orders/c9f/receipts/nope/refund", json={"refund_id": "rf", "amount_cents": 1}, headers=H)
    assert resp.status_code == 404

# --- 收款凭据：并发守恒 ------------------------------------------------------

def test_concurrent_receipt_lifecycle_conserves() -> None:
    oid = "c10"
    amount = 2000
    _make_order(oid, amount)
    errors: list[Exception] = []
    start = threading.Barrier(12)

    def lifecycle(i: int) -> None:
        try:
            start.wait()
            rid = f"rc-{i}"
            r = store.add_payment("t1", oid, 100, receipt_id=rid)
            if r is None:
                return
            # 一半确认后冲正，一半撤销；全部为合法路径。
            if i % 2 == 0:
                store.confirm_receipt("t1", oid, rid)
                store.refund_receipt("t1", oid, rid, f"rf-{i}", 100)
            else:
                store.revoke_receipt("t1", oid, rid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=lifecycle, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    order = store.get("t1", oid)
    assert order is not None
    effective_pay = sum(
        e["amount_cents"] for e in order["ledger"]
        if e["type"] == "payment" and e.get("receipt_status") != "revoked"
    )
    plain_pay = sum(e["amount_cents"] for e in order["ledger"] if e["type"] == "payment" and "receipt_id" not in e)
    refunds = sum(e["amount_cents"] for e in order["ledger"] if e["type"] == "refund")
    # paid = 生效收款（无凭据收款 + 未撤销凭据收款）− 生效冲正；撤销流水本身是减法留痕。
    assert order["paid_cents"] == plain_pay + effective_pay - refunds
    assert 0 <= order["paid_cents"] <= amount

def test_concurrent_duplicate_confirms_apply_once() -> None:
    oid = "c11"
    _make_order(oid, 500)
    store.add_payment("t1", oid, 500, receipt_id="rc-same")
    results: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def call() -> None:
        start.wait()
        r = store.confirm_receipt("t1", oid, "rc-same")
        with lock:
            results.append(bool(r and not r["idempotent_replay"]))

    threads = [threading.Thread(target=call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and results.count(False) == 9
    got = store.get("t1", oid)
    assert got["status"] == "settled" and got["paid_cents"] == 500 and len(got["ledger"]) == 1

# --- 收款凭据：跨租户不可见 --------------------------------------------------

def test_cross_tenant_receipt_is_not_found() -> None:
    _make_order("c12", 100, tenant="t1")
    client.post("/orders/c12/payments", json={"amount_cents": 100, "receipt_id": "rc-1"}, headers={"X-Tenant": "t1"})
    h2 = {"X-Tenant": "t2"}
    assert client.post("/orders/c12/receipts/rc-1/confirm", headers=h2).status_code == 404
    assert client.post("/orders/c12/receipts/rc-1/revoke", headers=h2).status_code == 404
    assert client.post("/orders/c12/receipts/rc-1/refund", json={"refund_id": "rf", "amount_cents": 100}, headers=h2).status_code == 404
    got = client.get("/orders/c12", headers={"X-Tenant": "t1"}).json()
    # 跨租户操作不留痕、不改余额。
    assert got["paid_cents"] == 100 and got["ledger"][0]["receipt_status"] == "pending"

def test_receipt_id_scope_is_per_tenant_order() -> None:
    _make_order("c13", 100, tenant="t1")
    _make_order("c13", 100, tenant="t2")
    p1 = client.post("/orders/c13/payments", json={"amount_cents": 100, "receipt_id": "shared-rc"}, headers={"X-Tenant": "t1"})
    p2 = client.post("/orders/c13/payments", json={"amount_cents": 100, "receipt_id": "shared-rc"}, headers={"X-Tenant": "t2"})
    assert p1.status_code == p2.status_code == 200
    assert client.post("/orders/c13/receipts/shared-rc/confirm", headers={"X-Tenant": "t1"}).json()["status"] == "settled"
    # t1 的确认不影响 t2：仍为待核销、未结清。
    got2 = client.get("/orders/c13", headers={"X-Tenant": "t2"}).json()
    assert got2["status"] == "accepted" and got2["ledger"][0]["receipt_status"] == "pending"
    assert client.post("/orders/c13/receipts/shared-rc/revoke", headers={"X-Tenant": "t2"}).json()["paid_cents"] == 0

def test_revoke_refused_when_pending_money_consumed_by_refund() -> None:
    # 待核销资金可被普通冲正占用；此时整笔撤销会使余额为负，409 且数据不变。
    _make_order("c14", 500)
    client.post("/orders/c14/payments", json={"amount_cents": 300, "receipt_id": "rc-1"}, headers=H)
    assert client.post("/orders/c14/refunds", json={"refund_id": "rf-1", "amount_cents": 300}, headers=H).status_code == 200
    resp = client.post("/orders/c14/receipts/rc-1/revoke", headers=H)
    assert resp.status_code == 409
    got = client.get("/orders/c14", headers=H).json()
    assert got["paid_cents"] == 0 and len(got["ledger"]) == 2
    assert got["ledger"][0]["receipt_status"] == "pending"

def test_receipt_refund_id_cannot_cross_receipts_or_plain_refunds() -> None:
    _make_order("c15", 1000)
    client.post("/orders/c15/payments", json={"amount_cents": 200, "receipt_id": "rc-a"}, headers=H)
    client.post("/orders/c15/payments", json={"amount_cents": 200, "receipt_id": "rc-b"}, headers=H)
    client.post("/orders/c15/receipts/rc-a/confirm", headers=H)
    client.post("/orders/c15/receipts/rc-b/confirm", headers=H)
    # 同一冲正标识先在凭据 a 上生效。
    assert client.post("/orders/c15/receipts/rc-a/refund", json={"refund_id": "rf-shared", "amount_cents": 200}, headers=H).status_code == 200
    # 换凭据复用同一冲正标识：409 且不冲正 b。
    cross = client.post("/orders/c15/receipts/rc-b/refund", json={"refund_id": "rf-shared", "amount_cents": 200}, headers=H)
    assert cross.status_code == 409
    got = client.get("/orders/c15", headers=H).json()
    assert got["paid_cents"] == 200 and len([e for e in got["ledger"] if e["type"] == "refund"]) == 1
    # 普通冲正复用同一冲正标识且同金额：按既有冲正规则幂等回放首次结果，不重复减少余额。
    plain = client.post("/orders/c15/refunds", json={"refund_id": "rf-shared", "amount_cents": 200}, headers=H)
    assert plain.status_code == 200 and plain.json()["idempotent_replay"] is True
    assert client.get("/orders/c15", headers=H).json()["paid_cents"] == 200
