"""幂等能力测试：重放、冲突、跨租户隔离、并发唯一生效、重启持久化。"""
import importlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from uvicorn import Config, Server

_DB_DIR = tempfile.mkdtemp()
os.environ["APP_DB"] = os.path.join(_DB_DIR, "idempotency.sqlite")

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

# 并发测试使用真实 uvicorn 进程内服务 + 真实 HTTP，避免 TestClient 的
# portal 线程把请求串行化，从而真正检验写锁下的并发唯一生效语义。
with socket.socket() as _sock:
    _sock.bind(("127.0.0.1", 0))
    _SERVER_PORT = _sock.getsockname()[1]
_BASE_URL = f"http://127.0.0.1:{_SERVER_PORT}"


@pytest.fixture(scope="session")
def http_server():
    server = Server(Config(app=app, host="127.0.0.1", port=_SERVER_PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            httpx.get(f"{_BASE_URL}/health", timeout=1)
            break
        except httpx.TransportError:
            time.sleep(0.1)
    yield _BASE_URL
    server.should_exit = True


H_T1 = {"X-Tenant": "t1"}


def _direct_order(tenant: str, order_id: str) -> sqlite3.Row | None:
    conn = sqlite3.connect(db_path())
    try:
        return conn.execute(
            "SELECT amount_cents, paid_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()


def test_create_order_replays_same_result() -> None:
    body = {"tenant": "t1", "order_id": "i1", "amount_cents": 500, "currency": "CNY"}
    first = client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-1"})
    assert first.status_code == 201
    assert "Idempotent-Replayed" not in first.headers

    second = client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-1"})
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert second.json() == first.json()

    row = _direct_order("t1", "i1")
    assert row is not None and row[0] == 500  # 只有一笔受理，金额未变


def test_payment_does_not_double_accumulate() -> None:
    body = {"tenant": "t1", "order_id": "i2", "amount_cents": 500, "currency": "CNY"}
    client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-2"})

    headers = {**H_T1, "Idempotency-Key": "key-pay-2"}
    first = client.post("/orders/i2/payments", json={"amount_cents": 200}, headers=headers)
    second = client.post("/orders/i2/payments", json={"amount_cents": 200}, headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    assert second.json()["paid_cents"] == 200
    assert second.json()["outstanding_cents"] == 300

    row = _direct_order("t1", "i2")
    assert row[1] == 200  # 已收金额没有被重复累加


def test_replay_returns_first_result_even_after_later_changes() -> None:
    body = {"tenant": "t1", "order_id": "i3", "amount_cents": 500, "currency": "CNY"}
    client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-3"})
    first = client.post("/orders/i3/payments", json={"amount_cents": 100},
                        headers={**H_T1, "Idempotency-Key": "key-pay-3"})
    # 之后另一笔收款改变了订单状态
    client.post("/orders/i3/payments", json={"amount_cents": 400},
                headers={**H_T1, "Idempotency-Key": "key-pay-3b"})
    replay = client.post("/orders/i3/payments", json={"amount_cents": 100},
                         headers={**H_T1, "Idempotency-Key": "key-pay-3"})
    assert replay.json() == first.json()
    assert replay.json()["paid_cents"] == 100  # 返回首次执行时的快照


def test_conflicting_amount_is_refused_and_changes_nothing() -> None:
    body = {"tenant": "t1", "order_id": "i4", "amount_cents": 500, "currency": "CNY"}
    first = client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-4"})
    assert first.status_code == 201

    changed = {**body, "amount_cents": 999}
    conflict = client.post("/orders", json=changed, headers={**H_T1, "Idempotency-Key": "key-4"})
    assert conflict.status_code == 409

    # 重复提交冲突请求，结论一致
    assert client.post("/orders", json=changed,
                       headers={**H_T1, "Idempotency-Key": "key-4"}).status_code == 409
    # 原始订单未被修改，原请求仍可重放
    assert client.post("/orders", json=body,
                       headers={**H_T1, "Idempotency-Key": "key-4"}).json() == first.json()
    assert _direct_order("t1", "i4")[0] == 500


def test_conflicting_currency_is_refused() -> None:
    body = {"tenant": "t1", "order_id": "i5", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-5"})
    changed = {**body, "currency": "USD"}
    assert client.post("/orders", json=changed,
                       headers={**H_T1, "Idempotency-Key": "key-5"}).status_code == 409
    assert _direct_order("t1", "i5")[0] == 100


def test_same_key_across_endpoints_is_refused() -> None:
    body = {"tenant": "t1", "order_id": "i6", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-6"})
    # 同一请求标识被用于“登记收款”这一不同业务
    resp = client.post("/orders/i6/payments", json={"amount_cents": 100},
                       headers={**H_T1, "Idempotency-Key": "key-6"})
    assert resp.status_code == 409
    assert _direct_order("t1", "i6")[1] == 0  # 没有产生收款


def test_conflicting_payment_amount_is_refused() -> None:
    body = {"tenant": "t1", "order_id": "i7", "amount_cents": 500, "currency": "CNY"}
    client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-7"})
    headers = {**H_T1, "Idempotency-Key": "key-pay-7"}
    assert client.post("/orders/i7/payments", json={"amount_cents": 200},
                       headers=headers).status_code == 200
    assert client.post("/orders/i7/payments", json={"amount_cents": 300},
                       headers=headers).status_code == 409
    assert _direct_order("t1", "i7")[1] == 200  # 冲突收款未累加


def test_same_key_is_independent_across_tenants() -> None:
    body1 = {"tenant": "t1", "order_id": "i8", "amount_cents": 100, "currency": "CNY"}
    body2 = {"tenant": "t2", "order_id": "i8", "amount_cents": 200, "currency": "USD"}
    r1 = client.post("/orders", json=body1, headers={**H_T1, "Idempotency-Key": "shared-key"})
    r2 = client.post("/orders", json=body2, headers={"X-Tenant": "t2",
                                                     "Idempotency-Key": "shared-key"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert _direct_order("t1", "i8") is not None
    assert _direct_order("t2", "i8") is not None
    # 各自租户内的重放仍生效
    again = client.post("/orders", json=body2,
                        headers={"X-Tenant": "t2", "Idempotency-Key": "shared-key"})
    assert again.status_code == 201 and again.headers.get("Idempotent-Replayed") == "true"


def test_concurrent_create_order_single_effect(http_server) -> None:
    body = {"tenant": "t1", "order_id": "i9", "amount_cents": 700, "currency": "CNY"}
    headers = {**H_T1, "Idempotency-Key": "key-9"}
    barrier = threading.Barrier(10)

    def call(_: int) -> tuple[int, dict, str | None]:
        barrier.wait()
        r = httpx.post(f"{http_server}/orders", json=body, headers=headers, timeout=10)
        return r.status_code, r.json(), r.headers.get("Idempotent-Replayed")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(call, range(10)))
    assert all(code == 201 for code, _, _ in results)
    assert len({json.dumps(b, sort_keys=True) for _, b, _ in results}) == 1
    assert sum(1 for _, _, replayed in results if replayed == "true") == 9
    assert _direct_order("t1", "i9")[0] == 700  # 只有一笔订单


def test_concurrent_payment_single_effect(http_server) -> None:
    body = {"tenant": "t1", "order_id": "i10", "amount_cents": 500, "currency": "CNY"}
    client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-10"})
    headers = {**H_T1, "Idempotency-Key": "key-pay-10"}
    barrier = threading.Barrier(10)

    def call(_: int) -> int:
        barrier.wait()
        return httpx.post(f"{http_server}/orders/i10/payments", json={"amount_cents": 500},
                          headers=headers, timeout=10).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(call, range(10)))
    assert codes == [200] * 10
    row = _direct_order("t1", "i10")
    assert row[1] == 500 and row[2] == "settled"  # 只收了一次


def test_decisions_survive_restart() -> None:
    body = {"tenant": "t1", "order_id": "i11", "amount_cents": 500, "currency": "CNY"}
    client.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-11"})
    client.post("/orders/i11/payments", json={"amount_cents": 100},
                headers={**H_T1, "Idempotency-Key": "key-pay-11"})
    changed = {**body, "amount_cents": 999}
    assert client.post("/orders", json=changed,
                       headers={**H_T1, "Idempotency-Key": "key-11"}).status_code == 409

    # 模拟进程重启：卸载全部 app.* 模块后重新导入，连接全部新建，数据库文件不变。
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    reloaded = importlib.import_module("app.entry")
    client2 = TestClient(reloaded.app)

    replay = client2.post("/orders", json=body, headers={**H_T1, "Idempotency-Key": "key-11"})
    assert replay.status_code == 201 and replay.headers.get("Idempotent-Replayed") == "true"

    pay_replay = client2.post("/orders/i11/payments", json={"amount_cents": 100},
                              headers={**H_T1, "Idempotency-Key": "key-pay-11"})
    assert pay_replay.status_code == 200
    assert pay_replay.json()["paid_cents"] == 100  # 没有重复收款

    # 曾被拒绝的冲突请求，重启后结论一致，且不会被误判为新请求
    assert client2.post("/orders", json=changed,
                        headers={**H_T1, "Idempotency-Key": "key-11"}).status_code == 409
    assert _direct_order("t1", "i11")[:2] == (500, 100)


def test_requests_without_key_keep_legacy_behavior() -> None:
    body = {"tenant": "t1", "order_id": "i12", "amount_cents": 300, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    assert client.post("/orders", json=body).status_code == 409  # 同租户重复受理
    assert client.post("/orders/i12/payments", json={"amount_cents": 400},
                       headers=H_T1).status_code == 409  # 超过未收金额
    assert client.get("/orders/i12", headers={"X-Tenant": "t2"}).status_code == 404
    assert _direct_order("t1", "i12")[1] == 0
