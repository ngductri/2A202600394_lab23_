const state = {
  eventCount: 0,
  threadId: "",
  interrupted: false,
  mermaidReady: false,
  allSteps: [],
  viewIndex: -1,
};

const byId = (id) => document.getElementById(id);

function setStatus(msg) {
  byId("statusBox").textContent = msg;
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || "Request failed");
  }
  return payload;
}

function updateKPI(finalState) {
  byId("kpiRoute").textContent = finalState.route ?? "-";
  byId("kpiAttempt").textContent = String(finalState.attempt ?? "-");
  byId("kpiEval").textContent = finalState.evaluation_result ?? "-";
  byId("kpiEvents").textContent = String(finalState.events_count ?? "-");
  byId("finalAnswer").textContent = finalState.final_answer || "(none)";
}

function appendStep(step, isCurrent = false) {
  const timeline = byId("timeline");
  const li = document.createElement("li");
  li.className = step.status;
  if (isCurrent) {
    li.classList.add("current");
  }

  const title = document.createElement("div");
  title.innerHTML =
    `<strong>[${step.status.toUpperCase()}]</strong> ` +
    `${step.node} <em>${step.event_type || ""}</em>`;

  const msg = document.createElement("div");
  msg.textContent = step.message || "";

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent =
    `route=${step.route} | attempt=${step.attempt} | ` +
    `eval=${step.evaluation_result} | next=${JSON.stringify(step.next || [])}`;

  li.appendChild(title);
  li.appendChild(msg);
  li.appendChild(meta);
  timeline.appendChild(li);
}

function baseFlowMermaid() {
  return [
    "flowchart TD",
    "  start((START)) --> intake[intake]",
    "  intake --> classify[classify]",
    "  classify -->|simple| answer[answer]",
    "  classify -->|tool| tool[tool]",
    "  classify -->|missing_info| clarify[clarify]",
    "  classify -->|risky| risky_action[risky_action]",
    "  classify -->|error| retry[retry]",
    "  risky_action --> approval[approval]",
    "  approval -->|approved| tool",
    "  approval -->|rejected| clarify",
    "  tool --> evaluate[evaluate]",
    "  evaluate -->|success| answer",
    "  evaluate -->|needs_retry| retry",
    "  retry -->|attempt<max| tool",
    "  retry -->|exhausted| dead_letter[dead_letter]",
    "  clarify --> finalize[finalize]",
    "  answer --> finalize",
    "  dead_letter --> finalize",
    "  finalize --> finish((END))",
    "",
    // Keep idle nodes dimmed but still readable.
    "  classDef idle fill:#f3f7f9,stroke:#c9d6de,stroke-width:1.1px,color:#8a9ca7,opacity:0.58;",
    "  classDef visited fill:#d7f0e8,stroke:#1d6f5d,stroke-width:2px,color:#143632,opacity:1;",
    "  classDef running fill:#e3ecff,stroke:#3f6fd1,stroke-width:3px,color:#122545,opacity:1;",
    "  classDef interrupted fill:#fde1e7,stroke:#b73652,stroke-width:2px,color:#5c1f2d,opacity:1;",
    "",
    // Dim all links/edge labels by default; active links are re-highlighted below.
    "  linkStyle default stroke:#9fb2be,stroke-width:1.2px,opacity:0.45;",
    "",
    "  class start,intake,classify,risky_action,approval,retry,tool,evaluate,clarify,answer,dead_letter,finalize,finish idle;",
  ].join("\n");
}

const EDGE_INDEX_BY_KEY = {
  "start->intake": 0,
  "intake->classify": 1,
  "classify->answer": 2,
  "classify->tool": 3,
  "classify->clarify": 4,
  "classify->risky_action": 5,
  "classify->retry": 6,
  "risky_action->approval": 7,
  "approval->tool": 8,
  "approval->clarify": 9,
  "tool->evaluate": 10,
  "evaluate->answer": 11,
  "evaluate->retry": 12,
  "retry->tool": 13,
  "retry->dead_letter": 14,
  "clarify->finalize": 15,
  "answer->finalize": 16,
  "dead_letter->finalize": 17,
  "finalize->finish": 18,
};

