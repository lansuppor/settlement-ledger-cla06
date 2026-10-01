-- 收款凭据：带凭据的收款先进入待核销（pending），核销确认后转为已核销（confirmed）；
-- 撤销（revoke）整笔移除待核销收款：删除活凭据行、在 payment_receipt_events 留撤销事件，
-- 并在 ledger_entries 补一条 receipt_revocation 流水；撤销后同名凭据可再次登记。
-- 已核销收款可按凭据整笔冲正，冲正后凭据状态为 refunded。
-- 凭据标识作用域为（租户，订单），与订单标识、冲正标识互不替代。
CREATE TABLE IF NOT EXISTS payment_receipts(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  status TEXT NOT NULL,                     -- 'pending'（待核销）/ 'confirmed'（已核销）/ 'refunded'（已冲正）
  payment_seq INTEGER NOT NULL,             -- 对应的登记收款流水序号（ledger_entries.entry_seq）
  refund_id TEXT,                           -- 凭据冲正标识，仅 status='refunded' 时填写
  confirmed_at TEXT,                        -- 首次核销确认时间（重复确认回放用）
  confirmed_paid_cents INTEGER,             -- 首次确认时的 paid_cents 快照（重复确认回放用）
  confirmed_status TEXT,                    -- 首次确认时的订单状态快照（重复确认回放用）
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, order_id, receipt_id)
);

-- 只追加的凭据事件表：撤销删除活行后，仍能回放“首次撤销结果”并标识旧收款条目的最终状态。
-- payment_seq 指向被撤销那一代凭据的收款流水；同名凭据再登记会产生新的 payment_seq。
CREATE TABLE IF NOT EXISTS payment_receipt_events(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  payment_seq INTEGER NOT NULL,
  event TEXT NOT NULL,                      -- 当前仅有 'revoked'
  amount_cents INTEGER NOT NULL,
  paid_after_cents INTEGER NOT NULL,        -- 撤销后的 paid_cents 快照
  status_after TEXT NOT NULL,               -- 撤销后的订单状态快照
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, payment_seq, event)
);

-- ledger_entries.ref_id 的约定扩展（不加列、迁移可重复执行）：
--   entry_type='payment'            带凭据收款时 ref_id 为凭据标识，无凭据收款仍为 NULL
--   entry_type='refund'             ref_id 为冲正标识（既有行为不变）
--   entry_type='receipt_revocation' ref_id 为被撤销的凭据标识，amount_cents 为撤销金额
-- 冲正标识唯一索引（002 迁移）只作用于 refund 行，其余类型不参与唯一约束。
