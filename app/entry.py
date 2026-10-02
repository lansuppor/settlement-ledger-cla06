import argparse
import base64
import json
from typing import Annotated

from fastapi import Body, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
from app.store.db import connect, migrate
from app.usecase import batch as batch_uc
from app.usecase import orders as orders_uc

app = FastAPI(title="settlement-ledger")

IDEMPOTENCY_HEADER = "Idempotency-Key"

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class ReversalIn(BaseModel):
    # 不使用 gt=0：金额 <= 0 属于业务非法，需与「超过已收」一起由业务层
    # 返回 409 及明确原因，与订单不存在(404)、幂等冲突(422) 区分开。
    amount_cents: int

class ReconcileIn(BaseModel):
    # 同 ReversalIn：金额 <= 0 由业务层返回 409 及明确原因
    amount_cents: int

class CorrectionIn(BaseModel):
    # 不使用 pydantic 约束：金额 <= 0、标识为空、币种不受支持都属于业务非法，
    # 需由业务层返回 409 及明确原因，与订单不存在(404)、幂等冲突(422) 区分开。
    order_id: str
    amount_cents: int
    currency: str

class RefundIn(BaseModel):
    # 不使用 gt=0：金额 <= 0 属于业务非法，需与「退款后已收为负/低于已核销」一起
    # 由业务层返回 409 及明确原因，与订单不存在(404)、幂等冲突(422) 区分开。
    amount_cents: int
    reason: str

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}

@app.post("/orders", status_code=201)
def create_order(body: OrderIn, idempotency_key: str = Header(default="", alias=IDEMPOTENCY_HEADER)) -> dict:
    try:
        order_rules.assert_currency(body.currency)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    if not idempotency_key:
        # 未携带请求标识：保持原有处理方式
        try:
            orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
        except Exception as error:
            if "UNIQUE" in str(error):
                raise HTTPException(status_code=409, detail="order already accepted")
            raise
        return orders.get(body.tenant, body.order_id)

    try:
        result = orders_uc.accept_order(
            body.tenant, idempotency_key, body.order_id, body.amount_cents, body.currency
        )
    except orders_uc.IdempotentConflict as error:
        raise HTTPException(status_code=422, detail=str(error))
    if result.status_code != 201:
        raise HTTPException(status_code=result.status_code, detail=result.body.get("detail"))
    return result.body

@app.post("/orders/import")
def import_orders(body: str = Body(default="", media_type="text/plain")) -> dict:
    # 逗号分隔文本按行受理：部分成功、逐行结论、可安全重试；不携带请求标识
    return batch_uc.import_orders(body)

# 条件检索的可选状态值：未收清 / 已结清
SEARCH_STATUSES = ("accepted", "settled")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

def _encode_cursor(accept_seq: int) -> str:
    """把上一页最后一条的受理序号编码为不透明游标。

    游标只含受理顺序位置（业务时间线受理条目的全库单调序号），不含租户、
    订单标识或任何数据库内部位置；调用方原样回传即可继续翻页。
    """
    raw = json.dumps({"v": 1, "s": accept_seq}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def _decode_cursor(token: str) -> int:
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except ValueError:
        # 含 base64 非法、JSON 非法等一切不可解析情形
        raise HTTPException(status_code=400, detail="invalid cursor")
    seq = data.get("s") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("v") != 1:
        raise HTTPException(status_code=400, detail="invalid cursor")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise HTTPException(status_code=400, detail="invalid cursor")
    return seq

def _parse_statuses(raw: list[str] | None) -> list[str]:
    """解析状态筛选：可重复传参或逗号分隔；未给出视为不限制。"""
    statuses: list[str] = []
    for item in raw or []:
        for value in item.split(","):
            value = value.strip()
            if not value:
                continue
            if value not in SEARCH_STATUSES:
                raise HTTPException(status_code=400, detail=f"unsupported status: {value}")
            if value not in statuses:
                statuses.append(value)
    return statuses

def _parse_amount_bound(raw: str | None, name: str) -> int | None:
    if raw is None:
        return None
    if not raw.isdigit():
        raise HTTPException(
            status_code=400, detail=f"{name} must be a non-negative integer in minor units"
        )
    return int(raw)

def _parse_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_PAGE_SIZE
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be an integer between 1 and {MAX_PAGE_SIZE}",
        )
    return int(raw)

@app.get("/orders")
def search_orders(
    x_tenant: str = Header(default=""),
    status: Annotated[list[str] | None, Query()] = None,
    currency: Annotated[str | None, Query()] = None,
    min_amount_cents: Annotated[str | None, Query()] = None,
    max_amount_cents: Annotated[str | None, Query()] = None,
    limit: Annotated[str | None, Query()] = None,
    cursor: Annotated[str, Query()] = "",
) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    statuses = _parse_statuses(status)
    if currency is not None:
        try:
            order_rules.assert_currency(currency)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
    min_amount = _parse_amount_bound(min_amount_cents, "min_amount_cents")
    max_amount = _parse_amount_bound(max_amount_cents, "max_amount_cents")
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise HTTPException(status_code=400, detail="min_amount_cents must not exceed max_amount_cents")
    page_size = _parse_limit(limit)
    after_seq = _decode_cursor(cursor) if cursor else None
    page, last_seq = orders.search(
        tenant, statuses, currency, min_amount, max_amount, page_size, after_seq
    )
    # 返回条数不足本次请求条数即为最后一页，游标为空
    next_cursor = _encode_cursor(last_seq) if last_seq is not None and len(page) == page_size else ""
    return {"orders": page, "next_cursor": next_cursor}

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.get("/orders/{order_id}/timeline")
def read_timeline(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    entries = orders.list_timeline(tenant, order_id)
    if entries is None:
        # 跨租户同样按不存在处理，不泄漏订单是否存在
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "timeline": entries}

