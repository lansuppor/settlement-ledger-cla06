-- 收款/冲正流水：登记收款与冲正按订单内单调序号留痕。
-- paid_cents 为 orders 表上的缓存余额，恒等于本订单 payment 金额之和 − refund 金额之和。
CREATE TABLE IF NOT EXISTS ledger_entries(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  entry_seq INTEGER NOT NULL,                 -- 订单内单调递增，即发生顺序
  entry_type TEXT NOT NULL,                   -- 'payment'（登记收款）/ 'refund'（冲正）
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  ref_id TEXT,                                -- 冲正标识；收款为 NULL
  balance_after_cents INTEGER NOT NULL CHECK(balance_after_cents >= 0),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, order_id, entry_seq)
);

-- 冲正标识幂等作用域：（租户，订单）。NULL 不参与唯一约束，收款行互不冲突。
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_refund_id
  ON ledger_entries(tenant, order_id, ref_id)
  WHERE entry_type = 'refund';
