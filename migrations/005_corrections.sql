-- 订单更正留痕：每次对已受理订单发起的更正（含被拒绝的结论）作为独立业务事实留痕
-- order_id 为该结论可读的订单：生效更正后订单按新标识生效，记录挂在更正后的订单标识上；
-- 被拒绝的更正不改变订单，记录挂在原目标订单标识上。
-- before_*/after_* 记录更正前后的订单标识、金额与币种，reject_reason 给出被拒绝的具体原因。
CREATE TABLE IF NOT EXISTS correction_records(
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant               TEXT NOT NULL,
  order_id             TEXT NOT NULL,           -- 结论所属订单标识（生效=更正后标识；拒绝=原标识）
  before_order_id      TEXT NOT NULL,
  before_amount_cents  INTEGER NOT NULL,
  before_currency      TEXT NOT NULL,
  after_order_id       TEXT NOT NULL,
  after_amount_cents   INTEGER NOT NULL,
  after_currency       TEXT NOT NULL,
  result               TEXT NOT NULL,           -- applied=已更正；rejected=被拒绝且订单不变
  reject_reason        TEXT,                    -- rejected 时的具体拒绝原因
  request_id           TEXT,                    -- 幂等请求标识（未携带时为 NULL）
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_correction_records_order
  ON correction_records(tenant, order_id, id);