@app.post("/orders/{order_id}/payments")
def add_payment(
    order_id: str,
    body: PaymentIn,
    x_tenant: str = Header(default=""),
    idempotency_key: str = Header(default="", alias=IDEMPOTENCY_HEADER),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")

    if not idempotency_key:
        # 未携带请求标识：保持原有处理方式
        try:
            order = orders.add_payment(x_tenant, order_id, body.amount_cents)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    try:
        result = orders_uc.register_payment(x_tenant, idempotency_key, order_id, body.amount_cents)
    except orders_uc.IdempotentConflict as error:
        raise HTTPException(status_code=422, detail=str(error))
    if result.status_code != 200:
        raise HTTPException(status_code=result.status_code, detail=result.body.get("detail"))
    return result.body

@app.post("/orders/{order_id}/reversals")
def add_reversal(
    order_id: str,
    body: ReversalIn,
    x_tenant: str = Header(default=""),
    idempotency_key: str = Header(default="", alias=IDEMPOTENCY_HEADER),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")

    if not idempotency_key:
        # 未携带请求标识：与收款一致的原有处理方式
        try:
            order = orders.reverse_payment(x_tenant, order_id, body.amount_cents)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    try:
        result = orders_uc.reverse_payment(x_tenant, idempotency_key, order_id, body.amount_cents)
    except orders_uc.IdempotentConflict as error:
        raise HTTPException(status_code=422, detail=str(error))
    if result.status_code != 200:
        raise HTTPException(status_code=result.status_code, detail=result.body.get("detail"))
    return result.body

@app.get("/orders/{order_id}/reversals")
def read_reversals(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    records = orders.list_reversals(tenant, order_id)
    if records is None:
        # 跨租户同样按不存在处理，不泄漏订单是否存在
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "reversals": records}

@app.post("/orders/{order_id}/reconciliations")
def add_reconciliation(
    order_id: str,
    body: ReconcileIn,
    x_tenant: str = Header(default=""),
    idempotency_key: str = Header(default="", alias=IDEMPOTENCY_HEADER),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")

    if not idempotency_key:
        # 未携带请求标识：与收款、冲正一致的原有处理方式
        try:
            order = orders.reconcile(x_tenant, order_id, body.amount_cents)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    try:
        result = orders_uc.reconcile_order(x_tenant, idempotency_key, order_id, body.amount_cents)
    except orders_uc.IdempotentConflict as error:
        raise HTTPException(status_code=422, detail=str(error))
    if result.status_code != 200:
        raise HTTPException(status_code=result.status_code, detail=result.body.get("detail"))
    return result.body

@app.get("/orders/{order_id}/reconciliations")
def read_reconciliations(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    records = orders.list_reconciliations(tenant, order_id)
    if records is None:
        # 跨租户同样按不存在处理，不泄漏订单是否存在
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "reconciliations": records}

@app.post("/orders/{order_id}/correction")
def correct_order(
    order_id: str,
    body: CorrectionIn,
    x_tenant: str = Header(default=""),
    idempotency_key: str = Header(default="", alias=IDEMPOTENCY_HEADER),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")

    if not idempotency_key:
        # 未携带请求标识：与收款、冲正、核销一致的处理方式
        try:
            order = orders.correct(
                x_tenant, order_id, body.order_id, body.amount_cents, body.currency
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    try:
        result = orders_uc.correct_order(
            x_tenant, idempotency_key, order_id, body.order_id, body.amount_cents, body.currency
        )
    except orders_uc.IdempotentConflict as error:
        raise HTTPException(status_code=422, detail=str(error))
    if result.status_code != 200:
        raise HTTPException(status_code=result.status_code, detail=result.body.get("detail"))
    return result.body

@app.get("/orders/{order_id}/corrections")
def read_corrections(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    records = orders.list_corrections(tenant, order_id)
    if records is None:
        # 跨租户同样按不存在处理，不泄漏订单是否存在
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "corrections": records}

@app.post("/orders/{order_id}/refunds")
def add_refund(
    order_id: str,
    body: RefundIn,
    x_tenant: str = Header(default=""),
    idempotency_key: str = Header(default="", alias=IDEMPOTENCY_HEADER),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")

    if not idempotency_key:
        # 未携带请求标识：与收款、冲正、核销一致的处理方式
        try:
            order = orders.refund(x_tenant, order_id, body.amount_cents, body.reason)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    try:
        result = orders_uc.refund_order(
            x_tenant, idempotency_key, order_id, body.amount_cents, body.reason
        )
    except orders_uc.IdempotentConflict as error:
        raise HTTPException(status_code=422, detail=str(error))
    if result.status_code != 200:
        raise HTTPException(status_code=result.status_code, detail=result.body.get("detail"))
    return result.body

@app.get("/orders/{order_id}/refunds")
def read_refunds(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    records = orders.list_refunds(tenant, order_id)
    if records is None:
        # 跨租户同样按不存在处理，不泄漏订单是否存在
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "refunds": records}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    migrate()
    if args.migrate:
        print("migrated")
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
