// Task graph: facts + intents, their detail panel, and the write forms.

import { api } from "./api.js";
import { state, emit } from "./store.js";
import {
  $, escapeHtml, kvRows, parseKv, toast, shortTime, FACTS_KINDS,
} from "./util.js";

const KIND_COLOR = {
  host: "#185fa5", service: "#0f6e56", vuln: "#a32d2d",
  cred: "#854f0b", loot: "#534ab7", flag: "#993556", note: "#5f5e5a",
};

export const KIND_LEGEND = KIND_COLOR;

export function buildGraph(data) {
  const facts = data.facts || [], intents = data.intents || [];
  const nodes = [], byId = new Map();

  facts.forEach((f) => {
    const n = {
      id: f.id, type: "fact", kind: f.kind || "note", group: f.kind || "note",
      label: (f.title || f.description || "").slice(0, 16),
      desc: f.description, raw: f,
    };
    nodes.push(n); byId.set(f.id, n);
  });
  intents.forEach((i) => {
    const n = {
      id: i.id, type: "intent", group: "intent",
      label: (i.title || i.description || "").slice(0, 16),
      status: i.to ? "done" : (i.status || "open"), desc: i.description, raw: i,
    };
    nodes.push(n); byId.set(i.id, n);
  });

  const links = [];
  intents.forEach((i) => {
    (i.from || []).forEach((fid) => {
      if (byId.has(fid)) links.push({ source: fid, target: i.id });
    });
    if (i.to && byId.has(i.to)) links.push({ source: i.id, target: i.to });
  });

  return { nodes, links };
}

export const taskShape = (d) => (d.type === "intent" ? "rect" : "circle");
export const taskRadius = (d) => (d.type === "intent" ? 8 : 9);
export const taskColor = (d) => {
  if (d.type === "intent") return d.status === "done" ? "#97c459" : "#d3d1c7";
  return KIND_COLOR[d.kind] || "#888";
};
export const taskLabel = (d) => d.label;

// ------------------------------------------------------------------ //
// detail panel
// ------------------------------------------------------------------ //
export function renderTaskDetail(node) {
  const box = $("#detail");
  if (!node) { box.innerHTML = `<div class="hint">点节点查看详情</div>`; return; }

  if (node.type === "fact") return renderFact(box, node.raw);
  return renderIntent(box, node.raw);
}

function renderFact(box, f) {
  box.innerHTML = `
    <div class="dtitle">${escapeHtml(f.title || f.description.slice(0, 30))}</div>
    <div class="kv">${f.id} · ${escapeHtml(f.kind || "note")} · ${escapeHtml(f.creator || "")}</div>
    <div class="kv">${escapeHtml(f.description)}</div>
    ${kvRows(f.data)}
    ${f.source_intent ? `<div class="kv">来自 ${escapeHtml(f.source_intent)}</div>` : ""}
    <div class="mrow">
      <button class="act" id="d-intent">用它建意图</button>
    </div>`;
  const btn = $("#d-intent");
  if (btn) { btn.disabled = !state.writeMode; btn.onclick = () => openIntentForm(f.id); }
}

