// Global search: one box, four sources, click to jump.

import { api } from "./api.js";
import { emit } from "./store.js";
import { $, escapeHtml } from "./util.js";

let timer = null;

export function wireSearch() {
  const input = $("#q");
  const box = $("#results");

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { box.style.display = "none"; return; }
    timer = setTimeout(() => run(q), 260);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { box.style.display = "none"; input.blur(); }
    if (e.key === "Enter") {
      const first = box.querySelector(".it");
      if (first) first.click();
    }
  });

  document.addEventListener("click", (e) => {
    if (!box.contains(e.target) && e.target !== input) box.style.display = "none";
  });
}

async function run(q) {
  const box = $("#results");
  box.innerHTML = `<div class="grp">搜索中…</div>`;
  box.style.display = "block";

  let d;
  try { d = await api.search(q); }
  catch (e) { box.innerHTML = `<div class="grp">搜索失败：${escapeHtml(e.message)}</div>`; return; }

  const groups = [
    ["facts", "事实", (x) => `${x.id} · ${x.kind || "note"} · ${x.title || (x.description || "").slice(0, 26)}`],
    ["intents", "意图", (x) => `${x.id} · ${x.to ? "done" : x.status} · ${x.title || (x.description || "").slice(0, 24)}`],
    ["assets", "资产", (x) => `${x.key} · ×${x.seen}`],
    ["traffic", "流量", (x) => `${x.status} ${x.method} ${x.host || ""}${x.path}`],
  ];

  let html = "";
  groups.forEach(([key, label, fmt]) => {
    const rows = d[key] || [];
    if (!rows.length) return;
    html += `<div class="grp">${label} ${rows.length}</div>`;
    rows.forEach((x) => {
      const id = key === "traffic" ? "t" + x.id : (x.id || x.key);
      html += `<div class="it" data-kind="${key}" data-id="${escapeHtml(id)}">
        <span class="id">${escapeHtml(id)}</span>${escapeHtml(fmt(x))}</div>`;
    });
  });
  box.innerHTML = html || `<div class="grp">没有匹配「${escapeHtml(q)}」</div>`;

  box.querySelectorAll(".it").forEach((el) => {
    el.onclick = () => {
      box.style.display = "none";
      const { kind, id } = el.dataset;
      if (kind === "traffic") return emit("openTraffic", id.slice(1));
      if (kind === "assets") return emit("openAsset", id);
      emit("openNode", id);
    };
  });
}
