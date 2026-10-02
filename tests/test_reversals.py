import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_tmp_dir = tempfile.mkdtemp()
DB_PATH = os.path.join(_tmp_dir, "reversals.sqlite")
os.environ.setdefault("APP_DB", DB_PATH)

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

TENANT = {"X-Tenant": "tr"}


def _order(order_id: str, amount: int, key: str | None = None) -> None:
    headers = {"Idempotency-Key": key} if key else {}
    resp = client.post(
        "/orders",
        json={"tenant": "tr", "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
        headers=headers,
    )
    assert resp.status_code == 201


def _pay(order_id: str, amount: int, key: str | None = None):
    headers = dict(TENANT)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers=headers)


def _reverse(order_id: str, amount, key: str | None = None, tenant: str = "tr"):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/orders/{order_id}/reversals", json={"amount_cents": amount}, headers=headers)


def _get(order_id: str, tenant: str = "tr") -> dict:
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant}).json()


def _reversals(order_id: str, tenant: str = "tr"):
    return client.get(f"/orders/{order_id}/reversals", headers={"X-Tenant": tenant})


def test_reversal_reduces_paid_and_restores_outstanding() -> None:
    _order("r-1", 1000)
    assert _pay("r-1", 600).status_code == 200

    resp = _reverse("r-1", 250)
    assert resp.status_code == 200
    body = resp.json()
    assert body["paid_cents"] == 350
    assert body["outstanding_cents"] == 650
    assert body["status"] == "accepted"
    assert body["paid_cents"] + body["outstanding_cents"] == body["amount_cents"]

    reversal = body["reversal"]
    assert reversal["amount_cents"] == 250
    assert reversal["paid_after_cents"] == 350
    assert reversal["outstanding_after_cents"] == 650


def test_reversal_records_are_listed_per_order_in_order() -> None:
    _order("r-2", 900)
    _pay("r-2", 900)
    assert _get("r-2")["status"] == "settled"

    _reverse("r-2", 300)
    _reverse("r-2", 100)

    resp = _reversals("r-2")
    assert resp.status_code == 200
    records = resp.json()["reversals"]
    assert [r["reversal_seq"] for r in records] == [1, 2]
    assert [r["amount_cents"] for r in records] == [300, 100]
    assert [r["paid_after_cents"] for r in records] == [600, 500]
    assert [r["outstanding_after_cents"] for r in records] == [300, 400]
    # 留痕合计与订单当前金额对得上：已收 = 收款总额 − 冲正总额
    assert _get("r-2")["paid_cents"] == 900 - 300 - 100


def test_pay_reverse_pay_again_conserves_and_settles() -> None:
    _order("r-3", 500)
    _pay("r-3", 500)
    assert _get("r-3")["status"] == "settled"

    _reverse("r-3", 200)
    got = _get("r-3")
    assert got["status"] == "accepted" and got["outstanding_cents"] == 200

    # 冲正后仍有未收金额，可以继续收款并重新结清
    resp = _pay("r-3", 200)
    assert resp.status_code == 200
    got = _get("r-3")
    assert got["status"] == "settled"
    assert got["paid_cents"] == 500 and got["outstanding_cents"] == 0


def test_reversal_to_zero_is_allowed() -> None:
    _order("r-4", 400)
    _pay("r-4", 400)
    resp = _reverse("r-4", 400)
    assert resp.status_code == 200
    got = _get("r-4")
    assert got["paid_cents"] == 0 and got["outstanding_cents"] == 400
    assert got["status"] == "accepted"


def test_invalid_reversal_amounts_rejected_without_change() -> None:
    _order("r-5", 300)
    _pay("r-5", 200)

    # 小于或等于零：参数校验拒绝（422，与订单不存在/业务冲突可区分）
    assert _reverse("r-5", 0).status_code == 422
    assert _reverse("r-5", -10).status_code == 422
    # 超过当前已收金额：业务冲突 409
    assert _reverse("r-5", 201).status_code == 409
    # 订单不存在 / 跨租户：404
    assert _reverse("r-missing", 1).status_code == 404
    assert _reverse("r-5", 1, tenant="other").status_code == 404

    # 所有拒绝都没有改变金额、状态与留痕
    got = _get("r-5")
    assert got["paid_cents"] == 200 and got["outstanding_cents"] == 100
    assert _reversals("r-5").json()["reversals"] == []
    assert _reversals("r-5", tenant="other").status_code == 404


