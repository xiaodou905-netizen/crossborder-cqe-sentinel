# Crossborder CQE Sentinel

海外跨境电商 VOC 质量归因与早期预警 Agent。它把海外平台的低星评论归到可排查的失效模式，按 SKU 和供应商找出短期增长，再调出历史 8D 的根因、整改措施和验证记录。CQE 可据此优先跟进影响 Listing 星级的重复客诉，并在整改后结合全量星级数据评估效果。

演示包含 80 条 1–2 星英文或中英双语评论、合成的 3–5 星评分分布和 5 份完整 8D。品牌 `Brand-Mock`、SKU、供应商、批次及案例数据均为虚构。预警报告描述差评样本中的变化，实物、批次追溯和检验记录用于确认当前根因。

## 一键启动

安装 Python 3.11 或以上版本。在项目目录运行以下命令，然后打开 [本地看板](http://127.0.0.1:8001/)。

```bash
./start.sh
```

首次启动会创建 `.venv` 并安装依赖。默认启用 `DEMO_MODE=1`。Tailwind 样式和 Chart.js 已编译或随项目交付，依赖安装结束后可以离线演示。停止服务时按 `Ctrl+C`。

Windows 可在 PowerShell 中执行。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## 系统架构

```mermaid
flowchart LR
    CSV["导入 CSV 或模拟实时抓取"] --> Tag["低星分类与原文失效关键词"]
    CSV --> Listing["样本加权星级与 30 天整改情景"]
    Tag --> Stats["问题占比和供应商 / SKU 排行"]
    Tag --> Signal["7 天窗口与 28 天基线对比"]
    KB["5 份历史 8D"] --> Search["TF-IDF 与关键词检索"]
    Signal --> Search
    Search --> Listing
    Signal --> Report["FastAPI 分析报告"]
    Stats --> Report
    Search --> Report
    Listing --> Report
    Report --> Web["Tailwind / Chart.js 看板"]
    Report --> Card["飞书模拟卡片与 JSON 预览"]
```

- [`core/tagger.py`](core/tagger.py) 用规则或模型为每条差评提取一个主类和原文关键词。Pydantic 校验类别、失效代码及字段结构，模型结果额外核对评论编号与关键词原文。分类支持来料不良、包装破损、功能故障和物流损坏，证据不足的评论进入待复核。
- [`core/alert_engine.py`](core/alert_engine.py) 按问题类别统计构成，按供应商和 SKU 排差评条数，并对同一 `SKU × 供应商` 下的失效分别比较两段时间。已知模式使用标准失效代码，其他现象按类别和首条标准化关键词聚合。
- [`core/rag_8d.py`](core/rag_8d.py) 用英文词和中文双字词做 TF-IDF，结合检索词重合度。在同类且截至当日已结案的案例中返回最相似的记录，并附 D4 根因、D5 措施和 D6 验证。低于阈值时提示人工排查。
- [`core/listing_metrics.py`](core/listing_metrics.py) 汇总各 SKU 的星级分布，并使用匹配到历史 8D 的预警估算整改收益。
- [`app.py`](app.py) 在启动、导入和模拟抓取后生成报告。看板提供两种排行、可展开的 8D 原文证据和飞书模拟预览。`/api/report` 返回当前分析结果，`/api/feishu-preview` 返回当前报告的本地模拟卡片 JSON。卡片结构参考 [飞书自定义机器人使用指南](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN%2C)。

## 导入真实导出的电商评论 CSV

1. 在有权限的电商后台导出评论。要评估星级，导出所选 SKU 和期间的全部星级，包含只有评分的记录。仅做客诉归因时可以只导出 1–2 星。
2. 调整表头，使每行对应一条评价。转为 UTF-8 逗号分隔的 `.csv` 文件，可带 BOM，单文件最大 5 MiB。
3. 在右上角点 **导入 CSV / 模拟实时抓取**，选文件。只有文件覆盖该期间的全部星级时才勾选 **包含所选期间的全星级评价**，然后点 **导入并分析**。

| 项目字段 | 如何从导出文件准备 |
| --- | --- |
| `review_id` | 评价编号，每行唯一 |
| `date` | 评价日期，统一为 `YYYY-MM-DD` |
| `sku` | Seller SKU 或稳定的 Listing 编码。多个站点的相同 SKU 建议加站点后缀 |
| `stars` | 1–5 的整数，例如把 `5.0 out of 5 stars` 转为 `5` |
| `review_text` | 评论原文。有评分无正文时留空，低星评价进入待复核 |
| `brand`、`supplier`、`lot` | 可选。根据产品或订单映射表补齐供应商及批次，空值显示「未标注」 |

额外列会被忽略。正文包含逗号、双引号或换行时，保留 CSV 导出工具生成的引号转义。默认本地规则适用于内置失效短语，超出规则覆盖的低星评论进入待复核。导入兼容 API 模式时，有正文的低星评价及其编号会传给已配置的服务，真实文件请先移除正文里的顾客标识及订单号。

导入以文件中的最新评价日作为回放终点，分析结果保留在本地服务内存。点击 **一键模拟新增 5 条包装破损差评** 会在示例看板追加五条同批次评论。可通过 **返回上次导入结果** 回到已分析的 CSV。重启服务后从初始示例重新开始。

也可通过 API 导入。在调用方确认文件包含全星级评价时，使用 `full_ratings=true`。

```bash
curl -fsS -H 'Content-Type: text/csv' --data-binary @reviews.csv \
  'http://127.0.0.1:8001/api/import?full_ratings=true'
```

## 预警规则

VOC 分析纳入 1–2 星评价，默认以样本中最新的评价日期为回放终点。日窗口包含起止日，近 7 天含终点，基线为紧邻且无重叠的此前 28 天。批次用于追溯，比较分母使用同一 SKU 与供应商在各自窗口的全部差评。

| 条件 | 触发规则 |
| --- | --- |
| 最小样本 | 近 7 天至少 6 条差评，基线至少 10 条差评 |
| 苗头关注 | 近 7 天同一失效至少 3 条，占比上升至少 15 个百分点。基线有命中时，占比和日均条数均达到基线的 2 倍 |
| 重点预警 | 满足苗头条件，且近 7 天同一失效至少 6 条，占比上升至少 30 个百分点 |

基线有足量差评且该失效为 0 条时，使用条数与占比增幅判断。环形图以已归类差评为分母，排行以全部样本差评为分母。看板最多显示排行前 10 名，`/api/report` 返回完整排名。

## Listing 星级和整改收益口径

- **当前加权星级**以各星级的评价数为权重。同一 SKU 的 `Σ(星级 × 该星级评价数) ÷ 总评价数` 是本次样本星级。Amazon 页面评分还会考虑时效和核验购买等平台因素。[Amazon 对评分方式的说明](https://sellercentral.amazon.com/seller-forums/discussions/t/9629cd2e-3989-46b4-80ae-aa87fafc8cd1)
- **预计 30 天星级挽回**以 30 天后的未整改情景为基准。两个情景均按近 7 天评价量和星级构成外推，整改情景假设已匹配到 8D 的失效有 70% 的后续低星评价改善为 5 星。用增加的星级分值除以「已有评价数 + 预计新增评价数」得到挽回幅度。历史评价保留原星级。
- 示例星级使用独立的合成 3–5 星分布。导入文件须声明覆盖全星级才计算当前星级，存在关联 8D 的预警才显示 30 天情景。

## 复现演示结果

在终端输出完整 JSON，或指定历史日期回放。

```bash
DEMO_MODE=1 ./start.sh --analyze
DEMO_MODE=1 ./start.sh --analyze --as-of 2026-11-12
```

截至 `2026-11-19`，80 条评论全部归类，触发两条预警。回放到 `2026-11-12` 时有 55 条差评，预警数为 0。

| 预警 | SKU / 供应商 | 近 7 天 | 28 天基线 | 对应历史案例 |
| --- | --- | ---: | ---: | --- |
| 重点预警 · 包装内衬压碎 | `SL-Pro-US` / `Supplier-A` | 8/12（66.7%） | 1/24（4.2%） | `8D-002` EPE 内衬与跌落验证 |
| 苗头关注 · 指纹低温识别率下降 | `SL-Max-EU` / `Supplier-B` | 3/8（37.5%） | 0/17（0.0%） | `8D-004` 密封压合与低温验证 |

包装异常集中于 `LOT-PU-04`，8 条均出现在最后 5 天。低温指纹评论集中于 `LOT-ME-03`。点击预警或展开 8D 卡片可核对批次和评论原文样例。

初始看板中，`SL-Pro-US` 的样本加权星级为 **4.1★**，预计 30 天挽回为 **+0.4★**。模拟抓取一次后，样本从 80 条增至 85 条，包装预警从 8/12（66.7%）升至 13/17（76.5%），供应商 A 的差评从 36 条增至 41 条。

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 接入 OpenAI 兼容接口

将 `OPENAI_API_KEY` 设为本地环境变量，再指定服务地址和模型。地址填写包含服务版本的 API 根路径，程序会追加 `chat/completions`。服务不支持 JSON 对象模式时可设置 `OPENAI_JSON_MODE=0`，Pydantic 校验仍然生效。[Chat Completions 接口文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

```bash
export OPENAI_BASE_URL='https://api.openai.com/v1'
export OPENAI_MODEL='<服务商支持的模型 ID>'
DEMO_MODE=0 ./start.sh
```

兼容模式按批向已配置的模型服务提交评论编号与原文。SKU、供应商、星级和批次字段用于本地统计。每次服务启动都会重新生成报告。本项目已用模拟 HTTP 响应验证接入格式与异常结果拦截，线上模型需要用所选服务完成联调。

## 文件与前端构建

- [`data/amazon_reviews_mock.csv`](data/amazon_reviews_mock.csv) 保存 80 条差评及原始批次字段。
- [`data/demo_listing_ratings.csv`](data/demo_listing_ratings.csv) 保存示例 3–5 星评价的按日汇总，供星级估算使用。
- [`data/historical_8d_kb.md`](data/historical_8d_kb.md) 保存 5 份 D1–D8 案例，涵盖来料虚焊、EPE 包装跌落、固件防误触、指纹低温和物流碰撞。
- [`static/`](static/) 保存页面和已构建的前端资源，第三方授权文本随 `static/vendor/` 提供。

修改页面样式后可用以下命令重建 Tailwind 样式。[Tailwind CLI 文档](https://tailwindcss.com/docs/installation/tailwind-cli)

```bash
npm install --no-save --no-package-lock tailwindcss@4.3.5 @tailwindcss/cli@4.3.5
npx tailwindcss -i static/tailwind.css -o static/dashboard.css --minify
```