function edgeIndex(from, to) {
  return EDGE_INDEX_BY_KEY[`${from}->${to}`];
}

function collectEdgeIndexes(steps, currentNode = null) {
  const visited = new Set();
  let running = null;

  if (steps.length === 0) {
    return { visited, running };
  }

  const firstNode = steps[0]?.node;
  const firstEdge = edgeIndex("start", firstNode);
  if (Number.isInteger(firstEdge)) {
    visited.add(firstEdge);
  }

  for (let i = 1; i < steps.length; i += 1) {
    const idx = edgeIndex(steps[i - 1].node, steps[i].node);
    if (Number.isInteger(idx)) {
      visited.add(idx);
    }
  }

  if (steps.some((step) => step.node === "finalize")) {
    const endIdx = edgeIndex("finalize", "finish");
    if (Number.isInteger(endIdx)) {
      visited.add(endIdx);
    }
  }

  if (currentNode) {
    if (steps.length === 1) {
      running = edgeIndex("start", currentNode);
    } else {
      const prev = steps[steps.length - 2]?.node;
      running = edgeIndex(prev, currentNode);
    }
  }

  return { visited, running };
}

function edgeStyleState(steps, currentNode = null) {
  const edges = collectEdgeIndexes(steps, currentNode);
  return {
    visited: Array.from(edges.visited),
    running: Number.isInteger(edges.running) ? edges.running : null,
  };
}

function buildExecutionMermaid(steps, currentNode = null) {
  const visited = new Set();
  const running = new Set();
  const interrupted = new Set();

  for (const step of steps) {
    const node = step.node;
    if (!node) continue;
    if (step.status === "interrupted") {
      interrupted.add(node);
    } else {
      visited.add(node);
    }
  }

  if (steps.length > 0) {
    visited.add("start");
  }
  if (steps.some((step) => step.node === "finalize")) {
    visited.add("finish");
  }

  if (currentNode) {
    running.add(currentNode);
  }

  const lines = [baseFlowMermaid()];
  if (visited.size > 0) {
    lines.push(`class ${Array.from(visited).join(",")} visited;`);
  }
  if (running.size > 0) {
    lines.push(`class ${Array.from(running).join(",")} running;`);
  }
  if (interrupted.size > 0) {
    lines.push(`class ${Array.from(interrupted).join(",")} interrupted;`);
  }

  const edgeStyles = collectEdgeIndexes(steps, currentNode);
  for (const idx of edgeStyles.visited) {
    lines.push(`linkStyle ${idx} stroke:#1d6f5d,stroke-width:2.8px,opacity:1;`);
  }
  if (Number.isInteger(edgeStyles.running)) {
    lines.push(
      `linkStyle ${edgeStyles.running} stroke:#3f6fd1,stroke-width:3.4px,opacity:1;`,
    );
  }

  return lines.join("\n");
}

function ensureMermaidReady() {
  if (state.mermaidReady) return true;
  if (!window.mermaid) return false;

  window.mermaid.initialize({
    startOnLoad: false,
    securityLevel: "loose",
    htmlLabels: true,
    flowchart: {
      useMaxWidth: true,
      nodeSpacing: 20,
      rankSpacing: 26,
      curve: "linear",
    },
    theme: "base",
    themeVariables: {
      primaryColor: "#f5faf7",
      primaryTextColor: "#1c2a2f",
      primaryBorderColor: "#1d6f5d",
      lineColor: "#7692a0",
      fontFamily: "IBM Plex Mono, Consolas, monospace",
      tertiaryColor: "#f2f6ea",
    },
  });
  state.mermaidReady = true;
  return true;
}

function applyEdgeLabelStyles(renderEl, styleState = null) {
  const labelNodes = renderEl.querySelectorAll(".edgeLabels .edgeLabel");
  if (!labelNodes.length) return;

  const visited = new Set(styleState?.visited || []);
  const running = styleState?.running;

  labelNodes.forEach((labelEl, idx) => {
    // Base dim for every edge label.
    labelEl.style.opacity = "0.42";
    labelEl.style.color = "#8ea2ae";

    // Visited edges are emphasized.
    if (visited.has(idx)) {
      labelEl.style.opacity = "1";
      labelEl.style.color = "#1a4d46";
      labelEl.style.fontWeight = "600";
    }

    // Current running edge gets strongest highlight.
    if (Number.isInteger(running) && idx === running) {
      labelEl.style.opacity = "1";
      labelEl.style.color = "#203f7f";
      labelEl.style.fontWeight = "700";
    }
  });
}

