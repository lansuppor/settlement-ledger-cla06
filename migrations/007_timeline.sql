-- 业务时间线：一张订单从受理至今的全部业务事实合并留痕
-- 每个业务环节（受理/收款/冲正/核销/更正/退款）的每次结论（生效或被拒绝）
-- 在与业务事实相同的写事务内追加一条时间线事件；全局自增 id 跨环节唯一，
-- 按 id 排序即业务发生顺序，重复读取、并发读取与重启后顺序与内容均不变。
-- 同一次业务请求至多产生一条事件：幂等回放不新增事件。
CREATE TABLE IF NOT EXISTS timeline_events(
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant               TEXT NOT NULL,
  order_id             TEXT NOT NULL,           -- 事件所属订单标识（更正改名时随订单迁移）
  stage                TEXT NOT NULL,           -- acceptance/payment/reversal/reconciliation/correction/refund
  result               TEXT NOT NULL,           -- applied=生效；rejected=被拒绝且未改变金额
  amount_cents         INTEGER,                 -- 本次请求金额（受理为订单金额；更正见 before/after）
  paid_after           INTEGER,                      -- 该结论生效后的累计已收（受理为 0；rejected 为 NULL）
  reconciled_after     INTEGER,                 -- 该结论生效后的累计已核销（受理为 0；rejected 为 NULL）
  reject_reason        TEXT,                    -- rejected 时的具体拒绝原因
  reason               TEXT,                    -- 退款原因（仅退款环节）
  before_order_id      TEXT,                    -- 更正前内容（仅更正环节）
  before_amount_cents  INTEGER,
  before_currency      TEXT,
  after_order_id       TEXT,                    -- 更正后内容（仅更正环节）
  after_amount_cents   INTEGER,
  after_currency       TEXT,
  request_id           TEXT,                    -- 幂等请求标识（未携带时为 NULL）
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_timeline_events_order
  ON timeline_events(tenant, order_id, id);
