import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("APP_DB", os.path.join(_tmp_dir, "corrections.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import MIGRATIONS_DIR, connect, migrate

migrate()
client = TestClient(app)

TENANT = "tcor"
OTHER = "tcor-other"

HAS_FACTS = "order already has payment, reversal or reconciliation facts and cannot be corrected"
TARGET_EXISTS = "correction target order id already exists"
AMOUNT_INVALID = "correction amount must be greater than zero"
ORDER_ID_EMPTY = "correction order id must not be empty"


def _accept(order_id: str, amount: int = 1000, currency: str = "CNY", tenant: str = TENANT,
            key: str | None = None) -> None:
    headers = {"Idempotency-Key": key} if key else {}
    resp = client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": currency},
        headers=headers,
    )
    assert resp.status_code in (201, 409)


def _correct(order_id: str, new_id: str, amount: int, currency: str, key: str | None = None,
             tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/orders/{order_id}/corrections",
        json={"order_id": new_id, "amount_cents": amount, "currency": currency},
        headers=headers,
    )


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
    return client.post(
        f"/orders/{order_id}/reconciliations", json={"amount_cents": amount}, headers=headers
    )


def _get(order_id: str, tenant: str = TENANT):
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant})


def _corrections(order_id: str, tenant: str = TENANT):
    return client.get(f"/orders/{order_id}/corrections", headers={"X-Tenant": tenant})


def test_correction_changes_id_amount_currency_and_reads_by_new_id() -> None:
    _accept("c1", 1000, "CNY")
    resp = _correct("c1", "c1-new", 800, "USD")
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == "c1-new"
    assert body["amount_cents"] == 800
    assert body["currency"] == "USD"
    assert body["paid_cents"] == 0 and body["reconciled_cents"] == 0
    # 守恒：已收 + 未收 = 更正后的订单金额
    assert body["paid_cents"] + body["outstanding_cents"] == 800
    assert body["status"] == "accepted"

    # 成功后按新标识可读，旧标识已不存在
    assert _get("c1-new").status_code == 200
    assert _get("c1").status_code == 404


def test_correction_can_keep_same_id() -> None:
    _accept("c2", 500, "CNY")
    resp = _correct("c2", "c2", 640, "EUR")
    assert resp.status_code == 200
    assert resp.json()["order_id"] == "c2"
    got = _get("c2").json()
    assert got["amount_cents"] == 640 and got["currency"] == "EUR"
    assert got["outstanding_cents"] == 640


def test_correction_blocked_once_payment_exists() -> None:
    _accept("c3", 500)
    assert _pay("c3", 200, key="c3-pay").status_code == 200

    resp = _correct("c3", "c3-x", 500, "CNY")
    assert resp.status_code == 409
    assert resp.json()["detail"] == HAS_FACTS

    got = _get("c3").json()
    assert got["amount_cents"] == 500 and got["paid_cents"] == 200
    assert _get("c3-x").status_code == 404


def test_correction_blocked_once_reconciliation_exists() -> None:
    _accept("c4", 500)
    _pay("c4", 300, key="c4-pay")
    assert _reconcile("c4", 300, key="c4-rec").status_code == 200
    # 全额冲正到已收下限 300 不可行，这里只验证有核销事实即拒绝
    resp = _correct("c4", "c4-x", 500, "CNY")
    assert resp.status_code == 409 and resp.json()["detail"] == HAS_FACTS
    assert _get("c4").json()["reconciled_cents"] == 300


def test_correction_blocked_by_traces_even_when_paid_back_to_zero() -> None:
    # 收款后全额冲正：paid_cents 归零，但收款/冲正留痕仍在，仍须拒绝
    _accept("c5", 500)
    _pay("c5", 200, key="c5-pay")
    _reverse("c5", 200, key="c5-rev")
    got = _get("c5").json()
    assert got["paid_cents"] == 0 and got["outstanding_cents"] == 500

    resp = _correct("c5", "c5-x", 500, "CNY")
    assert resp.status_code == 409 and resp.json()["detail"] == HAS_FACTS
    assert _get("c5").json()["amount_cents"] == 500


def test_invalid_correction_inputs_return_409_with_distinct_reasons() -> None:
    _accept("c6", 500, "CNY")

    zero = _correct("c6", "c6", 0, "CNY")
    assert zero.status_code == 409 and zero.json()["detail"] == AMOUNT_INVALID
    negative = _correct("c6", "c6", -10, "CNY")
    assert negative.status_code == 409 and negative.json()["detail"] == AMOUNT_INVALID
    empty_id = _correct("c6", "", 500, "CNY")
    assert empty_id.status_code == 409 and empty_id.json()["detail"] == ORDER_ID_EMPTY
    bad_ccy = _correct("c6", "c6", 500, "XYZ")
    assert bad_ccy.status_code == 409 and bad_ccy.json()["detail"] == "unsupported currency: XYZ"

    got = _get("c6").json()
    assert got["amount_cents"] == 500 and got["currency"] == "CNY"