function renderIntent(box, i) {
  const done = !!i.to || i.status === "done";
  const confirmed = !!i.confirmed_at;
  box.innerHTML = `
    <div class="dtitle">${escapeHtml(i.title || i.description.slice(0, 30))}</div>
    <div class="kv">${i.id} · ${escapeHtml(i.to ? "done" : (i.status || "open"))}${
      i.worker ? " · " + escapeHtml(i.worker) : ""}</div>
    <div class="kv">from ${(i.from || []).map((x) =>
      `<a href="#" data-id="${x}">${x}</a>`).join(", ")}</div>
    ${i.to ? `<div class="kv">→ ${escapeHtml(i.to)}</div>` : ""}
    <div class="kv">${escapeHtml(i.description)}</div>
    <div class="mrow">
      ${done ? `<span class="kv">已完成</span>` : `
        <button class="act" id="d-claim">认领</button>
        <button class="act" id="d-release">释放</button>
        <button class="act pri" id="d-dispatch">派发执行</button>
        <button class="act" id="d-done">完成…</button>`}
      ${!done && i.requires_confirmation && !confirmed
        ? `<button class="act" id="d-confirm">批准</button>` : ""}
      ${!done && i.requires_confirmation && confirmed
        ? `<span class="kv">已批准 ${escapeHtml(shortTime(i.confirmed_at))}</span>` : ""}
      <button class="act" id="d-stop" style="display:none">停止</button>
    </div>
    <pre id="runlog"></pre>`;

  box.querySelectorAll("a[data-id]").forEach((a) => {
    a.onclick = (ev) => { ev.preventDefault(); emit("select", a.dataset.id); };
  });

  const run = (fn) => async () => {
    try { await fn(); await emit("reload"); }
    catch (e) { toast(e.message); }
  };
  if ($("#d-claim")) $("#d-claim").onclick = run(() => api.claim(i.id));
  if ($("#d-release")) $("#d-release").onclick = run(() => api.release(i.id));
  if ($("#d-confirm")) $("#d-confirm").onclick = run(() => api.confirm(i.id));
  if ($("#d-done")) $("#d-done").onclick = () => openDoneForm(i.id);
  if ($("#d-dispatch")) $("#d-dispatch").onclick = () => dispatch(i.id);
  if (!state.writeMode) box.querySelectorAll(".act").forEach((b) => { b.disabled = true; });
}

// ------------------------------------------------------------------ //
// forms
// ------------------------------------------------------------------ //
export function openFactForm(kind) {
  const box = $("#detail");
  box.innerHTML = `
    <div class="dtitle">新建事实</div>
    <div class="form">
      <label>标题</label><input id="f-title" placeholder="例如 10.0.0.5:8080 Tomcat 9.0.31">
      <label>类型</label>
      <select id="f-kind">${FACTS_KINDS.map((k) =>
        `<option ${k === (kind || "note") ? "selected" : ""}>${k}</option>`).join("")}</select>
      <label>描述（必填，写清证据）</label>
      <textarea id="f-desc" placeholder="怎么确认的、看到了什么"></textarea>
      <label>data（每行 key=value）</label>
      <textarea id="f-data" placeholder="host=10.0.0.5&#10;port=8080"></textarea>
      <div class="mrow"><button class="act pri" id="f-save">保存</button>
      <button class="act" id="f-cancel">取消</button></div>
    </div>`;
  $("#f-cancel").onclick = () => emit("deselect");
  $("#f-save").onclick = async () => {
    try {
      const d = await api.fact({
        title: $("#f-title").value,
        description: $("#f-desc").value,
        kind: $("#f-kind").value,
        data: parseKv($("#f-data").value),
      });
      toast(`已创建 ${d.fact.id}`);
      emit("reloadAndSelect", d.fact.id);
    } catch (e) { toast("创建失败: " + e.message); }
  };
}

export function openIntentForm(prefillFrom) {
  const box = $("#detail");
  const facts = state.graph.facts || [];
  box.innerHTML = `
    <div class="dtitle">新建意图</div>
    <div class="form">
      <label>标题</label><input id="i-title" placeholder="例如 对 8080 做 AJP 探测">
      <label>描述（必填：动作、预期证据、时间盒）</label>
      <textarea id="i-desc"></textarea>
      <label>来源 fact（至少一个）</label>
      <div id="from-box">
        ${facts.map((f) => `<label>
          <input type="checkbox" value="${f.id}" ${f.id === prefillFrom ? "checked" : ""}>
          ${f.id} ${escapeHtml(f.title || (f.description || "").slice(0, 20))}</label>`).join("")
          || "（先创建 fact）"}
      </div>
      <label><input type="checkbox" id="i-confirm"> 需要人工批准后才能执行</label>
      <div class="mrow"><button class="act pri" id="i-save">保存</button>
      <button class="act" id="i-cancel">取消</button></div>
    </div>`;
  $("#i-cancel").onclick = () => emit("deselect");
  $("#i-save").onclick = async () => {
    const from = [...box.querySelectorAll("#from-box input:checked")].map((x) => x.value);
    try {
      const d = await api.intent({
        title: $("#i-title").value,
        description: $("#i-desc").value,
        from,
        requires_confirmation: $("#i-confirm").checked,
      });
      toast(`已创建 ${d.intent.id}`);
      emit("reloadAndSelect", d.intent.id);
    } catch (e) { toast("创建失败: " + e.message); }
  };
}

