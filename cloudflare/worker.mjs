import { DurableObject } from "cloudflare:workers";
import bundle from "./generated.json";

const SOURCES = ["reviews", "shipments", "inspections"];
const COOKIE = "sentinel_demo";
const SESSION_SECONDS = 12 * 60 * 60;
const MAX_RUNS = 8;
const TERMINAL = new Set(["ready", "failed"]);
const encoder = new TextDecoder("utf-8", { fatal: true });

class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function json(body, status = 200, headers = {}) {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store", ...headers } });
}

function respondToError(error) {
  if (error instanceof RequestError) return json({ detail: error.message }, error.status);
  console.error(error instanceof Error ? error.message : "Unrecognized sandbox error");
  return json({ detail: "演示服务暂时不可用，请重试。" }, 500);
}

function available(method) {
  throw new RequestError(405, `此接口使用 ${method}。`);
}

function asset(env, url, path) {
  return env.ASSETS.fetch(new Request(new URL(path, url).href));
}

async function digest(bytes) {
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(hash, byte => byte.toString(16).padStart(2, "0")).join("");
}

async function boundedBytes(request, limit) {
  if (Number(request.headers.get("Content-Length")) > limit) throw new RequestError(413, "请求内容超出演示限制。");
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const bytes = new Uint8Array(limit);
  let offset = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return bytes.subarray(0, offset);
      if (offset + value.length > limit) {
        await reader.cancel();
        throw new RequestError(413, "请求内容超出演示限制。");
      }
      bytes.set(value, offset);
      offset += value.length;
    }
  } finally {
    reader.releaseLock();
  }
}

async function jsonBody(request) {
  if (request.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "application/json") {
    throw new RequestError(415, "请提交 JSON 请求体。");
  }
  const bytes = await boundedBytes(request, 4096);
  try {
    const payload = JSON.parse(encoder.decode(bytes));
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error();
    return payload;
  } catch {
    throw new RequestError(422, "请求体必须是 JSON 对象。");
  }
}

function integerParam(url, name, fallback, min, max) {
  const raw = url.searchParams.get(name);
  if (raw === null) return fallback;
  const value = Number(raw);
  if (!/^\d+$/.test(raw) || !Number.isSafeInteger(value) || value < min || value > max) {
    throw new RequestError(422, `${name} 参数无效。`);
  }
  return value;
}

function unavailableFixture() {
  return json({ detail: "Synthetic upstream unavailable" }, 503);
}

function fixturePage(url, source) {
  const feed = bundle.feeds[source];
  const limit = integerParam(url, "limit", 50, 1, 100);
  const offset = integerParam(url, "cursor", 0, 0, Number.MAX_SAFE_INTEGER);
  const snapshot = url.searchParams.get("snapshot");
  const fault = url.searchParams.get("fault");
  if (fault !== null && fault !== "unavailable") throw new RequestError(422, "故障参数无效。");
  if (fault === "unavailable") return unavailableFixture();
  if (snapshot !== null && snapshot !== feed.snapshot_id) throw new RequestError(409, "来源快照已变化。");
  return json({
    schema_version: "1.0", snapshot_id: feed.snapshot_id, coverage: feed.coverage,
    items: feed.rows.slice(offset, offset + limit),
    next_cursor: offset + limit < feed.rows.length ? String(offset + limit) : null,
  });
}

function sessionId(request) {
  const cookie = request.headers.get("Cookie") ?? "";
  const existing = cookie.match(new RegExp(`(?:^|;\\s*)${COOKIE}=([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?=;|$)`, "i"));
  return existing ? { id: existing[1].toLowerCase(), fresh: false } : { id: crypto.randomUUID(), fresh: true };
}

function sessionResponse(response, session, url) {
  if (!session.fresh) return response;
  const headers = new Headers(response.headers);
  headers.set("Cache-Control", "no-store");
  headers.append(
    "Set-Cookie",
    `${COOKIE}=${session.id}; Path=/; Max-Age=${SESSION_SECONDS}; HttpOnly; SameSite=Lax${url.protocol === "https:" ? "; Secure" : ""}`,
  );
  return new Response(response.body, { status: response.status, headers });
}

