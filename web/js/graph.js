// Generic force-directed view used by both the task graph and the asset map.
// Responsibilities: layout, zoom, drag, selection/focus styling, hit-testing.
// Domain knowledge (what a node means) stays in the caller via callbacks.

export function createGraph(svgSelector, opts = {}) {
  const svg = d3.select(svgSelector);
  const root = svg.append("g");
  const linkLayer = root.append("g").attr("class", "links");
  const nodeLayer = root.append("g").attr("class", "nodes");

  const positions = new Map();   // id -> {x, y}
  let nodes = [], links = [], sim = null;
  let selected = null;
  let focusSet = null;
  let layout = "force";

  const zoom = d3.zoom().scaleExtent([0.15, 4]).on("zoom", (e) => {
    root.attr("transform", e.transform);
  });
  svg.call(zoom).on("dblclick.zoom", null);

  const nodeShape = opts.shape || (() => "circle");
  const nodeRadius = opts.radius || (() => 9);
  const nodeColor = opts.color || (() => "#888");
  const nodeLabel = opts.label || ((d) => d.label || d.id);
  const onSelect = opts.onSelect || (() => {});
  const onBackground = opts.onBackground || (() => {});
  const tooltip = opts.tooltip || null;

  svg.on("click", (e) => {
    if (e.target === svg.node()) onBackground();
  });

  function size() {
    const box = svg.node().getBoundingClientRect();
    return { w: box.width || 800, h: box.height || 600 };
  }

  function drag() {
    return d3.drag()
      .on("start", (e, d) => {
        if (!e.active) sim.alphaTarget(0.25).restart();
        d.fx = d.x; d.fy = d.y;
      })
      .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end", (e, d) => {
        if (!e.active) sim.alphaTarget(0);
        if (layout === "force") { d.fx = null; d.fy = null; }
      });
  }

  function applyStyles() {
    nodeLayer.selectAll("g.node")
      .classed("sel", (d) => d.id === selected)
      .classed("dim", (d) => focusSet && !focusSet.has(d.id))
      .classed("hit", (d) => focusSet && focusSet.has(d.id));

    linkLayer.selectAll("line")
      .classed("dim", (d) => {
        if (!focusSet) return false;
        const s = typeof d.source === "object" ? d.source.id : d.source;
        const t = typeof d.target === "object" ? d.target.id : d.target;
        return !(focusSet.has(s) && focusSet.has(t));
      });
  }

  function draw() {
    linkLayer.selectAll("*").remove();
    nodeLayer.selectAll("*").remove();

    const l = linkLayer.selectAll("line").data(links).join("line").attr("class", "link");

    const n = nodeLayer.selectAll("g").data(nodes, (d) => d.id).join("g")
      .attr("class", "node")
      .call(drag());

    n.each(function (d) {
      const el = d3.select(this);
      const shape = nodeShape(d);
      if (shape === "rect") {
        const r = nodeRadius(d);
        el.append("rect")
          .attr("x", -r).attr("y", -r).attr("width", r * 2).attr("height", r * 2)
          .attr("rx", 3)
          .attr("fill", nodeColor(d))
          .attr("stroke", "#fff").attr("stroke-width", 1.4);
      } else {
        el.append("circle")
          .attr("r", nodeRadius(d))
          .attr("fill", nodeColor(d))
          .attr("fill-opacity", 0.9)
          .attr("stroke", "#fff").attr("stroke-width", 1.4);
      }
      el.append("text")
        .attr("dy", nodeRadius(d) + 11)
        .attr("text-anchor", "middle")
        .text(nodeLabel(d));
    });

    n.on("click", (e, d) => { e.stopPropagation(); onSelect(d); })
     .on("dblclick", (e, d) => { e.stopPropagation(); zoomTo(d.id, 2.2); });

    if (tooltip) {
      const tip = document.getElementById("tip");
      const box = svg.node().getBoundingClientRect();
      n.on("mouseenter", (e, d) => {
        tip.innerHTML = tooltip(d);
        tip.style.display = "block";
      }).on("mousemove", (e) => {
        const x = Math.min(e.clientX - box.left + 14, box.width - 300);
        const y = Math.min(e.clientY - box.top + 14, box.height - 120);
        tip.style.left = x + "px";
        tip.style.top = y + "px";
      }).on("mouseleave", () => { tip.style.display = "none"; });
    }

    const cx = size().w / 2, cy = size().h / 2;
    sim = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id((d) => d.id).distance(opts.linkDistance || 80)
        .strength(0.55))
      .force("charge", d3.forceManyBody().strength(opts.charge || -260))
      .force("center", d3.forceCenter(cx, cy))
      // gravity keeps disconnected components from drifting to opposite corners
      .force("x", d3.forceX(cx).strength(0.06))
      .force("y", d3.forceY(cy).strength(0.06))
      .force("collide", d3.forceCollide((d) => nodeRadius(d) + 16));

    sim.on("tick", () => {
      l.attr("x1", (d) => d.source.x).attr("y1", (d) => d.source.y)
       .attr("x2", (d) => d.target.x).attr("y2", (d) => d.target.y);
      n.attr("transform", (d) => `translate(${d.x},${d.y})`);
      nodes.forEach((d) => positions.set(d.id, { x: d.x, y: d.y }));
    });

    if (layout === "grouped") applyGrouped();
    applyStyles();
  }

  function applyGrouped() {
    const groups = new Map();
    nodes.forEach((d) => {
      const key = d.group || d.type || "?";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(d);
    });
    const keys = [...groups.keys()].sort();
    const { w, h } = size();
    const colW = Math.max(120, (w - 80) / Math.max(keys.length, 1));
    keys.forEach((key, ci) => {
      const list = groups.get(key);
      const x = 60 + colW * (ci + 0.5);
      list.forEach((d, i) => {
        d.fx = x;
        d.fy = 60 + ((h - 120) / Math.max(list.length, 1)) * (i + 0.5);
      });
    });
    if (sim) { sim.alpha(0.1).restart(); }
  }

  function setData(nextNodes, nextLinks) {
    // keep prior positions so the layout does not jump on every poll
    nodes = nextNodes.map((d) => {
      const p = positions.get(d.id);
      return p ? { ...d, x: p.x, y: p.y } : { ...d };
    });
    links = nextLinks.map((e) => ({ ...e }));
    draw();
  }

  function setSelected(id) {
    selected = id;
    applyStyles();
  }

  function setFocus(ids) {
    focusSet = ids ? new Set(ids) : null;
    applyStyles();
  }

  function neighborsOf(id, depth = 1) {
    const keep = new Set([id]);
    let frontier = [id];
    for (let step = 0; step < depth; step += 1) {
      const next = [];
      links.forEach((e) => {
        const s = typeof e.source === "object" ? e.source.id : e.source;
        const t = typeof e.target === "object" ? e.target.id : e.target;
        if (frontier.includes(s) && !keep.has(t)) { keep.add(t); next.push(t); }
        if (frontier.includes(t) && !keep.has(s)) { keep.add(s); next.push(s); }
      });
      frontier = next;
    }
    return keep;
  }

  function zoomTo(id, scale) {
    const node = nodes.find((d) => d.id === id);
    if (!node || node.x == null) return;
    const { w, h } = size();
    const k = scale || 1.6;
    svg.transition().duration(400).call(
      zoom.transform,
      d3.zoomIdentity.translate(w / 2 - node.x * k, h / 2 - node.y * k).scale(k),
    );
  }

  function fit() {
    if (!nodes.length) return;
    const xs = nodes.map((d) => d.x || 0), ys = nodes.map((d) => d.y || 0);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const { w, h } = size();
    const k = Math.min(3, 0.85 / Math.max((maxX - minX) / w, (maxY - minY) / h, 0.001));
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    svg.transition().duration(400).call(
      zoom.transform,
      d3.zoomIdentity.translate(w / 2 - cx * k, h / 2 - cy * k).scale(k),
    );
  }

  function setLayout(next) {
    layout = next;
    if (layout === "force") {
      nodes.forEach((d) => { d.fx = null; d.fy = null; });
      if (sim) {
        sim.force("center", d3.forceCenter(size().w / 2, size().h / 2));
        sim.alpha(0.7).restart();
      }
    } else {
      if (sim) sim.force("center", null);
      applyGrouped();
    }
  }

  function resize() {
    if (sim) {
      sim.force("center", d3.forceCenter(size().w / 2, size().h / 2)).alpha(0.25).restart();
    }
  }

  return {
    setData, setSelected, setFocus, neighborsOf, zoomTo, fit, setLayout, resize,
    get layout() { return layout; },
    get nodes() { return nodes; },
  };
}
