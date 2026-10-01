import argparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
from app.store.db import connect, migrate
from app.store.orders import (
    ReceiptAlreadyRefunded,
    ReceiptAmountMismatch,
    ReceiptNotPending,
    ReceiptNotRefundable,
    RefundExceedsPaid,
    RefundIdMismatch,
    RevokeExceedsPaid,
)

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)
    # 调用方生成的凭据标识：非空时收款进入待核销；作用域（租户，订单），与订单标识、冲正标识互不替代。
    receipt_id: str | None = None

def _tenant(x_tenant: str) -> str:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return x_tenant

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
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    tenant = _tenant(x_tenant)
    receipt_id = body.receipt_id.strip() if body.receipt_id else None
    if body.receipt_id is not None and not receipt_id:
        raise HTTPException(status_code=400, detail="receipt_id must be a non-empty string when present")
    try:
        order = orders.add_payment(tenant, order_id, body.amount_cents, receipt_id)
    except ValueError as error:
        # 收款超额与凭据标识重复都按冲突处理。
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/refunds")
def refund_order(order_id: str, body: dict, x_tenant: str = Header(default="")) -> dict:
    tenant = _tenant(x_tenant)
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
    """核销确认：待核销 → 已核销，订单按现有规则判定结清；重复确认幂等回放首次结果。"""
    tenant = _tenant(x_tenant)
    try:
        result = orders.confirm_receipt(tenant, order_id, receipt_id)
    except ReceiptNotPending as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="receipt not found")
    return result

@app.post("/orders/{order_id}/receipts/{receipt_id}/revoke")
def revoke_receipt(order_id: str, receipt_id: str, x_tenant: str = Header(default="")) -> dict:
    """撤销：整笔移除待核销收款并补撤销流水；已核销凭据 409；重复撤销幂等回放首次结果。"""
    tenant = _tenant(x_tenant)
    try:
        result = orders.revoke_receipt(tenant, order_id, receipt_id)
    except ReceiptNotPending as error:
        raise HTTPException(status_code=409, detail=str(error))
    except RevokeExceedsPaid as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="receipt not found")
    return result

@app.post("/orders/{order_id}/receipts/{receipt_id}/refund")
def refund_receipt(order_id: str, receipt_id: str, body: dict, x_tenant: str = Header(default="")) -> dict:
    """以凭据冲正：只有已核销凭据可冲正；沿用冲正的幂等、上限与原子性。"""
    tenant = _tenant(x_tenant)
    refund_id = body.get("refund_id") if isinstance(body, dict) else None
    amount_cents = body.get("amount_cents") if isinstance(body, dict) else None
    if not isinstance(refund_id, str) or not refund_id.strip():
        raise HTTPException(status_code=400, detail="refund_id is required and must be a non-empty string")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise HTTPException(status_code=400, detail="amount_cents must be a positive integer")
    try:
        result = orders.refund_receipt(tenant, order_id, receipt_id, refund_id, amount_cents)
    except RefundExceedsPaid as error:
        raise HTTPException(status_code=409, detail=str(error))
    except RefundIdMismatch as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ReceiptNotRefundable as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ReceiptAlreadyRefunded as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ReceiptAmountMismatch as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="receipt not found")
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
