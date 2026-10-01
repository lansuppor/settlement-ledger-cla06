# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /orders/{order_id}/refunds`：冲正（退款）已收金额。请求字段 `refund_id`（调用方生成的冲正标识）、`amount_cents`（正整数），租户经 `X-Tenant` 传入。
  - 成功返回 200：`refund_id`、`amount_cents`、冲正后 `paid_cents`、`outstanding_cents`、`created_at`，`idempotent_replay` 表示是否为重复标识的回放。
  - 幂等：同一（租户, 订单, `refund_id`）重复请求不重复冲正，回放首次结果（即使金额不同）。
  - 冲正金额超过当前已收金额返回 409；订单不存在或跨租户返回 404；参数不合法返回 400。
  - 冲正后 `paid_cents` 减少；低于订单金额时订单回到 `accepted`（未结清），等于订单金额时 `settled`。
- `GET /health`：返回服务与数据库状态。

## 冲正调用示例

```bash
# 登记收款后冲正 300
curl -X POST localhost:8000/orders/INV-1/refunds \
  -H 'X-Tenant: acme' -H 'Content-Type: application/json' \
  -d '{"refund_id":"RFD-001","amount_cents":300}'

# GET /orders/INV-1 的 entries 按发生顺序列出 payment / refund 流水，
# 可解释 paid_cents：paid_cents = Σpayment − Σrefund
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 冲正已支持幂等与流水留痕；尚未实现外部渠道对账文件导入。
