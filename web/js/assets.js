// Asset map: the cross-engagement layer, made explorable.
// Filter by type, search, switch layout, click to focus a node and its
// neighbours, then act on it (inherit into this engagement / forget).

import { api } from "./api.js";
import { state, emit } from "./store.js";
import { $, $$, escapeHtml, kvRows, toast, shortDate } from "./util.js";

export const ASSET_COLOR = {
  ip: "#185fa5", domain: "#3b8fd6", service: "#0f6e56", vuln: "#a32d2d",
  cred: "#854f0b", loot: "#534ab7", endpoint: "#7a6f5d",
};

let last = { assets: [], edges: [], stats: { by_type: {} }, available: true };

export const assetShape = () => "circle";
export const assetRadius = (d) => 8 + Math.min(d.raw?.seen || 1, 6);
export const assetColor = (d) => ASSET_COLOR[d.group] || "#888";
export const assetLabel = (d) => String(d.label).slice(0, 24);

export function assetNodes(data) {
  return (data.assets || []).map((a) => ({
    id: a.key,
    type: "asset",
    group: a.type,
    label: a.value,
    raw: a,
    parent: a.parent,
  }));
}

export function assetLinks(data, byKey) {
  return (data.edges || [])
    .filter((e) => byKey.has(e.source) && byKey.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));
}

function matches(a, q) {
  if (!q) return true;
  const needle = q.toLowerCase();
  return [a.key, a.value, a.label, JSON.stringify(a.data || {})]
    .some((v) => String(v || "").toLowerCase().includes(needle));
}

function visibleAssets() {
  const typeOk = (a) => state.assetFilter.size === 0 || state.assetFilter.has(a.type);
  return (last.assets || []).filter((a) => typeOk(a) && matches(a, state.assetQuery));
}

export function filteredAssetData() {
  const assets = visibleAssets();
  const byKey = new Set(assets.map((a) => a.key));
  const edges = (last.edges || []).filter((e) => byKey.has(e.source) && byKey.has(e.target));
  return { assets, edges, stats: last.stats, available: last.available };
}

let chipsBuilt = false;

export function renderAssetTools() {
  const stats = last.stats || {};
  const counts = stats.by_type || {};
  const total = stats.total || 0;
  const chips = ["全部", ...Object.keys(ASSET_COLOR)];

  if (chipsBuilt) {
    // update in place: rebuilding kills focus/hover mid-click
    chips.forEach((t) => {
      const btn = document.querySelector(`#asset-chips .chip[data-type="${t}"]`);
      if (!btn) return;
      const n = t === "全部" ? total : (counts[t] || 0);
      btn.querySelector("span").textContent = n;
      btn.classList.toggle("on", t === "全部" ? state.assetFilter.size === 0 : state.assetFilter.has(t));
    });
  } else {
  $("#asset-chips").innerHTML = chips.map((t) => {
    const active = t === "全部"
      ? state.assetFilter.size === 0
      : state.assetFilter.has(t);
    const n = t === "全部" ? total : (counts[t] || 0);
    return `<button class="chip ${active ? "on" : ""}" data-type="${t}">
      ${t === "全部" ? "全部" : `<i style="background:${ASSET_COLOR[t]}"></i>${t}`}
      <span>${n}</span></button>`;
  }).join("");
  chipsBuilt = true;

  $$("#asset-chips .chip").forEach((btn) => {
    btn.onclick = () => {
      const t = btn.dataset.type;
      if (t === "全部") state.assetFilter.clear();
      else if (state.assetFilter.has(t)) state.assetFilter.delete(t);
      else state.assetFilter.add(t);
      emit("assets:refresh");
    };
  });
  }

  const q = $("#asset-q");
  if (q && q.value !== state.assetQuery) q.value = state.assetQuery;
  $("#asset-layout").textContent = state.assetLayout === "force" ? "布局：力导向" : "布局：按类型分组";
  $("#asset-meta").textContent = last.available === false
    ? "资产库为空：记录 fact 后自动沉淀"
    : `${total} 项 · 最近 ${shortDate(last.stats?.last_seen)}`;
}

export function setAssetsData(data) {
  last = data || last;
  renderAssetTools();
}

export function renderAssetDetail(key) {
  const box = $("#detail");
  const a = (last.assets || []).find((x) => x.key === key);
  if (!a) { box.innerHTML = `<div class="hint">资产 ${escapeHtml(key)} 不在当前筛选结果中</div>`; return; }

  const sources = (a.sources || []).slice(-5).reverse();
  box.innerHTML = `
    <div class="dtitle">${escapeHtml(a.value)}</div>
    <div class="kv">${escapeHtml(a.key)}</div>
    <div class="kv">类型 ${escapeHtml(a.type)} · 观察 ${a.seen} 次 · 最近 ${escapeHtml(a.last_seen)}</div>
    ${kvRows(a.data)}
    ${a.parent ? `<div class="kv">父级 <a href="#" id="a-parent">${escapeHtml(a.parent)}</a></div>` : ""}
    <div class="kv">来源 fact</div>
    ${sources.length
      ? sources.map((s) => `<div class="kv">· ${escapeHtml(s.fact || "?")} @ ${escapeHtml(shortDate(s.ts))}</div>`).join("")
      : `<div class="kv">（无记录）</div>`}
    <div class="mrow">
      <button class="act pri" id="a-seed">继承到当前图</button>
      <button class="act" id="a-focus">聚焦邻居</button>
      <button class="act" id="a-copy">复制 key</button>
      <button class="act danger" id="a-forget">删除</button>
    </div>`;

  const par = $("#a-parent");
  if (par) par.onclick = (ev) => { ev.preventDefault(); emit("select", a.parent); };

  const seed = $("#a-seed");
  if (seed) {
    seed.disabled = !state.writeMode;
    seed.onclick = async () => {
      try {
        const d = await api.assetAction("seed", a.key);
        toast(d.created ? `已继承 ${d.created} 条事实` : "该资产已在当前图中");
        emit("reload");
      } catch (e) { toast("继承失败: " + e.message); }
    };
  }
  const focus = $("#a-focus");
  if (focus) focus.onclick = () => emit("focus", a.key);

  const copy = $("#a-copy");
  if (copy) copy.onclick = async () => {
    try { await navigator.clipboard.writeText(a.key); toast("已复制"); }
    catch (e) { toast(a.key); }
  };

  const forget = $("#a-forget");
  if (forget) {
    forget.disabled = !state.writeMode;
    forget.onclick = async () => {
      if (!confirm(`从资产库删除 ${a.key}？\n（不会影响任何任务的 facts，只是不再跨任务继承）`)) return;
      try {
        const d = await api.assetAction("forget", a.key);
        toast(`已删除 ${d.deleted} 项`);
        emit("deselect");
        emit("assets:refresh");
      } catch (e) { toast("删除失败: " + e.message); }
    };
  }
}

export function toggleLayout() {
  state.assetLayout = state.assetLayout === "force" ? "grouped" : "force";
  renderAssetTools();
  emit("assets:layout", state.assetLayout);
}
