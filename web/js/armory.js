// Armory view: weapons, variants, evasion stacks, live confidence, and the
// three loops that make it self-evolving — score, attribute, derive.

import { api } from "./api.js";
import { $, $$, escapeHtml, toast } from "./util.js";

let last = { weapons: [], root: "", categories: {}, layers: {}, profile: [] };
let query = "";
let family = "";
// Profile comes from the URL first (shareable link), then localStorage.
// Rebuilding the toolbar on every render steals focus mid-edit, so a typed
// value alone is not a reliable way to set this.
let profile = new URLSearchParams(location.search).get("profile")
  || localStorage.getItem("sapper.profile") || "";
if (new URLSearchParams(location.search).get("profile")) {
  localStorage.setItem("sapper.profile", profile);
}
let attrFor = "";          // weapon id whose attribution panel is open
let attrRows = [];

// Layer categories get their own colours so a stack reads at a glance: you can
// see "this sample has no signing layer" without reading any text.
const CAT_COLOR = {
  obfuscate: "#185fa5",
  "anti-decompile": "#534ab7",
  "junk-code": "#8a5a2b",
  "anti-sandbox": "#0f6e56",
  pack: "#a32d2d",
  fileless: "#993556",
  sign: "#3b6d11",
  entropy: "#854f0b",
  traffic: "#5f5e5a",
};
const STATE_LABEL = { effective: "生效", saturated: "饱和", caught: "被抓",
                      mixed: "部分", neutral: "" };
const STATE_COLOR = { effective: "#3b6d11", saturated: "#854f0b",
                      caught: "#a32d2d", mixed: "#5f5e5a" };

export function currentProfile() {
  return profile.split(",").map((s) => s.trim()).filter(Boolean);
}

export async function loadArmory() {
  try {
    last = await api.armory(currentProfile().join(","));
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
    return [w.id, w.name, w.family,
            ...(w.variants || []).map((v) => `${v.vid} ${v.desc} ${v.command} ${(v.stack || []).join(" ")}`)]
      .join(" ").toLowerCase().includes(q);
  });
}

function spark(history) {
  const pts = (history || []).map((h) => Number(h.conf) || 0);
  if (pts.length < 2) return "";
  const w = 48, h = 14;
  const step = w / (pts.length - 1);
  const poly = pts.map((p, i) => `${(i * step).toFixed(1)},${(h - p * h).toFixed(1)}`).join(" ");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <polyline points="${poly}" fill="none" stroke="#8a7f5c" stroke-width="1.3"/></svg>`;
}

function layerChip(lid, effect) {
  const meta = (last.layers || {})[lid] || {};
  const cat = meta.cat || "traffic";
  const color = CAT_COLOR[cat] || "#5f5e5a";
  const state = effect && effect[lid];
  const mark = state && state !== "neutral"
    ? `<em class="st" style="background:${STATE_COLOR[state] || "#5f5e5a"}">${STATE_LABEL[state]}</em>`
    : "";
  return `<span class="lchip" title="${escapeHtml(meta.name || lid)} · ${escapeHtml(lid)}"
      style="border-color:${color}44;color:${color}">${escapeHtml(meta.name || lid)}${mark}</span>`;
}

function stackBlock(v) {
  const stack = v.stack || [];
  if (!stack.length) {
    return `<div class="stack empty">裸载荷 · 无规避层</div>`;
  }
  const cats = new Set(stack.map((l) => ((last.layers || {})[l] || {}).cat));
  const missing = Object.keys(last.categories || {}).filter((c) => !cats.has(c));
  return `<div class="stack">
    ${stack.map((l) => layerChip(l, v.effect)).join("")}
    <span class="stackmeta" title="${escapeHtml(missing.map((c) => (last.categories || {})[c]).join(" / "))}">
      sig分 ${Number(v.signature_score || 0).toFixed(1)} · 成本 ${v.cost || 0}${
      missing.length ? ` · 缺 ${missing.length} 类` : " · 全覆盖"}</span>
  </div>`;
}

