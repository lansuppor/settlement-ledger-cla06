import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "search.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

_seq = [0]


def _tenant() -> str:
    """每个用例使用独立租户，避免共享库内其他用例的订单干扰断言。"""
    _seq[0] += 1
    return f"ts-{_seq[0]}"


def _accept(order_id: str, amount: int, currency: str = "CNY", tenant: str = ""):
    return client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": currency},
    )


def _pay(order_id: str, amount: int, tenant: str):
    return client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def _search(tenant: str, **params):
    return client.get("/orders", params=params, headers={"X-Tenant": tenant})


def _walk(tenant: str, **params):
    """按游标翻页取完所有页，返回 (各页订单标识列表, 合并后的订单列表)。"""
    pages, merged = [], []
    cursor = ""
    while True:
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        resp = _search(tenant, **query)
        assert resp.status_code == 200
        body = resp.json()
        pages.append([o["order_id"] for o in body["orders"]])
        merged.extend(body["orders"])
        cursor = body["next_cursor"]
        if not cursor:
            return pages, merged


def test_search_returns_orders_in_acceptance_order_with_read_fields() -> None:
    tenant = _tenant()
    for order_id, amount in [("s-1", 500), ("s-2", 300), ("s-3", 800)]:
        assert _accept(order_id, amount, tenant=tenant).status_code == 201
    resp = _search(tenant)
    assert resp.status_code == 200
    body = resp.json()
    assert [o["order_id"] for o in body["orders"]] == ["s-1", "s-2", "s-3"]
    # 每条返回项与按标识读取订单得到的字段一致
    by_id = client.get("/orders/s-2", headers={"X-Tenant": tenant}).json()
    listed = next(o for o in body["orders"] if o["order_id"] == "s-2")
    assert listed == by_id
    assert set(listed) == {
        "tenant", "order_id", "amount_cents", "paid_cents",
        "reconciled_cents", "currency", "status", "outstanding_cents",
    }


def test_search_filters_by_status_currency_and_amount_range() -> None:
    tenant = _tenant()
    _accept("f-1", 100, "CNY", tenant)
    _accept("f-2", 200, "USD", tenant)
    _accept("f-3", 300, "CNY", tenant)
    _accept("f-4", 400, "CNY", tenant)
    _pay("f-3", 300, tenant)  # f-3 收清进入 settled

    body = _search(tenant, status="settled").json()
    assert [o["order_id"] for o in body["orders"]] == ["f-3"]

    body = _search(tenant, status="accepted", currency="CNY").json()
    assert [o["order_id"] for o in body["orders"]] == ["f-1", "f-4"]

    body = _search(tenant, currency="USD").json()
    assert [o["order_id"] for o in body["orders"]] == ["f-2"]

    body = _search(tenant, min_amount_cents="200", max_amount_cents="400", currency="CNY").json()
    assert [o["order_id"] for o in body["orders"]] == ["f-3", "f-4"]

    # 状态可同时包含两者（重复传参与逗号分隔等价）
    both = _search(tenant, status=["accepted", "settled"]).json()
    comma = _search(tenant, status="accepted,settled").json()
    assert [o["order_id"] for o in both["orders"]] == ["f-1", "f-2", "f-3", "f-4"]
    assert both == comma


def test_search_pagination_is_complete_ordered_and_replayable() -> None:
    tenant = _tenant()
    for i in range(5):
        _accept(f"p-{i}", 100 + i, tenant=tenant)
    pages, merged = _walk(tenant, limit="2")
    assert pages == [["p-0", "p-1"], ["p-2", "p-3"], ["p-4"]]
    assert len({o["order_id"] for o in merged}) == 5  # 不重不漏

    # 同一条件同一游标重放：返回完全相同的订单集合与顺序
    first = _search(tenant, limit="2").json()
    again = _search(tenant, limit="2").json()
    assert first == again
    page2 = _search(tenant, limit="2", cursor=first["next_cursor"]).json()
    page2_replay = _search(tenant, limit="2", cursor=first["next_cursor"]).json()
    assert page2 == page2_replay
    assert [o["order_id"] for o in page2["orders"]] == ["p-2", "p-3"]

    # 不足本页条数即最后一页，游标为空
    assert page2["next_cursor"]
    page3 = _search(tenant, limit="2", cursor=page2["next_cursor"]).json()
    assert [o["order_id"] for o in page3["orders"]] == ["p-4"]
    assert page3["next_cursor"] == ""


