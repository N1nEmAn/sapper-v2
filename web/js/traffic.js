// Traffic view: captured HTTP, filtered at capture time.

import { api } from "./api.js";
import { $, escapeHtml, shortTime } from "./util.js";

export async function loadTraffic() {
  try {
    renderTraffic(await api.traffic(200));
  } catch (e) {
    $("#tstats").textContent = "加载失败: " + e.message;
  }
}

export function renderTraffic(d) {
  const tbody = $("#ttable tbody");
  tbody.innerHTML = "";
  const s = d.stats || { total: 0, hosts: 0, by_status: {} };
  $("#tstats").textContent = d.available === false
    ? "还没有流量记录。抓包: mitmdump -s tools/mitm_sapper.py --set sapper_store=<项目目录>"
    : `${s.total} 条 · ${s.hosts} 个主机 · ${shortTime(s.first)} → ${shortTime(s.last)}`;

  $("#s1").innerHTML = `traffic <b>${s.total || 0}</b>`;
  $("#s2").innerHTML = `hosts <b>${s.hosts || 0}</b>`;
  $("#s3").innerHTML = `codes <b>${Object.keys(s.by_status || {}).length}</b>`;
  $("#s4").innerHTML = `最近 <b>${shortTime(s.last)}</b>`;

  if (d.available === false) return;
  (d.rows || []).forEach((r) => {
    const tr = document.createElement("tr");
    const cls = r.status ? "s" + String(r.status)[0] : "";
    tr.innerHTML = `<td class="${cls}">${r.status ?? "-"}</td><td>${r.method}</td>
      <td>${escapeHtml((r.host || "") + r.path)}</td>
      <td>${r.resp_size ?? ""}</td>
      <td>${(r.tags || []).join(",")}</td>`;
    tr.onclick = () => showTraffic(r.id);
    tbody.appendChild(tr);
  });
}

export async function showTraffic(id) {
  const box = $("#tdetail");
  try {
    const d = await api.trafficOne(id);
    box.style.display = "block";
    box.textContent = `#${d.id}  ${d.method} ${d.url}  -> ${d.status}\n\n`
      + `--- request ---\n${d.req_headers || ""}\n\n${d.req_body || ""}\n\n`
      + `--- response ---\n${d.resp_headers || ""}\n\n${d.resp_body || ""}`;
  } catch (e) { box.textContent = "加载失败: " + e.message; }
}
