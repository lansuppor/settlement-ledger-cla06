import argparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, reversals
from app.store.db import connect, migrate
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
    amount_cents: int = Field(gt=0)

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
        # 未携带请求标识：保持原有处理方式
        try:
            result = orders.reverse(x_tenant, order_id, body.amount_cents)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        if result is None:
            raise HTTPException(status_code=404, detail="order not found")
        order, reversal = result
        return {**order, "reversal": reversal}

    try:
        result = orders_uc.reverse_payment(x_tenant, idempotency_key, order_id, body.amount_cents)
    except orders_uc.IdempotentConflict as error:
        raise HTTPException(status_code=422, detail=str(error))
    if result.status_code != 200:
        raise HTTPException(status_code=result.status_code, detail=result.body.get("detail"))
    return result.body

@app.get("/orders/{order_id}/reversals")
def list_reversals(order_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if orders.get(x_tenant, order_id) is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "reversals": reversals.list(x_tenant, order_id)}

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
