-- 已核销金额：冲正后已收金额不得低于该下限（当前没有核销业务，恒为 0；预留以支撑规则）
ALTER TABLE orders ADD COLUMN reconciled_cents INTEGER NOT NULL DEFAULT 0;

-- 收款流水：每笔生效收款独立留痕，便于解释任意时点已收/未收金额的构成
CREATE TABLE IF NOT EXISTS payment_records(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant       TEXT NOT NULL,
  order_id     TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,           -- 本次收款金额
  paid_after   INTEGER NOT NULL,           -- 收款后的累计已收金额
  request_id   TEXT,                       -- 幂等请求标识（未携带时为 NULL）
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_payment_records_order
  ON payment_records(tenant, order_id, id);

-- 冲正流水：每笔冲正作为独立业务事实留痕，记录金额与结果（applied/rejected）
CREATE TABLE IF NOT EXISTS reversal_records(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant        TEXT NOT NULL,
  order_id      TEXT NOT NULL,
  amount_cents  INTEGER NOT NULL,          -- 请求冲正的金额
  result        TEXT NOT NULL,             -- applied=已冲正；rejected=被拒绝且未改变金额
  reject_reason TEXT,                     -- rejected 时的拒绝原因
  paid_after    INTEGER,                   -- applied 后订单已收金额；rejected 为 NULL
  request_id    TEXT,                      -- 幂等请求标识（未携带时为 NULL）
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reversal_records_order
  ON reversal_records(tenant, order_id, id);
