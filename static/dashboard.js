const $ = (selector, root = document) => root.querySelector(selector);
const percentage = (value) => `${value.toFixed(1)}%`;
const metric = ({count, sample, share}) => `${count}/${sample} 条（${percentage(share)}）`;
const colors = {
  来料不良: "#b67525",
  包装破损: "#c34454",
  功能故障: "#315d93",
  物流损坏: "#2c8074",
  待复核: "#7a8796",
};
const canChart = typeof Chart !== "undefined";
const rankButtons = document.querySelectorAll("[data-rank]");
let currentReport;
let categoryChart;
let rankingChart;
let rankingDimension = "supplier";
let updateInProgress = false;

function element(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function template(id) {
  return $(id).content.firstElementChild.cloneNode(true);
}

function fill(root, fields) {
  for (const [field, text] of Object.entries(fields)) {
    $(`[data-field="${field}"]`, root).textContent = text;
  }
}

function reviewQuote(row) {
  const quote = document.createElement("blockquote");
  const chars = Array.from(row.review_text);
  let cursor = 0;
  for (const span of row.spans ?? []) {
    if (span.start < cursor || span.end > chars.length) continue;
    quote.append(chars.slice(cursor, span.start).join(""), element("mark", chars.slice(span.start, span.end).join("")));
    cursor = span.end;
  }
  quote.append(chars.slice(cursor).join(""));
  return quote;
}

async function fetchJSON(path, options) {
  const response = await fetch(path, {cache: "no-store", ...options}).catch(() => {
    throw new Error("无法连接分析服务。");
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(typeof error?.detail === "string" ? error.detail : `请求失败（HTTP ${response.status}）。`);
  }
  return response.json();
}

function renderSignal(alert, index, expanded, report) {
  const banner = template("#signal-template");
  const details = template("#case-template");
  const isHigh = alert.level === "重点预警";
  const group = `${alert.sku} / ${alert.supplier}`;
  const lots = Object.entries(alert.lots).map(([lot, count]) => `${lot}（${count} 条）`).join("、");

  details.id = `case-${index + 1}`;
  details.dataset.signalKey = JSON.stringify([alert.sku, alert.supplier, alert.title]);
  details.open = expanded.has(details.dataset.signalKey);
  banner.href = `#${details.id}`;
  $("[data-field=level]", banner).dataset.level = isHigh ? "high" : "early";
  details.dataset.level = isHigh ? "high" : "early";
  banner.addEventListener("click", () => { details.open = true; });
  fill(banner, {
    level: alert.level,
    title: alert.title,
    group,
    share: percentage(alert.recent.share),
    comparison: `${alert.recent.count}/${alert.recent.sample} 条，基线 ${percentage(alert.baseline.share)}`,
  });
  if (alert.quality_evidence) showSignalEvidence($("[data-field=evidence-summary]", banner), alert.quality_evidence);

  const formatWindow = (window) => `${metric(window)}，${window.per_day.toFixed(2)} 条/天`;
  fill(details, {
    title: alert.title,
    group: `${group}  ·  ${alert.category}`,
    recent: formatWindow(alert.recent),
    baseline: formatWindow(alert.baseline),
    change: `+${alert.change_pp.toFixed(1)} 个百分点`,
    lots,
    keywords: `命中词 · ${alert.keywords.join(" / ")}`,
  });

  const evidence = $("[data-field=evidence]", details);
  for (const row of alert.evidence) {
    const item = document.createElement("li");
    const trace = row.trace_ref ? `  ·  ${row.trace_ref}` : "";
    item.append(
      element("p", `${row.review_id}  ·  ${row.date}  ·  ${row.stars} 星${trace}`),
      reviewQuote(row),
    );
    evidence.append(item);
  }

  if (alert.recommendation) {
    const {case: match, matched_keywords: terms} = alert.recommendation;
    fill(details, {
      "case-title": match.title,
      "case-meta": `${match.case_id}  ·  案例结案 ${match.closed_on}  ·  重排分 ${alert.recommendation.score.toFixed(2)}`,
      cause: match.root_cause,
      action: match.corrective_action,
      verification: match.verification,
      "matched-keywords": terms.length ? `相似词 · ${terms.join(" / ")}` : "",
    });
  } else {
    $("[data-field=matched-case]", details).hidden = true;
    $("[data-field=no-match]", details).hidden = false;
  }
  if (alert.quality_evidence) renderQualityEvidence($("[data-field=source-evidence]", details), alert, report);
  $("#signal-list").append(banner);
  $("#case-list").append(details);
}

function renderAlerts(report, preserveExpanded) {
  const {alerts, windows} = report;
  const expanded = new Set(preserveExpanded
    ? Array.from(document.querySelectorAll(".case-card[open]"), (card) => card.dataset.signalKey)
    : []);
  $("#signal-list").replaceChildren();
  $("#case-list").replaceChildren();
  $("#signal-section").dataset.clear = String(!alerts.length);
  $("#signal-count").textContent = `${alerts.length} 条`;
  $("#observation-window").textContent = `近 7 天  ${windows.recent.start} 至 ${windows.recent.end}`;
  $("#comparison-range").textContent = `28 天基线  ${windows.baseline.start} 至 ${windows.baseline.end}`;
  if (!alerts.length) {
    $("#signal-list").append(element("p", "当前窗口无达到阈值的客诉信号。", "empty-line"));
    $("#case-list").append(element("p", "有新预警时，这里会显示可参考的历史案例。", "empty-line"));
  }
  alerts.forEach((alert, index) => renderSignal(alert, index, expanded, report));
}

function renderListings(report) {
  const {listings, rating_scope: scope, listing_scenario: scenario} = report;
  const scopeLabels = {
    demo: "合成全星级 · 情景估算",
    full: (report.data_source === "sync" ? "同步全星级" : "导入全星级") + " · 情景估算",
    partial: "全星级数据待补",
  };
  $("#listing-scope").textContent = scopeLabels[scope];
  $("#listing-method-copy").textContent = scope === "partial"
    ? "导入所选期间的全部星级评价后可计算星级。当前文件仍可用于差评归因。"
    : `当前星级按各星级的评价条数加权。${scenario.days} 天挽回以未整改情景为基准，按近 7 天评价量和构成外推，假设匹配到 8D 的失效有 ${Math.round(scenario.recovery_share * 100)}% 的后续低星评价改善为 ${scenario.recovered_rating}★。`;
  $("#listing-empty").hidden = Boolean(listings.length);
  const items = document.createDocumentFragment();
  for (const item of listings) {
    const row = template("#listing-template");
    const hasStars = item.current_stars !== null;
    const hasLift = item.lift_stars !== null;
    let context = "暂无关联 8D 的预警";
    if (!hasStars) context = "导入全星级后可评估";
    if (hasLift) context = `30 天情景 ${item.without_8d_stars.toFixed(2)} → ${item.with_8d_stars.toFixed(2)}★`;
    fill(row, {
      sku: item.sku,
      "rating-count": `${item.rating_count} 条评分`,
      current: hasStars ? `${item.current_stars.toFixed(1)}★` : "—",
      lift: hasLift ? `+${item.lift_stars.toFixed(1)}★` : "—",
      context,
    });
    $("[data-field=current]", row).dataset.empty = String(!hasStars);
    $("[data-field=lift]", row).dataset.empty = String(!hasLift);
    items.append(row);
  }
  $("#listing-items").replaceChildren(items);
}

function renderDistribution(report) {
  const classified = report.total_reviews - report.unclassified_reviews;
  $("#classified-total").textContent = classified;
  const pending = report.unclassified_reviews ? `，${report.unclassified_reviews} 条待复核` : "";
  $("#classification-note").textContent = `已归类差评按主问题统计${pending}`;
  const list = $("#category-legend");
  list.replaceChildren();
  for (const row of report.categories) {
    const item = document.createElement("li");
    const dot = element("span", "", "legend-dot");
    dot.style.background = colors[row.category];
    item.append(
      dot,
      element("span", row.category, "category-name"),
      element("span", `${row.count} 条`, "category-count"),
      element("span", percentage(row.share), "category-share"),
    );
    list.append(item);
  }
  if (!canChart) {
    $(".donut").hidden = true;
    $(".distribution").classList.add("grid-cols-1");
    return;
  }
  const data = {
    labels: classified ? report.categories.map(({category}) => category) : ["暂无数据"],
    datasets: [{
      data: classified ? report.categories.map(({count}) => count) : [1],
      backgroundColor: classified ? report.categories.map(({category}) => colors[category]) : ["#e3eaf1"],
      borderColor: "#fff",
      borderWidth: classified ? 3 : 0,
    }],
  };
  if (categoryChart) {
    categoryChart.data = data;
    categoryChart.options.plugins.tooltip.enabled = Boolean(classified);
    categoryChart.update();
    return;
  }
  categoryChart = new Chart($("#category-chart"), {
    type: "doughnut",
    data,
    options: {
      maintainAspectRatio: false,
      animation: false,
      cutout: "73%",
      plugins: {legend: {display: false}, tooltip: {enabled: Boolean(classified)}},
    },
  });
}

function renderRankings(report) {
  const display = $("#rank-display");
  const list = $("#ranking-values");
  const groups = report.rankings[rankingDimension];
  const rows = groups.slice(0, 10);
  for (const button of rankButtons) button.setAttribute("aria-pressed", button.dataset.rank === rankingDimension);
  $("#ranking-caption").textContent = groups.length > rows.length
    ? `前 ${rows.length} 名 · 差评条数及全样本占比`
    : "差评条数及全样本占比";
  list.replaceChildren();
  display.hidden = !rows.length;
  $("#rank-empty").hidden = Boolean(rows.length);
  if (!rows.length) return;
  display.style.setProperty("--rank-height", `${Math.max(154, 36 * rows.length)}px`);

  for (const row of rows) {
    const item = document.createElement("li");
    item.append(
      element("span", row.name, "rank-name"),
      element("strong", `${row.count} 条`),
      element("span", percentage(row.share), "rank-share"),
    );
    list.append(item);
  }
  if (!canChart) {
    display.dataset.textOnly = "true";
    $("#chart-status").textContent = "图表组件加载失败，已显示排行数值。";
    $("#chart-status").hidden = false;
    return;
  }

  const categories = report.categories.map(({category}) => category);
  if (rows.some(({unclassified}) => unclassified)) categories.push("待复核");
  const labels = rows.map(({name}) => name);
  const datasets = categories.map((category) => ({
    label: category,
    data: rows.map((row) => category === "待复核" ? row.unclassified : row.by_category[category]),
    backgroundColor: colors[category],
    borderWidth: 1,
    borderColor: "#fff",
    maxBarThickness: 18,
  }));

  if (rankingChart) {
    rankingChart.data = {labels, datasets};
    rankingChart.resize();
    rankingChart.update();
    return;
  }
  rankingChart = new Chart($("#ranking-chart"), {
    type: "bar",
    data: {labels, datasets},
    options: {
      indexAxis: "y",
      maintainAspectRatio: false,
      animation: false,
      layout: {padding: {top: 3, bottom: 3}},
      scales: {
        x: {stacked: true, display: false, beginAtZero: true},
        y: {
          stacked: true,
          grid: {display: false},
          border: {display: false},
          ticks: {autoSkip: false, color: "#263b50", font: {family: "system-ui, sans-serif", weight: 600}},
        },
      },
      plugins: {legend: {display: false}},
    },
  });
}

async function showPreview() {
  const dialog = $("#preview-dialog");
  const state = $("#preview-status");
  const content = $("#preview-content");
  state.textContent = "正在生成预览…";
  content.hidden = true;
  dialog.showModal();

  try {
    const {payload} = await fetchJSON("/api/feishu-preview");
    const {header, body} = payload.card;
    $("#message-header").dataset.template = header.template;
    $("#message-title").textContent = header.title.content;
    $("#message-subtitle").textContent = header.subtitle.content;
    $("#message-json").textContent = JSON.stringify(payload, null, 2);
    const blocks = body.elements.map(({text}) => {
      const [title, ...lines] = text.content.split("\n");
      const block = element("div", "", "message-item");
      block.append(element("h4", title), ...lines.map((line) => element("p", line)));
      return block;
    });
    $("#message-body").replaceChildren(...blocks);
    state.textContent = "";
    content.hidden = false;
  } catch (error) {
    console.error(error);
    state.textContent = "预览加载失败。关闭后可重新打开预览。";
  }
}

function renderDashboard(report) {
  const period = report.sample_start ? `${report.sample_start} 至 ${report.as_of}` : `截至 ${report.as_of}`;
  $("#sample-period").textContent = `样本期间  ${period}  ·  ${report.total_reviews} 条差评`;
  const imported = report.data_source === "import";
  const modeLabel = workbenchConfig.interactive_sandbox ? "合成回放" : (report.demo_mode ? "本地规则" : "兼容 API");
  let dataLabel = imported ? `CSV 导入 · ${modeLabel}` : "合成数据 · " + (report.demo_mode ? "无密钥演示" : modeLabel);
  if (report.simulated_reviews) dataLabel = `合成数据 · 已追加 ${report.simulated_reviews} 条模拟差评`;
  if (report.sync) dataLabel = (report.sync.profile === "synthetic_http" ? "合成三源同步" : "已配置三源同步") + ` · ${modeLabel}`;
  $("#mode-label").textContent = dataLabel;
  $("#restore-import").hidden = imported || !report.has_import;
  $("#import-mode").textContent = workbenchConfig.interactive_sandbox
    ? "内置样例包含全星级评价。可切换星级覆盖范围，查看导入后的归类、排行与星级。"
    : (report.demo_mode
        ? "当前使用本地规则进行分析。"
        : "当前使用兼容 API，有正文的低星评价及其编号会传给已配置的模型服务。");
  renderListings(report);
  renderAlerts(report, currentReport?.data_source === report.data_source);
  $("#report").hidden = false;
  renderDistribution(report);
  renderRankings(report);
  currentReport = report;
  renderSourceContext(report);
}

function setImportStatus(text, error = false) {
  const status = $("#import-status");
  status.hidden = !text;
  status.dataset.error = String(error);
  status.textContent = text;
}

async function updateData(path, options, pending, success) {
  if (updateInProgress) return false;
  updateInProgress = true;
  const dialog = $("#import-dialog");
  const controls = dialog.querySelectorAll("button, input");
  for (const control of controls) control.disabled = true;
  setImportStatus(pending);
  try {
    const report = await fetchJSON(path, {method: "POST", ...options});
    renderDashboard(report);
    const notice = $("#update-notice");
    notice.hidden = false;
    notice.textContent = success(report);
    dialog.close();
    return true;
  } catch (error) {
    setImportStatus(error.message, true);
    return false;
  } finally {
    for (const control of controls) control.disabled = false;
    updateInProgress = false;
  }
}

function importCsv(body, checked) {
  return updateData(
    `/api/import?full_ratings=${checked}`,
    {headers: {"Content-Type": "text/csv"}, body},
    "正在导入 CSV…",
    (report) => `已导入 ${report.input_rows} 条评价，其中 ${report.total_reviews} 条差评。`,
  );
}

function bindControls(workbench) {
  for (const button of rankButtons) {
    button.addEventListener("click", () => {
      rankingDimension = button.dataset.rank;
      renderRankings(currentReport);
    });
  }
  $("#preview-button").addEventListener("click", showPreview);
  const importDialog = $("#import-dialog");
  importDialog.addEventListener("cancel", (event) => {
    if (updateInProgress) event.preventDefault();
  });
  $("#import-button").addEventListener("click", () => {
    setImportStatus("");
    importDialog.showModal();
  });
  if (workbench.interactive_sandbox) {
    for (const selector of ['label[for="review-csv"]', "#review-csv", "#import-submit"]) $(selector).hidden = true;
    $("#review-csv").required = false;
    $("#csv-help").textContent = "公网演示提供可下载的合成 CSV，真实评价可以在本机工作台导入。";
    $("#full-ratings").checked = true;
    $("#sample-import-actions").hidden = false;
    $("#download-sample").href = workbench.sample_csv.url;
    $("#import-sample").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const response = await fetch(workbench.sample_csv.url);
        if (!response.ok) throw new Error("暂时无法读取样例 CSV。");
        await importCsv(await response.blob(), $("#full-ratings").checked);
      } catch (error) {
        setImportStatus(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  }
  $("#csv-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const [file] = $("#review-csv").files;
    if (!file || !file.name.toLowerCase().endsWith(".csv")) {
      setImportStatus("请选择 .csv 文件。", true);
      return;
    }
    const checked = $("#full-ratings").checked;
    const imported = await importCsv(file, checked);
    if (imported) event.target.reset();
  });
  $("#simulate-button").addEventListener("click", () => updateData(
    "/api/demo-simulate", {}, "正在模拟抓取并重新分析…",
    (report) => `本次新增 5 条包装破损差评，示例样本现有 ${report.total_reviews} 条差评。`,
  ));
  $("#restore-import").addEventListener("click", () => updateData(
    "/api/restore-import", {}, "正在恢复导入结果…",
    (report) => `已返回上次导入结果，共 ${report.total_reviews} 条差评。`,
  ));
}

async function loadDashboard() {
  const state = $("#page-status");
  try {
    const [report, workbench] = await Promise.all([fetchJSON("/api/report"), fetchJSON("/api/v1/workbench")]);
    bindSourceWorkbench(workbench);
    renderDashboard(report);
    bindControls(workbench);
    $("#preview-button").disabled = false;
    $("#import-button").disabled = sourceBusy;
    $("#import-button").hidden = workbench.public_demo && !workbench.interactive_sandbox;
    state.hidden = true;
  } catch (error) {
    console.error(error);
    state.textContent = "看板加载失败。刷新页面重试。";
    state.hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", loadDashboard);
