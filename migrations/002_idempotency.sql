-- 幂等请求记录：同一租户内同一请求标识只允许产生一次业务效果
CREATE TABLE IF NOT EXISTS idempotent_requests(
  tenant        TEXT NOT NULL,
  request_id    TEXT NOT NULL,
  scope         TEXT NOT NULL,           -- order_create / payment
  request_hash  TEXT NOT NULL,           -- 首次请求业务内容指纹
  order_id      TEXT,                    -- 关联订单标识（受理时即写入）
  status_code   INTEGER NOT NULL,        -- 首次执行业务得到的 HTTP 状态码
  response_json TEXT NOT NULL,           -- 首次响应快照，重复请求原样回放
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, request_id)
);
