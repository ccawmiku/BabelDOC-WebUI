const form = document.querySelector("#translate-form");
const fileInput = document.querySelector("#pdf-input");
const dropZone = document.querySelector("#drop-zone");
const dropTitle = document.querySelector("#drop-title");
const dropSubtitle = document.querySelector("#drop-subtitle");
const fileMeta = document.querySelector("#file-meta");
const submitButton = document.querySelector("#submit-button");
const formError = document.querySelector("#form-error");
const emptyState = document.querySelector("#empty-state");
const progressState = document.querySelector("#progress-state");
const resultState = document.querySelector("#result-state");
const failedState = document.querySelector("#failed-state");
const jobStatus = document.querySelector("#job-status");
const cancelButton = document.querySelector("#cancel-button");
const retryButton = document.querySelector("#retry-button");
const newTaskButton = document.querySelector("#new-task-button");
const apiKeyInput = document.querySelector("#api-key");
const apiKeyStatus = document.querySelector("#api-key-status");
const apiKeyLabel = document.querySelector("#api-key-label");
const clearKeyButton = document.querySelector("#clear-key");
const modelSelect = document.querySelector("#model");
const modelStatus = document.querySelector("#model-status");
const refreshModelsButton = document.querySelector("#refresh-models");
const glossarySummary = document.querySelector("#glossary-summary");
const glossaryCount = document.querySelector("#glossary-count");
const glossaryTop = document.querySelector("#glossary-top");
const viewGlossaryButton = document.querySelector("#view-glossary-button");
const glossaryDialog = document.querySelector("#glossary-dialog");
const glossarySearch = document.querySelector("#glossary-search");
const glossaryViewStatus = document.querySelector("#glossary-view-status");
const glossaryTableBody = document.querySelector("#glossary-table-body");
const activityHint = document.querySelector("#activity-hint");

let activeJobId = null;
let pollTimer = null;
let apiKeySaved = false;
let glossaryEntries = [];
let glossaryEntryCount = 0;
let glossaryWasTruncated = false;

const stageAliases = {
  ParsePDF: "Parse PDF and Create Intermediate Representation",
  ParsePageLayout: "Parse Page Layout",
  ParseParagraph: "Parse Paragraphs",
  StylesAndFormulas: "Parse Formulas and Styles",
  Translate: "Translate Paragraphs",
  GeneratePDF: "Generate drawing instructions",
  AddDebugInformation: "Add Debug Information",
  AutomaticTermExtractor: "Automatic Term Extraction",
  TableParser: "Parse Table",
};

const stageCatalog = {
  "等待处理": ["等待处理", "任务已进入队列，正在等待后台开始。"],
  "准备模型": ["准备模型", "正在加载版面分析模型和字体资源。"],
  "Parse PDF and Create Intermediate Representation": ["解析 PDF", "正在读取文字、图片、字符坐标与页面结构。"],
  DetectScannedFile: ["检测扫描文档", "正在判断 PDF 是否缺少可用文字层。"],
  "Parse Page Layout": ["分析页面布局", "正在识别标题、正文、图片、表格等版面区域。"],
  "Parse Table": ["识别表格", "正在分析表格边界、行列与单元格结构。"],
  "Parse Paragraphs": ["整理文本段落", "正在合并断行并建立可翻译的段落单元。"],
  "Parse Formulas and Styles": ["处理公式与样式", "正在保护公式、字体样式和原有格式标记。"],
  "Automatic Term Extraction": ["自动提取术语", "正在分批调用模型，提取并统一专业词汇。"],
  "Translate Paragraphs": ["翻译文本段落", "正在分批发送正文，并等待模型返回译文。"],
  Typesetting: ["重新排版", "正在把译文放回页面并调整行距与位置。"],
  "Add Fonts": ["匹配字体", "正在选择并映射目标语言所需字体。"],
  "Generate drawing instructions": ["生成页面内容", "正在把译文、图片和版式转换成 PDF 绘制指令。"],
  "Subset font": ["裁剪字体", "正在只保留文档实际使用的字体字符。"],
  "Save PDF": ["保存 PDF", "正在写入最终 PDF 文件并完成输出。"],
  "Add Debug Information": ["写入调试信息", "正在添加用于问题诊断的页面信息。"],
  "处理完成": ["处理完成", "翻译、排版和文件保存均已完成。"],
};

function humanSize(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function setSelectedFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    showFormError("请选择 PDF 文件。");
    return;
  }
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  dropZone.classList.add("has-file");
  dropTitle.textContent = file.name;
  dropSubtitle.textContent = "已选择文档，点击可重新选择";
  fileMeta.textContent = humanSize(file.size);
  fileMeta.hidden = false;
  showFormError("");
}

function showFormError(message) {
  formError.textContent = message;
  formError.hidden = !message;
}

