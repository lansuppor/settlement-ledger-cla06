# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额、收款冲正并留痕；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

受理订单、登记收款与收款冲正支持基于请求标识（`Idempotency-Key` 请求头）的幂等：网络重试、重复点击、并发重复提交都不会造成重复受理、重复收款或重复冲正。

## 环境与安装

- Python 3.11+
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`

## 启动

- 启动时会自动执行 `migrations/` 下的建表迁移（含幂等记录表）：
  `python3 -m app.entry --port 8000`
- 只做迁移不启动：`python3 -m app.entry --migrate`
- 健康检查：`GET /health`

## 快速调用示例

```bash
# 1) 携带 Idempotency-Key 受理订单
curl -s -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: req-1001' \
  -d '{"tenant":"tenant-a","order_id":"ord-1","amount_cents":500,"currency":"CNY"}'

# 2) 用完全相同的请求体与请求标识再提交任意次 —— 返回与首次完全一致的订单，不产生第二笔受理
curl -s -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: req-1001' \
  -d '{"tenant":"tenant-a","order_id":"ord-1","amount_cents":500,"currency":"CNY"}'

# 3) 同一请求标识但金额/币种与首次不同 —— 422 拒绝，已有数据不变
curl -i -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: req-1001' \
  -d '{"tenant":"tenant-a","order_id":"ord-1","amount_cents":999,"currency":"CNY"}'

# 4) 按标识读取订单
curl -s http://127.0.0.1:8000/orders/ord-1 -H 'X-Tenant: tenant-a'

# 5) 携带 Idempotency-Key 登记收款（租户仍通过 X-Tenant 传入）
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/payments \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: pay-1001' \
  -d '{"amount_cents":200}'

# 6) 同标识重复收款 —— 回放首次结果，paid_cents 不重复累加
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/payments \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: pay-1001' \
  -d '{"amount_cents":200}'

# 7) 冲正已登记的收款：已收减少、未收增加，订单回到未收清状态
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/reversals \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: rev-1001' \
  -d '{"amount_cents":50}'

# 8) 同标识重复冲正 —— 回放首次结果，paid_cents 不重复冲减
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/reversals \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: rev-1001' \
  -d '{"amount_cents":50}'

# 9) 读取订单的冲正留痕（每笔冲正的金额与冲正后的已收/未收）
curl -s http://127.0.0.1:8000/orders/ord-1/reversals -H 'X-Tenant: tenant-a'

# 10) 非法冲正：金额超过当前已收返回 409，订单不存在/跨租户返回 404，金额小于等于零返回 422
curl -i -X POST http://127.0.0.1:8000/orders/ord-1/reversals \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -d '{"amount_cents":999999}'
```

重启服务后再次提交上述相同请求，结论与重启前一致：重复请求仍回放首次结果，冲突请求仍被拒绝。

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。
  - 成功返回 201 与订单对象；参数不合法返回 400。
  - 携带 `Idempotency-Key` 时：同租户同标识的重复提交返回与首次一致的状态码与响应体，不重复受理；标识相同但业务内容（订单标识、金额、币种）或操作类型不同返回 422 且不改变任何数据。
  - 不携带 `Idempotency-Key` 时：同一租户重复受理返回 409（原有行为不变）。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`。
  - 携带 `Idempotency-Key` 时：同租户同标识的重复提交回放首次结果（含首次的 200/409/404 结论），收款金额不重复累加；标识相同但收款金额或目标订单不同返回 422 且不登记收款。
  - 不携带 `Idempotency-Key` 时：超过未收金额返回 409、订单不存在返回 404（原有行为不变）。
  - 成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /orders/{order_id}/reversals`：收款冲正，纠正已登记的收款。请求字段 `amount_cents`。
  - 成功返回 200：订单 `paid_cents` 减少、`outstanding_cents` 相应增加（两者之和始终等于订单金额），订单回到未收清状态；响应中附带本次冲正留痕 `reversal`（冲正序号、金额、冲正后的已收/未收）。
  - 拒绝且不改变任何数据：金额小于等于零返回 422（参数校验）；超过当前已收金额（扣除已核销部分）返回 409；订单不存在或跨租户返回 404。
  - 携带 `Idempotency-Key` 时：同租户同标识的重复提交回放首次结果，不重复冲减；标识相同但金额、目标订单或操作类型不同返回 422。
- `GET /orders/{order_id}/reversals`：按订单读出全部冲正留痕（按冲正序号升序）；订单不存在或跨租户返回 404。
- `GET /health`：返回服务与数据库状态。

幂等作用域为「租户 + 请求标识」：不同租户使用相同请求标识互不影响；幂等记录持久化在数据库中，服务重启后继续有效。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入；同标识并发通过 SQLite `BEGIN IMMEDIATE` 写事务串行化，库级锁保证只有一次请求产生业务效果。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期、退款与对账；已核销金额字段（`written_off_cents`）已落库并参与冲正下限校验，但尚无核销操作入口（恒为 0）。
