import argparse
import sqlite3

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
from app.store.db import connect, migrate
from app.usecase import idempotency

app = FastAPI(title="settlement-ledger")

IDEMPOTENCY_HEADER = "Idempotency-Key"


class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)


def _replayed_response(result: idempotency.IdempotentResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/orders", status_code=201)
def create_order(body: OrderIn, idempotency_key: str = Header(default="", alias=IDEMPOTENCY_HEADER)):
    order_rules.assert_currency(body.currency)
    request_id = idempotency_key.strip()
    if request_id:
        try:
            result = idempotency.create_order(
                body.tenant, body.order_id, body.amount_cents, body.currency, request_id
            )
        except idempotency.IdempotencyConflict as error:
            raise HTTPException(status_code=409, detail=str(error))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="order already accepted")
        return _replayed_response(result)
    try:
        orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status_code=409, detail="order already accepted")
        raise
    return orders.get(body.tenant, body.order_id)


@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default=""),
                idempotency_key: str = Header(default="", alias=IDEMPOTENCY_HEADER)):
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    request_id = idempotency_key.strip()
    if request_id:
        try:
            result = idempotency.add_payment(x_tenant, order_id, body.amount_cents, request_id)
        except idempotency.IdempotencyConflict as error:
            raise HTTPException(status_code=409, detail=str(error))
        except idempotency.PaymentExceedsOutstanding as error:
            raise HTTPException(status_code=409, detail=str(error))
        except idempotency.OrderNotFound:
            raise HTTPException(status_code=404, detail="order not found")
        return _replayed_response(result)
    try:
        order = orders.add_payment(x_tenant, order_id, body.amount_cents)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


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