function docs() {
  return new Response(`<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Crossborder CQE Sentinel · Sandbox API</title>
<h1>Crossborder CQE Sentinel · Sandbox API</h1>
<p>公开会话使用合成三源，内置 CSV 以外的数据请在本机工作台处理。</p>
<ul><li><a href="/">工作台</a></li><li><a href="/openapi.json">OpenAPI JSON</a></li>
<li><a href="/api/v1/contracts">三源契约</a></li><li><a href="/healthz">服务状态</a></li></ul>
</html>`, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      if (path.startsWith("/_sandbox/")) return json({ detail: "资源不存在。" }, 404);
      if (path === "/healthz" && request.method === "GET") {
        return json({ status: "ok", sandbox: "cloudflare", bundle: bundle.version });
      }
      if (path === "/docs" && request.method === "GET") return docs();
      if (path === "/api/v1/contracts" && request.method === "GET") return json(bundle.contracts);
      if (path === "/demo-source/sample-reviews.csv" && request.method === "GET") {
        const response = await asset(env, url, "/_sandbox/sample-reviews.csv");
        return new Response(response.body, {
          status: response.status,
          headers: { "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": 'attachment; filename="sentinel-demo-reviews.csv"' },
        });
      }
      if (path.startsWith("/demo-source/v1/") && request.method === "GET") {
        const source = path.slice("/demo-source/v1/".length);
        if (SOURCES.includes(source)) return fixturePage(url, source);
      }
      if (path === "/" || path === "/index.html") {
        return sessionResponse(await asset(env, url, "/index.html"), sessionId(request), url);
      }
      if (path.startsWith("/api/")) {
        if (request.method === "POST" && request.headers.has("Origin") && request.headers.get("Origin") !== url.origin) {
          throw new RequestError(403, "操作须来自当前演示站点。");
        }
        const session = sessionId(request);
        return sessionResponse(await env.DEMO_SESSIONS.getByName(session.id).fetch(request), session, url);
      }
      return env.ASSETS.fetch(request);
    } catch (error) {
      return respondToError(error);
    }
  },
};

function sourceStatus(source, status = "pending") {
  return { source, status, rows: 0, pages: 0, retries: 0, duplicates: 0, elapsed_ms: 0, snapshot_id: null, sha256: null, error: null };
}

function freshState() {
  return {
    version: bundle.version, expiresAt: Date.now() + SESSION_SECONDS * 1000,
    runs: [], lastReady: null, active: "demo", imported: null, simulations: 0,
  };
}

function currentState(value) {
  if (!value || value.version !== bundle.version || value.expiresAt <= Date.now()) return freshState();
  const run = value.runs.at(-1)?.view;
  if (run && !TERMINAL.has(run.state) && Date.now() - Date.parse(run.created_at) > 30_000) {
    run.state = "failed";
    run.error = "本轮回放已超时，上一成功快照仍可读取。";
    run.updated_at = new Date().toISOString();
    for (const source of run.sources) {
      if (source.status === "pending" || source.status === "running") {
        source.status = "failed";
        source.error = run.error;
      }
    }
  }
  return value;
}

function getRun(state, id) {
  const run = state.runs.find(record => record.view.run_id === id);
  if (!run) throw new RequestError(404, "当前会话中没有此同步任务。");
  return run;
}

function viewOf(state) {
  if (state.active === "demo") return bundle.simulations[state.simulations - 1] ?? bundle.initial;
  return state.active === "import" ? bundle.imports[state.imported] : bundle.synced;
}

function snapshot(state, run) {
  if (run.view.state !== "ready") throw new RequestError(409, "本轮同步尚无可用快照。");
  const report = bundle.synced.report;
  return {
    ...report, has_import: state.imported !== null,
    sync: { ...report.sync, run_id: run.view.run_id },
    alerts: report.alerts.map(alert => ({ ...alert, triage: run.triage[alert.case_key] ?? alert.triage })),
  };
}

function currentReport(state) {
  if (state.active !== "demo" && state.active !== "import") return snapshot(state, getRun(state, state.active));
  return { ...viewOf(state).report, has_import: state.imported !== null };
}

function runReply(run) {
  return json(run, 202, { Location: `/api/v1/runs/${run.run_id}` });
}

export class DemoSession extends DurableObject {
  async read() {
    return currentState(await this.ctx.storage.get("demo"));
  }

  async mutate(operation) {
    const result = await this.ctx.storage.transaction(async transaction => {
      const previous = await transaction.get("demo");
      const state = currentState(previous);
      const response = operation(state);
      await transaction.put("demo", state);
      return { response, alarm: previous?.expiresAt !== state.expiresAt ? state.expiresAt : null };
    });
    if (result.alarm !== null) await this.ctx.storage.setAlarm(result.alarm);
    return result.response;
  }