export function renderArmory() {
  const list = $("#armory-list");
  const rows = visible();
  const fams = [...new Set((last.weapons || []).map((w) => w.family))].sort();
  const prof = currentProfile();

  $("#armory-tools").innerHTML = `
    <input id="armory-q" placeholder="搜索武器 / 变体 / 命令 / 免杀层…" value="${escapeHtml(query)}">
    <select id="armory-fam">
      <option value="">全部家族 (${(last.weapons || []).length})</option>
      ${fams.map((f) => `<option ${f === family ? "selected" : ""}>${escapeHtml(f)}</option>`).join("")}
    </select>
    <input id="armory-profile" placeholder="目标防护: defender,sandbox-cuckoo,edr"
           value="${escapeHtml(profile)}" title="逗号分隔，用于按防护产品调权与三态判定">
    <button class="act" id="armory-recommend">推荐可用</button>
    <button class="act" id="armory-seed">灌种子</button>
    <button class="act" id="armory-new">＋ 新武器</button>
    <span class="hint">${last.weapons?.length || 0} 个武器${prof.length ? " · 画像 " + prof.length + " 项" : " · 未设画像"}</span>`;

  $("#armory-q").addEventListener("input", (e) => { query = e.target.value.trim(); renderArmory(); });
  $("#armory-fam").addEventListener("change", (e) => { family = e.target.value; renderArmory(); });
  const applyProfile = (e) => {
    profile = e.target.value.trim();
    localStorage.setItem("sapper.profile", profile);
    loadArmory();
  };
  $("#armory-profile").addEventListener("change", applyProfile);
  // Enter should apply immediately; blur-only feels broken when the graph is tall
  $("#armory-profile").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
  });
  $("#armory-recommend").onclick = showRecommend;
  $("#armory-seed").onclick = () => ops({ action: "seed" }, "已灌入种子武器");
  $("#armory-new").onclick = () => openNewWeapon();

  if (!rows.length) {
    list.innerHTML = `<div class="hint">没有匹配的武器。可以先「灌种子」。</div>`;
    return;
  }

  list.innerHTML = rows.map((w) => `
    <article class="wcard" data-wid="${w.id}">
      <header>
        <span class="wid">${w.id}</span>
        <span class="wname">${escapeHtml(w.name)}</span>
        <span class="wfam">${escapeHtml(w.family)}</span>
        ${(w.cve || []).map((c) => `<em class="tag cve">${escapeHtml(c)}</em>`).join("")}
        <span class="wstat">${w.tries} 次 · 成功 ${w.ok} · 被拦 ${w.blocked}</span>
      </header>
      <div class="attrbox" id="attr-${w.id}" style="display:none"></div>
      <div class="vlist">${(w.variants || []).map((v) => variantRow(w, v)).join("")}</div>
      <footer>
        最优 <b>${w.best || "-"}</b>
        <button class="act" data-act="use-best">用最优变体</button>
        <button class="act" data-act="evolve">自进化</button>
        <button class="act" data-act="attr">归因</button>
        <span class="spacer"></span>
        <span class="hint">${(w.variants || []).length} 变体</span>
      </footer>
    </article>`).join("");

  $$("#armory-list .vrow button").forEach((btn) => {
    btn.onclick = () => {
      const wid = btn.closest(".wcard").dataset.wid;
      const vid = btn.closest(".vrow").dataset.vid;
      const act = btn.dataset.act;
      if (act === "use") useWeapon(wid, vid);
      else if (act === "derive") showDerive(wid, vid);
      else if (act === "evolve") showEvolve(wid, vid);
      else report(wid, vid, act);
    };
  });
  $$("#armory-list button[data-act=use-best]").forEach((btn) => {
    btn.onclick = () => useWeapon(btn.dataset.wid, null);
  });
  $$("#armory-list button[data-act=attr]").forEach((btn) => {
    btn.onclick = () => toggleAttribution(btn.closest(".wcard").dataset.wid);
  });
  $$("#armory-list button[data-act=evolve]").forEach((btn) => {
    btn.onclick = () => {
      const wcard = btn.closest(".wcard");
      const vid = btn.closest(".vrow")?.dataset.vid || null;
      showEvolve(wcard.dataset.wid, vid);
    };
  });

  if (attrFor && attrRows.length) renderAttrInto(attrFor);
}

function variantRow(w, v) {
  const stats = v.stats || {};
  const conf = Number(v.confidence ?? 0.5);
  const est = Number(v.estimate ?? conf);
  const dep = v.deprecated;
  return `<div class="vrow ${dep ? "dep" : ""}" data-vid="${v.vid}">
    <span class="vid">${escapeHtml(v.vid)}${dep ? '<em class="tag">弃</em>' : ""}${
      v.derived_from ? `<em class="tag src">←${escapeHtml(v.derived_from)}</em>` : ""}</span>
    <span class="vdesc" title="${escapeHtml(v.command || "")}">
      ${escapeHtml(v.desc || v.command || "")}</span>
    <span class="conf" title="实测 ${conf.toFixed(2)} · 预估 ${est.toFixed(2)}">
      <i style="width:${Math.round(est * 100)}%"></i><b>${est.toFixed(2)}</b></span>
    ${spark(v.history)}
    <span class="vstats" title="尝试/成功 · 连败">${stats.tries || 0}/${stats.ok || 0} · 败${v.streak_fail || 0}</span>
    <span class="vacts">
      <button class="act" data-act="use">用</button>
      <button class="act" data-act="derive">派生</button>
      <button class="act" data-act="evolve">进化</button>
      <button class="act ok" data-act="ok">成功</button>
      <button class="act warn" data-act="blocked">被拦</button>
      <button class="act fail" data-act="fail">失败</button>
    </span>
    ${stackBlock(v)}
  </div>`;
}