def test_correction_missing_or_cross_tenant_is_404() -> None:
    assert _correct("c-missing", "c-x", 100, "CNY").status_code == 404

    _accept("c7", 500)
    # 跨租户更正：按不存在处理，不留痕、不改数据
    assert _correct("c7", "c7-x", 500, "CNY", tenant=OTHER).status_code == 404
    assert _corrections("c7", tenant=OTHER).status_code == 404
    assert _get("c7").json()["amount_cents"] == 500


def test_target_order_id_conflict_within_tenant_rejected() -> None:
    _accept("c8", 500, "CNY")
    _accept("c8-taken", 700, "USD")

    resp = _correct("c8", "c8-taken", 500, "CNY")
    assert resp.status_code == 409 and resp.json()["detail"] == TARGET_EXISTS

    # 原订单与目标订单均保持不变
    assert _get("c8").json()["amount_cents"] == 500
    assert _get("c8-taken").json()["amount_cents"] == 700


def test_same_target_id_in_other_tenant_is_not_a_conflict() -> None:
    _accept("c9", 500, "CNY", tenant=TENANT)
    _accept("shared-name", 900, "EUR", tenant=OTHER)
    # 本租户内无 shared-name，跨租户同名不构成冲突
    resp = _correct("c9", "shared-name", 500, "CNY")
    assert resp.status_code == 200
    assert _get("shared-name", tenant=TENANT).json()["amount_cents"] == 500
    # 另一租户的同名订单不受影响
    assert _get("shared-name", tenant=OTHER).json()["amount_cents"] == 900


def test_correction_ledger_records_applied_and_rejected_in_order() -> None:
    _accept("c10", 1000, "CNY")

    # 一次被拒绝的更正（非法金额）也要留痕
    bad = _correct("c10", "c10", 0, "CNY", key="c10-bad")
    assert bad.status_code == 409
    # 一次生效的更正（含改名）
    ok = _correct("c10", "c10-v2", 1200, "USD", key="c10-ok")
    assert ok.status_code == 200

    records = _corrections("c10-v2").json()["corrections"]
    assert [r["result"] for r in records] == ["rejected", "applied"]

    rejected, applied = records
    assert rejected["before_order_id"] == "c10"
    assert rejected["before_amount_cents"] == 1000 and rejected["before_currency"] == "CNY"
    assert rejected["after_order_id"] == "c10" and rejected["after_amount_cents"] == 0
    assert rejected["after_currency"] == "CNY"
    assert rejected["reject_reason"] == AMOUNT_INVALID
    assert rejected["request_id"] == "c10-bad"

    assert applied["before_order_id"] == "c10" and applied["after_order_id"] == "c10-v2"
    assert applied["before_amount_cents"] == 1000 and applied["after_amount_cents"] == 1200
    assert applied["before_currency"] == "CNY" and applied["after_currency"] == "USD"
    assert applied["reject_reason"] is None and applied["request_id"] == "c10-ok"


def test_idempotent_correction_applies_once_and_replays() -> None:
    _accept("c11", 500, "CNY")
    first = _correct("c11", "c11", 900, "EUR", key="cor-11-1")
    assert first.status_code == 200 and first.json()["amount_cents"] == 900

    replay = _correct("c11", "c11", 900, "EUR", key="cor-11-1")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    got = _get("c11").json()
    assert got["amount_cents"] == 900 and got["currency"] == "EUR"
    assert len(_corrections("c11").json()["corrections"]) == 1


def test_replay_after_rename_returns_first_order_body() -> None:
    _accept("c12", 500)
    first = _correct("c12", "c12-renamed", 500, "CNY", key="cor-12-1")
    assert first.status_code == 200
    # 旧标识已不存在，但同标识重复提交仍回放首次响应（改名后的订单）
    replay = _correct("c12", "c12-renamed", 500, "CNY", key="cor-12-1")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(_corrections("c12-renamed").json()["corrections"]) == 1


def test_first_rejected_correction_replays_same_conclusion() -> None:
    _accept("c13", 500)
    _pay("c13", 100, key="c13-pay")
    first = _correct("c13", "c13", 500, "CNY", key="cor-13-bad")
    assert first.status_code == 409 and first.json()["detail"] == HAS_FACTS

    replay = _correct("c13", "c13", 500, "CNY", key="cor-13-bad")
    assert replay.status_code == 409 and replay.json() == first.json()
    # rejected 结论只固化一次，不重复留痕
    records = _corrections("c13").json()["corrections"]
    assert len(records) == 1 and records[0]["result"] == "rejected"
    assert _get("c13").json()["paid_cents"] == 100


def test_correction_key_conflicts_return_422_without_change() -> None:
    _accept("c14", 500)
    assert _correct("c14", "c14", 600, "CNY", key="cor-14-x").status_code == 200

    # 同标识但更正后金额不同
    assert _correct("c14", "c14", 700, "CNY", key="cor-14-x").status_code == 422
    # 同标识但更正后币种不同
    assert _correct("c14", "c14", 600, "USD", key="cor-14-x").status_code == 422
    # 同标识但更正后订单标识不同
    assert _correct("c14", "c14-z", 600, "CNY", key="cor-14-x").status_code == 422
    # 同标识用于另一张目标订单
    _accept("c14b", 500)
    assert _correct("c14b", "c14b", 600, "CNY", key="cor-14-x").status_code == 422
    # 同标识用于不同操作类型（收款）
    assert _pay("c14", 1, key="cor-14-x").status_code == 422

    assert _get("c14").json()["amount_cents"] == 600
    assert _get("c14-z").status_code == 404