def test_reversal_idempotent_replay_does_not_double_apply() -> None:
    _order("r-6", 800)
    _pay("r-6", 500)

    first = _reverse("r-6", 200, key="rev-1")
    assert first.status_code == 200 and first.json()["paid_cents"] == 300

    second = _reverse("r-6", 200, key="rev-1")
    assert second.status_code == 200
    assert second.json() == first.json()
    assert _get("r-6")["paid_cents"] == 300  # 没有重复冲减
    assert len(_reversals("r-6").json()["reversals"]) == 1


def test_same_key_different_content_rejected() -> None:
    _order("r-7", 800)
    _order("r-8", 800)
    _pay("r-7", 500)
    _pay("r-8", 500)

    assert _reverse("r-7", 100, key="rev-2").status_code == 200
    # 同标识不同金额
    assert _reverse("r-7", 150, key="rev-2").status_code == 422
    # 同标识不同订单
    assert _reverse("r-8", 100, key="rev-2").status_code == 422
    # 同标识不同操作类型（收款）
    pay = client.post(
        "/orders/r-7/payments",
        json={"amount_cents": 100},
        headers={"X-Tenant": "tr", "Idempotency-Key": "rev-2"},
    )
    assert pay.status_code == 422

    assert _get("r-7")["paid_cents"] == 400
    assert _get("r-8")["paid_cents"] == 500
    assert len(_reversals("r-7").json()["reversals"]) == 1
    assert _reversals("r-8").json()["reversals"] == []


def test_rejected_reversal_result_is_replayed() -> None:
    _order("r-9", 300)
    _pay("r-9", 100)
    first = _reverse("r-9", 200, key="rev-3")
    assert first.status_code == 409
    second = _reverse("r-9", 200, key="rev-3")
    assert second.status_code == 409
    assert _get("r-9")["paid_cents"] == 100
    assert _reversals("r-9").json()["reversals"] == []


def test_concurrent_same_key_reverses_once() -> None:
    _order("r-10", 1000)
    _pay("r-10", 700)

    def submit(_: int):
        return TestClient(app).post(
            "/orders/r-10/reversals",
            json={"amount_cents": 300},
            headers={"X-Tenant": "tr", "Idempotency-Key": "rev-conc"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.text for r in responses}) == 1
    assert _get("r-10")["paid_cents"] == 400
    assert len(_reversals("r-10").json()["reversals"]) == 1


def test_concurrent_payments_and_reversals_stay_consistent() -> None:
    _order("r-11", 2000)
    _pay("r-11", 1000)

    def submit(i: int):
        c = TestClient(app)
        if i % 2 == 0:
            return c.post(
                "/orders/r-11/payments",
                json={"amount_cents": 100},
                headers={"X-Tenant": "tr", "Idempotency-Key": f"mix-pay-{i}"},
            )
        return c.post(
            "/orders/r-11/reversals",
            json={"amount_cents": 50},
            headers={"X-Tenant": "tr", "Idempotency-Key": f"mix-rev-{i}"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)

    got = _get("r-11")
    # 4 笔收款 ×100、4 笔冲正 ×50，各生效一次
    assert got["paid_cents"] == 1000 + 4 * 100 - 4 * 50
    assert got["paid_cents"] >= 0
    assert got["paid_cents"] + got["outstanding_cents"] == got["amount_cents"]
    records = _reversals("r-11").json()["reversals"]
    assert len(records) == 4
    # 留痕金额与订单金额守恒：paid_after + outstanding_after == 订单金额
    assert all(r["paid_after_cents"] + r["outstanding_after_cents"] == 2000 for r in records)


def test_reversals_survive_restart() -> None:
    _order("r-12", 600, key="rev-restart-order")
    _pay("r-12", 600, )
    assert _reverse("r-12", 250, key="rev-restart").status_code == 200

    # 模拟重启：丢弃进程内状态，重新迁移并建新客户端
    migrate()
    client2 = TestClient(app)

    repeat = client2.post(
        "/orders/r-12/reversals",
        json={"amount_cents": 250},
        headers={"X-Tenant": "tr", "Idempotency-Key": "rev-restart"},
    )
    assert repeat.status_code == 200
    assert repeat.json()["paid_cents"] == 350  # 回放首次结果，不重复冲减

    got = client2.get("/orders/r-12", headers=TENANT).json()
    assert got["paid_cents"] == 350 and got["outstanding_cents"] == 250
    records = client2.get("/orders/r-12/reversals", headers=TENANT).json()["reversals"]
    assert len(records) == 1 and records[0]["amount_cents"] == 250
