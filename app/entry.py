import argparse

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
from app.store.db import connect, migrate
from app.usecase import imports as imports_uc
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
async def import_orders(request: Request) -> dict:
    """批量导入订单：请求体为逗号分隔的订单行文本（text/plain），逐行受理。

    批量导入不携带请求标识；每行独立定论，返回计数闭合的导入结果。
    """
    raw = await request.body()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="body must be UTF-8 text") from None
    return imports_uc.import_orders_text(text)

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

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
