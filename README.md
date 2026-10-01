# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款（可带收款凭据，待核销/已核销分离）、核销确认、撤销、冲正（退款）与按凭据冲正并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`，可选 `receipt_id`（调用方生成的非空字符串，作用域（租户，订单），与订单标识、冲正标识互不替代）；超过未收金额返回 409，凭据标识重复返回 409。成功返回 200 与订单的 `paid_cents`、`outstanding_cents`、`ledger`。
  - 不带 `receipt_id`：既有行为，收款直接参与结清判定。
  - 带 `receipt_id`：收款进入**待核销**，立即计入 `paid_cents` 与 `ledger`（凭据条目带 `receipt_id`、`receipt_status` 与余额快照），可继续用于收款与冲正的上限计算，但订单不因待核销收款结清。
- `POST /orders/{order_id}/receipts/{receipt_id}/confirm`：核销确认，待核销 → 已核销，订单按现有规则判定结清与否；无需请求体。重复确认返回首次结果、HTTP 200、`idempotent_replay=true`，不重复留痕、不改余额。
- `POST /orders/{order_id}/receipts/{receipt_id}/revoke`：撤销，整笔移除待核销收款：`paid_cents` 减少该金额并补一条 `receipt_revocation` 流水（含凭据标识与余额快照），只影响该笔；撤销后同名凭据可再次登记。已核销/已冲正凭据撤销返回 409 且数据不变；重复撤销返回首次结果、HTTP 200、`idempotent_replay=true`。
- `POST /orders/{order_id}/receipts/{receipt_id}/refund`：以凭据冲正。请求字段 `refund_id`（调用方生成的非空字符串）、`amount_cents`（正整数，须与凭据金额一致），沿用普通冲正的幂等、上限、原子性与 400/404/409 规则；只有已核销凭据可冲正，待核销或已撤销凭据直接冲正返回 409 且数据不变，冲正后凭据标记 `refunded`。
- 三个凭据操作均返回 `receipt_id`、`receipt_status`（`pending`/`confirmed`/`revoked`/`refunded`）与订单 `paid_cents`、`outstanding_cents`、`status`，以及 `created_at`、`idempotent_replay`；凭据不存在或跨租户返回 404，参数不合法返回 400。
- `POST /orders/{order_id}/refunds`：冲正（退款）。请求字段 `refund_id`（调用方生成的非空字符串）、`amount_cents`（正整数），租户经 `X-Tenant` 传入。
  - 成功返回 200：`refund_id`、`refunded_cents`、冲正后的 `paid_cents`、`outstanding_cents`、`created_at`、`idempotent_replay`。
  - 幂等：同一（租户，订单，`refund_id`）重试返回首次结果、HTTP 200、`idempotent_replay=true`，不重复减少已收金额；同一标识携带不同金额返回 409。
  - 冲正金额大于当前已收金额返回 409（`refund exceeds paid amount`），不留任何痕迹；订单不存在或跨租户返回 404；参数不合法返回 400。
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

# 带凭据收款：资金到账立即计入余额，但订单保持未结清（待核销）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"rc1","amount_cents":500,"currency":"CNY"}'
curl -s -X POST localhost:8000/orders/rc1/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"amount_cents":500,"receipt_id":"rcpt-1"}'
# 核销确认后才按规则结清；重复确认幂等回放首次结果
curl -s -X POST localhost:8000/orders/rc1/receipts/rcpt-1/confirm -H 'X-Tenant: t1'
# 已核销收款按凭据整笔冲正（金额须与凭据一致；refund_id 同样由调用方生成）
curl -s -X POST localhost:8000/orders/rc1/receipts/rcpt-1/refund -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"refund_id":"rf-2","amount_cents":500}'

# 撤销另一条待核销收款：仅待核销可撤销；释放未收并补撤销流水，同名凭据可再登记
curl -s -X POST localhost:8000/orders/rc1/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' -d '{"amount_cents":100,"receipt_id":"rcpt-2"}'
curl -s -X POST localhost:8000/orders/rc1/receipts/rcpt-2/revoke -H 'X-Tenant: t1'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 迁移文件位于 `migrations/`，按文件名顺序应用，均可重复执行：`001_init.sql`（订单表）、`002_refunds.sql`（收款/冲正流水表与冲正标识唯一索引）、`003_receipts.sql`（收款凭据表与只追加的凭据事件表）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 账务不变量

- `paid_cents` = 本订单生效收款之和（无凭据收款 + 未撤销的凭据收款）− 生效冲正之和，恒满足 `0 ≤ paid_cents ≤ amount_cents`；撤销流水（`receipt_revocation`）是待核销收款移除的减法留痕。
- 登记收款、冲正、核销确认与撤销均在单写事务内同时更新余额/状态与写流水，`BEGIN IMMEDIATE` 串行化并发写，失败整体回滚。
- 待核销收款立即计入 `paid_cents` 并参与收款/冲正上限计算，但订单在仍存在待核销收款时不结清；核销确认后按现有规则判定，已收等于订单金额时 `status=settled`，冲正或撤销后低于订单金额回到 `accepted`；`outstanding_cents = amount_cents − paid_cents`。
- 凭据状态机：`pending`（待核销）→ `confirmed`（已核销）→ `refunded`（已冲正）；`pending` 可 `revoked`（撤销，删除活行并在事件表留痕，同名凭据可再登记）。三个凭据操作的重复请求均幂等回放首次结果。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款按金额逐笔登记，未实现分期计划与主动对账。