  async alarm() {
    await this.ctx.storage.deleteAll();
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      if (path === "/api/report" && request.method === "GET") return json(currentReport(await this.read()));
      if (path === "/api/feishu-preview" && request.method === "GET") return json(viewOf(await this.read()).card);
      if (path === "/api/v1/workbench" && request.method === "GET") {
        const state = await this.read();
        return json({
          public_demo: true, synthetic_sources: true, interactive_sandbox: true, sample_csv: bundle.sample_csv,
          latest: state.runs.at(-1)?.view ?? null,
          latest_ready: state.lastReady ? getRun(state, state.lastReady).view : null,
        });
      }
      if (path === "/api/import") {
        if (request.method !== "POST") available("POST");
        return await this.importSample(request, url);
      }
      if (path === "/api/demo-simulate" || path === "/api/restore-import") {
        if (request.method !== "POST") available("POST");
        const report = await this.mutate(state => {
          if (path === "/api/restore-import") {
            if (state.imported === null) throw new RequestError(404, "本次会话还没有导入样例。");
            state.active = "import";
          } else {
            if (state.simulations >= bundle.simulations.length) {
              throw new RequestError(409, "已达到当前会话的追加上限，可在新的无痕窗口重新演示。");
            }
            state.simulations += 1;
            state.active = "demo";
          }
          return currentReport(state);
        });
        return json(report);
      }
      if (path === "/api/v1/runs") {
        if (request.method !== "POST") available("POST");
        return await this.startRun(request, url);
      }
      const match = path.match(/^\/api\/v1\/runs\/([0-9a-f]{32})(?:\/(report|activate|cases\/([0-9a-f]+)\/triage))?$/);
      if (!match) throw new RequestError(404, "接口不存在。");
      const [, id, action, caseKey] = match;
      if (!action || action === "report") {
        if (request.method !== "GET") available("GET");
        const state = await this.read();
        const run = getRun(state, id);
        return json(action ? snapshot(state, run) : run.view);
      }
      if (request.method !== "POST") available("POST");
      if (action === "activate") {
        return json(await this.mutate(state => {
          const report = snapshot(state, getRun(state, id));
          state.active = id;
          return report;
        }));
      }
      return json(await this.triage(id, caseKey, await jsonBody(request)));
    } catch (error) {
      return respondToError(error);
    }
  }

  async importSample(request, url) {
    if (request.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "text/csv") {
      throw new RequestError(415, "请使用 text/csv 提交内置样例。");
    }
    const scope = url.searchParams.get("full_ratings") ?? "false";
    if (scope !== "true" && scope !== "false") throw new RequestError(422, "full_ratings 须为 true 或 false。");
    const bytes = await boundedBytes(request, bundle.sample_csv.bytes);
    if (bytes.length !== bundle.sample_csv.bytes || await digest(bytes) !== bundle.sample_csv.sha256) {
      throw new RequestError(422, "公网演示仅接收本站提供的内置合成 CSV。");
    }
    return json(await this.mutate(state => {
      state.imported = scope;
      state.active = "import";
      return currentReport(state);
    }));
  }

  async startRun(request, url) {
    const payload = await jsonBody(request);
    const scenario = payload.scenario ?? "normal";
    if (Object.keys(payload).some(key => key !== "scenario") || !["normal", "trace_unavailable"].includes(scenario)) {
      throw new RequestError(422, "回放场景无效。");
    }
    const key = request.headers.get("Idempotency-Key");
    if (key !== null && (!key.length || key.length > 80)) throw new RequestError(422, "幂等键长度无效。");
    const result = await this.mutate(state => {
      const existing = key === null ? null : state.runs.find(item => item.key === key);
      if (existing) {
        if (existing.view.scenario !== scenario) throw new RequestError(409, "该幂等键已用于其他场景。");
        return { view: existing.view, created: false };
      }
      const pending = state.runs.at(-1);
      if (pending && !TERMINAL.has(pending.view.state)) throw new RequestError(409, "已有同步任务在运行。");
      const now = new Date().toISOString();
      const view = {
        run_id: crypto.randomUUID().replaceAll("-", ""), scenario, profile: "synthetic_http",
        state: "queued", created_at: now, updated_at: now, sources: SOURCES.map(name => sourceStatus(name)),
        error: null, input_sha256: null, report_path: null, wall_ms: null,
      };
      state.runs.push({ view, key, triage: {} });
      if (state.runs.length > MAX_RUNS) {
        const protectedIds = new Set([state.lastReady, state.active, view.run_id]);
        state.runs.splice(state.runs.findIndex(item => !protectedIds.has(item.view.run_id)), 1);
      }
      return { view, created: true };
    });
    if (result.created) this.ctx.waitUntil(this.replay(result.view, url));
    return runReply(result.view);
  }

  async replay(run, url) {
    const start = Date.now();
    const update = (state, apply) => {
      const current = getRun(state, run.run_id).view;
      if (TERMINAL.has(current.state)) return;
      apply(current);
      current.updated_at = new Date().toISOString();
    };
    try {
      await this.mutate(state => update(state, current => {
        current.state = "fetching";
        current.sources = SOURCES.map(name => sourceStatus(name, "running"));
      }));
      const results = await Promise.all(SOURCES.map(async source => {
        const status = await readSource(this.env, url, source, run.scenario);
        await this.mutate(state => update(state, current => {
          current.sources = current.sources.map(item => item.source === source ? status : item);
        }));
        return status;
      }));
      if (results.some(result => result.status !== "succeeded")) {
        await this.mutate(state => update(state, current => {
          current.state = "failed";
          current.error = "至少一条来源失败。已成功发布的快照可继续使用。";
          current.wall_ms = Date.now() - start;
        }));
        return;
      }
      await this.mutate(state => update(state, current => { current.state = "analyzing"; }));
      await this.mutate(state => update(state, current => {
        current.state = "ready";
        current.input_sha256 = bundle.synced.report.sync.input_sha256;
        current.report_path = `/api/v1/runs/${current.run_id}/report`;
        current.wall_ms = Date.now() - start;
        state.lastReady = current.run_id;
      }));
    } catch (error) {
      console.error(error instanceof Error ? error.message : "Unable to replay");
      await this.mutate(state => update(state, current => {
        current.state = "failed";
        current.error = "本轮回放未完成。";
        current.wall_ms = Date.now() - start;
      }));
    }
  }

  async triage(id, caseKey, payload) {
    const { expected_revision: revision, next_phase: phase, note } = payload;
    if (!Number.isSafeInteger(revision) || revision < 0 || typeof note !== "string"
        || note.trim().length < 4 || note.trim().length > 500
        || !["investigating", "verification_requested"].includes(phase)
        || Object.keys(payload).some(key => !["expected_revision", "next_phase", "note"].includes(key))) {
      throw new RequestError(422, "研判请求无效。");
    }
    return this.mutate(state => {
      const run = getRun(state, id);
      if (run.view.state !== "ready") throw new RequestError(409, "同步尚未完成。");
      const alert = bundle.synced.report.alerts.find(item => item.case_key === caseKey);
      if (!alert) throw new RequestError(404, "此快照内没有该质量事件。");
      const previous = run.triage[caseKey] ?? alert.triage;
      if (previous.revision !== revision) throw new RequestError(409, "研判版本已变化，请刷新后重试。");
      const expected = { pending: "investigating", investigating: "verification_requested" }[previous.phase];
      if (phase !== expected) throw new RequestError(409, "当前研判状态不允许此流转。");
      if (phase === "verification_requested" && alert.quality_evidence.stage !== "reproduced") {
        throw new RequestError(409, "提交复验需要同 SKU、批次和失效代码的复现记录。");
      }
      const event = {
        revision: revision + 1, from_phase: previous.phase, to_phase: phase,
        note: note.trim(), recorded_at: new Date().toISOString(),
      };
      const result = { phase, revision: event.revision, history: [...previous.history, event] };
      run.triage[caseKey] = result;
      return result;
    });
  }
}

