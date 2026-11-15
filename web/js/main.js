// App shell: wires the views, the mode switch, hash routing and polling.

import { api } from "./api.js";
import { createGraph } from "./graph.js";
import { state, on, emit } from "./store.js";
import { $, $$, escapeHtml } from "./util.js";
import {
  buildGraph, taskShape, taskRadius, taskColor, taskLabel,
  renderTaskDetail, openFactForm, openIntentForm, KIND_LEGEND,
} from "./taskgraph.js";
import {
  ASSET_COLOR, assetShape, assetRadius, assetColor, assetLabel,
  assetNodes, assetLinks, setAssetsData, renderAssetTools, renderAssetDetail,
  filteredAssetData, toggleLayout,
} from "./assets.js";
import { loadTraffic, renderTraffic, showTraffic } from "./traffic.js";
import { loadArmory } from "./armory.js";
import { loadPlan } from "./plan.js";
import { loadCve } from "./cve.js";
import { loadChat, wireChat } from "./chat.js";
import { wireSearch } from "./search.js";

let view = null;
let lastSeq = 0;
let taskNodes = [];
let assetSig = "";
let assetData = { assets: [], edges: [], stats: {} };
const tick = { assets: 0, traffic: 0 };

const isGraphMode = () => state.mode === "graph";
const isAssetMode = () => state.mode === "assets";

// ------------------------------------------------------------------ //
// bootstrap
// ------------------------------------------------------------------ //
async function init() {
  view = createGraph("#svg", {
    shape: (d) => (isAssetMode() ? assetShape(d) : taskShape(d)),
    radius: (d) => (isAssetMode() ? assetRadius(d) : taskRadius(d)),
    color: (d) => (isAssetMode() ? assetColor(d) : taskColor(d)),
    label: (d) => (isAssetMode() ? assetLabel(d) : taskLabel(d)),
    onSelect: (d) => select(d.id),
    onBackground: () => deselect(),
    tooltip: (d) => tooltipHtml(d),
    charge: -340,
  });

  wireSearch();
  wireChat(refreshGraph);
  wireButtons();
  wireEvents();

  layoutLegend(KIND_LEGEND);
  await refreshHealth();
  await refreshGraph();
  applyHash(true);
  connectStream();
  setInterval(poll, 2000);
  setInterval(() => { if (state.mode === "assets") refreshAssets(); }, 8000);
  window.addEventListener("resize", () => view.resize());
}

function wireButtons() {
  $("#btn-fact").onclick = () => openFactForm();
  $("#btn-intent").onclick = () => {
    if (!(state.graph.facts || []).length) {
      alert("先创建一个 fact，意图必须挂在事实上");
      return;
    }
    openIntentForm();
  };
  $("#btn-refresh").onclick = () => {
    if (state.mode === "traffic") loadTraffic();
    else if (state.mode === "chat") loadChat();
    else if (state.mode === "armory") loadArmory();
    else if (state.mode === "plan") loadPlan();
    else if (state.mode === "cve") loadCve();
    else if (isAssetMode()) refreshAssets();
    else refreshGraph();
  };
  $("#asset-layout").onclick = () => toggleLayout();
  $("#asset-fit").onclick = () => view.fit();
  $("#asset-clear").onclick = () => {
    state.assetFilter.clear();
    state.assetQuery = "";
    emit("assets:refresh");
  };
  const q = $("#asset-q");
  if (q) q.addEventListener("input", () => {
    state.assetQuery = q.value.trim();
    emit("assets:refresh");
  });
  $$("nav.tabs button").forEach((b) => {
    b.addEventListener("click", () => setMode(b.dataset.mode));
  });
}

function wireEvents() {
  on("select", (id) => select(id));
  on("deselect", () => deselect());
  on("reload", () => (state.mode === "traffic" ? loadTraffic()
    : isAssetMode() ? refreshAssets() : refreshGraph()));
  on("reloadAndSelect", async (id) => { await refreshGraph(); select(id); });
  on("assets:refresh", () => renderAssets());
  on("assets:layout", (layout) => {
    view.setLayout(layout);
    setTimeout(() => view.fit(), 350);
  });
  on("focus", (key) => { state.assetFocus = key; view.setFocus(view.neighborsOf(key, 1)); });
  on("openNode", (id) => { setMode("graph"); select(id); });
  on("openAsset", (key) => { setMode("assets"); setTimeout(() => select(key), 300); });
  on("openTraffic", (id) => { setMode("traffic"); showTraffic(id); });
}

