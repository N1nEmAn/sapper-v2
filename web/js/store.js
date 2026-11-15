// Shared UI state + a tiny event bus. No framework, no build step.

export const state = {
  mode: "graph",        // graph | assets | traffic | chat
  writeMode: false,
  graph: { facts: [], intents: [], stats: {} },
  selectedId: null,
  assetFilter: new Set(),   // empty = all types
  assetQuery: "",
  assetLayout: "force",     // force | grouped
  assetFocus: null,         // asset key focused
  busy: false,
};

const bus = new EventTarget();

export function on(name, fn) {
  bus.addEventListener(name, (e) => fn(e.detail));
}

export function emit(name, detail) {
  bus.dispatchEvent(new CustomEvent(name, { detail }));
}
