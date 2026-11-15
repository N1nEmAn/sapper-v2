// Chat view: the model reads the graph and acts through controlled tools.

import { api } from "./api.js";
import { $, escapeHtml } from "./util.js";

export async function loadChat() {
  const box = $("#chatlog");
  try {
    const d = await api.chat();
    if (d.enabled === false) {
      box.innerHTML = `<div class="msg assistant"><span class="who">提示</span>
        对话需要可写模式：<code>sapper serve --allow-write</code></div>`;
      $("#chatsend").disabled = true;
      return;
    }
    renderChat(d.messages || []);
  } catch (e) {
    box.innerHTML = `<div class="msg assistant">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

export function renderChat(messages) {
  const box = $("#chatlog");
  box.innerHTML = "";
  if (!messages.length) {
    box.innerHTML = `<div class="msg assistant"><span class="who">提示</span>` +
      `让它读图、查事实、派意图。写入都经过守卫与事件链；它不能执行命令。</div>`;
    return;
  }
  messages.forEach((m) => {
    const div = document.createElement("div");
    if (m.role === "tool") {
      div.className = "msg tool";
      const args = Object.entries(m.input || {})
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
        .join(" ");
      div.innerHTML = `<b>${escapeHtml(m.text)}</b> ${escapeHtml(args).slice(0, 200)}`;
    } else {
      div.className = "msg " + (m.role === "user" ? "user" : "assistant");
      div.innerHTML = `<span class="who">${m.role === "user" ? "你" : "参谋"}</span>${escapeHtml(m.text)}`;
    }
    box.appendChild(div);
  });
  box.scrollTop = box.scrollHeight;
}

let sending = false;

export async function sendChat(onGraphChange) {
  if (sending) return;
  const input = $("#chatinput");
  const text = input.value.trim();
  if (!text) return;

  const box = $("#chatlog");
  input.value = "";
  if (box.textContent.includes("让它读图")) box.innerHTML = "";
  const echo = document.createElement("div");
  echo.className = "msg user";
  echo.innerHTML = `<span class="who">你</span>${escapeHtml(text)}`;
  box.appendChild(echo);
  box.scrollTop = box.scrollHeight;

  sending = true;
  $("#chatsend").disabled = true;
  $("#chatstatus").textContent = "参谋思考中…（读图 / 调工具）";
  try {
    const d = await api.sendChat(text);
    renderChat(d.messages || []);
    $("#chatstatus").textContent = (d.actions && d.actions.length)
      ? "已执行：" + d.actions.map((a) => a.summary).join("；")
      : "";
    if (d.actions && d.actions.length && onGraphChange) await onGraphChange();
  } catch (e) {
    $("#chatstatus").textContent = "";
    const err = document.createElement("div");
    err.className = "msg assistant";
    err.textContent = "失败：" + e.message;
    box.appendChild(err);
    box.scrollTop = box.scrollHeight;
  } finally {
    sending = false;
    $("#chatsend").disabled = false;
    input.focus();
  }
}

export function wireChat(onGraphChange) {
  $("#chatsend").onclick = () => sendChat(onGraphChange);
  $("#chatinput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(onGraphChange); }
  });
  $("#chatreset").onclick = async () => {
    if (!confirm("清空对话记录？（图不受影响）")) return;
    try { await api.resetChat(); } catch (e) { /* ignore */ }
    loadChat();
    $("#chatstatus").textContent = "";
  };
}
