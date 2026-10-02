# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、批量导入订单（逗号分隔文本按行受理）、登记收款并核对未收金额、对已登记收款进行冲正、对收款进行对账核销；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

受理订单、登记收款、收款冲正与核销支持基于请求标识（`Idempotency-Key` 请求头）的幂等：网络重试、重复点击、并发重复提交都不会造成重复受理、重复收款、重复冲正或重复核销。

## 环境与安装

- Python 3.11+
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`

## 启动

- 启动时会自动执行 `migrations/` 下的建表迁移（按版本记录，只执行一次；含幂等记录表与收款/冲正/核销留痕表）：
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

# 7) 携带 Idempotency-Key 冲正已登记的收款：已收减少、未收增加，订单回到未收清
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/reversals \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: rev-1001' \
  -d '{"amount_cents":150}'
# => paid_cents=50, outstanding_cents=450, status=accepted

# 8) 同标识重复冲正 —— 回放首次结果，不重复减少已收；换金额/订单/操作类型则 422
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/reversals \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: rev-1001' \
  -d '{"amount_cents":150}'

# 9) 非法冲正金额 —— 409 且原因可区分：<=0 / 超过当前已收 / 低于已核销金额
curl -i -X POST http://127.0.0.1:8000/orders/ord-1/reversals \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -d '{"amount_cents":999}'
#   订单不存在（含跨租户）为 404；幂等标识被用于不同请求内容为 422

# 10) 按订单读出每笔冲正的金额与结果（按发生顺序）
curl -s http://127.0.0.1:8000/orders/ord-1/reversals -H 'X-Tenant: tenant-a'

# 11) 冲正后仍有未收金额时可以继续收款，收清后重新进入 settled
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/payments \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: pay-1002' \
  -d '{"amount_cents":450}'
# => paid_cents=500, outstanding_cents=0, status=settled

# 12) 携带 Idempotency-Key 核销已确认收款：已核销金额累加，已收/未收/订单金额不变
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/reconciliations \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: rec-1001' \
  -d '{"amount_cents":300}'
# => reconciled_cents=300, paid_cents=500, outstanding_cents=0

# 13) 同标识重复核销 —— 回放首次结果，不重复累加；换金额/订单/操作类型则 422
curl -s -X POST http://127.0.0.1:8000/orders/ord-1/reconciliations \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: rec-1001' \
  -d '{"amount_cents":300}'

# 14) 非法核销 —— 409 且原因可区分：<=0 / 累计核销超过当前已收
curl -i -X POST http://127.0.0.1:8000/orders/ord-1/reconciliations \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -d '{"amount_cents":999}'
#   订单不存在（含跨租户）为 404；幂等标识被用于不同请求内容为 422

# 15) 按订单读出每笔核销的金额与结果（按发生顺序）
curl -s http://127.0.0.1:8000/orders/ord-1/reconciliations -H 'X-Tenant: tenant-a'

# 16) 核销生效后立即约束冲正：冲正不得使已收低于已核销 300
curl -i -X POST http://127.0.0.1:8000/orders/ord-1/reversals \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -d '{"amount_cents":300}'
# => 409 "reversal would make paid amount less than reconciled amount"（与超过已收的 409 原因不同）

# 17) 批量导入订单：逗号分隔文本按行受理（text/plain，每行：租户,订单标识,金额,币种）
printf 'tenant,order_id,amount_cents,currency\ntenant-a,bulk-1,1200,CNY\ntenant-a,bulk-2,800,USD\ntenant-a,bulk-3,abc,CNY\ntenant-a,bulk-1,999,CNY\n' > /tmp/batch.txt
curl -s -X POST http://127.0.0.1:8000/orders/import \
  -H 'Content-Type: text/plain' --data-binary @/tmp/batch.txt
# => {"submitted":4,"succeeded":2,"skipped":0,"failed":2,
#     "failures":[{"line":4,"order_id":"bulk-3","reason":"invalid_amount",...},
#                 {"line":5,"order_id":"bulk-1","reason":"order_conflict",...}], "skips":[]}
#    计数闭合：submitted = succeeded + skipped + failed；失败清单带行号、订单标识与可区分原因

# 18) 原样重试同一份导入 —— 已受理的行按跳过处理，失败行重新判定，不产生重复订单
curl -s -X POST http://127.0.0.1:8000/orders/import \
  -H 'Content-Type: text/plain' --data-binary @/tmp/batch.txt
# => {"submitted":4,"succeeded":0,"skipped":2,"failed":2, ...}（skips 逐行给出 reason=order_exists）

# 19) 导入的订单可按标识读取，收款/冲正/核销及其幂等规则照常适用
curl -s http://127.0.0.1:8000/orders/bulk-1 -H 'X-Tenant: tenant-a'
curl -s -X POST http://127.0.0.1:8000/orders/bulk-1/payments \
  -H 'Content-Type: application/json' -H 'X-Tenant: tenant-a' \
  -H 'Idempotency-Key: pay-bulk-1' -d '{"amount_cents":200}'
```