function openDoneForm(iid) {
  const box = $("#detail");
  box.innerHTML = `
    <div class="dtitle">完成 ${iid}（同时写入产出事实）</div>
    <div class="form">
      <label>产出了什么（必填，带证据）</label>
      <textarea id="d-desc"></textarea>
      <label>事实类型</label>
      <select id="d-kind">${FACTS_KINDS.map((k) => `<option>${k}</option>`).join("")}</select>
      <label>data（每行 key=value）</label>
      <textarea id="d-data"></textarea>
      <label>武器战绩（可选，计入置信度）</label>
      <div class="mrow" style="margin-top:0">
        <select id="d-weapon" style="flex:1"><option value="">不记分</option></select>
        <select id="d-vid"><option value="">变体</option></select>
        <select id="d-result">
          <option value="ok">成功</option><option value="blocked">被拦</option>
          <option value="fail">失败</option>
        </select>
      </div>
      <div class="mrow"><button class="act pri" id="dd-save">完成并记录</button>
      <button class="act" id="dd-cancel">取消</button></div>
    </div>`;

  // lazily populate the armory pickers; failing to load must not block the form
  api.armory().then((d) => {
    const sel = $("#d-weapon");
    if (!sel) return;
    (d.weapons || []).forEach((w) => {
      const opt = document.createElement("option");
      opt.value = w.id;
      opt.textContent = `${w.id} ${w.name}`;
      opt.dataset.variants = JSON.stringify((w.variants || []).map((v) => v.vid));
      sel.appendChild(opt);
    });
    sel.onchange = () => {
      const vid = $("#d-vid");
      vid.innerHTML = `<option value="">变体</option>`;
      const chosen = sel.selectedOptions[0];
      const list = chosen && chosen.dataset.variants ? JSON.parse(chosen.dataset.variants) : [];
      list.forEach((v) => {
        const o = document.createElement("option");
        o.value = v; o.textContent = v;
        vid.appendChild(o);
      });
    };
  }).catch(() => {});
  $("#dd-cancel").onclick = () => emit("deselect");
  $("#dd-save").onclick = async () => {
    const weapon = $("#d-weapon") ? $("#d-weapon").value : "";
    const vid = $("#d-vid") ? $("#d-vid").value : "";
    if (weapon && !vid) { toast("选了武器就要选变体（用于记分）"); return; }
    try {
      const body = {
        description: $("#d-desc").value,
        kind: $("#d-kind").value,
        data: parseKv($("#d-data").value),
        worker: "web",
      };
      if (weapon) Object.assign(body, { weapon, vid, result: $("#d-result").value });
      const d = await api.done(iid, body);
      const score = d.weapon ? ` · ${weapon}/${vid} 置信度 ${Number(d.weapon.confidence).toFixed(2)}` : "";
      toast(`${iid} → ${d.fact.id}${score}`);
      emit("reloadAndSelect", d.fact.id);
    } catch (e) { toast("失败: " + e.message); }
  };
}

// ------------------------------------------------------------------ //
// dispatch
// ------------------------------------------------------------------ //
let runTimer = null;

async function dispatch(iid) {
  const log = $("#runlog");
  try {
    const d = await api.dispatch(iid);
    if (log) { log.style.display = "block"; log.textContent = `started pid ${d.pid}\n`; }
    const stop = $("#d-stop");
    if (stop) {
      stop.style.display = "";
      stop.onclick = async () => {
        try { await api.stopDispatch(iid); toast("已请求停止"); } catch (e) { toast(e.message); }
      };
    }
    pollRun(iid);
  } catch (e) { toast("派发失败: " + e.message); }
}

function pollRun(iid) {
  clearTimeout(runTimer);
  const tick = async () => {
    try {
      const d = await api.dispatchStatus(iid);
      const log = $("#runlog");
      if (log && d.tail) {
        log.style.display = "block";
        log.textContent = d.tail;
        log.scrollTop = log.scrollHeight;
      }
      if (d.running) { runTimer = setTimeout(tick, 1500); return; }
      const stop = $("#d-stop");
      if (stop) stop.style.display = "none";
      emit("reload");
    } catch (e) { runTimer = setTimeout(tick, 3000); }
  };
  runTimer = setTimeout(tick, 800);
}