function fitMermaidSvg(renderEl) {
  const svg = renderEl.querySelector("svg");
  if (!svg) return;

  // Keep Mermaid's own viewBox and force predictable responsive sizing.
  const currentViewBox = svg.getAttribute("viewBox");
  if (!currentViewBox) {
    const w = Number(svg.getAttribute("width")) || 1200;
    const h = Number(svg.getAttribute("height")) || 700;
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  }

  svg.removeAttribute("style");
  svg.removeAttribute("width");
  svg.removeAttribute("height");
  svg.setAttribute("width", "100%");
  svg.style.width = "100%";
  svg.style.height = "auto";
  svg.style.maxWidth = "100%";
  svg.style.minWidth = "0";
  svg.style.minHeight = "0";
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.style.display = "block";
}

async function renderMermaid(mermaidText, styleState = null) {
  const renderEl = byId("diagramRender");
  if (!ensureMermaidReady()) {
    renderEl.textContent = "Mermaid library is not loaded.";
    return;
  }

  try {
    // Defensive cleanup: remove hidden Unicode/control chars that can break Mermaid parser.
    const safeMermaidText = mermaidText
      .replace(/[\u200B-\u200D\uFEFF]/g, "")
      .replace(/[^\x09\x0A\x0D\x20-\x7E]/g, "");

    const renderId = `mmd-${Date.now()}-${Math.floor(Math.random() * 100000)}`;
    const rendered = await window.mermaid.render(renderId, safeMermaidText);
    renderEl.innerHTML = rendered.svg;
    if (typeof rendered.bindFunctions === "function") {
      rendered.bindFunctions(renderEl);
    }
    applyEdgeLabelStyles(renderEl, styleState);
    fitMermaidSvg(renderEl);
  } catch (err) {
    renderEl.textContent = `Mermaid render error: ${err.message}`;
  }
}

function updateStepIndicator() {
  const total = state.allSteps.length;
  const current = state.viewIndex >= 0 ? state.viewIndex + 1 : 0;
  byId("stepIndicator").textContent = `step ${current}/${total}`;
}

function renderTimelineAt(index) {
  const timeline = byId("timeline");
  timeline.innerHTML = "";
  if (index < 0) return;

  const steps = state.allSteps.slice(0, index + 1);
  for (let i = 0; i < steps.length; i += 1) {
    appendStep(steps[i], i === index);
  }
}

async function renderGraphAt(index) {
  if (index < 0) {
    const code = baseFlowMermaid();
    byId("diagramBox").textContent = code
      .replace(/[\u200B-\u200D\uFEFF]/g, "")
      .replace(/[^\x09\x0A\x0D\x20-\x7E]/g, "");
    await renderMermaid(code, edgeStyleState([]));
    return;
  }

  const steps = state.allSteps.slice(0, index + 1);
  const currentNode = state.allSteps[index]?.node || null;
  const code = buildExecutionMermaid(steps, currentNode);
  const styleState = edgeStyleState(steps, currentNode);
  byId("diagramBox").textContent = code
    .replace(/[\u200B-\u200D\uFEFF]/g, "")
    .replace(/[^\x09\x0A\x0D\x20-\x7E]/g, "");
  await renderMermaid(code, styleState);
}

async function animateTimeline(steps) {
  for (const step of steps) {
    state.allSteps.push(step);
    state.viewIndex = state.allSteps.length - 1;
    renderTimelineAt(state.viewIndex);
    await renderGraphAt(state.viewIndex);
    updateStepIndicator();
    await new Promise((resolve) => setTimeout(resolve, 120));
  }
}

async function moveStep(delta) {
  const total = state.allSteps.length;
  if (total === 0) return;

  const nextIndex = Math.max(0, Math.min(total - 1, state.viewIndex + delta));
  state.viewIndex = nextIndex;
  renderTimelineAt(state.viewIndex);
  await renderGraphAt(state.viewIndex);
  updateStepIndicator();
}