def test_correction_scope_conflicts_with_order_acceptance_key() -> None:
    _accept("c15", 500, key="cor-15-shared")
    # 同一请求标识先用于受理，再用于更正 → 422
    conflict = _correct("c15", "c15", 500, "CNY", key="cor-15-shared")
    assert conflict.status_code == 422


def test_same_correction_key_across_tenants_is_independent() -> None:
    _accept("c16", 500, tenant=TENANT)
    _accept("c16", 300, tenant=OTHER)
    r1 = _correct("c16", "c16", 500, "CNY", key="cor-16-shared")
    r2 = _correct("c16", "c16", 300, "CNY", key="cor-16-shared", tenant=OTHER)
    assert r1.status_code == 200 and r2.status_code == 200
    assert _get("c16", tenant=TENANT).json()["amount_cents"] == 500
    assert _get("c16", tenant=OTHER).json()["amount_cents"] == 300


def test_concurrent_same_correction_key_applies_once() -> None:
    _accept("c17", 1000)
    headers = {"X-Tenant": TENANT, "Idempotency-Key": "cor-17-conc"}
    payload = {"order_id": "c17", "amount_cents": 400, "currency": "CNY"}

    def submit(_: int):
        return TestClient(app).post("/orders/c17/corrections", json=payload, headers=headers)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["amount_cents"] == 400 for r in responses)
    assert len(_corrections("c17").json()["corrections"]) == 1


def test_history_follows_rename_and_does_not_leak_to_reused_id() -> None:
    # c18 先改名，旧标识被释放后由一张全新订单复用
    _accept("c18", 500)
    assert _correct("c18", "c18-moved", 500, "CNY", key="cor-18-1").status_code == 200
    _accept("c18", 999)  # 复用已释放的旧标识

    # 改名后的订单按新标识读到自己的更正历史
    moved = _corrections("c18-moved").json()["corrections"]
    assert len(moved) == 1 and moved[0]["after_order_id"] == "c18-moved"
    # 复用旧标识的新订单读不到前者的留痕
    assert _corrections("c18").json()["corrections"] == []
    assert _get("c18").json()["amount_cents"] == 999


def test_corrections_survive_restart() -> None:
    _accept("c19", 500)
    assert _correct("c19", "c19-restart", 700, "USD", key="cor-19-1").status_code == 200

    client2 = TestClient(app)
    got = client2.get("/orders/c19-restart", headers={"X-Tenant": TENANT})
    assert got.status_code == 200
    assert got.json()["amount_cents"] == 700 and got.json()["currency"] == "USD"
    assert client2.get("/orders/c19", headers={"X-Tenant": TENANT}).status_code == 404

    # 重启后重复更正：回放首次结果，不再次更正
    replay = client2.post(
        "/orders/c19/corrections",
        json={"order_id": "c19-restart", "amount_cents": 700, "currency": "USD"},
        headers={"X-Tenant": TENANT, "Idempotency-Key": "cor-19-1"},
    )
    assert replay.status_code == 200 and replay.json()["amount_cents"] == 700

    records = client2.get(
        "/orders/c19-restart/corrections", headers={"X-Tenant": TENANT}
    ).json()["corrections"]
    assert len(records) == 1 and records[0]["result"] == "applied"


def test_migration_005_is_versioned_and_runs_once() -> None:
    # 只含 001/002 的旧库升级到 005：补 reconciled_cents、ledger_id 并建更正留痕表，
    # 重复迁移不报错（沿用 test_reversals 的旧库升级方式）
    original_db = os.environ["APP_DB"]
    legacy = os.path.join(_tmp_dir, "legacy-cor.sqlite")
    os.environ["APP_DB"] = legacy
    try:
        conn = connect()
        try:
            for name in ("001_init.sql", "002_idempotency.sql"):
                conn.executescript((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
                "VALUES('t','legacy-o',10,0,'CNY','accepted')"
            )
        finally:
            conn.close()

        migrate()
        migrate()  # 第二次不应重复执行 ALTER TABLE ADD COLUMN

        conn = connect()
        try:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
            assert {"reconciled_cents", "ledger_id"} <= cols
            # 历史订单的 ledger_id 由 005 回填为非空唯一值
            ledger_id = conn.execute(
                "SELECT ledger_id FROM orders WHERE tenant='t' AND order_id='legacy-o'"
            ).fetchone()["ledger_id"]
            assert ledger_id
            conn.execute(
                "INSERT INTO correction_records"
                "(tenant, order_id, ledger_id, before_order_id, before_amount_cents, before_currency, "
                "after_order_id, after_amount_cents, after_currency, result) "
                "VALUES('t','legacy-o',?,'legacy-o',10,'CNY','legacy-o',10,'CNY','applied')",
                (ledger_id,),
            )
        finally:
            conn.close()
    finally:
        os.environ["APP_DB"] = original_db
