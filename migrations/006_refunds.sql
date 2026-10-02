-- 退款流水：每笔退款（含被拒绝的结论）作为独立业务事实留痕，记录金额、退款原因与结果
-- 退款是真实出账退钱：生效后已收金额减少、未收金额相应增加；
-- 任意时刻已收 = 收款留痕合计 − 已生效冲正合计 − 已生效退款合计，且不低于已核销、不为负
CREATE TABLE IF NOT EXISTS refund_records(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant        TEXT NOT NULL,
  order_id      TEXT NOT NULL,
  amount_cents  INTEGER NOT NULL,          -- 请求退款的金额
  reason        TEXT NOT NULL,             -- 调用方提交的退款原因
  result        TEXT NOT NULL,             -- applied=已退款；rejected=被拒绝且未改变金额
  reject_reason TEXT,                      -- rejected 时的具体拒绝原因
  paid_after    INTEGER,                   -- applied 后订单已收金额；rejected 为 NULL
  request_id    TEXT,                      -- 幂等请求标识（未携带时为 NULL）
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_refund_records_order
  ON refund_records(tenant, order_id, id);
