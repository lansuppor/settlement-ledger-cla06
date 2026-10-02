import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("APP_DB", os.path.join(_tmp_dir, "search.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _accept(tenant: str, order_id: str, amount: int = 100, currency: str = "CNY", key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": currency},
        headers=headers,
    )


def _pay(tenant: str, order_id: str, amount: int, key: str | None = None):
    headers = {"X-Tenant": tenant}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers=headers)


def _reverse(tenant: str, order_id: str, amount: int):
    return client.post(
        f"/orders/{order_id}/reversals", json={"amount_cents": amount}, headers={"X-Tenant": tenant}
    )


def _search(tenant: str, query: str = ""):
    url = f"/orders{('?' + query) if query else ''}"
    return client.get(url, headers={"X-Tenant": tenant})


def _ids(resp) -> list[str]:
    return [o["order_id"] for o in resp.json()["orders"]]


def _walk(tenant: str, query: str, page_size: int = 2) -> list[str]:
    """用游标翻完整个结果集，返回全部订单标识。"""
    seen: list[str] = []
    resp = _search(tenant, f"{query}{'&' if query else ''}limit={page_size}")
    while True:
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.extend(_ids(resp))
        cursor = body["next_cursor"]
        if not cursor:
            return seen
        resp = _search(tenant, f"cursor={cursor}&limit={page_size}")


def test_results_follow_acceptance_order_including_batch_import() -> None:
    tenant = "q-order"
    _accept(tenant, "m1", 100)
    text = "q-order,m2,100,CNY\nq-order,m3,100,CNY\nq-order,m4,100,CNY\n"
    assert client.post("/orders/import", content=text, headers={"Content-Type": "text/plain"}).status_code == 200
    _accept(tenant, "m5", 100)
    assert _ids(_search(tenant)) == ["m1", "m2", "m3", "m4", "m5"]


def test_each_item_has_exactly_the_single_order_read_fields() -> None:
    tenant = "q-fields"
    _accept(tenant, "f1", 500, "USD")
    _pay(tenant, "f1", 200)
    item = _search(tenant).json()["orders"][0]
    single = client.get("/orders/f1", headers={"X-Tenant": tenant}).json()
    assert item == single
    assert set(item) == {
        "tenant", "order_id", "amount_cents", "paid_cents", "reconciled_cents",
        "currency", "status", "outstanding_cents",
    }


def test_filter_by_status_currency_and_amount_range() -> None:
    tenant = "q-filter"
    _accept(tenant, "a1", 100, "CNY")
    _accept(tenant, "a2", 500, "USD")
    _accept(tenant, "a3", 900, "CNY")
    _pay(tenant, "a3", 900)  # settled
    assert _ids(_search(tenant, "status=accepted")) == ["a1", "a2"]
    assert _ids(_search(tenant, "status=settled")) == ["a3"]
    assert _ids(_search(tenant, "currency=CNY")) == ["a1", "a3"]
    assert _ids(_search(tenant, "currency=USD")) == ["a2"]
    assert _ids(_search(tenant, "min_amount=200")) == ["a2", "a3"]
    assert _ids(_search(tenant, "max_amount=500")) == ["a1", "a2"]
    assert _ids(_search(tenant, "min_amount=100&max_amount=500")) == ["a1", "a2"]
    assert _ids(_search(tenant, "min_amount=500&max_amount=500")) == ["a2"]
    # 组合条件：未收清 + CNY
    assert _ids(_search(tenant, "status=accepted&currency=CNY")) == ["a1"]
    # 未给条件即不限制
    assert _ids(_search(tenant)) == ["a1", "a2", "a3"]


def test_empty_result_is_empty_list_not_error() -> None:
    tenant = "q-empty"
    _accept(tenant, "e1", 100, "CNY")
    resp = _search(tenant, "currency=EUR")
    assert resp.status_code == 200
    assert resp.json() == {"orders": [], "next_cursor": ""}
    # 完全没有订单的租户同样返回空列表
    assert _search("q-empty-nothing").json() == {"orders": [], "next_cursor": ""}


def test_pagination_is_complete_without_duplication_or_gap() -> None:
    tenant = "q-page"
    for i in range(1, 8):
        _accept(tenant, f"p{i}", 100)
    assert _walk(tenant, "", page_size=3) == [f"p{i}" for i in range(1, 8)]
    # limit=1 逐张翻页同样不重不漏
    assert _walk(tenant, "", page_size=1) == [f"p{i}" for i in range(1, 8)]


