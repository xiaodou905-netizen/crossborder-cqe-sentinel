const sourceLabels = {reviews: "渠道评分", shipments: "出货追溯", inspections: "QMS 检验"};
const sourceStates = {pending: "待拉取", running: "拉取中", succeeded: "已校验", failed: "失败"};
const runStates = {queued: "已受理", fetching: "并行拉取", analyzing: "关联与分析", ready: "快照已生成", failed: "同步失败"};
const evidenceStages = {signal: "客诉信号", traced: "已关联批次", inspection: "关联检验观察", reproduced: "有同批次复现"};
const traceRejectionReasons = {
  missing_reference: "评价未提供追溯编号",
  unknown_reference: "出货源未找到该编号",
  sku_mismatch: "出货与评价 SKU 不一致",
  shipment_after_review: "出货日期晚于评价日期",
};
const triageStates = {pending: "待研判", investigating: "调查中", verification_requested: "已提交复验"};
let workbenchConfig;
let latestReadyRun;
let sourceBusy = false;

function showRun(run) {
  const cards = $("#source-list");
  cards.replaceChildren();
  for (const [source, label] of Object.entries(sourceLabels)) {
    const status = run?.sources.find((item) => item.source === source);
    const card = element("li", "", "source-tile");
    const heading = element("div", "", "source-tile-heading");
    const state = status?.status ?? "pending";
    const badge = element("span", sourceStates[state], "source-state");
    badge.dataset.state = state;
    heading.append(element("strong", label), badge);
    card.append(heading);
    const retries = status?.retries ? ` · 重试 ${status.retries} 次` : "";
    if (status?.status === "succeeded") {
      card.append(element("p", `${status.rows} 条 · ${status.pages} 页 · ${status.elapsed_ms} ms${retries}`));
    } else if (status?.error) {
      card.append(element("p", `${status.error}${retries}`));
    }
    cards.append(card);
  }
  const status = $("#run-status");
  status.dataset.error = String(run?.state === "failed");
  if (!run) {
    status.textContent = "待同步";
    return;
  }
  const digest = run.input_sha256 ? ` · SHA-256 ${run.input_sha256.slice(0, 12)}` : "";
  status.textContent = `${runStates[run.state]} · ${run.run_id.slice(0, 8)}${digest}${run.error ? ` · ${run.error}` : ""}`;
}

function renderSourceContext(report) {
  const trace = $("#trace-summary");
  const link = $("#snapshot-link");
  const sync = report.sync;
  trace.hidden = !sync;
  link.hidden = !sync;
  if (sync) {
    const coverage = sync.traceability;
    trace.textContent = `差评追溯 ${coverage.linked}/${report.total_reviews} · 无编号 ${coverage.missing} · 未命中 ${coverage.unmatched} · 冲突 ${coverage.conflict}`;
    link.href = `/api/v1/runs/${sync.run_id}/report`;
  }
  const details = $("#trace-rejections");
  const examples = sync?.trace_rejections ?? [];
  details.hidden = !examples.length;
  if (examples.length) {
    const unlinked = report.total_reviews - sync.traceability.linked;
    $("summary", details).textContent = unlinked === examples.length
      ? `查看 ${unlinked} 条未关联差评`
      : `查看未关联差评样本（${examples.length}/${unlinked}）`;
  }
  $("ul", details).replaceChildren(...examples.map((entry) => {
    const item = element("li", "");
    const ref = entry.trace_ref ? ` · ${entry.trace_ref}` : "";
    item.append(
      element("strong", `${entry.review_id} · ${entry.sku}${ref}`),
      element("span", traceRejectionReasons[entry.reason]),
    );
    return item;
  }));
  $("#latest-sync").hidden = !latestReadyRun || latestReadyRun.run_id === sync?.run_id;
}

function showSignalEvidence(summary, quality) {
  const {stage, inspections} = quality;
  summary.hidden = false;
  summary.dataset.stage = stage;
  summary.append(element("strong", evidenceStages[stage]));
  const record = stage === "reproduced"
    ? inspections.find((item) => item.outcome === "reproduced")
    : inspections[0];
  if (record) {
    summary.append(element("span", `${record.inspection_id} · ${record.lot} · ${record.failures}/${record.sample_size} 件异常`));
  }
}

function setSourceBusy(busy) {
  sourceBusy = busy;
  for (const selector of ["#sync-button", "#fault-button", "#latest-sync", "#import-button"]) {
    $(selector).disabled = busy;
  }
}

async function activateRun(run) {
  const report = await fetchJSON(`/api/v1/runs/${run.run_id}/activate`, {method: "POST"});
  renderDashboard(report);
  const notice = $("#update-notice");
  notice.hidden = false;
  notice.textContent = `已载入同步快照，共 ${report.input_rows} 条评分、${report.total_reviews} 条差评。`;
}