def test_search_cursor_not_disturbed_by_concurrent_writes() -> None:
    tenant = _tenant()
    for i in range(4):
        _accept(f"c-{i}", 100, tenant=tenant)
    page1 = _search(tenant, limit="2").json()
    assert [o["order_id"] for o in page1["orders"]] == ["c-0", "c-1"]

    # 翻页过程中：已翻过的订单收款并核销、新订单受理、未翻到的订单收款后退款
    _pay("c-0", 100, tenant)
    client.post(
        "/orders/c-0/reconciliations", json={"amount_cents": 100}, headers={"X-Tenant": tenant}
    )
    _accept("c-4", 100, tenant=tenant)
    _pay("c-3", 40, tenant)
    client.post(
        "/orders/c-3/refunds",
        json={"amount_cents": 10, "reason": "partial return"},
        headers={"X-Tenant": tenant},
    )

    page2 = _search(tenant, limit="2", cursor=page1["next_cursor"]).json()
    # 已翻过的页不重复出现、未翻到的页不漏订单
    assert [o["order_id"] for o in page2["orders"]] == ["c-2", "c-3"]
    page3 = _search(tenant, limit="2", cursor=page2["next_cursor"]).json()
    assert [o["order_id"] for o in page3["orders"]] == ["c-4"]  # 新受理排在最后
    assert page3["next_cursor"] == ""

    # 游标不含租户、订单标识等可读内容
    assert tenant not in page1["next_cursor"]
    assert "c-0" not in page1["next_cursor"]


def test_search_default_page_size_is_50_and_import_line_order() -> None:
    tenant = _tenant()
    lines = "\n".join(f"{tenant},d-{i},100,CNY" for i in range(55))
    resp = client.post("/orders/import", content=lines, headers={"Content-Type": "text/plain"})
    assert resp.json()["succeeded"] == 55

    page1 = _search(tenant).json()
    assert len(page1["orders"]) == 50
    # 批量导入受理的多张订单按导入行先后排列
    assert [o["order_id"] for o in page1["orders"][:3]] == ["d-0", "d-1", "d-2"]
    page2 = _search(tenant, cursor=page1["next_cursor"]).json()
    assert [o["order_id"] for o in page2["orders"]] == [f"d-{i}" for i in range(50, 55)]
    assert page2["next_cursor"] == ""


def test_search_limit_out_of_range_returns_400() -> None:
    tenant = _tenant()
    for bad in ("0", "201", "-1", "abc", "1.5"):
        assert _search(tenant, limit=bad).status_code == 400
    assert _search(tenant, limit="1").status_code == 200
    assert _search(tenant, limit="200").status_code == 200


def test_search_invalid_conditions_return_400() -> None:
    tenant = _tenant()
    assert _search(tenant, status="paid").status_code == 400
    assert _search(tenant, currency="GBP").status_code == 400
    assert _search(tenant, min_amount_cents="-5").status_code == 400
    assert _search(tenant, max_amount_cents="abc").status_code == 400
    assert _search(tenant, min_amount_cents="500", max_amount_cents="100").status_code == 400
    assert _search(tenant, cursor="not-a-cursor").status_code == 400
    assert _search(tenant, cursor="").status_code == 200  # 空游标即第一页


def test_search_tenant_isolation_and_empty_result() -> None:
    tenant = _tenant()
    other = _tenant()
    _accept("x-1", 100, tenant=other)
    body = _search(tenant, status="settled", currency="JPY").json()
    assert body == {"orders": [], "next_cursor": ""}  # 空结果返回空列表而不是错误

    # 其他租户的条件与游标都读不到本租户订单
    _accept("x-2", 200, tenant=tenant)
    foreign = _search(other).json()
    assert [o["order_id"] for o in foreign["orders"]] == ["x-1"]
    mine = _search(tenant).json()["orders"]
    assert all(o["tenant"] == tenant for o in mine)
    assert {o["order_id"] for o in mine} == {"x-2"}


def test_search_missing_tenant_header_returns_400() -> None:
    assert client.get("/orders").status_code == 400
