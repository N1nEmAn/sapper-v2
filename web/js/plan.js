// Plan view: the multi-step chain that survives commander wake-ups.
// A stateless planner either front-loads a whole exploit chain or forgets it;
// this panel is the memory that makes step-by-step progress possible.

import { api } from "./api.js";
import { $, $$, escapeHtml, toast } from "./util.js";

let last = { steps: [], progress: {}, ready: [] };

const STATUS = {
  todo: { mark: "☐", label: "待办", color: "#5f5e5a" },
  doing: { mark: "▶", label: "进行中", color: "#185fa5" },
  done: { mark: "✔", label: "完成", color: "#3b6d11" },
  blocked: { mark: "✕", label: "阻塞", color: "#a32d2d" },
  dropped: { mark: "—", label: "放弃", color: "#8a8781" },
};

export async function loadPlan() {
  try {
    last = await api.plan();
    renderPlan();
  } catch (e) {
    $("#plan-list").innerHTML =
      `<div class="hint">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function actionsFor(s) {
  // Done steps only offer "重开"; a finished step does not need "开始".
  if (s.status === "done" || s.status === "dropped") {
    return `<button class="act" data-act="todo">重开</button>`;
  }
  const parts = [];
  if (s.status !== "doing") parts.push(`<button class="act" data-act="doing">开始</button>`);
  parts.push(`<button class="act ok" data-act="done">完成</button>`);
  if (s.status !== "blocked") parts.push(`<button class="act warn" data-act="blocked">阻塞</button>`);
  return parts.join("");
}

export function renderPlan() {
  const steps = last.steps || [];
  const prog = last.progress || {};
  const ready = new Set(last.ready || []);

  $("#plan-tools").innerHTML = `
    <input id="plan-title" placeholder="新增步骤，例如 用注入读配置拿凭据">
    <select id="plan-after">
      <option value="">无前置</option>
      ${steps.map((s) => `<option value="${s.n}">前置: ${s.n}. ${escapeHtml(s.title.slice(0, 18))}</option>`).join("")}
    </select>
    <button class="act" id="plan-add">＋ 添加</button>
    <span class="hint">${prog.done || 0}/${prog.total || 0} 完成 · ${prog.percent || 0}%${
      prog.blocked ? ` · 阻塞 ${prog.blocked}` : ""}</span>`;

  $("#plan-add").onclick = async () => {
    const title = $("#plan-title").value.trim();
    if (!title) { toast("先写标题"); return; }
    const after = $("#plan-after").value;
    try {
      await api.planOp({ action: "add", title, after: after || null });
      $("#plan-title").value = "";
      await loadPlan();
    } catch (e) { toast("添加失败: " + e.message); }
  };
  $("#plan-title").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#plan-add").click();
  });

  if (!steps.length) {
    $("#plan-list").innerHTML = `<div class="hint">还没有规划待办。
      多步链路（发现注入点 → 取凭据 → 横向 → 提权）请先写下来，commander 每次唤醒都会看到它。</div>`;
    return;
  }

  $("#plan-list").innerHTML = steps.map((s) => {
    const meta = STATUS[s.status] || STATUS.todo;
    const dep = (s.depends_on || []).length
      ? `<span class="hint">前置 ${s.depends_on.join(",")}</span>` : "";
    const intent = s.intent
      ? `<span class="tag" data-intent="${escapeHtml(s.intent)}">${escapeHtml(s.intent)}</span>` : "";
    const canStart = ready.has(s.n);
    return `<div class="prow ${s.status}" data-n="${s.n}">
      <span class="pmark" style="color:${meta.color}">${meta.mark}</span>
      <span class="pn">${s.n}</span>
      <span class="ptitle">${escapeHtml(s.title)}
        ${s.note ? `<span class="hint"> — ${escapeHtml(s.note)}</span>` : ""}</span>
      ${intent}${dep}
      ${canStart ? `<span class="tag ready">可派</span>` : ""}
      <span class="pacts">${actionsFor(s)}</span>
    </div>`;
  }).join("");

  $$("#plan-list button").forEach((btn) => {
    btn.onclick = async () => {
      const n = Number(btn.closest(".prow").dataset.n);
      const status = btn.dataset.act;
      let note = "";
      if (status === "blocked") {
        note = prompt("阻塞原因（会跨唤醒保留）") || "";
      }
      try {
        await api.planOp({ action: "set", n, status, note });
        await loadPlan();
      } catch (e) { toast("更新失败: " + e.message); }
    };
  });
}
