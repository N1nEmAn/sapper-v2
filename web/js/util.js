// DOM + formatting helpers shared by every view.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

export const shortTime = (iso) => (iso ? String(iso).slice(11, 19) : "-");
export const shortDate = (iso) => (iso ? String(iso).slice(0, 10) : "-");

export function kvRows(data, cls = "kv") {
  return Object.entries(data || {})
    .map(([k, v]) => `<div class="${cls}"><code>${escapeHtml(k)}</code> = ${escapeHtml(v)}</div>`)
    .join("");
}

export function parseKv(text) {
  const out = {};
  String(text || "").split("\n").forEach((line) => {
    const m = line.match(/^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*)$/);
    if (m) out[m[1]] = m[2];
  });
  return out;
}

export function toast(msg) {
  const node = document.getElementById("toast");
  if (!node) return;
  node.textContent = msg;
  node.classList.add("on");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => node.classList.remove("on"), 2600);
}

export const FACTS_KINDS = ["host", "service", "vuln", "cred", "loot", "flag", "note"];