def test_last_page_short_has_empty_cursor_full_page_emits_cursor() -> None:
    tenant = "q-last"
    _accept(tenant, "l1")
    _accept(tenant, "l2")
    first = _search(tenant, "limit=2").json()
    assert [o["order_id"] for o in first["orders"]] == ["l1", "l2"]
    assert first["next_cursor"]  # 本页取满，给出游标
    # 游标之后已无命中：下一次返回空列表与空游标
    nxt = _search(tenant, f"cursor={first['next_cursor']}&limit=2").json()
    assert nxt == {"orders": [], "next_cursor": ""}


def test_default_limit_is_50_and_bounds_enforced() -> None:
    tenant = "q-limit"
    text = "".join(f"q-limit,d{i},100,CNY\n" for i in range(51))
    client.post("/orders/import", content=text, headers={"Content-Type": "text/plain"})
    first = _search(tenant).json()
    assert len(first["orders"]) == 50 and first["next_cursor"]
    second = _search(tenant, f"cursor={first['next_cursor']}").json()
    assert len(second["orders"]) == 1 and second["next_cursor"] == ""

    assert _search(tenant, "limit=1").status_code == 200
    assert _search(tenant, "limit=200").status_code == 200
    for bad in ("0", "201", "-1", "abc", "1.5"):
        assert _search(tenant, f"limit={bad}").status_code == 400, bad


def test_invalid_filter_params_return_400() -> None:
    tenant = "q-bad"
    assert _search(tenant, "status=foo").status_code == 400
    assert _search(tenant, "currency=GBP").status_code == 400
    assert _search(tenant, "min_amount=x").status_code == 400
    assert _search(tenant, "max_amount=-3").status_code == 400
    assert _search(tenant, "min_amount=900&max_amount=100").status_code == 400
    assert client.get("/orders").status_code == 400


def test_tenant_isolation_and_cross_tenant_cursor() -> None:
    tenant = "q-iso-a"
    _accept(tenant, "same", 100)
    _accept(tenant, "only-a", 200)
    _accept("q-iso-b", "same", 999)
    ids = _ids(_search(tenant))
    assert ids == ["same", "only-a"]
    for order_id in ids:
        item = _search(tenant).json()["orders"]
        assert all(o["tenant"] == tenant for o in item)
    # 游标绑定租户：其他租户原样回传也不得越界读取
    cursor = _search(tenant, "limit=1").json()["next_cursor"]
    assert _search("q-iso-b", f"cursor={cursor}&limit=1").status_code == 400
    # 篡改/伪造游标同样 400
    assert _search(tenant, "cursor=" + "0" * 64).status_code == 400
    assert _search(tenant, "cursor=not-a-token").status_code == 400


def test_cursor_is_opaque_and_replayable() -> None:
    tenant = "q-replay"
    _accept(tenant, "r1")
    _accept(tenant, "r2")
    resp = _search(tenant, "status=accepted&limit=1")
    first = resp.json()
    # 游标为不透明十六进制摘要，不含订单标识、租户或数据库内部位置
    token = first["next_cursor"]
    assert isinstance(token, str) and len(token) == 64
    assert all(ch in "0123456789abcdef" for ch in token)
    assert tenant not in token and "r1" not in token and "r2" not in token
    # 同租户同条件同游标：多次查询返回完全相同的集合与顺序
    again = _search(tenant, "status=accepted&limit=1").json()
    assert again == first
    # 只回传游标（不重复筛选条件）也按游标绑定的条件继续
    second = _search(tenant, f"cursor={token}&limit=1").json()
    assert [o["order_id"] for o in second["orders"]] == ["r2"]
    # 显式给出与游标不一致的筛选：400，不跨条件混用
    assert _search(tenant, f"cursor={token}&status=settled&limit=1").status_code == 400
    assert _search(tenant, f"cursor={token}&currency=USD&limit=1").status_code == 400


