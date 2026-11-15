// Armory view: weapons, variants, live confidence and the score buttons that
// drive the self-evolution loop.

import { api } from "./api.js";
import { state } from "./store.js";
import { $, $$, escapeHtml, toast } from "./util.js";

let last = { weapons: [], root: "" };
let query = "";
let family = "";

export async function loadArmory() {
  try {
    last = await api.armory();
    renderArmory();
  } catch (e) {
    $("#armory-list").innerHTML =
      `<div class="hint">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function visible() {
  const q = query.toLowerCase();
  return (last.weapons || []).filter((w) => {
    if (family && w.family !== family) return false;
    if (!q) return true;
    return [w.id, w.name, w.family, ...(w.variants || []).map((v) => `${v.vid} ${v.desc} ${v.command}`)]
      .join(" ").toLowerCase().includes(q);
  });
}

function spark(history) {
  const pts = (history || []).map((h) => Number(h.conf) || 0);
  if (pts.length < 2) return "";
  const w = 56, h = 16;
  const step = w / (pts.length - 1);
  const poly = pts.map((p, i) => `${(i * step).toFixed(1)},${(h - p * h).toFixed(1)}`).join(" ");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <polyline points="${poly}" fill="none" stroke="#8a7f5c" stroke-width="1.4"/></svg>`;
}

export function renderArmory() {
  const list = $("#armory-list");
  const rows = visible();
  const fams = [...new Set((last.weapons || []).map((w) => w.family))].sort();

  $("#armory-tools").innerHTML = `
    <input id="armory-q" placeholder="搜索武器 / 变体 / 命令…" value="${escapeHtml(query)}">
    <select id="armory-fam">
      <option value="">全部家族 (${(last.weapons || []).length})</option>
      ${fams.map((f) => `<option ${f === family ? "selected" : ""}>${escapeHtml(f)}</option>`).join("")}
    </select>
    <button class="act" id="armory-seed">灌入种子武器</button>
    <button class="act" id="armory-new">＋ 新武器</button>
    <span class="hint">${last.weapons?.length || 0} 个武器 · 置信度 = (成功+1)/(尝试+2)</span>`;

  $("#armory-q").addEventListener("input", (e) => { query = e.target.value.trim(); renderArmory(); });
  $("#armory-fam").addEventListener("change", (e) => { family = e.target.value; renderArmory(); });
  $("#armory-seed").onclick = () => ops({ action: "seed" }, "已灌入种子武器");
  $("#armory-new").onclick = () => openNewWeapon();

  if (!rows.length) {
    list.innerHTML = `<div class="hint">没有匹配的武器。可以先「灌入种子武器」。</div>`;
    return;
  }

  list.innerHTML = rows.map((w) => `
    <article class="wcard" data-wid="${w.id}">
      <header>
        <span class="wid">${w.id}</span>
        <span class="wname">${escapeHtml(w.name)}</span>
        <span class="wfam">${escapeHtml(w.family)}</span>
        <span class="wstat">${w.tries} 次 · 成功 ${w.ok} · 被拦 ${w.blocked}</span>
      </header>
      <div class="vlist">
        ${(w.variants || []).map((v) => variantRow(w, v)).join("")}
      </div>
      <footer>
        最优 <b>${w.best || "-"}</b>
        <button class="act" data-act="use-best" data-wid="${w.id}">用最优变体</button>
      </footer>
    </article>`).join("");

  $$("#armory-list .vrow button").forEach((btn) => {
    btn.onclick = () => {
      const wid = btn.closest(".wcard").dataset.wid;
      const vid = btn.closest(".vrow").dataset.vid;
      const act = btn.dataset.act;
      if (act === "use") useWeapon(wid, vid);
      else report(wid, vid, act);
    };
  });
  $$("#armory-list button[data-act=use-best]").forEach((btn) => {
    btn.onclick = () => useWeapon(btn.dataset.wid, null);
  });
}

function variantRow(w, v) {
  const stats = v.stats || {};
  const conf = Number(v.confidence ?? 0.5);
  const dep = v.deprecated;
  return `<div class="vrow ${dep ? "dep" : ""}" data-vid="${v.vid}">
    <span class="vid">${escapeHtml(v.vid)}${dep ? '<em class="tag">弃</em>' : ""}</span>
    <span class="vdesc" title="${escapeHtml(v.command || "")}">
      ${escapeHtml(v.desc || v.command || "")}</span>
    <span class="conf" title="置信度 ${conf.toFixed(2)}">
      <i style="width:${Math.round(conf * 100)}%"></i><b>${conf.toFixed(2)}</b></span>
    ${spark(v.history)}
    <span class="vstats" title="尝试/成功 · 连败">
      ${stats.tries || 0}/${stats.ok || 0} · 败${v.streak_fail || 0}</span>
    <span class="vacts">
      <button class="act" data-act="use">用</button>
      <button class="act ok" data-act="ok">成功</button>
      <button class="act warn" data-act="blocked">被拦</button>
      <button class="act fail" data-act="fail">失败</button>
    </span>
  </div>`;
}

async function ops(body, okText) {
  try {
    const d = await api.armoryOp(body);
    if (okText) toast(okText);
    await loadArmory();
    return d;
  } catch (e) { toast("操作失败: " + e.message); return null; }
}

async function report(wid, vid, result) {
  const d = await ops({ action: "report", weapon: wid, vid, result });
  if (!d) return;
  const label = { ok: "成功", blocked: "被拦", fail: "失败" }[result] || result;
  const extra = d.deprecated ? " · 已弃用" : "";
  toast(`${wid}/${vid} ${label} → 置信度 ${Number(d.confidence).toFixed(2)}${extra}`);
}

async function useWeapon(wid, vid) {
  const d = await ops({ action: "use", weapon: wid, vid });
  if (!d) return;
  const box = $("#armory-out");
  box.style.display = "block";
  box.innerHTML = `<div class="kv">${escapeHtml(d.weapon)}/${escapeHtml(d.variant)}
      · 置信度 ${Number(d.confidence).toFixed(2)}${d.deprecated ? " · 已弃用" : ""}</div>
    <pre id="armory-cmd">${escapeHtml(d.command || "(该变体没有命令)")}</pre>
    <button class="act" id="armory-copy">复制命令</button>`;
  const copy = $("#armory-copy");
  if (copy) copy.onclick = async () => {
    try { await navigator.clipboard.writeText(d.command || ""); toast("已复制"); }
    catch (e) { toast("复制失败"); }
  };
}

function openNewWeapon() {
  const box = $("#armory-out");
  box.style.display = "block";
  box.innerHTML = `
    <div class="dtitle">新建武器</div>
    <div class="form">
      <label>名称</label><input id="w-name" placeholder="例如 Tomcat AJP 文件读取">
      <label>家族</label><input id="w-family" placeholder="exploit/tomcat">
      <label>变体 v1 命令</label><textarea id="w-cmd" placeholder="ajpShooter 127.0.0.1 8009 /WEB-INF/web.xml"></textarea>
      <div class="mrow"><button class="act pri" id="w-save">保存</button>
      <button class="act" id="w-cancel">取消</button></div>
    </div>`;
  $("#w-cancel").onclick = () => { box.style.display = "none"; };
  $("#w-save").onclick = async () => {
    const body = {
      action: "add",
      name: $("#w-name").value,
      family: $("#w-family").value || "misc",
      variants: [{ vid: "v1", desc: "原始变体", command: $("#w-cmd").value }],
    };
    const d = await ops(body, "已创建武器");
    if (d) { box.style.display = "none"; }
  };
}