// ------------------------------------------------------------------ //
// data
// ------------------------------------------------------------------ //
async function refreshHealth() {
  try {
    const d = await api.health();
    state.writeMode = !!d.write;
    const badge = $("#wmode");
    badge.className = state.writeMode ? "" : "ro";
    badge.innerHTML = state.writeMode
      ? "可写 · <b>fact / intent / 认领 / 完成 / 派发 / 对话</b>"
      : "只读 · 加 --allow-write 启动才能操作";
    $("#btn-fact").disabled = !state.writeMode;
    $("#btn-intent").disabled = !state.writeMode;
  } catch (e) { /* server starting */ }
}

export async function refreshGraph() {
  try {
    const data = await api.graph();
    state.graph = data;
    const built = buildGraph(data);
    taskNodes = built.nodes;
    if (isGraphMode()) {
      view.setData(built.nodes, built.links);
      setEmpty(built.nodes.length === 0, "这张图还是空的：点「＋ 事实」开始");
    }
    updateStats(data.stats);
    if (isGraphMode() && state.selectedId) {
      view.setSelected(state.selectedId);
      renderTaskDetail(taskNodes.find((n) => n.id === state.selectedId));
    }
  } catch (e) { /* transient */ }
}

async function refreshAssets() {
  try {
    const data = await api.assets();
    assetData = data;
    setAssetsData(data);
    renderAssets();
  } catch (e) { /* transient */ }
}

function renderAssets() {
  const data = filteredAssetData();
  const nodes = assetNodes(data);
  const byKey = new Set(nodes.map((n) => n.id));
  const links = assetLinks(data, byKey);
  const sig = nodes.map((n) => n.id).join(",") + "|"
    + links.map((l) => l.source + ">" + l.target).join(",");
  if (isAssetMode() && sig !== assetSig) {
    assetSig = sig;
    view.setData(nodes, links);
    setEmpty(nodes.length === 0, lastAvailable()
      ? "没有匹配的资产（试试清筛选）"
      : "资产库为空：记录 fact 后自动沉淀");
  }
  renderAssetTools();
  updateAssetStats(data);
  if (isAssetMode() && state.selectedId) {
    view.setSelected(state.selectedId);
    view.setFocus(state.assetFocus ? view.neighborsOf(state.selectedId, 1) : null);
    renderAssetDetail(state.selectedId);
  }
}

function lastAvailable() {
  return (assetData && assetData.available) !== false;
}

function setEmpty(show, text) {
  const el = $("#empty");
  el.style.display = show ? "flex" : "none";
  if (text) el.textContent = text;
}

function updateStats(s) {
  if (!s || !isGraphMode()) return;
  $("#s1").innerHTML = `facts <b>${s.facts || 0}</b>`;
  $("#s2").innerHTML = `intents <b>${s.intents || 0}</b>`;
  $("#s3").innerHTML = `open <b>${s.open || 0}</b>`;
}

function updateAssetStats(d) {
  const assets = d.assets || [];
  $("#s1").innerHTML = `assets <b>${assets.length}</b>`;
  $("#s2").innerHTML = `types <b>${new Set(assets.map((a) => a.type)).size}</b>`;
  $("#s3").innerHTML = `edges <b>${(d.edges || []).length}</b>`;
  $("#s4").innerHTML = `跨任务 <b>${(d.stats?.first_seen || "-").slice(0, 10)}</b>`;
}

// ------------------------------------------------------------------ //
// selection + detail
// ------------------------------------------------------------------ //
function select(id) {
  state.selectedId = id;
  view.setSelected(id);
  view.zoomTo(id, 1.5);
  if (isAssetMode()) {
    state.assetFocus = id;
    view.setFocus(view.neighborsOf(id, 1));
    renderAssetDetail(id);
  } else {
    renderTaskDetail(taskNodes.find((n) => n.id === id));
  }
  try { history.replaceState(null, "", `#${state.mode}/${id}`); } catch (e) { /* noop */ }
}

function deselect() {
  state.selectedId = null;
  state.assetFocus = null;
  view.setSelected(null);
  view.setFocus(null);
  $("#detail").innerHTML = `<div class="hint">点节点或搜索查看详情</div>`;
  try { history.replaceState(null, "", `#${state.mode}`); } catch (e) { /* noop */ }
}

