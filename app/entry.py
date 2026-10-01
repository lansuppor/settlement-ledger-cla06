import argparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
from app.store.db import connect, migrate
from app.store.orders import (
    ReceiptConflict,
    ReceiptNotFound,
    RefundExceedsPaid,
    RefundIdMismatch,
)

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)
    receipt_id: str | None = None

def _require_tenant(x_tenant: str) -> str:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return x_tenant

def _validate_receipt_id(receipt_id: str) -> str:
    if not receipt_id.strip():
        raise HTTPException(status_code=400, detail="receipt_id must be a non-empty string")
    return receipt_id

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}

@app.post("/orders", status_code=201)
def create_order(body: OrderIn) -> dict:
    order_rules.assert_currency(body.currency)
    try:
        orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status_code=409, detail="order already accepted")
        raise
    return orders.get(body.tenant, body.order_id)

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    receipt_id = body.receipt_id.strip() if body.receipt_id is not None else None
    if body.receipt_id is not None and not receipt_id:
        raise HTTPException(status_code=400, detail="receipt_id must be a non-empty string")
    try:
        order = orders.add_payment(tenant, order_id, body.amount_cents, receipt_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ReceiptConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/refunds")
def refund_order(order_id: str, body: dict, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    refund_id = body.get("refund_id") if isinstance(body, dict) else None
    amount_cents = body.get("amount_cents") if isinstance(body, dict) else None
    if not isinstance(refund_id, str) or not refund_id.strip():
        raise HTTPException(status_code=400, detail="refund_id is required and must be a non-empty string")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise HTTPException(status_code=400, detail="amount_cents must be a positive integer")
    try:
        result = orders.add_refund(tenant, order_id, refund_id, amount_cents)
    except RefundExceedsPaid as error:
        # 409 且原因区别于“订单不存在”：订单在，但可冲正余额不足。
        raise HTTPException(status_code=409, detail=str(error))
    except RefundIdMismatch as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    return result

@app.post("/orders/{order_id}/receipts/{receipt_id}/confirm")
def confirm_receipt(order_id: str, receipt_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    _validate_receipt_id(receipt_id)
    try:
        result = orders.confirm_receipt(tenant, order_id, receipt_id)
    except ReceiptNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except ReceiptConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    return result

@app.post("/orders/{order_id}/receipts/{receipt_id}/revoke")
def revoke_receipt(order_id: str, receipt_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    _validate_receipt_id(receipt_id)
    try:
        result = orders.revoke_receipt(tenant, order_id, receipt_id)
    except ReceiptNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except ReceiptConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    return result

@app.post("/orders/{order_id}/receipts/{receipt_id}/refunds")
def refund_by_receipt(order_id: str, receipt_id: str, body: dict,
                      x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    _validate_receipt_id(receipt_id)
    refund_id = body.get("refund_id") if isinstance(body, dict) else None
    amount_cents = body.get("amount_cents") if isinstance(body, dict) else None
    if not isinstance(refund_id, str) or not refund_id.strip():
        raise HTTPException(status_code=400, detail="refund_id is required and must be a non-empty string")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise HTTPException(status_code=400, detail="amount_cents must be a positive integer")
    try:
        result = orders.refund_receipt(tenant, order_id, receipt_id, refund_id, amount_cents)
    except ReceiptNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except ReceiptConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except RefundExceedsPaid as error:
        raise HTTPException(status_code=409, detail=str(error))
    except RefundIdMismatch as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    return result

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
