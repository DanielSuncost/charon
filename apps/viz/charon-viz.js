/** Charon renderer. Model content is only assigned through textContent. */
export const RENDERER_VERSION = '1.1.0';
export const SCHEMA_VERSIONS = Object.freeze([2]);
const NS = 'http://www.w3.org/2000/svg';
const LEGEND = 'Claims: declared [D] solid · observed [O] dashed · inferred [I] dotted. Freshness: fresh / stale / unknown. Imports are observations, not design intent.';
const TOKENS = ['surface', 'surface-raised', 'text', 'text-muted', 'edge', 'edge-inferred', 'accent', 'warn', 'focus-ring', 'radius', 'font', 'font-mono'];
const text = value => typeof value === 'string' ? value : JSON.stringify(value) ?? '';
const el = (tag, content, attrs = {}, svg = false) => {
  const node = svg ? document.createElementNS(NS, tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (content !== undefined) node.textContent = text(content);
  return node;
};
const badge = item => `[${({declared: 'D', observed: 'O', inferred: 'I'})[item.claim || 'declared']}] ${item.claim || 'declared'} · ${item.freshness || 'unknown'}`;

function validate(model) {
  if (!SCHEMA_VERSIONS.includes(model?.version)) throw Error(`Unsupported schema ${model?.version}; supported: ${SCHEMA_VERSIONS.join(', ')}`);
  if (!model.system?.id || !Array.isArray(model.nodes) || !Array.isArray(model.relationships)) throw Error('Invalid system, nodes or relationships');
  const nodes = new Map(), ids = new Set(), children = new Map();
  for (const n of model.nodes) {
    if (typeof n.id !== 'string' || !n.id || ids.has(n.id)) throw Error(`Invalid or duplicate node id: ${n.id}`);
    ids.add(n.id); nodes.set(n.id, n);
    const parent = n.parentId ?? null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(n);
  }
  const done = new Set();
  for (const n of model.nodes) {
    let cur = n; const path = new Set();
    while (cur && !done.has(cur.id)) {
      if (path.has(cur.id)) throw Error(`Containment cycle: ${cur.id}`);
      path.add(cur.id);
      if (cur.parentId != null && !nodes.has(cur.parentId)) throw Error(`Missing parent: ${cur.parentId}`);
      cur = nodes.get(cur.parentId);
    }
    for (const id of path) done.add(id);
  }
  for (const r of model.relationships) {
    if (typeof r.id !== 'string' || !r.id || ids.has(r.id)) throw Error(`Invalid or duplicate relationship id: ${r.id}`);
    ids.add(r.id);
    for (const end of [r.from, r.to]) {
      if (!end?.systemId || !end.nodeId) throw Error(`Invalid endpoint: ${r.id}`);
      if (end.systemId === model.system.id && !nodes.has(end.nodeId)) throw Error(`Missing local endpoint: ${end.nodeId}`);
    }
  }
  for (const item of [...model.nodes, ...model.relationships]) {
    for (const key of ['code', 'evidence']) if (item[key] !== undefined && (!Array.isArray(item[key]) || item[key].some(ref => !ref || typeof ref !== 'object'))) throw Error(`Invalid ${key}: ${item.id}`);
    if (item.claim && !['declared', 'observed', 'inferred'].includes(item.claim)) throw Error(`Invalid claim: ${item.id}`);
    if (item.freshness && !['fresh', 'stale', 'unknown'].includes(item.freshness)) throw Error(`Invalid freshness: ${item.id}`);
  }
  return {nodes, children};
}

// Port of Acheron mapview.js layerNodes/layoutGraph: dependency depth, then
// declaration-ordered subsystem lanes. Keep claims separate; never merge facts.
const COL = 360, ROW = 155, PAD = 65;
const FLOW = new Set(['reads', 'writes', 'calls', 'produces']);
function directed(r, mode) {
  if (mode === 'data-flow' && r.kind === 'reads') return [r.to.nodeId, r.from.nodeId];
  return [r.from.nodeId, r.to.nodeId];
}
function layers(nodes, edges, mode) {
  const deps = new Map(nodes.map(n => [n.id, []])), depth = new Map();
  for (const e of edges) { const [a, b] = directed(e, mode); if (a !== b && deps.has(a) && deps.has(b)) deps.get(a).push(b); }
  const remaining = new Set(deps.keys());
  while (remaining.size) {
    let progressed = false;
    for (const id of remaining) if (!deps.get(id).some(d => remaining.has(d))) {
      depth.set(id, 1 + Math.max(-1, ...deps.get(id).map(d => depth.get(d))));
      remaining.delete(id); progressed = true;
    }
    if (!progressed) {
      const id = remaining.values().next().value; // deterministic cycle break
      depth.set(id, 1 + Math.max(-1, ...deps.get(id).filter(d => depth.has(d)).map(d => depth.get(d))));
      remaining.delete(id);
    }
  }
  const max = Math.max(0, ...depth.values());
  return new Map(nodes.map(n => [n.id, max - depth.get(n.id)]));
}

/** Count inversions between edges joining the same pair of columns, excluding
 * shared endpoints and feedback edges. This is a layer-order metric, not a count
 * of intersections after orthogonal routing. */
function crossingCount(edges, positions, mode) {
  let count = 0;
  const pairs = edges.map(e => directed(e, mode)).filter(([a,b]) => positions.has(a) && positions.has(b));
  for (let i = 0; i < pairs.length; i++) for (let j = i + 1; j < pairs.length; j++) {
    const [a,b] = pairs[i], [c,d] = pairs[j];
    if (a === c || b === d) continue;
    const [pa,pb,pc,pd] = [a,b,c,d].map(id => positions.get(id));
    if (pa.x < pb.x && pa.x === pc.x && pb.x === pd.x && (pa.y - pc.y) * (pb.y - pd.y) < 0) count++;
  }
  return count;
}

export function layoutSystem(nodes, edges, index, mode = 'overview', previous = new Map(), pins = {}) {
  const ids = new Set(nodes.map(n => n.id));
  const localEdges = edges.filter(e => ids.has(e.from.nodeId) && ids.has(e.to.nodeId));
  const columns = layers(nodes, localEdges, mode), laneOf = new Map(), laneNames = new Map();
  for (const n of nodes) {
    let ancestor = n, lane = n.kind === 'external' ? '__external' : '__root';
    while (ancestor) {
      if (ancestor.kind === 'subsystem') { lane = ancestor.id; break; }
      if (ancestor.parentId == null && ancestor.id !== n.id) lane = ancestor.id;
      ancestor = index.nodes.get(ancestor.parentId);
    }
    laneOf.set(n.id, lane);
    laneNames.set(lane, lane === '__external' ? 'External systems' : lane === '__root' ? 'System components' : index.nodes.get(lane)?.name || lane);
    // A visible subsystem is the lane's context, not a dependency sink.
    if (n.id === lane && nodes.some(other => other.parentId === n.id)) columns.set(n.id, -1);
  }
  const groups = new Map();
  for (const n of nodes) {
    const key = JSON.stringify([laneOf.get(n.id), columns.get(n.id)]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(n.id);
  }
  const order = new Map(nodes.map((n,i) => [n.id,i]));
  function freshPositions() {
    const out = new Map(); let top = 30;
    for (const lane of laneNames.keys()) {
      let slots = 1;
      for (const [key, members] of groups) {
        const [l,c] = JSON.parse(key); if (l !== lane) continue;
        slots = Math.max(slots, members.length);
        members.forEach((id,i) => out.set(id, {x: 45 + (c + 1) * COL, y: top + PAD + i * ROW}));
      }
      top += PAD + slots * ROW + 35;
    }
    return out;
  }
  let best = freshPositions(), baseline = crossingCount(localEdges, best, mode), bestCount = baseline;
  // Barycenter sweeps reorder only within a lane/column; declaration order breaks
  // equal barycenters. Keep the best pass so crossing count can never increase.
  for (let pass = 0; pass < 4; pass++) {
    const working = freshPositions();
    const keys = [...groups.keys()].sort((a,b) => (JSON.parse(a)[1] - JSON.parse(b)[1]) * (pass % 2 ? -1 : 1));
    for (const key of keys) {
      const members = groups.get(key), scores = new Map();
      for (const id of members) {
        const neighbors = [];
        for (const e of localEdges) {
          const [a,b] = directed(e,mode);
          if (pass % 2 === 0 && b === id && columns.get(a) < columns.get(b)) neighbors.push(working.get(a).y);
          if (pass % 2 === 1 && a === id && columns.get(a) < columns.get(b)) neighbors.push(working.get(b).y);
        }
        scores.set(id, neighbors.length ? neighbors.reduce((a,b) => a+b,0) / neighbors.length : working.get(id).y);
      }
      members.sort((a,b) => scores.get(a) - scores.get(b) || order.get(a) - order.get(b));
      const ys = members.map(id => working.get(id).y).sort((a,b) => a-b);
      members.forEach((id,i) => working.set(id, {...working.get(id), y: ys[i]}));
    }
    const count = crossingCount(localEdges, working, mode);
    if (count < bestCount) { best = new Map(working); bestCount = count; }
  }
  const pos = new Map(previous);
  for (const n of nodes) if (pins[n.id]) {
    const p = pins[n.id];
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) throw Error(`Invalid pinned position: ${n.id}`);
    pos.set(n.id, {...p});
  }
  for (const n of nodes) if (!pos.has(n.id)) {
    const p = {...best.get(n.id)};
    // Incremental additions retain their semantic column and every existing slot.
    // If a lane fills, a labelled continuation region is added below it.
    while ([...pos].some(([id,q]) => ids.has(id) && Math.abs(q.x-p.x) < 300 && Math.abs(q.y-p.y) < ROW)) p.y += ROW;
    const otherLane = [...pos].filter(([id]) => ids.has(id) && laneOf.get(id) !== laneOf.get(n.id));
    if (otherLane.some(([,q]) => Math.abs(q.y-p.y) < ROW)) p.y = Math.max(p.y, ...[...pos].filter(([id]) => ids.has(id)).map(([,q]) => q.y + ROW + PAD));
    pos.set(n.id,p);
  }
  const width = Math.max(500, ...nodes.map(n => pos.get(n.id).x + 320));
  const regions = [];
  const ordered = [...nodes].sort((a,b) => pos.get(a.id).y-pos.get(b.id).y || order.get(a.id)-order.get(b.id));
  for (const n of ordered) {
    const lane = laneOf.get(n.id), p = pos.get(n.id);
    let region = regions.at(-1);
    if (!region || region.id !== lane) { region = {id: lane, name: laneNames.get(lane), y: p.y-PAD, bottom: p.y+135, members: []}; regions.push(region); }
    region.bottom = Math.max(region.bottom,p.y+135); region.members.push(n.id);
  }
  const regionOf = new Map();
  regions.forEach((r,i) => r.members.forEach(id => regionOf.set(id,i)));
  return {pos, regions, regionOf, width, columns, baselineCrossings: baseline, crossings: bestCount,
    actualCrossings: crossingCount(localEdges,pos,mode)};
}

function routeRelationship(a, b, fromRegion, toRegion, layout, track) {
  const feedback = b.x <= a.x;
  const sx = a.x + 270, sy = a.y + 52, tx = b.x, ty = b.y + 52;
  if (fromRegion === toRegion && !feedback && b.x-a.x <= COL) {
    const mid = sx + 25;
    return {path: `M ${sx} ${sy} H ${mid} V ${ty} H ${tx}`, feedback};
  }
  const ar = layout.regions[fromRegion], br = layout.regions[toRegion];
  const ay = ar.y + 38 + track % 4 * 5, by = br.y + 38 + track % 4 * 5;
  const rail = layout.width + 15 + track % 8 * 8;
  return {path: `M ${sx} ${sy} H ${sx+20} V ${ay} H ${rail} V ${by} H ${tx-20} V ${ty} H ${tx}`, feedback};
}

export function mount(element, {model, view = {}, theme = {}, callbacks = {}} = {}) {
  let current, config = {}, index, positions = new Map(), drawn, svg, world, inspector;
  let camera = {x: 0, y: 0, zoom: 1}, selection = null, expanded = new Set(), focusNode = null;
  let destroyed = false, generation = 0;
  const modePositions = new Map();
  const root = el('section', undefined, {class: 'charon-viz', 'aria-label': 'System visualization'});
  const error = el('div', '', {class: 'viz-error', role: 'alert'});
  const content = el('div'); root.append(error, content); element.append(root);
  const oldTokens = new Map();
  for (const token of TOKENS) {
    const key = `--viz-${token}`;
    if (theme[key] !== undefined) { oldTokens.set(key, element.style.getPropertyValue(key)); element.style.setProperty(key, String(theme[key])); }
  }
  const fail = exc => { if (!destroyed) error.textContent = `Visualization error: ${exc.message || exc}`; };
  const invoke = (name, ...args) => {
    try { const value = callbacks[name]?.(...args); if (value?.catch) value.catch(fail); return value; } catch (exc) { fail(exc); }
  };
  const getState = () => ({camera: {...camera}, selection, expandedIds: [...expanded], focusNode});
  const changed = () => invoke('onStateChange', getState());
  const button = (label, action) => { const b = el('button', label, {type: 'button'}); b.onclick = action; return b; };
  const crumbs = (id, idx = index) => {
    const result = [];
    while (id != null) { const n = idx.nodes.get(id); if (!n) break; result.push({id, name: n.name || id}); id = n.parentId; }
    return result.reverse();
  };
  function plan(m, v, idx, basePositions) {
    const f = v.focusNode ?? null;
    if (f != null && !idx.nodes.has(f)) throw Error(`Unknown focus: ${f}`);
    const depth = v.visibleDepth ?? 1, max = v.maxNodes ?? 200;
    if (!Number.isInteger(depth) || depth < 1 || !Number.isInteger(max) || max < 1 || max > 200) throw Error('visibleDepth must be positive; maxNodes must be 1–200');
    if (v.mode && !['overview', 'dependency', 'data-flow'].includes(v.mode)) throw Error(`Unknown mode: ${v.mode}`);
    const collapsed = new Set(v.collapsedGroups || []), visible = [], queue = (idx.children.get(f) || []).map(n => [n, 1]);
    if (f && !queue.length) queue.push([idx.nodes.get(f), 1]);
    for (let i = 0; i < queue.length && visible.length < max; i++) {
      const [n, level] = queue[i]; visible.push(n);
      if (!collapsed.has(n.id) && (level < depth || expanded.has(n.id))) {
        for (const child of idx.children.get(n.id) || []) queue.push([child, level + 1]);
      }
    }
    const ids = new Set(visible.map(n => n.id));
    const local = e => e.systemId === m.system.id && ids.has(e.nodeId);
    const relevant = m.relationships.filter(r => local(r.from) || local(r.to));
    const filtered = relevant.filter(r => !v.relationshipKinds?.length || v.relationshipKinds.includes(r.kind));
    const mode = v.mode || 'overview';
    const semantic = filtered.filter(r => mode === 'data-flow' ? FLOW.has(r.kind) : !FLOW.has(r.kind));
    const edges = semantic.slice(0, 500);
    const layout = layoutSystem(visible, edges.filter(r => r.from.systemId === m.system.id && r.to.systemId === m.system.id), idx, mode, basePositions, v.pinnedPositions || {});
    return {visible, ids, edges, ...layout, hidden: m.nodes.length - visible.length, filtered: relevant.length - semantic.length, edgeLimit: semantic.length - edges.length};
  }

  function evidence(parent, refs) {
    for (const ref of refs || []) parent.append(button(`${ref.path || ref.file || ref.url || ref.rootId || 'Reference'}${ref.line ? ':' + ref.line : ''}${ref.symbol ? '#' + ref.symbol : ''}`, () => {
      if (!callbacks.onOpenEvidence) { fail(Error('No evidence resolver is attached to this host.')); return; }
      invoke('onOpenEvidence', ref);
    }));
  }
  function inspect() {
    if (!inspector) return;
    inspector.replaceChildren();
    const item = index.nodes.get(selection) || current.relationships.find(r => r.id === selection);
    if (!item) { inspector.append(el('p', 'Select a node or relationship to inspect its evidence.')); return; }
    inspector.append(el('h3', item.name || item.label || item.id), el('p', item.description || ''), el('p', badge(item)));
    evidence(inspector, [...(item.code || []), ...(item.evidence || [])]);
    if (index.nodes.has(item.id)) {
      inspector.append(button('Focus', () => handle.focus(item.id)), button('Expand children', () => expand(item.id)));
      for (const r of current.relationships.filter(r => r.from.nodeId === item.id || r.to.nodeId === item.id)) inspector.append(button(`${r.from.nodeId} → ${r.to.nodeId} · ${r.label || r.kind} · ${badge(r)}`, () => handle.select(r.id)));
    }
    if (callbacks.onEditIntent) {
      for (const field of ['label', 'description', 'note']) {
        const input = el('input', undefined, {'aria-label': `New ${field}`});
        inspector.append(input, button(`Propose ${field}`, () => invoke('onEditIntent', {type: field, targetId: item.id, value: input.value})));
      }
      if (index.nodes.has(item.id)) inspector.append(button('Propose pin', () => invoke('onEditIntent', {type: 'pin', nodeId: item.id, position: positions.get(item.id)})), button('Propose collapse', () => invoke('onEditIntent', {type: 'collapse', nodeId: item.id, collapsed: !(config.collapsedGroups || []).includes(item.id)})));
    }
  }
  async function expand(id) {
    if (destroyed) return;
    const ticket = generation;
    try {
      let result;
      if (callbacks.onRequestChildren) {
        error.textContent = `Loading children: ${id}`;
        result = await callbacks.onRequestChildren(id);
        if (destroyed || ticket !== generation) return;
      }
      const previous = new Set(expanded); expanded.add(id);
      const merge = (old, incoming) => [...new Map([...old, ...(incoming || [])].map(n => [n.id, n])).values()];
      const next = result ? {...current, nodes: merge(current.nodes, result.nodes), relationships: merge(current.relationships, result.relationships)} : current;
      if (!update({model: next})) expanded = previous;
      else changed();
    } catch (exc) { fail(exc); }
  }
  function paint(m, v, idx, d) {
    const box = el('div'), header = el('header');
    header.append(el('h2', `${m.system.name || m.system.id} · revision ${text(m.revision ?? 'unknown')}`));
    const nav = el('nav', undefined, {'aria-label': 'Breadcrumbs'});
    nav.append(button(m.system.id, () => handle.focus(null)));
    for (const c of crumbs(v.focusNode, idx)) nav.append(button(c.name, () => handle.focus(c.id)));
    const search = el('input', undefined, {type: 'search', placeholder: 'Search loaded model', 'aria-label': 'Search loaded model'});
    const results = el('div');
    search.oninput = () => {
      results.replaceChildren(); if (!search.value) return;
      const hits = m.nodes.filter(n => `${n.name} ${n.description} ${n.id}`.toLowerCase().includes(search.value.toLowerCase()));
      for (const n of hits.slice(0, 50)) results.append(button(n.name || n.id, () => { handle.focus(n.id); handle.select(n.id); }));
      if (hits.length > 50) results.append(el('p', `Search limit: ${hits.length - 50} more results; refine your query.`));
    };
    header.append(el('p', v.mode === 'data-flow' ? 'Flow view · left → right: source to consumer. Reads: resource → reader; writes, calls, produces: actor → target. Arrows describe relations, not clock time.' : 'Structural view · left → right: depends on. Leaves and externals sit to the right. Dashed imports are observed, not declared design. ↶ marks feedback.', {class: 'viz-axis'}));
    header.append(nav, search, button('Fit', () => handle.fit()), button('Auto-layout', () => handle.autoLayout()), button('Zoom +', () => zoom(1.2)), button('Zoom −', () => zoom(1 / 1.2)));
    const modes = el('select', undefined, {'aria-label': 'View mode'});
    for (const mode of ['overview', 'dependency', 'data-flow']) modes.append(el('option', mode === 'data-flow' ? 'Flow' : mode === 'overview' ? 'Structural overview' : 'Structural dependencies', {value: mode}));
    modes.value = v.mode || 'overview'; modes.onchange = () => { if (update({view: {...config, mode: modes.value}})) handle.fit(); };
    header.append(modes, results); box.append(header);
    const limits = el('p', `View: ${d.visible.length} nodes. ${d.hidden ? `Limit state: ${d.hidden} nodes not drawn (outside focus, depth, collapse or ${v.maxNodes || 200}-node bound); search or focus to navigate.` : 'All nodes drawn.'} ${d.filtered} relationships excluded by active mode/kind filter. ${d.edgeLimit ? `Limit state: ${d.edgeLimit} relationships exceed the 500-edge bound.` : ''}`, {class: 'viz-limit', role: 'status'});
    box.append(limits);
    for (const _ of Object.keys(m.extensions || {})) box.append(el('details', undefined));
    // Every unknown extension has a readable, inert fallback, including full data.
    let extIndex = 0;
    for (const [name, ext] of Object.entries(m.extensions || {})) {
      const details = box.querySelectorAll('details')[extIndex++];
      details.append(el('summary', `Extension ${name} v${ext?.version ?? '?'} — fallback`), el('pre', ext?.fallback || JSON.stringify(ext, null, 2)));
    }
    const s = el('svg', undefined, {xmlns: NS, viewBox: '0 0 1000 650', role: 'img', 'aria-label': `${m.system.name} system graph`, tabindex: 0}, true);
    s.append(el('title', `${m.system.id} · revision ${text(m.revision)}`, {}, true));
    const w = el('g', undefined, {}, true); s.append(w);
    const endpoint = e => e.systemId !== m.system.id ? `Unresolved reference ${e.systemId}/${e.nodeId}` : d.ids.has(e.nodeId) ? e.nodeId : `Boundary reference ${e.nodeId}`;
    for (const region of d.regions) {
      w.append(el('rect', undefined, {x: 15, y: region.y, width: d.width, height: region.bottom-region.y, rx: 10, class: 'viz-lane'}, true));
      w.append(el('text', region.name, {x: 30, y: region.y+24, class: 'viz-lane-title'}, true));
    }
    const boundaryCounts = new Map();
    const edgeList = el('details');
    if (d.edges.some(r => r.from.systemId !== m.system.id || r.to.systemId !== m.system.id || !d.ids.has(r.from.nodeId) || !d.ids.has(r.to.nodeId))) edgeList.open = true;
    edgeList.append(el('summary', `Relationships (${d.edges.length}) and boundary references`));
    for (const r of d.edges) {
      edgeList.append(button(`${endpoint(r.from)} → ${endpoint(r.to)} · ${r.label || r.kind} · ${badge(r)}`, () => handle.select(r.id)));
      const a = r.from.systemId === m.system.id && d.ids.has(r.from.nodeId) && d.pos.get(r.from.nodeId);
      const b = r.to.systemId === m.system.id && d.ids.has(r.to.nodeId) && d.pos.get(r.to.nodeId);
      if (a && b) {
        const [fromId,toId] = directed(r, v.mode);
        const start = d.pos.get(fromId), end = d.pos.get(toId);
        const route = routeRelationship(start, end, d.regionOf.get(fromId), d.regionOf.get(toId), d, d.edges.indexOf(r));
        const label = `${r.label || r.kind} · ${badge(r)}${route.feedback ? ' · ↶ feedback' : ''}`;
        const path = el('path', undefined, {d: route.path, class: `viz-edge ${r.claim || 'declared'}${route.feedback ? ' viz-feedback' : ''}`, 'data-edge-id': r.id, 'data-feedback': route.feedback, tabindex: 0, role: 'button', 'aria-label': label}, true);
        path.append(el('title', `${r.from.nodeId} → ${r.to.nodeId}: ${label}`, {}, true));
        path.onclick = () => handle.select(r.id); path.onkeydown = e => { if (e.key === 'Enter') handle.select(r.id); }; w.append(path);
        w.append(el('text', '▶', {x: end.x-10, y: end.y+56, class: 'viz-arrow'}, true));
        if (route.feedback) w.append(el('text', '↶ feedback', {x: start.x+275, y: start.y+42, class: 'viz-feedback-label'}, true));
      } else {
        const p = a || b;
        if (p) { const key = a ? r.from.nodeId : r.to.nodeId; boundaryCounts.set(key, (boundaryCounts.get(key) || 0) + 1); }
      }
    }
    for (const [id, count] of boundaryCounts) { const p = d.pos.get(id); w.append(el('text', `↗ ${count} boundary / unresolved references (see list)`, {x: p.x, y: p.y + 125, class: 'viz-boundary'}, true)); }
    const tree = el('details'); tree.append(el('summary', 'Readable tree of current level'));
    const list = el('ul'); tree.append(list);
    for (const n of d.visible) {
      const p = d.pos.get(n.id), group = el('g', undefined, {transform: `translate(${p.x},${p.y})`, class: `viz-node ${n.claim || 'declared'}`, 'data-id': n.id, tabindex: 0, role: 'button', 'aria-label': `${n.name || n.id} ${badge(n)}`}, true);
      group.append(el('rect', undefined, {width: 270, height: 105, rx: 8}, true), el('title', `${n.name}\n${n.description || ''}\n${badge(n)}`, {}, true));
      for (const [str, y, cls] of [[n.name || n.id, 25, 'viz-name'], [n.description || n.kind, 50, 'viz-description'], [badge(n), 77, 'viz-status']]) group.append(el('text', text(str).length > 34 ? text(str).slice(0, 31) + '…' : str, {x: 12, y, class: cls}, true));
      group.onclick = () => handle.select(n.id); group.ondblclick = () => handle.focus(n.id);
      group.onkeydown = e => { if (e.key === 'Enter') handle.select(n.id); if (e.key === 'ArrowRight') { e.preventDefault(); handle.focus(n.id); } };
      w.append(group);
      const li = el('li'); li.append(button(`${crumbs(n.id, idx).map(c => c.name).join(' / ')} · ${badge(n)}`, () => handle.select(n.id))); list.append(li);
    }
    s.onkeydown = e => {
      const delta = {ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40]}[e.key];
      if (e.target === s && delta) { e.preventDefault(); camera.x += delta[0]; camera.y += delta[1]; applyCamera(); changed(); }
    };
    let drag;
    s.onpointerdown = e => { if (e.target === s) { drag = {x: e.clientX, y: e.clientY}; s.setPointerCapture(e.pointerId); } };
    s.onpointermove = e => { if (drag) { camera.x += (e.clientX - drag.x) * 1000 / s.clientWidth; camera.y += (e.clientY - drag.y) * 650 / s.clientHeight; drag = {x: e.clientX, y: e.clientY}; applyCamera(); } };
    s.onpointerup = () => { drag = null; changed(); };
    const aside = el('aside', undefined, {'aria-label': 'Inspector'}), surface = el('div', undefined, {class: 'viz-surface'}); surface.append(s, aside);
    box.append(surface, edgeList, tree, el('p', LEGEND, {class: 'viz-legend'}));
    return {box, s, w, aside};
  }
  function applyCamera() { world?.setAttribute('transform', `translate(${camera.x},${camera.y}) scale(${camera.zoom})`); }
  function zoom(factor) { camera.zoom = Math.max(.05, Math.min(5, camera.zoom * factor)); applyCamera(); changed(); }
  function update(next = {}) {
    if (destroyed) return false;
    try {
      // Snapshot caller data; callbacks can never mutate an accepted model in place.
      const m = structuredClone(next.model ?? current), v = structuredClone(next.view ?? config);
      const idx = validate(m), mode = v.mode || 'overview';
      const base = mode === (config.mode || 'overview') ? positions : modePositions.get(mode) || new Map();
      const d = plan(m, v, idx, base), ui = paint(m, v, idx, d);
      modePositions.set(config.mode || 'overview', positions);
      current = m; config = v; index = idx; drawn = d; positions = d.pos; focusNode = v.focusNode ?? null;
      svg = ui.s; world = ui.w; inspector = ui.aside; generation++;
      content.replaceChildren(ui.box); error.textContent = ''; applyCamera(); inspect();
      for (const n of svg.querySelectorAll('[data-id]')) n.classList.toggle('selected', n.getAttribute('data-id') === selection);
      return true;
    } catch (exc) { fail(exc); return false; }
  }
  const handle = {
    update,
    focus(id) { if (update({view: {...config, focusNode: id}})) { handle.fit(); invoke('onFocus', id, crumbs(id)); changed(); } },
    select(id) { if (destroyed) return; if (id != null && !index?.nodes.has(id) && !current?.relationships.some(r => r.id === id)) { fail(Error(`Unknown selection: ${id}`)); return; } selection = id; inspect(); for (const n of svg?.querySelectorAll('[data-id]') || []) n.classList.toggle('selected', n.getAttribute('data-id') === id); invoke('onSelect', id); changed(); },
    fit() { if (!drawn || destroyed) return; const ps = drawn.visible.map(n => positions.get(n.id)); if (!ps.length) return; const minX = Math.min(0, ...ps.map(p => p.x)), minY = Math.min(0, ...drawn.regions.map(r => r.y)); const width = drawn.width + 90 - minX, height = Math.max(...drawn.regions.map(r => r.bottom)) + 20 - minY; const z = Math.min(1, 950 / width, 600 / height); camera = {x: 25 - minX * z, y: 25 - minY * z, zoom: z}; applyCamera(); changed(); },
    autoLayout() { const old = positions; positions = new Map(); if (!update({view: {...config, pinnedPositions: {}}})) positions = old; else { handle.fit(); changed(); } },
    exportSVG() {
      if (!svg) return '';
      const copy = svg.cloneNode(true), computed = getComputedStyle(root);
      // Inline computed SVG presentation so exported graphics have no CSS dependency.
      const originals = [svg, ...svg.querySelectorAll('*')], copies = [copy, ...copy.querySelectorAll('*')];
      for (let i = 0; i < originals.length; i++) {
        const cs = getComputedStyle(originals[i]);
        for (const key of ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'font-family', 'font-size', 'font-weight']) copies[i].style.setProperty(key, cs.getPropertyValue(key));
      }
      copy.setAttribute('viewBox', '0 0 1000 740'); copy.style.background = computed.getPropertyValue('--viz-surface') || '#f7f8fa';
      for (const [line, y] of [[config.mode === 'data-flow' ? 'Flow: source → consumer; reads run resource → reader. Arrows are relations, not clock time.' : 'Structural: left → right depends on. Dashed imports are observations. ↶ feedback.', 655], [`${current.system.id} · revision ${text(current.revision)}`, 680], [LEGEND.slice(0, 100), 705], [LEGEND.slice(100), 725]]) copy.append(el('text', line, {x: 15, y, fill: computed.color, 'font-size': 12}, true));
      return new XMLSerializer().serializeToString(copy);
    },
    exportText() { if (!drawn) return ''; return [`${current.system.id} · revision ${text(current.revision)}`, ...drawn.visible.map(n => `${crumbs(n.id).map(c => c.name).join(' / ')}: ${n.description || ''} (${badge(n)})`), ...drawn.edges.map(r => `${r.from.systemId}/${r.from.nodeId} → ${r.to.systemId}/${r.to.nodeId}: ${r.label || r.kind} (${badge(r)})\n${(r.evidence || []).map(e => `${e.path || e.file}:${e.line || ''}`).join('\n')}`), `${drawn.hidden} nodes outside drawn view; ${drawn.edgeLimit} edges exceed bound; ${drawn.filtered} filtered edges.`, LEGEND].join('\n'); },
    getState,
    destroy() { destroyed = true; generation++; root.remove(); content.replaceChildren(); root.replaceChildren(); for (const [key, value] of oldTokens) { if (value) element.style.setProperty(key, value); else element.style.removeProperty(key); } svg = world = inspector = drawn = current = index = null; positions.clear(); modePositions.clear(); callbacks = {}; },
  };
  if (update({model, view})) handle.fit();
  return handle;
}
