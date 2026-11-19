# 企业来源接入契约 v1

渠道侧提供经授权导出或接口。MES、WMS 或售后系统提供出货关联，QMS 提供有记录编号的现场检验。适配器读取三个独立端点。标准化层将平台 SKU 映射成内部稳定标识，跨站同名 SKU 应在上游映射后传入。

## HTTP 交换格式

每个端点支持 `GET`，接收 `limit`。下一页接收上一页的 `next_cursor` 作为 `cursor`，并携带首次响应的 `snapshot_id` 作为 `snapshot`。空游标表示完成。上游应为分页提供稳定的快照读视图。

```json
{
  "schema_version": "1.0",
  "snapshot_id": "marketplace-export-20261119-r1",
  "coverage": "all_ratings",
  "items": [
    {
      "review_id": "VOC-078",
      "listing_sku": "SL-Pro-US",
      "posted_on": "2026-11-19",
      "rating": 1,
      "body": "Crushed inner tray with the outer carton intact.",
      "brand": "Brand-Mock",
      "trace_ref": "TR-LOT-PU-04"
    }
  ],
  "next_cursor": null
}
```

| 来源 | 记录字段 | 业务含义 |
| --- | --- | --- |
| 渠道评分 | `review_id`、`listing_sku`、`posted_on`、`rating`、`body`、`brand`、`trace_ref` | `trace_ref` 可为 null。来自授权的订单关联或追溯映射。正文可空，星级是 1–5 的整数 |
| 出货追溯 | `trace_ref`、`sku`、`lot`、`supplier`、`released_on` | 要求 trace_ref 一致、SKU 一致且出货日期不晚于评论 |
| QMS 检验 | `inspection_id`、`sku`、`lot`、`inspected_on`、`issue`、`outcome`、`sample_size`、`failures`、`method` | `issue` 采用标准失效代码。结果支持观察 `observed`、复现 `reproduced` 和复验通过 `fix_verified` |

日期使用 `YYYY-MM-DD`。渠道的 `coverage` 为 `all_ratings` 或 `partial_ratings`，其他来源为 `records`。只有渠道声明 `all_ratings` 时计算 Listing 样本星级。检验失败件数不得超过样本数。复现至少有一件异常，复验通过的异常件数为零。

完整 JSON Schema 可从 [本机契约端点](http://127.0.0.1:8001/api/v1/contracts) 获取，字段定义位于 [adapters/contracts.py](../adapters/contracts.py)。

## 并发、失败与追溯

- 每轮三条源并行读取，每个来源内按游标顺序读取。单页 1 MiB、最多 64 页，拉取阶段总预算 24 秒。429、5xx 和连接或读取错误最多重试两次。契约错误、中途换快照、循环游标与同 ID 内容冲突直接失败。
- 完全相同的源记录按外部 ID 去重。每源内容按记录 ID 排序后计算 SHA-256，再将三源摘要合成输入指纹。每个上游的快照 ID 单独留档。
- 发布成功快照前，三条源均须通过校验。快照写入及任务进入成功状态同事务提交。服务重启时会标记中断任务，最近成功的快照仍可读取。
- 缺少、找不到或冲突的追溯引用归入未标注。新快照的 `sync.trace_rejections` 最多列出 8 条低星样本，包含评价 ID、SKU、追溯引用和拒绝原因。全量分母取 `sync.traceability`。QMS 关联按 SKU、批次、标准失效代码和回放截止日核对，所用检验编号保留在告警中。

追溯拒绝原因使用 `missing_reference`、`unknown_reference`、`sku_mismatch` 和 `shipment_after_review`。后两者计入覆盖率中的 `conflict`。

## 配置真实端点

本地模式的服务端环境变量以 `SENTINEL_` 开头。三方 HTTPS URL 同时配置，开发联调可使用 loopback HTTP。每源可选对应的 `_TOKEN` 作为 Bearer Token，令牌通过部署环境注入。

```bash
export SENTINEL_REVIEWS_URL='https://reviews.example.invalid/v1/reviews'
export SENTINEL_SHIPMENTS_URL='https://trace.example.invalid/v1/shipments'
export SENTINEL_INSPECTIONS_URL='https://qms.example.invalid/v1/inspections'
./start.sh
```

变量一览

| 变量 | 用途 |
| --- | --- |
| `SENTINEL_REVIEWS_TOKEN`、`SENTINEL_SHIPMENTS_TOKEN`、`SENTINEL_INSPECTIONS_TOKEN` | 各上游的授权令牌 |
| `DEMO_MODE` | 默认 `1`，使用本地规则。设 `0` 可接兼容模型 |
| `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` | 兼容模型凭据、API 根路径和模型 ID。会追加 `chat/completions` |
| `OPENAI_JSON_MODE` | 默认 JSON 对象模式。设 `0` 使用文本输出后做相同的本地校验 |
| `SENTINEL_DB_PATH` | 本地 SQLite 路径。默认为 `.sentinel/workbench.sqlite3` |
| `SENTINEL_PUBLIC_DEMO` | 设 `1` 时仅允许合成源，快照仅存在内存，不接受 CSV 或研判写入 |
| `SENTINEL_HOST`、`PORT` | HTTP 监听。默认 `127.0.0.1:8001` |

协议示例和故障注入由 [内置合成上游](../adapters/demo_provider.py) 提供。现场源需要生产授权和字段映射。本地工作台以单进程运行，共享环境接入前需补齐身份、权限、数据留存与审计策略。兼容模型的结构校验在本机完成，并按配置向模型服务发送评论编号与原文。[Chat Completions 接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

## 操作 API

| 调用 | 行为 |
| --- | --- |
| `POST /api/v1/runs` | 接收 `{"scenario":"normal"}`，返回 202 和任务查询路径。可带 `Idempotency-Key` |
| `GET /api/v1/runs/{run_id}` | 查询阶段、每源状态、页数、重试和输入摘要 |
| `GET /api/v1/runs/{run_id}/report` | 回读成功快照及其研判记录 |
| `POST /api/v1/runs/{run_id}/activate` | 将该快照选为当前本地视图，飞书预览读同一视图 |
| `POST /api/v1/runs/{run_id}/cases/{case_key}/triage` | 提交 `expected_revision`、`next_phase` 和 `note`，版本冲突返回 409 |
| `POST /api/import?full_ratings=true` | 导入规范化 CSV。请求体为原始 UTF-8 文件，Content-Type 为 `text/csv` |

同一幂等键重放返回原任务。同键更换请求场景或并发创建第二个活跃任务会返回 409。故障演示场景 `trace_unavailable` 仅供本地合成源使用。
