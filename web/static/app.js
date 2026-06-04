const stages = [
  ["thinking_budget_zero", "thinkingBudget=0"],
  ["field_sampling", "字段采样"],
  ["error_path_leak", "错误路径泄露"],
  ["cached_contents", "cachedContents"],
  ["http_headers", "HTTP 头"],
  ["count_tokens", "countTokens"],
  ["identity", "identity 自报家门"],
  ["knowledge", "知识截止探针"],
  ["self_sig", "自 sig 重复性"],
  ["cross_sig", "跨 key sig 矩阵"],
];

const form = document.getElementById("auditForm");
const timeline = document.getElementById("timeline");
const logOutput = document.getElementById("logOutput");
const statusText = document.getElementById("statusText");
const statusPill = document.getElementById("statusPill");
const startButton = document.getElementById("startButton");
const manualProbeForm = document.getElementById("manualProbeForm");
const modelEnumForm = document.getElementById("modelEnumForm");
const manualStartButton = document.getElementById("manualStartButton");
const modelEnumStartButton = document.getElementById("modelEnumStartButton");
const tabButtons = document.querySelectorAll("[data-tab]");
const toolForms = document.querySelectorAll("[data-tool-panel]");
const reportPanel = document.getElementById("reportPanel");
const reportTitle = document.getElementById("reportTitle");
const reportMeta = document.getElementById("reportMeta");
const summaryGrid = document.getElementById("summaryGrid");
const detailGrid = document.getElementById("detailGrid");
const artifactLinks = document.getElementById("artifactLinks");
const markdownOutput = document.getElementById("markdownOutput");
const clearLogButton = document.getElementById("clearLogButton");
const authToken = new URLSearchParams(window.location.search).get("token") || "";

let events = null;
let currentRunId = null;
let currentTool = "audit";
let activeStage = null;
const seenStages = new Set();

initTimeline();
initTabs();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitRun("audit", "/api/runs", formPayload(), startButton);
});

manualProbeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitRun("manual_probe", "/api/tools/manual-probe", manualProbePayload(), manualStartButton);
});

modelEnumForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitRun("model_enum", "/api/tools/model-enum", modelEnumPayload(), modelEnumStartButton);
});

clearLogButton.addEventListener("click", () => {
  logOutput.textContent = "";
});

function formPayload() {
  const data = Object.fromEntries(new FormData(form).entries());
  data.skip_active = form.elements.skip_active.checked;
  data.skip_tier4 = form.elements.skip_tier4.checked;
  data.skip_cross_sig = form.elements.skip_cross_sig.checked;
  return data;
}

function manualProbePayload() {
  const data = Object.fromEntries(new FormData(manualProbeForm).entries());
  data.steps = Array.from(manualProbeForm.querySelectorAll("input[name='steps']:checked"))
    .map((input) => input.value);
  return data;
}

function modelEnumPayload() {
  return Object.fromEntries(new FormData(modelEnumForm).entries());
}

async function submitRun(tool, endpoint, payload, button) {
  currentTool = tool;
  button.disabled = true;
  closeEvents();
  resetRunUi();
  setStatus("running", "创建检测任务");

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    currentRunId = data.id;
    setStatus(data.status, `任务 ${data.id}`);
    connectEvents(data.id);
  } catch (error) {
    setStatus("failed", error.message);
    button.disabled = false;
  }
}