async function readSource(env, url, source, scenario) {
  const start = Date.now();
  const feed = bundle.feeds[source];
  const status = sourceStatus(source, "running");
  const unavailable = scenario === "trace_unavailable" && source === "shipments";
  try {
    for (const page of feed.pages) {
      let response;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        response = unavailable ? unavailableFixture() : await asset(env, url, page.path);
        if (response.ok) break;
        const retryable = response.status === 429 || (response.status >= 500 && response.status < 600);
        if (!retryable || attempt === 2) {
          throw new Error(`上游 HTTP ${response.status}`);
        }
        status.retries += 1;
        await response.body?.cancel();
        await new Promise(resolve => setTimeout(resolve, 120 * 2 ** attempt));
      }
      const bytes = await response.arrayBuffer();
      if (await digest(bytes) !== page.sha256) throw new Error("分页指纹与发布的合成快照不符。");
      const result = JSON.parse(encoder.decode(bytes));
      if (result.snapshot_id !== feed.snapshot_id || result.coverage !== feed.coverage) {
        throw new Error("分页快照或覆盖类型不一致。");
      }
      status.pages += 1;
      status.rows += result.items.length;
    }
    if (status.rows !== feed.rows.length) throw new Error("分页记录数与合成快照不符。");
    status.status = "succeeded";
    status.snapshot_id = feed.snapshot_id;
    status.sha256 = feed.sha256;
  } catch (error) {
    status.status = "failed";
    status.error = error instanceof Error ? error.message : "上游回放失败。";
  }
  status.elapsed_ms = Date.now() - start;
  return status;
}
