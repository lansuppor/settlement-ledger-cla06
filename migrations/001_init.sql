CREATE TABLE IF NOT EXISTS orders(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id)
);

-- 收付款流水：收款（payment）与冲正（refund）按发生顺序留痕，
-- paid_cents 恒等于本单 payment 金额之和减去 refund 金额之和。
CREATE TABLE IF NOT EXISTS order_ledger(
  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('payment', 'refund')),
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  ref_id TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

-- 冲正标识作用域为（租户, 订单, 冲正标识）；收款行 ref_id 为 NULL，不参与幂等约束。
CREATE UNIQUE INDEX IF NOT EXISTS idx_order_ledger_refund_ref
  ON order_ledger(tenant, order_id, ref_id)
  WHERE kind = 'refund';