function resetView() {
  byId("timeline").innerHTML = "";
  byId("finalAnswer").textContent = "(none)";
  byId("interruptPayload").textContent = "(none)";
  byId("historyBox").innerHTML = "";
  byId("metricsSummary").innerHTML = "";
  byId("metricsTable").innerHTML = "";

  state.eventCount = 0;
  state.interrupted = false;
  state.allSteps = [];
  state.viewIndex = -1;
  updateStepIndicator();
}

function collectRunPayload() {
  const inputMode = byId("inputMode").value;
  return {
    config_path: byId("configPath").value.trim(),
    input_mode: inputMode,
    scenario_id: inputMode === "scenario" ? byId("scenarioId").value : null,
    query: inputMode === "free" ? byId("freeQuery").value : null,
    max_attempts: Number(byId("maxAttempts").value || 3),
    thread_id: byId("threadId").value.trim() || null,
  };
}

async function loadScenarios() {
  try {
    const configPath = byId("configPath").value.trim();
    const data = await fetchJSON(`/api/scenarios?config_path=${encodeURIComponent(configPath)}`);
    const selector = byId("scenarioId");
    selector.innerHTML = "";
    for (const item of data.scenarios) {
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = `${item.id} | ${item.expected_route}`;
      selector.appendChild(opt);
    }
    setStatus(`Loaded ${data.scenarios.length} scenarios.`);
  } catch (err) {
    setStatus(`Scenario load error: ${err.message}`);
  }
}

