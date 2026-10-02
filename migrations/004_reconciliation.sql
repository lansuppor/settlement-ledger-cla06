-- 核销流水：每笔生效核销作为独立业务事实留痕，记录金额与结果，
-- 便于解释任意时点订单已核销金额（reconciled_cents）的构成
CREATE TABLE IF NOT EXISTS reconciliation_records(
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant           TEXT NOT NULL,
  order_id         TEXT NOT NULL,
  amount_cents     INTEGER NOT NULL,          -- 本次核销金额
  result           TEXT NOT NULL,             -- applied=已核销
  reconciled_after INTEGER NOT NULL,          -- 核销后的累计已核销金额
  request_id       TEXT,                      -- 幂等请求标识（未携带时为 NULL）
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_records_order
  ON reconciliation_records(tenant, order_id, id);
