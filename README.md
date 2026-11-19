# Crossborder CQE Sentinel

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Cloudflare-F38020?logo=cloudflare&logoColor=white)](https://crossborder-cqe-sentinel.xtony.workers.dev/)

**面向智能硬件、IoT 与智能制造的 CQE / SQE / DQE 质量参考架构。**

从海外客诉追到出货批次，再用现场检验判断是否具备复验条件。工作台用可回查的同步快照串起渠道评价、出货追溯和 QMS 检验，附历史 8D 与 Listing 星级情景。

[公网体验（无需 API Key）](https://crossborder-cqe-sentinel.xtony.workers.dev/) · [本地运行与验收](#本地运行与验收) · [在线演示部署](#在线演示部署) · [数据接入契约](docs/integration.md) · [可复跑评测](docs/evaluation.md) · [面试与设计问答](docs/interview.md)

| 已在本地运行的证据 | 结果 |
| --- | --- |
| 三源 HTTP 分页回放 | **408 条评分、13 条追溯、4 条检验**。80 条差评中 **77 条**关联批次 |
| 同批次质量证据 | 包装压溃 `LOT-PU-04`，`QMS-26-117` 角跌后 **3/6 件异常，已复现**。低温指纹 `LOT-ME-03`，`QMS-26-123` **1/5 件异常，检验观察**。[QMS 样例](data/demo_qms_inspections.json) |
| 异常恢复 | 追溯源 503，重试两次后本轮失败，上一成功快照继续可读 |
| VOC 与检索基准 | **89.8% Macro-F1**（58 条）。8D **Recall@3 / MRR@3 均为 1.000**（11 条有标签的检索题，库内 5 案例） |

仓库使用脱敏合成数据。已验证同进程 demo 的实际 HTTP 请求，也用三个独立本地进程作为配置化上游完成联调。接入契约和业务样例可在本机复跑。

![CQE 多源质量证据工作台](docs/dashboard.jpg)

## 它解决什么

| CQE 的实际问题 | 工作台提供的证据与动作 |
| --- | --- |
| 包装内衬问题和承运撞击都可能表现为到货有划痕 | 保留英文或双语原句和触发词位置，区分失效模式。以追溯引用关联供方和批次 |
| 指纹低温异常条数少，手工拉表容易错过增长 | 对比同 SKU、同供方近 7 天和此前 28 天的比例及日均条数，列出样本分母与回放日期 |
| 历史 8D 看起来相关，现场证据仍待核实 | 检索结果附 D4/D5/D6 与重排分。只有同 SKU、批次、失效代码的 QMS 记录才进入当前告警 |
| 质量团队需要说明整改对 Listing 有什么价值 | 全星级样本用于估算当前星级，按未来 30 天的未整改情景计算挽回幅度 |

## 本地运行与验收

需要 Python 3.11+。在仓库根目录启动后打开 [CQE 工作台](http://127.0.0.1:8001/)。首次安装 Python 依赖需要网络，前端资源随仓库提供。

```bash
./start.sh
```

下面的命令会启动隔离沙盒并在验收后退出。它使用动态 loopback 端口和内存 SQLite，输出三源条数、两条告警的 QMS 记录和故障后快照回读结果。

```bash
./start.sh --verify-demo
```

Windows PowerShell 先运行 `py -3 -m venv .venv` 和 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`。工作台入口为 `.\.venv\Scripts\python.exe app.py`，验收入口为 `.\.venv\Scripts\python.exe -m benchmarks.smoke_http --standalone`。

1. 在 [CQE 工作台](http://127.0.0.1:8001/) 点 **同步三源**。打开 **查看 3 条未关联差评**，可定位原记录和原因。打开 **快照 JSON** 核对输入 SHA-256。
2. 对照两张告警卡的复现和观察阶段。展开 **包装内衬压碎**，顺着评论原句、`TR-LOT-PU-04`、`QMS-26-117` 到历史案例 `8D-002`。填写备注后可开始调查、提交复验。
3. 点 **回放来源故障**，查看追溯源重试与失败状态。已生成的快照仍可查看。点 **导入 CSV / 模拟实时抓取** 可测试另一个入口。

接口文档可直接打开 [本机 OpenAPI](http://127.0.0.1:8001/docs)。

## 数据流与状态

```mermaid
flowchart LR
    M["渠道评分 · 分页快照"] --> A["异步 HTTP Adapter"]
    S["MES / 出货追溯"] --> A
    Q["QMS 现场检验"] --> A
    A --> J["合并与校验 trace_ref / SKU / 时间"]
    J --> T["规则或兼容模型 · 原文跨度校验"]
    T --> W["7 日与 28 日预警"]
    W --> K["同类历史 8D · TF-IDF 候选与重排"]
    W --> E["关联同 SKU / 批次 / 失效的检验"]
    K --> P["SQLite 同步快照"]
    E --> P
    P --> UI["前端 · 飞书预览 · Listing 情景"]
    P --> H["CQE 研判备注与复验移交"]
```

- **同步状态**从受理、拉取到分析。三源全部通过校验才提交成功快照。请求幂等键复用同一任务，失败与重启中断可以回查。
- **调查状态**从待研判、调查中到已提交复验。保存备注和版本，只有同批次同失效的复现记录才可提交复验。正式根因、措施有效性与结案依现场 QMS 流程。
- **规则和模型**共享分类枚举与失效代码。模型输出要通过 Pydantic、评论 ID 覆盖和原文定位。已知失效另过可观察证据门槛。低温指纹要求同时出现指纹、低温和识别下降线索。
- **历史方案**先排除分类冲突和当时尚未结案的案例。TF-IDF 召回后按词相似度、检索词覆盖和失效代码重排，低分弃答。展示既有 D4/D5/D6 原文。

## 评测与可复现命令

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m benchmarks.evaluate --format markdown
./start.sh --verify-demo
```

评测包含未见表达、否定语句、无匹配案例、案例结案日晚于回放日，以及逐日窗口。8 个回放日检出 2/2 处植入信号，距首条植入分别为 **1 天和 3 天**，另有 **1 条额外告警**。分母、逐类结果和错例均可回查。[评测报告](docs/evaluation.md)

[GitHub Actions 工作流](.github/workflows/validate.yml)使用 Python 3.11 和 3.12 执行单测、评测报告对拍与真实 HTTP 冒烟。本仓库基准使用规则引擎和本地检索，兼容模型使用模拟响应检查契约和拒答行为。

## 导入真实电商评论 CSV

1. 下载有权限使用的评价导出。如果要看星级，导出同一观察期间所有星级，包括无正文评分。
2. 保留唯一的 `review_id`，映射 `date`（`YYYY-MM-DD`）、`sku`、`stars`（1–5）和 `review_text`。可补 `brand`、`supplier` 和 `lot`，以订单或追溯记录填充。缺失显示「未标注」。
3. 保存为 UTF-8 逗号分隔文件，可带 BOM，最大 5 MiB。双引号、逗号和正文换行按 CSV 转义保留。
4. 点击 **导入 CSV / 模拟实时抓取** → **导入并分析**。文件包含全量星级时勾选对应选项。模拟新增 5 条只作用于示例视图，可返回上次导入结果。

默认无密钥规则运行。有正文的低星评价可用 `DEMO_MODE=0` 发送给配置的兼容模型。模型仅收评论编号和正文，使用真实文件前应对正文脱敏。CSV 分析结果留在当前进程，三源同步快照和研判记录保存在本地 SQLite。[字段契约和环境变量](docs/integration.md)

## Listing 星级口径

**当前加权星级**用各星级的评价数加权。**30 天挽回**是整改情景和同一时期未整改情景的差值。两个情景均沿用近 7 天评价量和构成，假设关联到 8D 的失效中，70% 的后续低星评价改善到 5 星。历史评价保留原星级。示例 `SL-Pro-US` 为 **4.1★**，在该情景下 **+0.4★**。差评占比用于质量排查，估算故障率需销量或在网量分母。Amazon 页面采用自身模型并考虑时效、核验购买等因素。[Amazon 的评分说明](https://sellercentral.amazon.com/seller-forums/discussions/t/9629cd2e-3989-46b4-80ae-aa87fafc8cd1)

## 在线演示部署

[打开 Cloudflare Live Demo](https://crossborder-cqe-sentinel.xtony.workers.dev/)。默认开启无密钥的合成演示，支持三源同步、503 故障回放、批次证据与 8D 查看、调查与复验、飞书卡片预览、内置 CSV 导入和模拟追加。真实评价与企业接口在本机工作台接入。

部署构建由 Python 引擎生成合成回放结果。Cloudflare Worker 在运行时逐页核验三源指纹，再发布对应快照。每个访客的操作由 SQLite Durable Object 分开保存，演示会话有效期为 12 小时。部署使用 Workers Free 支持的组件。[Workers Static Assets](https://developers.cloudflare.com/workers/static-assets/) · [Durable Objects 免费层](https://developers.cloudflare.com/durable-objects/platform/pricing/)

在仓库根目录用已登录的 Wrangler 发布。部署命令会调用 `./start.sh --build-cloudflare` 生成静态资源与回放快照。

```bash
npx --yes wrangler@4.165.0 deploy --config wrangler.jsonc
.venv/bin/python -m benchmarks.smoke_cloudflare --base https://crossborder-cqe-sentinel.xtony.workers.dev
```

公网接口文档可从 [Cloudflare 演示 API](https://crossborder-cqe-sentinel.xtony.workers.dev/docs) 打开。

## 代码入口

| 模块 | 职责 |
| --- | --- |
| [`adapters/`](adapters/) | Pydantic 来源契约、HTTP 分页与合成上游 |
| [`service/`](service/) | 异步同步、批次关联、快照存储、研判状态、OpenAPI |
| [`core/`](core/) | 分类、预警、检索重排、星级情景 |
| [`static/`](static/) | Tailwind / Chart.js 工作台和可回查原文 |
| [`benchmarks/`](benchmarks/) · [`tests/`](tests/) | 标签集、离线评测、HTTP 冒烟和故障验证 |

[MIT License](LICENSE) · 供应商、SKU、检验记录和评论均为虚构。前端第三方授权文本见 [vendor](static/vendor/)。