async function runFlow() {
  try {
    setStatus("Running flow...");
    resetView();
    await renderGraphAt(-1);

    const payload = collectRunPayload();
    const data = await fetchJSON("/api/flow/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    state.threadId = data.thread_id;
    byId("threadId").value = data.thread_id;
    state.eventCount = data.event_count;
    state.interrupted = data.interrupted;

    await animateTimeline(data.timeline || []);
    updateKPI(data.final_state || {});
    await refreshAuxPanels({ silent: true });

    if (data.interrupted) {
      byId("interruptPayload").textContent = JSON.stringify(data.interrupt_payload, null, 2);
      setStatus("Interrupted at approval. Choose Approve or Reject.");
    } else {
      setStatus("Flow completed.");
    }
  } catch (err) {
    setStatus(`Run error: ${err.message}`);
  }
}

async function resumeFlow(approved) {
  try {
    if (!state.threadId) {
      throw new Error("No thread_id available. Run flow first.");
    }

    setStatus(`Resuming flow with approved=${approved}...`);
    const payload = {
      config_path: byId("configPath").value.trim(),
      thread_id: state.threadId,
      approved,
      reviewer: byId("reviewer").value,
      comment: byId("comment").value,
      known_event_count: state.eventCount,
    };

    const data = await fetchJSON("/api/flow/resume", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    state.eventCount = data.event_count;
    state.interrupted = data.interrupted;

    await animateTimeline(data.timeline || []);
    updateKPI(data.final_state || {});
    await refreshAuxPanels({ silent: true });

    if (data.interrupted) {
      byId("interruptPayload").textContent = JSON.stringify(data.interrupt_payload, null, 2);
      setStatus("Still interrupted. Decision required.");
    } else {
      byId("interruptPayload").textContent = "(none)";
      setStatus("Resume completed.");
    }
  } catch (err) {
    setStatus(`Resume error: ${err.message}`);
  }
}

async function loadHistory() {
  return loadHistoryWithOptions({ silent: false });
}

async function loadHistoryWithOptions({ silent = false } = {}) {
  try {
    const threadId = byId("threadId").value.trim();
    if (!threadId) {
      if (!silent) {
        throw new Error("thread_id is required");
      }
      return;
    }
    if (!silent) {
      setStatus("Loading history...");
    }

    const configPath = byId("configPath").value.trim();
    const data = await fetchJSON(
      `/api/history?thread_id=${encodeURIComponent(threadId)}&limit=30&config_path=${encodeURIComponent(configPath)}`,
    );

    const box = byId("historyBox");
    box.innerHTML = "";
    for (const item of data.items) {
      const div = document.createElement("div");
      div.className = "history-item";
      div.textContent =
        `checkpoint_id=${item.checkpoint_id} | route=${item.route} | ` +
        `attempt=${item.attempt} | next=${JSON.stringify(item.next_nodes)}`;
      box.appendChild(div);
    }
    if (!silent) {
      setStatus(`Loaded ${data.count} checkpoints.`);
    }
  } catch (err) {
    if (!silent) {
      setStatus(`History error: ${err.message}`);
    }
  }
}

async function loadMetrics() {
  return loadMetricsWithOptions({ silent: false });
}

async function loadMetricsWithOptions({ silent = false } = {}) {
  try {
    if (!silent) {
      setStatus("Loading metrics...");
    }
    const data = await fetchJSON("/api/metrics");

    const summary = byId("metricsSummary");
    summary.innerHTML = `
      <div class="chip">success_rate: <strong>${(data.success_rate * 100).toFixed(2)}%</strong></div>
      <div class="chip">total_scenarios: <strong>${data.total_scenarios}</strong></div>
      <div class="chip">total_retries: <strong>${data.total_retries}</strong></div>
      <div class="chip">total_interrupts: <strong>${data.total_interrupts}</strong></div>
    `;

    const rows = data.scenario_metrics
      .map((row) => `
        <tr>
          <td>${row.scenario_id}</td>
          <td>${row.expected_route}</td>
          <td>${row.actual_route ?? ""}</td>
          <td>${row.success}</td>
          <td>${row.retry_count}</td>
          <td>${row.interrupt_count}</td>
        </tr>`)
      .join("");

    byId("metricsTable").innerHTML = `
      <table>
        <thead>
          <tr>
            <th>scenario_id</th><th>expected</th><th>actual</th><th>success</th><th>retries</th><th>interrupts</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;

    if (!silent) {
      setStatus("Metrics loaded.");
    }
  } catch (err) {
    if (!silent) {
      setStatus(`Metrics error: ${err.message}`);
    }
  }
}

async function refreshAuxPanels({ silent = true } = {}) {
  await Promise.all([
    loadMetricsWithOptions({ silent }),
    loadHistoryWithOptions({ silent }),
  ]);
}

function bindEvents() {
  byId("runBtn").addEventListener("click", runFlow);
  byId("approveBtn").addEventListener("click", () => resumeFlow(true));
  byId("rejectBtn").addEventListener("click", () => resumeFlow(false));
  byId("resetBtn").addEventListener("click", async () => {
    resetView();
    await renderGraphAt(-1);
    setStatus("Reset done.");
  });

  byId("prevStepBtn").addEventListener("click", () => moveStep(-1));
  byId("nextStepBtn").addEventListener("click", () => moveStep(1));

  byId("configPath").addEventListener("change", loadScenarios);
  byId("inputMode").addEventListener("change", () => {
    const isFree = byId("inputMode").value === "free";
    byId("freeQuery").disabled = !isFree;
    byId("scenarioId").disabled = isFree;
  });

  window.addEventListener("resize", () => {
    const renderEl = byId("diagramRender");
    fitMermaidSvg(renderEl);
  });

  setupSplitter();
}

function setupSplitter() {
  const handle = byId("splitHandle");
  const layout = document.querySelector(".viz-layout");
  if (!handle || !layout) return;

  let resizing = false;

  const onMove = (event) => {
    if (!resizing) return;
    const rect = layout.getBoundingClientRect();
    const handleWidth = 10;
    const nextWidth = rect.right - event.clientX - handleWidth / 2;
    const clamped = Math.max(250, Math.min(760, nextWidth));
    document.documentElement.style.setProperty("--timeline-width", `${Math.round(clamped)}px`);
    fitMermaidSvg(byId("diagramRender"));
  };

  const stopResize = () => {
    if (!resizing) return;
    resizing = false;
    document.body.classList.remove("is-resizing");
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", stopResize);
  };

  handle.addEventListener("mousedown", (event) => {
    if (window.innerWidth <= 1020) return;
    resizing = true;
    document.body.classList.add("is-resizing");
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stopResize);
    event.preventDefault();
  });
}

async function main() {
  bindEvents();
  byId("freeQuery").disabled = true;
  updateStepIndicator();
  await renderGraphAt(-1);
  await loadScenarios();
  await loadMetricsWithOptions({ silent: true });
}

main();