// ------------------------------------------------------------------ //
// mode switch + routing
// ------------------------------------------------------------------ //
export async function setMode(next, updateHash = true) {
  if (state.mode === next) return;
  state.mode = next;
  state.selectedId = null;
  state.assetFocus = null;
  if (updateHash) {
    try { history.replaceState(null, "", `#${next}`); } catch (e) { /* noop */ }
  }
  $$("nav.tabs button").forEach((b) => b.classList.toggle("on", b.dataset.mode === next));

  const chat = next === "chat", traffic = next === "traffic", assets = next === "assets";
  const armory = next === "armory", plan = next === "plan", cve = next === "cve";
  $("#chat").style.display = chat ? "flex" : "none";
  $("#traffic").style.display = traffic ? "block" : "none";
  $("#armory").style.display = armory ? "flex" : "none";
  $("#plan").style.display = plan ? "flex" : "none";
  $("#cve").style.display = cve ? "flex" : "none";
  $("#asset-tools").classList.toggle("on", assets);
  $("#wrap").classList.toggle("with-tools", assets);
  $("#svg").style.display = (chat || traffic || armory || plan || cve) ? "none" : "block";
  $("#legend").style.display = (chat || traffic || armory || plan || cve) ? "none" : "block";
  $("#tip").style.display = "none";
  if (chat || traffic || armory || plan || cve) $("#empty").style.display = "none";
  $("#detail").innerHTML = `<div class="hint">点节点或搜索查看详情</div>`;

  if (assets) {
    layoutLegend(ASSET_COLOR);
    await refreshAssets();
    setTimeout(() => view.fit(), 450);
  } else if (traffic) {
    await loadTraffic();
  } else if (chat) {
    await loadChat();
  } else if (armory) {
    await loadArmory();
  } else if (plan) {
    await loadPlan();
  } else if (cve) {
    await loadCve();
  } else {
    layoutLegend(KIND_LEGEND);
    await refreshGraph();
  }
}

function layoutLegend(map) {
  $("#legend").innerHTML = Object.entries(map)
    .map(([k, c]) => `<div><i style="background:${c}"></i>${k}</div>`).join("");
}

function applyHash(first = false) {
  const raw = (location.hash || "").replace("#", "");
  if (!raw) { layoutLegend(KIND_LEGEND); return; }
  const [mode, id] = raw.split("/");
  if (mode && mode !== state.mode) setMode(mode, false);
  if (id) setTimeout(() => select(id), first ? 400 : 200);
}

window.addEventListener("hashchange", () => applyHash(false));

// ------------------------------------------------------------------ //
// polling
// ------------------------------------------------------------------ //
// Real-time feed. EventSource reconnects on its own; polling below stays as a
// fallback for the views that do not use the event stream.
let stream = null;
function connectStream() {
  if (stream) stream.close();
  stream = new EventSource(`/api/stream?since=${lastSeq}`);
  stream.onmessage = async (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (!d.events || !d.events.length) return;
    lastSeq = d.last;
    pushEvents(d.events);
    $("#s4").innerHTML = `seq <b>${lastSeq}</b>`;
    if (state.mode === "graph") await refreshGraph();
  };
  stream.onerror = () => { /* browser retries; if it gives up we keep polling */ };
}

async function poll() {
  if (state.mode === "graph") {
    try {
      const d = await api.events(lastSeq);
      if (d.events && d.events.length) {
        lastSeq = d.last;
        pushEvents(d.events);
        await refreshGraph();
      }
      $("#s4").innerHTML = `seq <b>${lastSeq}</b>`;
    } catch (e) { /* server restarting */ }
  } else if (state.mode === "traffic") {
    tick.traffic = (tick.traffic + 1) % 3;
    if (tick.traffic === 0) await loadTraffic();
  }
}

function pushEvents(list) {
  const box = $("#events");
  list.slice(-30).reverse().forEach((ev) => {
    const div = document.createElement("div");
    div.className = "ev";
    div.innerHTML = `<span class="t">${String(ev.ts).slice(11, 19)}</span>
      <span class="k">${escapeHtml(ev.event)}</span>`;
    box.prepend(div);
  });
  while (box.children.length > 60) box.lastChild.remove();
}

init();