function showState(name) {
  emptyState.hidden = name !== "empty";
  progressState.hidden = name !== "progress";
  resultState.hidden = name !== "result";
  failedState.hidden = name !== "failed";
}

function statusLabel(status) {
  return {
    queued: "队列中",
    running: "处理中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status] || "等待任务";
}

function stageDetails(stage) {
  if (!stage) return { label: "处理中", description: "后台正在处理当前文档。" };
  const shortName = stage.split(".").at(-1);
  const canonical = stageAliases[stage] || stageAliases[shortName] || stage;
  const details = stageCatalog[canonical];
  if (details) return { label: details[0], description: details[1], canonical };
  return { label: stage, description: `正在执行：${stage}`, canonical: stage };
}

function activityMessage(job) {
  if (job.status === "queued") return "仍在工作 · 任务已进入队列";
  const updatedAt = Date.parse(job.last_activity_at || job.started_at || job.created_at);
  const seconds = Number.isFinite(updatedAt)
    ? Math.max(0, Math.floor((Date.now() - updatedAt) / 1000))
    : 0;
  if (seconds < 10) return "仍在工作 · 刚刚有新进展";
  if (seconds < 60) return `仍在工作 · ${seconds} 秒前有新进展`;
  const minutes = Math.max(1, Math.floor(seconds / 60));
  const canonical = stageDetails(job.stage).canonical;
  if (["Automatic Term Extraction", "Translate Paragraphs"].includes(canonical)) {
    return `仍在工作 · 正在等待模型响应 · ${minutes} 分钟前有进展`;
  }
  return `仍在工作 · 当前步骤耗时较长 · ${minutes} 分钟前有进展`;
}

function formatTokenCount(value) {
  const count = Math.max(0, Number(value) || 0);
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(count);
}

function renderTokenUsage(job) {
  const usage = job.token_usage || {};
  document.querySelectorAll("[data-token-key]").forEach((element) => {
    element.textContent = formatTokenCount(usage[element.dataset.tokenKey]);
  });
}

function renderGlossarySummary(glossary) {
  const available = Boolean(glossary?.available);
  glossarySummary.hidden = !available;
  glossaryTop.replaceChildren();
  if (!available) return;

  glossaryCount.textContent = formatTokenCount(glossary.count);
  viewGlossaryButton.dataset.url = glossary.url || "";
  viewGlossaryButton.disabled = !glossary.url;
  const terms = Array.isArray(glossary.top_terms) ? glossary.top_terms.slice(0, 10) : [];
  if (!terms.length) {
    const empty = document.createElement("p");
    empty.className = "glossary-empty";
    empty.textContent = "未提取到可显示的术语";
    glossaryTop.append(empty);
    return;
  }

  terms.forEach((term, index) => {
    const row = document.createElement("div");
    row.className = "glossary-term";
    const number = document.createElement("b");
    number.textContent = String(index + 1).padStart(2, "0");
    const source = document.createElement("span");
    source.textContent = term.source || "—";
    source.title = term.source || "";
    const arrow = document.createElement("i");
    arrow.textContent = "→";
    const target = document.createElement("span");
    target.className = "term-target";
    target.textContent = term.target || "—";
    target.title = term.target || "";
    row.append(number, source, arrow, target);
    glossaryTop.append(row);
  });
}

function renderGlossaryTable(entries) {
  glossaryTableBody.replaceChildren();
  if (!entries.length) {
    const row = document.createElement("tr");
    row.className = "empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = glossarySearch.value ? "没有匹配的术语" : "术语表为空";
    row.append(cell);
    glossaryTableBody.append(row);
  } else {
    const fragment = document.createDocumentFragment();
    entries.forEach((term, index) => {
      const row = document.createElement("tr");
      [index + 1, term.source || "—", term.target || "—"].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      fragment.append(row);
    });
    glossaryTableBody.append(fragment);
  }
  const suffix = glossaryWasTruncated ? "，文件较大，仅加载前 20,000 条" : "";
  glossaryViewStatus.textContent = glossarySearch.value
    ? `${formatTokenCount(entries.length)} / ${formatTokenCount(glossaryEntryCount)} 条${suffix}`
    : `共 ${formatTokenCount(glossaryEntryCount)} 条${suffix}`;
}

function filterGlossary() {
  const query = glossarySearch.value.trim().toLocaleLowerCase();
  if (!query) {
    renderGlossaryTable(glossaryEntries);
    return;
  }
  renderGlossaryTable(
    glossaryEntries.filter((term) =>
      `${term.source || ""}\n${term.target || ""}`.toLocaleLowerCase().includes(query),
    ),
  );
}

async function openGlossary() {
  const url = viewGlossaryButton.dataset.url;
  if (!url) return;
  viewGlossaryButton.disabled = true;
  glossarySearch.value = "";
  glossaryViewStatus.textContent = "正在读取…";
  glossaryTableBody.replaceChildren();
  if (!glossaryDialog.open) glossaryDialog.showModal();
  try {
    const payload = await api(url);
    glossaryEntries = Array.isArray(payload.entries) ? payload.entries : [];
    glossaryEntryCount = Number(payload.count) || glossaryEntries.length;
    glossaryWasTruncated = Boolean(payload.truncated);
    renderGlossaryTable(glossaryEntries);
  } catch (error) {
    renderGlossaryTable([]);
    glossaryViewStatus.textContent = error.message;
  } finally {
    viewGlossaryButton.disabled = false;
  }
}

function renderJob(job) {
  jobStatus.textContent = statusLabel(job.status);
  jobStatus.className = `job-badge ${job.status}`;
  document.querySelector("#current-filename").textContent = job.filename;
  const currentStage = stageDetails(job.stage);
  document.querySelector("#job-message").textContent = currentStage.description;
  renderTokenUsage(job);

  if (["queued", "running"].includes(job.status)) {
    showState("progress");
    const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
    const stageProgress = Math.max(0, Math.min(100, Number(job.stage_progress) || 0));
    document.querySelector("#progress-value").textContent = progress.toFixed(1);
    document.querySelector("#progress-ring").style.setProperty("--progress", `${progress * 3.6}deg`);
    document.querySelector("#progress-bar").style.width = `${progress}%`;
    document.querySelector("#stage-name").textContent = currentStage.label;
    document.querySelector("#stage-progress").textContent = `${stageProgress.toFixed(1)}%`;
    activityHint.querySelector("span").textContent = activityMessage(job);
    cancelButton.disabled = Boolean(job.cancel_requested);
    cancelButton.textContent = job.cancel_requested ? "正在取消…" : "取消任务";
    return;
  }

  if (job.status === "completed") {
    showState("result");
    renderGlossarySummary(job.glossary);
    const list = document.querySelector("#download-list");
    list.replaceChildren();
    const downloadableOutputs = (job.outputs || []).filter((output) =>
      output.name.toLowerCase().endsWith(".pdf"),
    );
    if (!downloadableOutputs.length) {
      const message = document.createElement("p");
      message.className = "form-error";
      message.textContent = "任务已完成，但没有发现可下载文件。";
      list.append(message);
    }
    downloadableOutputs.forEach((output) => {
      const link = document.createElement("a");
      link.className = "download-link";
      link.href = output.url;
      link.innerHTML = `<span>↓</span><div><strong>${escapeHtml(output.label)}</strong><small>${escapeHtml(output.name)}</small></div><b>›</b>`;
      list.append(link);
    });
    stopPolling();
    submitButton.disabled = false;
    return;
  }

  showState("failed");
  document.querySelector("#failed-title").textContent = job.status === "cancelled" ? "任务已取消" : "处理失败";
  document.querySelector("#failed-message").textContent = job.error || job.message || "请检查参数后重试。";
  stopPolling();
  submitButton.disabled = false;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(payload?.detail || `请求失败（${response.status}）`);
  }
  return payload;
}

