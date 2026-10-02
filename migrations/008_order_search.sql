-- 订单条件检索：以受理顺序作为租户内稳定排序
-- accept_seq 取该订单受理条目在业务时间线中的全库单调序号（timeline_events.id）：
-- 受理顺序在业务时间线上恒为全序，批量导入的多张订单按导入行先后得到不同序号；
-- 订单只增不删、更正只在原行改名改金额，故 accept_seq 一经写入永不改变，
-- 可作为 keyset 分页的稳定游标位置，不受其他订单收款/冲正/核销/更正/退款影响。
ALTER TABLE orders ADD COLUMN accept_seq INTEGER;

-- 既有数据回填：每张订单的受理条目（stage='acceptance'）唯一且恒为该订单最早的事实。
UPDATE orders SET accept_seq = (
  SELECT te.id FROM timeline_events te
  WHERE te.tenant = orders.tenant
    AND te.order_id = orders.order_id
    AND te.stage = 'acceptance'
);

-- 租户内受理序号唯一：分页按 (tenant, accept_seq) 做 keyset 检索。
-- 新受理订单在 insert_conn 内随受理时间线条目写入 accept_seq，不留 NULL。
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_tenant_accept_seq
  ON orders(tenant, accept_seq);

-- 服务端键值元信息：存放游标令牌签名密钥等实例数据，重启后继续生效。
CREATE TABLE IF NOT EXISTS app_meta(
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 游标 HMAC 密钥：每个库一份随机密钥，只存库内不下发；
-- 令牌为该密钥对（租户、筛选条件、分页位置）计算的 HMAC，游标本身不暴露内部位置。
INSERT OR IGNORE INTO app_meta(key, value)
VALUES ('order_search_cursor_secret', lower(hex(randomblob(32))));

-- 已签发的检索游标：令牌为主键，记录其绑定的租户、筛选条件与分页位置。
-- 游标为服务端不透明令牌（HMAC 摘要，hex），不含任何数据库内部位置或跨租户地址；
-- 同一（租户、筛选、位置）经 INSERT OR IGNORE 复用同一行，重复查询/重启后均可重放。
CREATE TABLE IF NOT EXISTS order_search_cursors(
  token            TEXT PRIMARY KEY,
  tenant           TEXT NOT NULL,
  status_filter    TEXT NOT NULL DEFAULT '',   -- ''=不限；accepted/settled
  currency_filter  TEXT NOT NULL DEFAULT '',   -- ''=不限
  min_amount       INTEGER,                    -- NULL=不限
  max_amount       INTEGER,                    -- NULL=不限
  after_accept_seq INTEGER NOT NULL,           -- 下一页起点（不含该位置）
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
