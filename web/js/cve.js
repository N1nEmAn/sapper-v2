// CVE knowledge base view: local YAML entries + one-click import → weapon.

import { api } from "./api.js";
import { $, $$, escapeHtml, toast } from "./util.js";

let last = { entries: [], root: "" };
let query = "";

export async function loadCve() {
  try {
    last = await api.cve(query);
    renderCve();
  } catch (e) {
    $("#cve-list").innerHTML = `<div class="hint">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

export function renderCve() {
  const entries = last.entries || [];

  $("#cve-tools").innerHTML = `
    <input id="cve-q" placeholder="搜索 CVE 编号 / 描述 / 产品 / 标签…" value="${escapeHtml(query)}">
    <input id="cve-import" placeholder="CVE-YYYY-NNNN 在线拉取">
    <button class="act" id="cve-go">导入</button>
    <span class="hint">${entries.length} 条 · 数据源 CIRCL(cvelistV5) · YAML 存于 ${escapeHtml(last.root || "")}</span>`;

  $("#cve-q").addEventListener("input", (e) => { query = e.target.value.trim(); loadCve(); });
  const go = () => {
    const cid = $("#cve-import").value.trim();
    if (!cid) { toast("先填 CVE 编号"); return; }
    importCve(cid);
  };
  $("#cve-go").onclick = go;
  $("#cve-import").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });

  if (!entries.length) {
    $("#cve-list").innerHTML = `<div class="hint">CVE 库为空。
      上方输入 CVE 编号在线拉取，或用命令行 sapper cve seed 灌入种子。</div>`;
    return;
  }

  $("#cve-list").innerHTML = entries.map((e) => {
    const tags = (e.tags || []).map((t) => `<em class="tag">${escapeHtml(t)}</em>`).join("");
    const weapons = (e.weapons || []).length ? `<em class="tag src">武器 ${e.weapons.length}</em>` : "";
    const sev = e.severity && e.severity !== "UNKNOWN" ? `<span class="sev ${e.severity.toLowerCase()}">${escapeHtml(e.severity)}</span>` : "";
    return `<div class="cverow" data-cid="${e.id}">
      <div class="cvehead">
        <span class="wid">${escapeHtml(e.id)}</span>${sev}
        <span class="hint">${escapeHtml(e.vendor || "")} ${escapeHtml(e.product || "")}</span>
        ${tags}${weapons}
      </div>
      <div class="cvedesc">${escapeHtml((e.description || "").slice(0, 220))}</div>
      <div class="cveacts">
        <button class="act" data-act="gen">生成武器</button>
        <button class="act" data-act="show">详情</button>
      </div>
    </div>`;
  }).join("");

  $$("#cve-list button").forEach((btn) => {
    btn.onclick = () => {
      const cid = btn.closest(".cverow").dataset.cid;
      if (btn.dataset.act === "gen") genWeapon(cid);
      else showCve(cid);
    };
  });
}

async function importCve(cid) {
  try {
    const d = await api.cveOp({ action: "import", cve: cid });
    toast(`已导入 ${cid} → 建议家族 ${d.family}`);
    await loadCve();
  } catch (e) { toast("导入失败: " + e.message); }
}

async function genWeapon(cid) {
  try {
    const d = await api.cveOp({ action: "to-weapon", cve: cid });
    toast(`已生成 ${d.weapon.id} [${d.weapon.family}] ← ${cid}`);
    await loadCve();
  } catch (e) { toast("生成失败: " + e.message); }
}

function showCve(cid) {
  const e = (last.entries || []).find((x) => x.id === cid);
  if (!e) return;
  const refs = (e.references || []).slice(0, 3).map((r) => `<div class="hint">· ${escapeHtml(r)}</div>`).join("");
  const box = $("#cve-out");
  box.style.display = "block";
  box.innerHTML = `<div class="dtitle">${escapeHtml(e.id)} ${escapeHtml(e.severity || "UNKNOWN")}</div>
    <div class="cvedesc">${escapeHtml(e.description || "")}</div>
    ${e.versions?.length ? `<div class="hint">版本: ${escapeHtml(e.versions.slice(0,4).join("; "))}</div>` : ""}
    ${e.cwe?.length ? `<div class="hint">CWE: ${escapeHtml(e.cwe.join(", "))}</div>` : ""}
    ${refs}
    <div class="mrow"><button class="act" id="cve-close">关闭</button></div>`;
  $("#cve-close").onclick = () => { box.style.display = "none"; };
}