async function pollJob() {
  if (!activeJobId) return;
  try {
    renderJob(await api(`/api/jobs/${activeJobId}`));
  } catch (error) {
    showFormError(error.message);
  }
}

function startPolling() {
  stopPolling();
  pollJob();
  pollTimer = window.setInterval(pollJob, 1000);
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = null;
}

function ensureModelOption(model) {
  if (!model) return;
  const exists = Array.from(modelSelect.options).some((option) => option.value === model);
  if (!exists) modelSelect.add(new Option(model, model));
  modelSelect.value = model;
}

function renderSettings(settings) {
  apiKeySaved = Boolean(settings.api_key_saved);
  apiKeyStatus.textContent = apiKeySaved
    ? "API Key 已加密保存到本机，可直接使用。"
    : "填写后会加密保存到本机。";
  apiKeyStatus.classList.toggle("saved", apiKeySaved);
  apiKeyLabel.textContent = apiKeySaved ? "已保存" : "首次填写";
  clearKeyButton.hidden = !apiKeySaved;
  apiKeyInput.placeholder = apiKeySaved ? "已保存，留空即可使用" : "sk-…";
  if (settings.base_url) form.elements.base_url.value = settings.base_url;
  ensureModelOption(settings.model);
}

async function loadModels() {
  refreshModelsButton.disabled = true;
  refreshModelsButton.textContent = "读取中…";
  modelStatus.textContent = "正在连接接口并读取模型列表…";
  const preferred = modelSelect.value;
  try {
    const data = new FormData();
    data.set("api_key", apiKeyInput.value.trim());
    data.set("base_url", form.elements.base_url.value.trim());
    const result = await api("/api/models", { method: "POST", body: data });
    modelSelect.replaceChildren();
    result.models.forEach((model) => modelSelect.add(new Option(model, model)));
    let selectedModel = result.models[0];
    if (result.models.includes(preferred)) selectedModel = preferred;
    else if (result.model && result.models.includes(result.model)) selectedModel = result.model;
    modelSelect.value = selectedModel;
    modelStatus.textContent = `已读取 ${result.models.length} 个可用模型。`;
    apiKeyInput.value = "";
    renderSettings({...result, model: selectedModel});
  } catch (error) {
    modelStatus.textContent = error.message;
  } finally {
    refreshModelsButton.disabled = false;
    refreshModelsButton.textContent = "读取列表";
  }
}

