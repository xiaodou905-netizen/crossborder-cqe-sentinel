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

async function fetchJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function renderSignal(alert, index) {
  const banner = template("#signal-template");
  const details = template("#case-template");
  const isHigh = alert.level === "重点预警";
  const group = `${alert.sku} / ${alert.supplier}`;
  const lots = Object.entries(alert.lots).map(([lot, count]) => `${lot}（${count} 条）`).join("、");

  details.id = `case-${index + 1}`;
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
    item.append(
      element("p", `${row.review_id}  ·  ${row.date}  ·  ${row.stars} 星`),
      element("blockquote", row.review_text),
    );
    evidence.append(item);
  }

  if (alert.recommendation) {
    const {case: match, matched_keywords: terms} = alert.recommendation;
    fill(details, {
      "case-title": match.title,
      "case-meta": `${match.case_id}  ·  案例结案 ${match.closed_on}`,
      cause: match.root_cause,
      action: match.corrective_action,
      verification: match.verification,
      "matched-keywords": terms.length ? `相似词 · ${terms.join(" / ")}` : "",
    });
  } else {
    $("[data-field=matched-case]", details).hidden = true;
    $("[data-field=no-match]", details).hidden = false;
  }
  $("#signal-list").append(banner);
  $("#case-list").append(details);
}

function renderAlerts(report) {
  const {alerts, windows} = report;
  $("#signal-count").textContent = `${alerts.length} 条`;
  $("#observation-window").textContent = `近 7 天  ${windows.recent.start} 至 ${windows.recent.end}`;
  $("#comparison-range").textContent = `28 天基线  ${windows.baseline.start} 至 ${windows.baseline.end}`;
  if (!alerts.length) {
    $("#signal-section").dataset.clear = "true";
    $("#signal-list").append(element("p", "当前窗口无达到阈值的客诉信号。", "empty-line"));
    $("#case-list").append(element("p", "有新预警时，这里会显示可参考的历史案例。", "empty-line"));
  }
  alerts.forEach(renderSignal);
}

function renderDistribution(report, canChart) {
  const classified = report.total_reviews - report.unclassified_reviews;
  $("#classified-total").textContent = classified;
  if (report.unclassified_reviews) {
    $("#classification-note").textContent += `，${report.unclassified_reviews} 条待复核`;
  }
  const list = $("#category-legend");
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
  new Chart($("#category-chart"), {
    type: "doughnut",
    data: {
      labels: classified ? report.categories.map(({category}) => category) : ["暂无数据"],
      datasets: [{
        data: classified ? report.categories.map(({count}) => count) : [1],
        backgroundColor: classified ? report.categories.map(({category}) => colors[category]) : ["#e3eaf1"],
        borderColor: "#fff",
        borderWidth: classified ? 3 : 0,
      }],
    },
    options: {
      maintainAspectRatio: false,
      animation: false,
      cutout: "73%",
      plugins: {legend: {display: false}, tooltip: {enabled: Boolean(classified)}},
    },
  });
}

function renderRankings(report, canChart) {
  const display = $("#rank-display");
  const list = $("#ranking-values");
  const buttons = document.querySelectorAll("[data-rank]");
  if (!canChart) {
    display.dataset.textOnly = "true";
    $("#chart-status").textContent = "图表组件加载失败，已显示排行数值。";
    $("#chart-status").hidden = false;
  }
  let chart;

  function select(dimension) {
    for (const button of buttons) button.setAttribute("aria-pressed", button.dataset.rank === dimension);
    const rows = report.rankings[dimension];
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
    if (!canChart) return;

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

    if (chart) {
      chart.data = {labels, datasets};
      chart.update();
      return;
    }
    chart = new Chart($("#ranking-chart"), {
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

  for (const button of buttons) button.addEventListener("click", () => select(button.dataset.rank));
  select("supplier");
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

async function loadDashboard() {
  const state = $("#page-status");
  try {
    const report = await fetchJSON("/api/report");
    const period = report.sample_start ? `${report.sample_start} 至 ${report.as_of}` : `截至 ${report.as_of}`;
    $("#sample-period").textContent = `样本期间  ${period}  ·  ${report.total_reviews} 条差评`;
    $("#mode-label").textContent = report.demo_mode ? "合成数据 · 无密钥演示" : "合成数据 · 兼容 API";
    renderAlerts(report);
    $("#report").hidden = false;
    state.hidden = true;
    const canChart = typeof Chart !== "undefined";
    renderDistribution(report, canChart);
    renderRankings(report, canChart);
    const button = $("#preview-button");
    button.disabled = false;
    button.addEventListener("click", showPreview);
  } catch (error) {
    console.error(error);
    state.textContent = "看板加载失败。确认本地服务已启动后，刷新页面重试。";
    state.hidden = false;
  }
}

loadDashboard();