async function ops(body, okText) {
  try {
    const d = await api.armoryOp({ ...body, profile: currentProfile() });
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
  const eff = (d.effect || []).filter((e) => e.state !== "neutral");
  box.style.display = "block";
  box.innerHTML = `<div class="kv">${escapeHtml(d.weapon)}/${escapeHtml(d.variant)}
      · 实测 ${Number(d.confidence).toFixed(2)} · 预估 ${Number(d.estimate || 0).toFixed(2)}
      · sig分 ${Number(d.signature_score || 0).toFixed(1)} · 成本 ${d.cost || 0}</div>
    <div class="stack">${(d.stack || []).map((l) => layerChip(l, {})).join("") || "<span class='hint'>裸载荷</span>"}</div>
    ${eff.length ? `<div class="kv">${eff.map((e) =>
      `<span style="color:${STATE_COLOR[e.state] || "#5f5e5a"}">${escapeHtml(e.name)}:${STATE_LABEL[e.state]}</span>`).join(" · ")}</div>` : ""}
    <pre id="armory-cmd">${escapeHtml(d.command || "(该变体没有命令)")}</pre>
    <button class="act" id="armory-copy">复制命令</button>
    <button class="act" id="armory-close">关闭</button>`;
  $("#armory-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(d.command || ""); toast("已复制"); }
    catch (e) { toast("复制失败"); }
  };
  $("#armory-close").onclick = () => { box.style.display = "none"; };
}

async function showDerive(wid, vid) {
  const d = await ops({ action: "derive", weapon: wid, vid });
  if (!d) return;
  const box = $("#armory-out");
  const cands = d.candidates || [];
  box.style.display = "block";
  box.innerHTML = `<div class="dtitle">${escapeHtml(wid)}/${escapeHtml(vid)} 派生建议
      <span class="hint">当前 ${(d.stack || []).length} 层</span></div>
    ${cands.length ? cands.map((c, i) => `
      <div class="cand">
        <label><input type="checkbox" data-i="${i}" ${i < 2 ? "checked" : ""}>
          <b>${escapeHtml(c.name)}</b>
          <span class="tag">${escapeHtml(c.cat)}</span>
          <span class="hint">score ${c.score} · cost ${c.cost} · risk ${c.risk}</span></label>
        <div class="hint">${escapeHtml((c.reasons || []).join(" · "))}</div>
        ${(c.bundle || []).length > 1 ? `<div class="hint">成套: ${c.bundle.map(escapeHtml).join(" + ")}</div>` : ""}
      </div>`).join("")
      : "<div class='hint'>没有可加的层（全被覆盖/前置不满足/与目标冲突）</div>"}
    <div class="mrow">
      <button class="act pri" id="der-apply" ${cands.length ? "" : "disabled"}>派生新变体</button>
      <button class="act" id="der-close">关闭</button>
    </div>`;
  $("#der-close").onclick = () => { box.style.display = "none"; };
  const apply = $("#der-apply");
  if (apply) apply.onclick = async () => {
    const picked = $$("#armory-out input[type=checkbox]:checked").map((x) => Number(x.dataset.i));
    if (!picked.length) { toast("至少选一条"); return; }
    const body = { action: "derive", weapon: wid, vid, apply: true };
    // send explicit layer ids via top-N selection on the server side
    const r = await api.armoryOp({ ...body, profile: currentProfile(), top: Math.max(...picked) + 1 });
    if (r && r.applied) {
      toast(`已派生 ${wid}/${r.applied.vid}（${(r.applied.stack || []).length} 层）`);
      box.style.display = "none";
      await loadArmory();
    }
  };
}

async function toggleAttribution(wid) {
  const box = $(`#attr-${wid}`);
  if (attrFor === wid) {
    attrFor = ""; attrRows = [];
    box.style.display = "none";
    return;
  }
  attrFor = wid;
  try {
    const d = await api.armoryAttr(wid, currentProfile().join(","));
    attrRows = d.rows || [];
  } catch (e) { toast("归因失败: " + e.message); return; }
  renderAttrInto(wid);
  box.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function renderAttrInto(wid) {
  const box = $(`#attr-${wid}`);
  if (!box) return;
  box.style.display = "block";
  if (!attrRows.length) {
    box.innerHTML = `<div class="hint">还没有足够记录：同一把武器至少跑两个不同栈才能归因。</div>`;
    return;
  }
  const rows = attrRows.slice(0, 14);
  box.innerHTML = `<div class="dtitle">免杀层归因
      <span class="hint">lift = 有该层成功率 − 无该层成功率</span></div>
    <table class="attrtab">
      <thead><tr><th>层</th><th>类别</th><th>有/无</th><th>成功率</th><th>lift</th></tr></thead>
      <tbody>${rows.map((r) => {
        const lift = r.lift;
        const color = lift == null ? "#8a8781" : lift > 0 ? "#3b6d11" : "#a32d2d";
        return `<tr>
          <td>${escapeHtml(r.name || r.layer)}
            ${(r.collinear_with || []).length ? `<em class="tag warn" title="与其他 ${r.collinear_with.length} 层总是同现，无法区分">共线</em>` : ""}</td>
          <td class="hint">${escapeHtml(r.cat || "")}</td>
          <td>${r.with ?? 0}/${r.without ?? 0}</td>
          <td>${Number(r.rate_with ?? 0).toFixed(2)} vs ${Number(r.rate_without ?? 0).toFixed(2)}</td>
          <td style="color:${color};font-weight:500">${
            lift == null ? "无对照" : (lift > 0 ? "+" : "") + Number(lift).toFixed(2)}</td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
}

async function showRecommend() {
  const box = $("#armory-out");
  let d;
  try {
    d = await api.armoryOp({ action: "recommend", profile: currentProfile(), limit: 5 });
  } catch (e) { toast("推荐失败: " + e.message); return; }
  const picks = d.picks || [];
  box.style.display = "block";
  box.innerHTML = `<div class="dtitle">目标画像下的可用武器
      <span class="hint">${currentProfile().join(",") || "未设画像，仅按结构与战绩"}</span></div>
    ${picks.length ? picks.map((p, i) => `
      <div class="pick">
        <div><b>${i + 1}. ${escapeHtml(p.weapon)}/${escapeHtml(p.variant)}</b>
          <span class="hint">est ${Number(p.estimate).toFixed(2)} · 实测 ${Number(p.confidence).toFixed(2)}
          · sig分 ${Number(p.signature_score).toFixed(1)} · 成本 ${p.cost}</span></div>
        <div class="hint">${escapeHtml(p.name)} · ${escapeHtml(p.summary || "")}</div>
        ${(p.reasons || []).map((r) => `<div class="hint">· ${escapeHtml(r)}</div>`).join("")}
      </div>`).join("") : "<div class='hint'>没有匹配的武器</div>"}
    <div class="mrow"><button class="act" id="rec-close">关闭</button></div>`;
  $("#rec-close").onclick = () => { box.style.display = "none"; };
}

async function showEvolve(wid, vid) {
  const engines = last.engines || {};
  const avail = Object.entries(engines)
    .filter(([k, v]) => v.available && k !== "mock");
  const box = $("#armory-out");
  box.style.display = "block";
  box.innerHTML = `
    <div class="dtitle">${escapeHtml(wid)}${vid ? "/" + escapeHtml(vid) : ""} 自进化
      <span class="hint">投样本给本地 AV，被拦自动派生下一变体</span></div>
    <div class="form">
      <label>探测引擎</label>
      <select id="evo-engine">
        ${avail.map(([k, v]) =>
          `<option value="${k}" ${k === "huorong" ? "selected" : ""}>${k} · ${escapeHtml(v.note || "")}</option>`).join("")}
        <option value="mock">mock · 演练（不落盘/不触发 AV）</option>
      </select>
      <label>轮数上限</label><input id="evo-rounds" type="number" value="5" min="1" max="20">
      <label>每轮加层数</label><input id="evo-top" type="number" value="1" min="1" max="3">
      <label>C2 回连地址（动态关）</label>
      <input id="evo-c2" placeholder="127.0.0.1:45678" title="本地 mock 测行为链路；真实 C2 才能触发动态拦截">
      <label>真实载荷（留空=EICAR 静态 + 无害动态）</label>
      <textarea id="evo-payload" placeholder="自定义 payload 内容，用于测真实样本"></textarea>
      <div class="mrow">
        <button class="act pri" id="evo-start">开始进化</button>
        <button class="act" id="evo-close">关闭</button>
      </div>
      <div id="evo-out"></div>
    </div>`;
  $("#evo-close").onclick = () => { box.style.display = "none"; };
  $("#evo-start").onclick = async () => {
    const engine = $("#evo-engine").value;
    const rounds = parseInt($("#evo-rounds").value) || 5;
    const top = parseInt($("#evo-top").value) || 1;
    const payload = $("#evo-payload").value.trim();
    const c2raw = $("#evo-c2").value.trim() || "127.0.0.1:45678";
    const [c2_host, c2_port] = c2raw.includes(":")
      ? [c2raw.slice(0, c2raw.lastIndexOf(":")), c2raw.slice(c2raw.lastIndexOf(":") + 1)]
      : [c2raw, "45678"];
    const out = $("#evo-out");
    out.innerHTML = `<div class="hint">进化中…静态落盘扫描 + 动态运行观测（每轮约 ${rounds}×3s）</div>`;
    try {
      const d = await api.armoryOp({
        action: "evolve", weapon: wid, vid, engine, rounds, top,
        payload: payload || null, c2_host, c2_port, profile: currentProfile(),
      });
      renderEvolveResult(out, d);
      await loadArmory();
    } catch (e) {
      out.innerHTML = `<div class="hint" style="color:#a32d2d">进化失败: ${escapeHtml(e.message)}</div>`;
    }
  };
}

function renderEvolveResult(out, d) {
  const steps = d.steps || [];
  const mark = { clean: "放行", detected: "被拦", error: "异常" };
  const gateName = { static: "静态", dynamic: "动态" };
  out.innerHTML = `
    <div class="dtitle" style="color:${d.success ? "#3b6d11" : "#a32d2d"}">
      ${d.success ? "✅ 双关通过" : "❌ 未突破"} — ${escapeHtml(d.reason)}</div>
    ${steps.map((s) => {
      const st = mark[s.static_result] || s.static_result;
      const dyn = s.dynamic_result ? ` · 动态${mark[s.dynamic_result]}` : "";
      const bcn = s.beacon_reached != null ? ` · 回连${s.beacon_reached ? "✓" : "✗"}` : "";
      const sApp = (s.static_applied || []).map((a) => a.transform).join("+") || "(裸)";
      const dApp = (s.dynamic_applied || []).map((a) => a.behavior).join("+");
      return `<div class="pick">
        <div><b>R${s.round} ${escapeHtml(s.variant)}</b>
          <span class="hint">栈[${s.stack.length}] · ${gateName[s.gate] || s.gate}关 · 静态${st}${dyn}${bcn}</span></div>
        <div class="hint">静态[${escapeHtml(sApp)}]${dApp ? ` · 动态[${escapeHtml(dApp)}]` : ""}
          ${s.new_variant ? ` → 派生 ${escapeHtml(s.new_variant)}` : ""}</div>
      </div>`;
    }).join("")}
    ${d.note ? `<div class="hint">${escapeHtml(d.note)}</div>` : ""}
    <div class="mrow"><button class="act" id="evo-done">完成</button></div>`;
  $("#evo-done").onclick = () => { $("#armory-out").style.display = "none"; };
}

function openNewWeapon() {
  const box = $("#armory-out");
  box.style.display = "block";
  box.innerHTML = `
    <div class="dtitle">新建武器</div>
    <div class="form">
      <label>名称</label><input id="w-name" placeholder="例如 Tomcat AJP 文件读取">
      <label>家族</label><input id="w-family" placeholder="exploit/tomcat 或 payload/windows">
      <label>变体 v1 命令</label><textarea id="w-cmd" placeholder="ajpShooter 127.0.0.1 8009 /WEB-INF/web.xml"></textarea>
      <label>免杀层（逗号分隔，可空）</label>
      <input id="w-stack" placeholder="obf.string-enc,fileless.srdi">
      <div class="mrow"><button class="act pri" id="w-save">保存</button>
      <button class="act" id="w-cancel">取消</button></div>
    </div>`;
  $("#w-cancel").onclick = () => { box.style.display = "none"; };
  $("#w-save").onclick = async () => {
    const stack = $("#w-stack").value.split(",").map((s) => s.trim()).filter(Boolean);
    const body = {
      action: "add",
      name: $("#w-name").value,
      family: $("#w-family").value || "misc",
      variants: [{ vid: "v1", desc: "原始变体", command: $("#w-cmd").value, stack }],
    };
    const d = await ops(body, "已创建武器");
    if (d) box.style.display = "none";
  };
}
