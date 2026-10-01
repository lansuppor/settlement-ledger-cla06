-- 收款凭据：带凭据的收款先进入待核销（pending），核销确认后为 confirmed，
-- 以凭据冲正后为 refunded；撤销（revoked）整笔移除并允许同名凭据再次登记。
-- ledger_entries 增列：凭据收款行与其冲正/撤销流水通过 receipt_id 关联；
-- receipt_status 记录凭据收款行随生命周期变化的状态（不新增流水行）；
-- order_status_after 为该笔流水后订单状态快照（幂等回放首次结果用）。
ALTER TABLE ledger_entries ADD COLUMN receipt_id TEXT;
ALTER TABLE ledger_entries ADD COLUMN receipt_status TEXT;
ALTER TABLE ledger_entries ADD COLUMN order_status_after TEXT;

CREATE TABLE IF NOT EXISTS payment_receipts(
  id INTEGER PRIMARY KEY,
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL,                    -- 调用方生成，作用域（租户，订单）
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  status TEXT NOT NULL CHECK(status IN ('pending','confirmed','refunded','revoked')),
  payment_seq INTEGER NOT NULL,                -- 凭据收款流水序号
  revocation_seq INTEGER,                      -- 撤销流水序号
  confirmed_paid_cents INTEGER,                -- 首次确认时 paid 快照（重复确认回放）
  confirmed_status TEXT,                       -- 首次确认时订单状态快照
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  confirmed_at TEXT,
  revoked_at TEXT,
  FOREIGN KEY(tenant, order_id, payment_seq) REFERENCES ledger_entries(tenant, order_id, entry_seq)
);

-- 同名凭据至多有一个“生效中”的代次；已撤销代次不参与唯一约束，故撤销后可再次登记。
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_active
  ON payment_receipts(tenant, order_id, receipt_id)
  WHERE status IN ('pending','confirmed','refunded');