重启服务后再次提交上述相同请求，结论与重启前一致：重复请求仍回放首次结果，冲突请求仍被拒绝；已生效的冲正、核销及其留痕继续保留。

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。
  - 成功返回 201 与订单对象；参数不合法返回 400。
  - 携带 `Idempotency-Key` 时：同租户同标识的重复提交返回与首次一致的状态码与响应体，不重复受理；标识相同但业务内容（订单标识、金额、币种）或操作类型不同返回 422 且不改变任何数据。
  - 不携带 `Idempotency-Key` 时：同一租户重复受理返回 409（原有行为不变）。
- `POST /orders/import`：批量导入订单。请求体为 `text/plain` 的逗号分隔文本，每行 `租户,订单标识,金额,币种` 四个字段；首个内容行允许是与 `tenant,order_id,amount_cents,currency` 一致的表头（不计入提交行数），全空白行忽略；行号按物理行从 1 计。
  - 逐行校验并在独立写事务内受理：整行合法且受理成功才落库，任一行失败不影响其他行；导入中途失败时已受理的行保持生效。
  - 受理规则与逐张受理完全相同：金额为最小货币单位正整数、币种限服务支持币种、租户互相隔离；导入的订单不携带请求标识，可用 `GET /orders/{order_id}` 读取，后续收款、冲正、核销及其幂等规则照常适用。
  - 重复导入安全：同租户同订单标识且金额与币种完全一致的行按「跳过」处理；金额或币种与已有订单不一致的行按「失败」处理；二者均不改变已有数据。
  - 始终返回 200 与计数闭合的结果：`submitted = succeeded + skipped + failed`；`failures` 逐行给出 `line`、`order_id`（能解析出时）、`reason` 与 `detail`，`reason` 可区分：`invalid_fields`（字段缺失或格式错误）、`invalid_amount`（金额不是正整数）、`unsupported_currency`（币种不受支持）、`order_conflict`（订单标识与已有订单的业务内容不一致）；`skips` 逐行给出跳过明细，`reason` 为 `order_exists`（订单标识已存在，同租户重复受理）。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`。
  - 携带 `Idempotency-Key` 时：同租户同标识的重复提交回放首次结果（含首次的 200/409/404 结论），收款金额不重复累加；标识相同但收款金额或目标订单不同返回 422 且不登记收款。
  - 不携带 `Idempotency-Key` 时：超过未收金额返回 409、订单不存在返回 404（原有行为不变）。
  - 成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /orders/{order_id}/reversals`：冲正已登记的收款。请求字段 `amount_cents`。
  - 冲正使订单 `paid_cents` 减少、`outstanding_cents` 相应增加（二者之和始终等于订单金额），仍有未收金额时状态回到 `accepted`；之后可继续收款，收清后重新进入 `settled`。
  - 成功返回 200 与更新后的订单对象；同时写一条 `result=applied` 的冲正留痕。
  - 非法金额返回 409，`detail` 区分具体原因：`reversal amount must be greater than zero`（金额 ≤ 0）、`reversal amount exceeds paid amount`（超过当前已收金额）、`reversal would make paid amount less than reconciled amount`（会使已收低于已核销金额）；拒绝不改变任何金额、状态与留痕。
  - 订单不存在或跨租户访问返回 404（跨租户不泄漏订单是否存在）。
  - 携带 `Idempotency-Key` 时：同租户同标识的重复冲正回放首次结果（含首次的 200/409/404 结论），不重复冲正；标识相同但冲正金额、目标订单或操作类型不同返回 422 且不改变任何数据。
- `GET /orders/{order_id}/reversals`：按发生顺序读出该订单的每笔冲正留痕，字段含 `amount_cents`、`result`、`paid_after`、`request_id`、`created_at`；不存在或跨租户返回 404。
- `POST /orders/{order_id}/reconciliations`：核销已确认收款。请求字段 `amount_cents`。
  - 核销使订单 `reconciled_cents`（已核销金额）累加，作为冲正后已收金额的下限；已收、未收与订单金额均不变（任意时刻已收 + 未收 = 订单金额）。
  - 成功返回 200 与更新后的订单对象；同时写一条 `result=applied` 的核销留痕。
  - 非法金额返回 409，`detail` 区分具体原因：`reconciliation amount must be greater than zero`（金额 ≤ 0）、`reconciled amount would exceed paid amount`（累计核销超过当前已收金额）；拒绝不改变任何金额、状态与核销留痕。
  - 订单不存在或跨租户访问返回 404（跨租户不泄漏订单是否存在）。
  - 携带 `Idempotency-Key` 时：同租户同标识的重复核销回放首次结果（含首次的 200/409/404 结论），不重复累加核销金额；标识相同但核销金额、目标订单或操作类型不同返回 422 且不改变任何数据。
- `GET /orders/{order_id}/reconciliations`：按发生顺序读出该订单的每笔核销留痕，字段含 `amount_cents`、`result`、`reconciled_after`、`request_id`、`created_at`；不存在或跨租户返回 404。
- `GET /health`：返回服务与数据库状态。

幂等作用域为「租户 + 请求标识」：不同租户使用相同请求标识互不影响；幂等记录持久化在数据库中，服务重启后继续有效。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入；同标识并发通过 SQLite `BEGIN IMMEDIATE` 写事务串行化，库级锁保证只有一次请求产生业务效果。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入为同步逐行受理，适合中小批量样本。
- 收款支持多次登记、收款冲正与对账核销；未实现分期单据、主动退款与自动对账。