def test_status_changes_ahead_do_not_move_cursor_or_skip_targets() -> None:
    tenant = "q-mut"
    for i in range(1, 6):
        _accept(tenant, f"s{i}", 100)
    # 首页取 s1,s2；随后 s3（游标之后）结清掉出 accepted 集合
    first = _search(tenant, "status=accepted&limit=2")
    assert _ids(first) == ["s1", "s2"]
    _pay(tenant, "s3", 100)
    # s4,s5 不被跳过；s3 不再命中
    second = _search(tenant, f"status=accepted&limit=2&cursor={first.json()['next_cursor']}")
    assert _ids(second) == ["s4", "s5"]
    # 改动已翻过页的订单不影响后续：s1 结清后第二页不变
    _pay(tenant, "s1", 100)
    second_again = _search(tenant, f"status=accepted&limit=2&cursor={first.json()['next_cursor']}")
    assert _ids(second_again) == ["s4", "s5"]
    # s3 在已结清视图中可见
    assert _ids(_search(tenant, "status=settled")) == ["s1", "s3"]


def test_order_re_entering_filter_ahead_of_cursor_appears_once() -> None:
    tenant = "q-reenter"
    _accept(tenant, "t1", 100)
    _accept(tenant, "t2", 100)
    _accept(tenant, "t3", 100)
    _accept(tenant, "t4", 100)
    _pay(tenant, "t3", 100)  # t3 先结清，不在 accepted 结果中
    first = _search(tenant, "status=accepted&limit=1")
    assert _ids(first) == ["t1"]
    # t3 冲正后重新未收清：受理序号固定，在游标之后恰好出现一次，t2/t4 不被跳过
    assert _reverse(tenant, "t3", 100).status_code == 200
    cursor = first.json()["next_cursor"]
    assert _ids(_search(tenant, f"status=accepted&limit=2&cursor={cursor}")) == ["t2", "t3"]
    # 同游标重复查询结论一致
    assert _ids(_search(tenant, f"status=accepted&limit=2&cursor={cursor}")) == ["t2", "t3"]
    assert _walk(tenant, "status=accepted", page_size=1) == ["t1", "t2", "t3", "t4"]


def test_new_acceptances_during_paging_land_at_end() -> None:
    tenant = "q-new"
    _accept(tenant, "n1")
    _accept(tenant, "n2")
    first = _search(tenant, "limit=1")
    assert _ids(first) == ["n1"]
    _accept(tenant, "n3")  # 翻页途中新受理：序号大于既有全部游标位置
    assert _walk(tenant, f"cursor={first.json()['next_cursor']}", page_size=1) == ["n2", "n3"]


def test_correction_keeps_position_and_uses_corrected_content() -> None:
    tenant = "q-corr"
    _accept(tenant, "c1", 100, "CNY")
    _accept(tenant, "c2", 100, "CNY")
    corrected = client.post(
        "/orders/c1/correction",
        json={"order_id": "c1x", "amount_cents": 800, "currency": "USD"},
        headers={"X-Tenant": tenant},
    )
    assert corrected.status_code == 200
    # 受理位置不变，按更正后标识与金额呈现
    assert _ids(_search(tenant)) == ["c1x", "c2"]
    assert _ids(_search(tenant, "min_amount=500")) == ["c1x"]
    assert _ids(_search(tenant, "currency=USD")) == ["c1x"]
    # 旧标识按不存在处理
    assert client.get("/orders/c1", headers={"X-Tenant": tenant}).status_code == 404


def test_search_is_read_only() -> None:
    tenant = "q-ro"
    _accept(tenant, "z1", 500)
    _pay(tenant, "z1", 200)
    before = client.get("/orders/z1", headers={"X-Tenant": tenant}).json()
    for query in ("", "status=accepted", "currency=CNY", "min_amount=1&max_amount=999999", "limit=1"):
        _search(tenant, query)
    after = client.get("/orders/z1", headers={"X-Tenant": tenant}).json()
    assert after == before


def test_cursor_survives_process_restart() -> None:
    tenant = "q-restart"
    _accept(tenant, "k1", 100)
    _accept(tenant, "k2", 100)
    token = _search(tenant, "status=accepted&limit=1").json()["next_cursor"]
    # 全新进程打开同一数据库文件：密钥与游标行均已持久化，游标继续可用且结论一致
    code = (
        "import json;"
        "from fastapi.testclient import TestClient;"
        "from app.entry import app;"
        "from app.store.db import migrate;"
        "migrate();"
        "c = TestClient(app);"
        "r = c.get('/orders?status=accepted&limit=1&cursor=" + token + "', headers={'X-Tenant': '" + tenant + "'});"
        "print(json.dumps({'status': r.status_code, 'ids': [o['order_id'] for o in r.json()['orders']]}))"
    )
    env = {**os.environ, "APP_DB": str(db_path())}
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"status": 200, "ids": ["k2"]}
