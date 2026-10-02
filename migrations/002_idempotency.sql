-- 幂等请求登记表：同一租户内同一请求标识只产生一次业务效果。
-- 仅登记首次执行成功的请求；冲突请求（同标识、不同内容）不登记，
-- 但其内容与首次不同这一结论由已登记行的 fingerprint 持久保证，重启后一致。
CREATE TABLE IF NOT EXISTS idempotency_keys(
  tenant        TEXT    NOT NULL,
  request_id    TEXT    NOT NULL,
  endpoint      TEXT    NOT NULL,            -- 业务端点，防止同一标识串用到不同业务
  fingerprint   TEXT    NOT NULL,            -- 首次请求业务内容的规范化摘要
  order_id      TEXT,                        -- 受理订单时关联的订单标识
  response_code INTEGER NOT NULL,            -- 首次响应状态码（201/200）
  response_body TEXT    NOT NULL,            -- 首次响应体快照（JSON），重复请求原样返回
  created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, request_id)
);
