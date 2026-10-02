-- 收款冲正：独立的业务事实留痕；订单增加已核销金额下限（当前无核销能力，恒为 0）
ALTER TABLE orders ADD COLUMN written_off_cents INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS reversals(
  tenant                 TEXT NOT NULL,
  order_id               TEXT NOT NULL,
  reversal_seq           INTEGER NOT NULL,   -- 订单内冲正序号，从 1 递增
  amount_cents           INTEGER NOT NULL,   -- 本次冲正金额
  paid_after_cents       INTEGER NOT NULL,   -- 冲正后的已收金额
  outstanding_after_cents INTEGER NOT NULL,  -- 冲正后的未收金额
  request_id             TEXT,               -- 幂等请求标识（未携带时为 NULL）
  created_at             TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, order_id, reversal_seq)
);
