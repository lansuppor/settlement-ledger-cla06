-- 业务时间线：一张订单从受理至今的全部业务事实合并留痕
-- 每个到达既有订单的业务请求（受理/收款/冲正/核销/更正/退款）至多追加一条：
-- 受理与收款只记录生效结论；冲正、核销、更正、退款同时记录被拒绝结论（含拒绝原因）。
-- id 为全库单调序号：同一订单内按 id 排列即业务发生顺序，跨环节可稳定排序与重放，
-- 不依赖各留痕表各自独立的时间字段；内容持久化，服务重启后顺序与内容不变。
CREATE TABLE IF NOT EXISTS timeline_events(
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant              TEXT NOT NULL,
  order_id            TEXT NOT NULL,
  stage               TEXT NOT NULL,        -- acceptance/payment/reversal/reconciliation/correction/refund
  result              TEXT NOT NULL,        -- applied=生效；rejected=被拒绝且未改变金额
  amount_cents        INTEGER,              -- 本环节金额（受理=订单金额；更正环节为 NULL）
  currency            TEXT,                 -- 受理环节的订单币种
  paid_after          INTEGER,              -- 收款/冲正/退款生效后的累计已收；其余为 NULL
  reconciled_after    INTEGER,              -- 核销生效后的累计已核销；其余为 NULL
  reason              TEXT,                 -- 退款原因（退款环节）
  reject_reason       TEXT,                 -- 被拒绝时的具体原因
  before_order_id     TEXT,                 -- 更正前内容（更正环节）
  before_amount_cents INTEGER,
  before_currency     TEXT,
  after_order_id      TEXT,                 -- 更正后内容（更正环节）
  after_amount_cents  INTEGER,
  after_currency      TEXT,
  request_id          TEXT,                 -- 幂等请求标识（未携带时为 NULL）
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_timeline_events_order
  ON timeline_events(tenant, order_id, id);

-- 既有数据回填：受理事实恒为订单首条（任何业务事实都以订单存在为前提），
-- 其余环节按各自留痕的时间与表内序号合并；同一订单内相对顺序确定，
-- 回填结果不随执行时刻变化（幂等迁移，只执行一次）。
INSERT INTO timeline_events(
  tenant, order_id, stage, result, amount_cents, currency, paid_after, reconciled_after,
  reason, reject_reason, before_order_id, before_amount_cents, before_currency,
  after_order_id, after_amount_cents, after_currency, request_id, created_at
)
SELECT tenant, order_id, stage, result, amount_cents, currency, paid_after, reconciled_after,
       reason, reject_reason, before_order_id, before_amount_cents, before_currency,
       after_order_id, after_amount_cents, after_currency, request_id, created_at
FROM (
  SELECT o.tenant AS tenant, o.order_id AS order_id, 'acceptance' AS stage, 'applied' AS result,
         -- 受理时的订单内容：若发生过生效更正，orders 行已是更正后内容，
         -- 取该订单最早一条更正留痕的更正前内容（即受理时内容），否则取当前订单内容
         COALESCE((SELECT cr.before_amount_cents FROM correction_records cr
                    WHERE cr.tenant=o.tenant AND cr.order_id=o.order_id
                    ORDER BY cr.id LIMIT 1), o.amount_cents) AS amount_cents,
         COALESCE((SELECT cr.before_currency FROM correction_records cr
                    WHERE cr.tenant=o.tenant AND cr.order_id=o.order_id
                    ORDER BY cr.id LIMIT 1), o.currency) AS currency,
         NULL AS paid_after, NULL AS reconciled_after, NULL AS reason, NULL AS reject_reason,
         NULL AS before_order_id, NULL AS before_amount_cents, NULL AS before_currency,
         NULL AS after_order_id, NULL AS after_amount_cents, NULL AS after_currency,
         (SELECT ir.request_id FROM idempotent_requests ir
           WHERE ir.tenant=o.tenant AND ir.order_id=o.order_id
             AND ir.scope='order_create' AND ir.status_code=201
           ORDER BY ir.created_at, ir.request_id LIMIT 1) AS request_id,
         COALESCE((SELECT MIN(ir2.created_at) FROM idempotent_requests ir2
           WHERE ir2.tenant=o.tenant AND ir2.order_id=o.order_id
             AND ir2.scope='order_create' AND ir2.status_code=201),
           datetime('now')) AS created_at,
         0 AS stage_rank, 0 AS ref_id
  FROM orders o
  UNION ALL
  SELECT tenant, order_id, 'payment', 'applied', amount_cents, NULL, paid_after, NULL,
         NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, request_id, created_at, 1, id
  FROM payment_records
  UNION ALL
  SELECT tenant, order_id, 'reversal', result, amount_cents, NULL, paid_after, NULL,
         NULL, reject_reason, NULL, NULL, NULL, NULL, NULL, NULL, request_id, created_at, 2, id
  FROM reversal_records
  UNION ALL
  SELECT tenant, order_id, 'reconciliation', result, amount_cents, NULL, NULL, reconciled_after,
         NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, request_id, created_at, 3, id
  FROM reconciliation_records
  UNION ALL
  SELECT tenant, order_id, 'correction', result, NULL, NULL, NULL, NULL,
         NULL, reject_reason, before_order_id, before_amount_cents, before_currency,
         after_order_id, after_amount_cents, after_currency, request_id, created_at, 4, id
  FROM correction_records
  UNION ALL
  SELECT tenant, order_id, 'refund', result, amount_cents, NULL, paid_after, NULL,
         reason, reject_reason, NULL, NULL, NULL, NULL, NULL, NULL, request_id, created_at, 5, id
  FROM refund_records
)
ORDER BY tenant, order_id,
         CASE WHEN stage='acceptance' THEN 0 ELSE 1 END,
         created_at, stage_rank, ref_id;