function jsonHeaders() {
  const headers = {"Content-Type": "application/json"};
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`;
  }
  return headers;
}

function connectEvents(runId) {
  const query = authToken ? `?token=${encodeURIComponent(authToken)}` : "";
  events = new EventSource(`/api/runs/${runId}/events${query}`);
  events.onmessage = (message) => {
    const event = JSON.parse(message.data);
    handleRunEvent(event);
  };
  events.onerror = () => {
    appendLog("[web] event stream closed");
    closeEvents();
    startButton.disabled = false;
    manualStartButton.disabled = false;
    modelEnumStartButton.disabled = false;
  };
}

function handleRunEvent(event) {
  if (event.kind !== "heartbeat" && event.line) {
    appendLog(event.line);
  }
  if (event.kind === "stage") {
    setActiveStage(event.stage);
  }
  if (event.kind === "key") {
    statusText.textContent = `检测 ${event.key}`;
  }
  if (event.kind === "error") {
    setStatus("failed", "检测出现错误");
  }
  if (event.kind === "status") {
    setStatus(event.status, event.line || event.status);
    if (event.status === "completed") {
      finishStages();
      if (currentTool === "audit") {
        fetchReport();
      } else {
        fetchToolRun();
      }
    }
    if (event.status === "failed") {
      failActiveStage();
      if (currentTool === "audit") {
        fetchReport();
      } else {
        fetchToolRun();
      }
    }
  }
  if (event.kind === "done") {
    closeEvents();
    startButton.disabled = false;
    manualStartButton.disabled = false;
    modelEnumStartButton.disabled = false;
  }
}

function appendLog(line) {
  const atBottom = logOutput.scrollTop + logOutput.clientHeight >= logOutput.scrollHeight - 24;
  logOutput.textContent += `${line}\n`;
  if (atBottom) {
    logOutput.scrollTop = logOutput.scrollHeight;
  }
}

function initTimeline() {
  timeline.innerHTML = "";
  for (const [stage, label] of stages) {
    const item = document.createElement("li");
    item.dataset.stage = stage;
    item.textContent = label;
    timeline.appendChild(item);
  }
}

function initTabs() {
  for (const button of tabButtons) {
    button.addEventListener("click", () => {
      const selected = button.dataset.tab;
      for (const item of tabButtons) {
        item.classList.toggle("active", item === button);
      }
      for (const panel of toolForms) {
        panel.classList.toggle("hidden", panel.dataset.toolPanel !== selected);
        panel.classList.toggle("active", panel.dataset.toolPanel === selected);
      }
    });
  }
}

function setActiveStage(stage) {
  if (!stage || stage === "unknown") {
    return;
  }
  if (activeStage && activeStage !== stage) {
    markStage(activeStage, "done");
  }
  activeStage = stage;
  seenStages.add(stage);
  markStage(stage, "active");
}

function markStage(stage, state) {
  const item = timeline.querySelector(`[data-stage="${stage}"]`);
  if (!item) {
    return;
  }
  item.classList.remove("active", "done", "failed");
  item.classList.add(state);
}

function finishStages() {
  for (const stage of seenStages) {
    markStage(stage, "done");
  }
}

function failActiveStage() {
  if (activeStage) {
    markStage(activeStage, "failed");
  }
}

function setStatus(status, text) {
  statusText.textContent = text || status;
  statusPill.textContent = status || "Idle";
  statusPill.className = `status-pill ${status || "idle"}`;
}

async function fetchReport() {
  if (!currentRunId) {
    return;
  }
  try {
    const response = await fetch(`/api/runs/${currentRunId}/report`, {headers: jsonHeaders()});
    if (!response.ok) {
      return;
    }
    const report = await response.json();
    renderReport(report);
  } catch (error) {
    appendLog(`[web] failed to load report: ${error.message}`);
  }
}

async function fetchToolRun() {
  if (!currentRunId) {
    return;
  }
  try {
    const response = await fetch(`/api/runs/${currentRunId}`, {headers: jsonHeaders()});
    if (!response.ok) {
      return;
    }
    const run = await response.json();
    renderToolRun(run);
  } catch (error) {
    appendLog(`[web] failed to load tool run: ${error.message}`);
  }
}

function renderReport(report) {
  const raw = report.raw || {};
  const meta = raw.meta || {};
  const verdict = raw.verdict || {};
  reportPanel.classList.remove("hidden");
  reportTitle.textContent = "可视化报告";
  reportMeta.textContent = `${meta.name || ""} ${meta.started_at || ""} ${meta.base || ""}`.trim();
  artifactLinks.innerHTML = `
    <a href="${report.artifacts.verdict_json}" target="_blank" rel="noreferrer">verdict.json</a>
    <a href="${report.artifacts.report_md}" target="_blank" rel="noreferrer">report.md</a>
  `;
  markdownOutput.textContent = report.markdown || "";
  renderSummary(verdict.per_key || {});
  renderDetails(raw);
}

function renderToolRun(run) {
  reportPanel.classList.remove("hidden");
  reportTitle.textContent = currentTool === "model_enum" ? "模型枚举结果" : "手工探针结果";
  reportMeta.textContent = `${run.id} · ${run.status} · ${run.request?.base || ""}`;
  const artifacts = run.artifacts || {};
  artifactLinks.innerHTML = Object.entries(artifacts).map(([name, url]) => (
    `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(name)}</a>`
  )).join("");
  summaryGrid.innerHTML = `
    <article class="result-card">
      <h3>${escapeHtml(toolLabel(currentTool))}</h3>
      <div class="label-line">${escapeHtml(run.status || "?")}</div>
      <span class="confidence">returncode: ${escapeHtml(run.returncode ?? "?")}</span>
      <ul class="evidence-list">
        <li>base: ${escapeHtml(run.request?.base || "?")}</li>
        <li>created: ${escapeHtml(run.created_at || "?")}</li>
        <li>finished: ${escapeHtml(run.finished_at || "?")}</li>
      </ul>
    </article>
  `;
  detailGrid.innerHTML = "";
  markdownOutput.textContent = "";
}

function renderSummary(perKeyVerdict) {
  summaryGrid.innerHTML = "";
  for (const [keyName, verdict] of Object.entries(perKeyVerdict)) {
    const card = document.createElement("article");
    card.className = "result-card";
    const evidence = (verdict.evidence || []).slice(-4).map((item) => (
      `<li><strong>Tier ${escapeHtml(item.tier || "?")}</strong> ${escapeHtml(item.name || "")}: ${escapeHtml(item.fact || "")}</li>`
    )).join("");
    const caveats = (verdict.caveats || []).slice(0, 3).map((item) => (
      `<li>${escapeHtml(item)}</li>`
    )).join("");
    card.innerHTML = `
      <h3>${escapeHtml(keyName)}</h3>
      <div class="label-line">${escapeHtml(verdict.label || "?")}</div>
      <span class="confidence">confidence: ${escapeHtml(verdict.confidence || "?")}</span>
      <ul class="evidence-list">${evidence || "<li>无证据数据</li>"}</ul>
      ${caveats ? `<ul class="evidence-list">${caveats}</ul>` : ""}
    `;
    summaryGrid.appendChild(card);
  }
}

function renderDetails(raw) {
  detailGrid.innerHTML = "";
  const perKey = raw.per_key || {};
  for (const [keyName, data] of Object.entries(perKey)) {
    const active = data.active || {};
    const tier4 = data.tier4 || {};
    detailGrid.appendChild(fieldSamplingCard(keyName, active.field_sampling || {}));
    detailGrid.appendChild(headersCard(keyName, active.http_headers || {}));
    detailGrid.appendChild(tier4Card(keyName, tier4));
  }
  if (raw.cross_sig_matrix && !raw.cross_sig_matrix.skipped) {
    detailGrid.appendChild(crossMatrixCard(raw.cross_sig_matrix));
  }
}

function fieldSamplingCard(keyName, fieldSampling) {
  const card = document.createElement("article");
  card.className = "detail-card";
  const counts = fieldSampling.upstream_count || {};
  const valid = Number(fieldSampling.valid_n || fieldSampling.n || 0);
  const bars = Object.entries(counts).map(([name, count]) => barRow(name, count, valid)).join("");
  const modelVersions = Object.entries(fieldSampling.modelVersion_count || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([name, count]) => `<li>${escapeHtml(name)}: ${escapeHtml(count)}</li>`)
    .join("");
  card.innerHTML = `
    <h3>${escapeHtml(keyName)} · 字段采样</h3>
    ${bars || "<p class=\"empty\">无字段采样数据</p>"}
    <ul class="kv-list">
      <li>有效样本: ${escapeHtml(valid)}</li>
      <li>错误样本: ${escapeHtml(fieldSampling.error_count || 0)}</li>
      <li>占位符率: ${escapeHtml(percent(fieldSampling.placeholder_rate || 0))}</li>
      ${modelVersions}
    </ul>
  `;
  return card;
}

function headersCard(keyName, headers) {
  const card = document.createElement("article");
  card.className = "detail-card";
  const interesting = headers.interesting_headers || {};
  const rows = Object.entries(interesting).map(([name, value]) => (
    `<li>${escapeHtml(name)}: ${escapeHtml(value)}</li>`
  )).join("");
  card.innerHTML = `
    <h3>${escapeHtml(keyName)} · HTTP 头</h3>
    <ul class="kv-list">
      <li>框架: ${escapeHtml(headers.framework_hint || "?")}</li>
      <li>上游: ${escapeHtml(headers.upstream_hint || "?")}</li>
      ${rows || "<li>无关键响应头</li>"}
    </ul>
  `;
  return card;
}

function tier4Card(keyName, tier4) {
  const card = document.createElement("article");
  card.className = "detail-card";
  const knowledge = tier4.knowledge || {};
  const selfSig = tier4.self_sig || {};
  const answers = (knowledge.results || []).map((result) => (
    `<li>${escapeHtml(result.id)}: ${escapeHtml(result.outcome)} · ${escapeHtml(result.answer || "")}</li>`
  )).join("");
  card.innerHTML = `
    <h3>${escapeHtml(keyName)} · Tier 4</h3>
    <ul class="kv-list">
      <li>知识探针: ${escapeHtml(knowledge.verdict || "未运行")}</li>
      <li>答对: ${escapeHtml(knowledge.pass_count || 0)} / ${escapeHtml(knowledge.answered_n || 0)} / ${escapeHtml(knowledge.total || 0)}</li>
      <li>自 sig: ${escapeHtml(selfSig.verdict || "未运行")}</li>
      <li>PASS/FAIL: ${escapeHtml(selfSig.pass || 0)} / ${escapeHtml(selfSig.fail || 0)}</li>
      ${answers}
    </ul>
  `;
  return card;
}

function crossMatrixCard(cross) {
  const card = document.createElement("article");
  card.className = "detail-card";
  const keys = cross.keys || [];
  const header = keys.map((key) => `<th>${escapeHtml(key)}</th>`).join("");
  const rows = keys.map((src) => {
    const cells = keys.map((dst) => {
      const status = ((cross.matrix || {})[src] || {})[dst]?.status || "?";
      const cls = status === "PASS" ? "pass" : (status.includes("FAIL") ? "fail" : "");
      return `<td class="${cls}">${escapeHtml(status)}</td>`;
    }).join("");
    return `<tr><th>${escapeHtml(src)}</th>${cells}</tr>`;
  }).join("");
  const insights = (cross.insights || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  card.innerHTML = `
    <h3>跨 key sig 矩阵</h3>
    <table class="matrix"><thead><tr><th>src \\ dst</th>${header}</tr></thead><tbody>${rows}</tbody></table>
    <ul class="kv-list">${insights}</ul>
  `;
  return card;
}

function barRow(name, count, total) {
  const pct = total > 0 ? Math.round((Number(count) / total) * 100) : 0;
  return `
    <div class="bar-row">
      <span>${escapeHtml(name)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <strong>${escapeHtml(count)} / ${escapeHtml(total)}</strong>
    </div>
  `;
}

function percent(value) {
  return `${Math.round(Number(value) * 1000) / 10}%`;
}

function resetRunUi() {
  logOutput.textContent = "";
  reportPanel.classList.add("hidden");
  summaryGrid.innerHTML = "";
  detailGrid.innerHTML = "";
  artifactLinks.innerHTML = "";
  markdownOutput.textContent = "";
  seenStages.clear();
  activeStage = null;
  initTimeline();
}

function toolLabel(tool) {
  if (tool === "manual_probe") {
    return "手工低频探针";
  }
  if (tool === "model_enum") {
    return "模型枚举";
  }
  return "完整审计";
}

function closeEvents() {
  if (events) {
    events.close();
    events = null;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
