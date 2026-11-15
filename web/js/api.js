// Every HTTP call the console makes. One place to change if the API moves.

async function parse(resp) {
  let data = {};
  try { data = await resp.json(); } catch (e) { /* empty body */ }
  if (resp.status === 401) {
    throw new Error("会话已失效：请用 sapper serve 启动时打印的 ?t= 链接重新打开");
  }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

export const getJSON = (url) => fetch(url).then(parse);

// X-Sapper-Request is what makes writes unforgeable from a cross-site form:
// a form cannot set custom headers, and a cross-origin fetch would need a
// preflight that this server refuses.
export const postJSON = (url, body) =>
  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Sapper-Request": "1" },
    body: JSON.stringify(body || {}),
  }).then(parse);

export const api = {
  health: () => getJSON("/api/health"),
  graph: () => getJSON("/api/graph"),
  events: (since) => getJSON(`/api/events?since=${since}`),
  assets: (params = "") => getJSON(`/api/assets${params}`),
  traffic: (limit = 200) => getJSON(`/api/traffic?limit=${limit}`),
  trafficOne: (id) => getJSON(`/api/traffic/${id}`),
  chat: () => getJSON("/api/chat"),
  sendChat: (message) => postJSON("/api/chat", { message }),
  resetChat: () => postJSON("/api/chat/reset", {}),
  search: (q) => getJSON(`/api/search?q=${encodeURIComponent(q)}&limit=8`),
  confirm: (id) => postJSON(`/api/confirm/${id}`, {}),
  fact: (body) => postJSON("/api/ops/fact", body),
  intent: (body) => postJSON("/api/ops/intent", body),
  claim: (id) => postJSON(`/api/ops/claim/${id}`, { worker: "web" }),
  release: (id) => postJSON(`/api/ops/release/${id}`, { worker: "web" }),
  done: (id, body) => postJSON(`/api/ops/done/${id}`, body),
  dispatch: (id) => postJSON(`/api/ops/dispatch/${id}`, { skip_permissions: true }),
  dispatchStatus: (id) => getJSON(`/api/ops/dispatch/${id}`),
  stopDispatch: (id) => postJSON(`/api/ops/dispatch_stop/${id}`, {}),
  assetAction: (action, key) => postJSON("/api/ops/assets", { action, key }),
};