async function readRunUntilTerminal(run) {
  const deadline = Date.now() + 120_000;
  while (true) {
    showRun(run);
    if (run.state === "ready" || run.state === "failed") return run;
    if (Date.now() >= deadline) throw new Error("任务仍在运行。刷新后可查看进度。");
    await new Promise((resolve) => setTimeout(resolve, 320));
    run = await fetchJSON(`/api/v1/runs/${run.run_id}`);
  }
}

function showRunError(error) {
  const status = $("#run-status");
  status.dataset.error = "true";
  status.textContent = error.message;
}

async function observeRun(run) {
  const result = await readRunUntilTerminal(run);
  if (result.state === "ready") {
    latestReadyRun = result;
    await activateRun(result);
  }
}

async function submitRun(scenario) {
  if (sourceBusy) return;
  setSourceBusy(true);
  try {
    const run = await fetchJSON("/api/v1/runs", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({scenario}),
    });
    await observeRun(run);
  } catch (error) {
    showRunError(error);
  } finally {
    setSourceBusy(false);
  }
}

function bindSourceWorkbench(config) {
  workbenchConfig = config;
  latestReadyRun = config.latest_ready;
  $("#source-profile").textContent = config.interactive_sandbox
    ? "公网沙盒 · 合成三源分页核验 · 会话隔离"
    : (config.synthetic_sources
        ? "合成来源 · 真实 HTTP 分页 · 追溯到批次与检验记录"
        : "已配置 HTTP 来源 · 追溯到批次与检验记录");
  $("#fault-button").hidden = !config.synthetic_sources || (config.public_demo && !config.interactive_sandbox);
  showRun(config.latest);
  $("#sync-button").addEventListener("click", () => submitRun("normal"));
  $("#fault-button").addEventListener("click", () => submitRun("trace_unavailable"));
  $("#latest-sync").addEventListener("click", async () => {
    if (sourceBusy) return;
    setSourceBusy(true);
    try {
      await activateRun(latestReadyRun);
    } catch (error) {
      showRunError(error);
    } finally {
      setSourceBusy(false);
    }
  });
  if (config.latest && !["ready", "failed"].includes(config.latest.state)) {
    setSourceBusy(true);
    observeRun(config.latest).catch(showRunError).finally(() => setSourceBusy(false));
  }
}

function renderQualityEvidence(section, alert, report) {
  section.hidden = false;
  const {quality_evidence: quality, triage} = alert;
  section.append(element("h4", "批次关联、现场检验与 CQE 研判"));
  const meta = evidenceStages[quality.stage] + " · " + triageStates[triage.phase];
  section.append(element("p", meta, "quality-meta"));
  const outcome = {observed: "观察", reproduced: "已复现", fix_verified: "整改复验通过"};
  if (!quality.inspections.length) section.append(element("p", "本快照暂无匹配的现场检验记录。"));
  for (const record of quality.inspections) {
    const entry = element("div", "", "qms-entry");
    entry.append(
      element("strong", `${record.inspection_id} · ${record.lot} · ${outcome[record.outcome]}`),
      element("p", `${record.inspected_on} · ${record.failures}/${record.sample_size} 件异常 · ${record.method}`),
    );
    section.append(entry);
  }
  for (const event of triage.history) {
    section.append(element("p", `${triageStates[event.to_phase]} · ${event.note}`, "triage-history"));
  }
  if ((workbenchConfig.public_demo && !workbenchConfig.interactive_sandbox) || triage.phase === "verification_requested") return;
  const investigating = triage.phase === "investigating";
  if (investigating && quality.stage !== "reproduced") {
    section.append(element("p", "提交复验前需同批次、同失效的复现记录。", "quality-meta"));
    return;
  }
  const form = element("form", "", "triage-form");
  const label = element("label", "研判备注");
  const input = element("input", "");
  input.type = "text";
  input.required = true;
  input.minLength = 4;
  input.maxLength = 500;
  label.append(input);
  const button = element("button", investigating ? "提交复验" : "开始调查", "action-outline");
  button.type = "submit";
  const message = element("p", "", "quality-meta");
  message.setAttribute("role", "status");
  form.append(label, button, message);
  const {run_id: runId} = report.sync;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    try {
      await fetchJSON(`/api/v1/runs/${runId}/cases/${alert.case_key}/triage`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          expected_revision: triage.revision, note: input.value,
          next_phase: investigating ? "verification_requested" : "investigating",
        }),
      });
      renderDashboard(await fetchJSON("/api/report"));
    } catch (error) {
      message.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  section.append(form);
}