async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    renderSettings(settings);
    if (settings.api_key_saved) await loadModels();
  } catch (error) {
    apiKeyStatus.textContent = error.message;
  }
}

function savePreferences() {
  const preferences = {
    lang_in: form.elements.lang_in.value,
    lang_out: form.elements.lang_out.value,
    qps: form.elements.qps.value,
    reasoning: form.elements.reasoning.value,
    output_mode: form.elements.output_mode.value,
  };
  localStorage.setItem("babeldoc-web-preferences", JSON.stringify(preferences));
}

function loadPreferences() {
  try {
    const preferences = JSON.parse(localStorage.getItem("babeldoc-web-preferences") || "{}");
    Object.entries(preferences).forEach(([name, value]) => {
      if (form.elements[name] && value != null) form.elements[name].value = value;
    });
  } catch {
    localStorage.removeItem("babeldoc-web-preferences");
  }
}

fileInput.addEventListener("change", () => setSelectedFile(fileInput.files[0]));
["dragenter", "dragover"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
});
["dragleave", "drop"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
});
dropZone.addEventListener("drop", (event) => setSelectedFile(event.dataTransfer.files[0]));

document.querySelector("#toggle-key").addEventListener("click", (event) => {
  const visible = apiKeyInput.type === "text";
  apiKeyInput.type = visible ? "password" : "text";
  event.currentTarget.textContent = visible ? "显示" : "隐藏";
  event.currentTarget.setAttribute("aria-label", visible ? "显示 API Key" : "隐藏 API Key");
});

refreshModelsButton.addEventListener("click", loadModels);

clearKeyButton.addEventListener("click", async () => {
  clearKeyButton.disabled = true;
  try {
    renderSettings(await api("/api/settings/api-key", { method: "DELETE" }));
    modelStatus.textContent = "已清除密钥；填写新密钥后重新读取模型。";
  } catch (error) {
    showFormError(error.message);
  } finally {
    clearKeyButton.disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showFormError("");
  if (!fileInput.files[0]) {
    showFormError("请先选择一个 PDF 文件。");
    return;
  }
  if (!apiKeySaved && !apiKeyInput.value.trim()) {
    showFormError("请先填写 API Key；提交后会加密保存到本机。");
    return;
  }
  submitButton.disabled = true;
  submitButton.querySelector("span").textContent = "正在提交…";
  savePreferences();
  try {
    const data = new FormData(form);
    const job = await api("/api/jobs", { method: "POST", body: data });
    apiKeyInput.value = "";
    renderSettings({
      api_key_saved: true,
      base_url: form.elements.base_url.value,
      model: modelSelect.value,
    });
    activeJobId = job.id;
    renderJob(job);
    startPolling();
    document.querySelector(".result-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showFormError(error.message);
    submitButton.disabled = false;
  } finally {
    submitButton.querySelector("span").textContent = "开始翻译";
  }
});

cancelButton.addEventListener("click", async () => {
  if (!activeJobId) return;
  cancelButton.disabled = true;
  try {
    renderJob(await api(`/api/jobs/${activeJobId}/cancel`, { method: "POST" }));
  } catch (error) {
    showFormError(error.message);
    cancelButton.disabled = false;
  }
});

function resetTask() {
  stopPolling();
  activeJobId = null;
  jobStatus.textContent = "等待任务";
  jobStatus.className = "job-badge idle";
  glossarySummary.hidden = true;
  if (glossaryDialog.open) glossaryDialog.close();
  showState("empty");
  submitButton.disabled = false;
  window.scrollTo({ top: document.querySelector(".workspace-grid").offsetTop - 24, behavior: "smooth" });
}

retryButton.addEventListener("click", resetTask);
newTaskButton.addEventListener("click", resetTask);
viewGlossaryButton.addEventListener("click", openGlossary);
glossarySearch.addEventListener("input", filterGlossary);
glossaryDialog.addEventListener("click", (event) => {
  if (event.target === glossaryDialog) glossaryDialog.close();
});

loadPreferences();
form.elements.watermark_mode.value = "no_watermark";
loadSettings();
showState("empty");
