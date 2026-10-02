# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额，并为受理与收款提供基于请求标识的幂等能力；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11+
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`

## 启动

- `python3 -m app.entry --port 8000`（启动时自动执行 `migrations/` 下全部迁移）
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`
- 幂等能力端到端演示（自动选空闲端口、自动建库、自动重启）：`bash scripts/demo_idempotency.sh`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `GET /health`：返回服务与数据库状态。

## 幂等能力（网络重试 / 重复提交安全）

调用方在受理订单或登记收款时携带请求头 `Idempotency-Key: <调用方生成的唯一标识>` 即可获得幂等保证。作用域为**同一租户内**：同一 `(X-Tenant, Idempotency-Key)` 视为同一次业务请求。

行为约定：

| 情形 | 结果 |
| --- | --- |
| 首次提交 | 正常执行业务，受理返回 201 / 收款返回 200 |
| 同标识、同业务内容重复提交（含网络重试、并发） | 不再次执行业务，返回**首次响应的状态码与响应体**，并带响应头 `Idempotent-Replayed: true` |
| 同标识、不同业务内容（金额、币种、收款金额不同，或把同一标识用于另一个接口） | 返回 409 拒绝，不写入、不改动任何数据；重复提交冲突请求结论一致 |
| 不同租户使用相同标识 | 互不影响，各自在本租户内去重 |
| 服务重启后 | 已成功请求仍按重复请求重放；曾被拒绝的冲突请求结论不变，不会被误判为新请求 |

实现要点：首次请求在单个 `BEGIN IMMEDIATE` 事务内完成“查重 → 业务写入 → 登记指纹与首次响应快照”，登记行与业务数据同事务提交并持久化在 `idempotency_keys` 表中；SQLite 写锁串行化保证并发提交同一标识时只有一次请求真正产生业务效果。

### 调用示例

```bash
# 受理订单并携带请求标识
curl -i -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-20261002-0001' \
  -d '{"tenant":"tenant-a","order_id":"o1","amount_cents":500,"currency":"CNY"}'

# 网络重试：同样的请求再发任意次，响应体与首次完全一致，且只有一笔受理
curl -i -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-20261002-0001' \
  -d '{"tenant":"tenant-a","order_id":"o1","amount_cents":500,"currency":"CNY"}'

# 同标识但金额不同 -> 409，已有订单金额不变
curl -i -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-20261002-0001' \
  -d '{"tenant":"tenant-a","order_id":"o1","amount_cents":999,"currency":"CNY"}'

# 登记收款并携带请求标识；重复提交不会重复累加 paid_cents
curl -i -X POST http://127.0.0.1:8000/orders/o1/payments \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: pay-20261002-0001' -d '{"amount_cents":200}'
```

### 兼容性

- 不携带 `Idempotency-Key` 的请求与旧版行为完全一致：同一租户重复受理仍返回 409，收款超过未收金额仍返回 409，跨租户读取仍按不存在（404）处理。
- 金额仍为最小货币单位的整数，未收金额 = 订单金额 − 已收金额。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。
- 表结构：`orders`（订单）、`idempotency_keys`（请求标识登记：租户、标识、业务端点、内容指纹、首次响应快照）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优；幂等的并发唯一生效依赖 SQLite 单库写锁。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期、退款与对账。
- 幂等登记长期保留（无清理/过期策略）。
