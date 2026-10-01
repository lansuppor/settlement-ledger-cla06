# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款（可选收款凭据：待核销 → 核销确认/撤销）、冲正（退款，含按凭据冲正）并核对未收金额；资金到账与结算确认分开留痕。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`

## 启动

- `python3 -m app.entry --port 8000`（启动即执行 migrations 目录下的全部迁移，可重复执行）
- 仅执行迁移：`python3 -m app.entry --migrate`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。返回体含 `ledger` 收款流水（登记收款与冲正按发生顺序，含每笔后余额快照），可解释 `paid_cents` 的由来。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`，可选 `receipt_id`（调用方生成的非空字符串，作用域为（租户，订单），与订单标识、冲正标识互不替代）。超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`、`status`、`ledger`。
  - 不带 `receipt_id`：既有行为，收足即结清。
  - 带 `receipt_id`：收款进入**待核销**，立即计入 `paid_cents` 与 ledger（凭据条目带 `receipt_id`、`receipt_status` 与余额快照），可继续用于收款与冲正的上限计算，但存在待核销收款时订单不因收足而结清；同名生效凭据重复登记返回 409 且不留痕。
- `POST /orders/{order_id}/refunds`：冲正（退款）。请求字段 `refund_id`（调用方生成的非空字符串）、`amount_cents`（正整数），租户经 `X-Tenant` 传入。
  - 成功返回 200：`refund_id`、`refunded_cents`、冲正后的 `paid_cents`、`outstanding_cents`、`created_at`、`idempotent_replay`。
  - 幂等：同一（租户，订单，`refund_id`）重试返回首次结果、HTTP 200、`idempotent_replay=true`，不重复减少已收金额；同一标识携带不同金额返回 409。
  - 冲正金额大于当前已收金额返回 409（`refund exceeds paid amount`），不留任何痕迹；订单不存在或跨租户返回 404；参数不合法返回 400。
- `POST /orders/{order_id}/receipts/{receipt_id}/confirm`：**核销确认**。待核销收款转已核销，订单按现有规则判定结清。返回 `receipt_id`、`receipt_status`、`paid_cents`、`outstanding_cents`、`status`、`idempotent_replay`；重复确认 HTTP 200、`idempotent_replay=true`，回放首次结果，不重复留痕、不改余额。凭据不存在（含已撤销且未再登记）或跨租户返回 404；已冲正凭据返回 409。
- `POST /orders/{order_id}/receipts/{receipt_id}/revoke`：**撤销**。整笔移除待核销收款：`paid_cents` 减少该金额并补一条 `revocation` 流水（含凭据标识与余额快照），只影响该笔；撤销后同名凭据可再次登记。已核销或已冲正凭据返回 409 且数据不变；重复撤销 HTTP 200、`idempotent_replay=true`，回放首次结果；凭据不存在或跨租户返回 404。
- `POST /orders/{order_id}/receipts/{receipt_id}/refunds`：**以凭据冲正**。请求字段 `refund_id`（调用方生成）、`amount_cents`，沿用普通冲正的幂等、上限、原子性与 400/404/409 规则；只有**已核销**收款可冲正，待核销或已撤销返回 409 且数据不变；冲正后凭据标记 `refunded`。返回凭据标识、凭据状态、冲正标识与订单的 `paid_cents`、`outstanding_cents`、`status`、`idempotent_replay`。
- `GET /health`：返回服务与数据库状态。

### 调用示例

```bash
# 受理并收足
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"o9","amount_cents":500,"currency":"CNY"}'
curl -s -X POST localhost:8000/orders/o9/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"amount_cents":500}'
# 冲正 300（refund_id 由调用方生成，重试时原样重发即可）
curl -s -X POST localhost:8000/orders/o9/refunds -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"refund_id":"rf-1","amount_cents":300}'
# 查看含流水的订单：paid_cents 恒等于流水中 payment 之和 − refund 之和
curl -s localhost:8000/orders/o9 -H 'X-Tenant: t1'

# 收款凭据：待核销登记 → 核销确认（资金到账与结算确认分开留痕）
curl -s -X POST localhost:8000/orders/o9/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"amount_cents":100,"receipt_id":"rc-1"}'
curl -s -X POST localhost:8000/orders/o9/receipts/rc-1/confirm -H 'X-Tenant: t1'
# 已核销收款按凭据冲正（refund_id 仍由调用方生成，重试原样重发）
curl -s -X POST localhost:8000/orders/o9/receipts/rc-1/refunds -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"refund_id":"rf-2","amount_cents":100}'
# 另一笔待核销收款可整笔撤销（释放未收金额、补撤销流水），撤销后同名凭据可再次登记
curl -s -X POST localhost:8000/orders/o9/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"amount_cents":100,"receipt_id":"rc-2"}'
curl -s -X POST localhost:8000/orders/o9/receipts/rc-2/revoke -H 'X-Tenant: t1'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 迁移文件位于 `migrations/`，按文件名顺序应用，记录在 `schema_migrations` 表中（每文件仅应用一次）：`001_init.sql`（订单表）、`002_refunds.sql`（收款/冲正流水表与冲正标识唯一索引）、`003_receipts.sql`（收款凭据表、凭据字段与生效凭据唯一索引）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 账务不变量

- `paid_cents` = 本订单生效收款之和 − 生效冲正之和 − 生效撤销之和，恒满足 `0 ≤ paid_cents ≤ amount_cents`。
- 待核销（pending）收款立即计入 `paid_cents`，可用于收款与冲正的上限计算，但在全部待核销收款核销确认前订单不结清；已收等于订单金额且无待核销收款时 `status=settled`，冲正或撤销后低于订单金额回到 `accepted`；`outstanding_cents = amount_cents − paid_cents`。
- 凭据生命周期：`pending`（待核销）→ 核销确认 `confirmed` → 以凭据冲正 `refunded`；待核销可撤销为 `revoked`（整笔移除、允许同名再登记）；每个状态迁移与余额、订单状态、流水在同一 `BEGIN IMMEDIATE` 事务内提交，失败整体回滚。
- 登记收款、冲正、撤销与核销均在单写事务内同时更新余额与写流水，串行化并发写。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款按金额逐笔登记，未实现分期计划与主动对账。
