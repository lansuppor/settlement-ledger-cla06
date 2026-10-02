-- 订单更正：只允许在尚未产生任何收款、冲正、核销业务事实时更正订单金额/币种/标识。
-- 更正可能改变订单标识，而旧标识在改名后会被释放、可能被后续新订单复用；
-- 因此为每张订单分配不可变的链路键 ledger_id（受理时写入），使同一单据的更正
-- 留痕在改名后仍可按当前标识读出，且不会串到复用了旧标识的新订单。
ALTER TABLE orders ADD COLUMN ledger_id TEXT NOT NULL DEFAULT '';

-- 回填历史订单：逐行写入互不相同的随机链路键
UPDATE orders SET ledger_id = lower(hex(randomblob(16))) WHERE ledger_id = '';

-- 更正流水：每一次更正（含被拒绝的更正）作为独立业务事实留痕，
-- 记录更正前后的内容与生效/拒绝结果，按发生顺序读出。
-- order_id 为发起更正时订单的当前标识；ledger_id 为该单据不可变的链路键。
CREATE TABLE IF NOT EXISTS correction_records(
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant              TEXT NOT NULL,
  order_id            TEXT NOT NULL,            -- 发起更正时订单的当前（更正前）标识
  ledger_id           TEXT NOT NULL,            -- 不可变链路键，改名后仍可串联同一单据
  before_order_id     TEXT NOT NULL,
  before_amount_cents INTEGER NOT NULL,
  before_currency     TEXT NOT NULL,
  after_order_id      TEXT NOT NULL,            -- 请求更正后的标识（被拒绝时也记录请求内容）
  after_amount_cents  INTEGER NOT NULL,
  after_currency      TEXT NOT NULL,
  result              TEXT NOT NULL,            -- applied=已生效；rejected=被拒绝且订单未改变
  reject_reason       TEXT,                     -- rejected 时的拒绝原因
  request_id          TEXT,                     -- 幂等请求标识（未携带时为 NULL）
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_correction_records_ledger
  ON correction_records(tenant, ledger_id, id);
