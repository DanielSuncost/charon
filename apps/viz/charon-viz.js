/** Charon renderer. Model content is only assigned through textContent. */
export const RENDERER_VERSION = '1.0.0';
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

export function mount(element, {model, view = {}, theme = {}, callbacks = {}} = {}) {
  let current, config = {}, index, positions = new Map(), drawn, svg, world, inspector;
  let camera = {x: 0, y: 0, zoom: 1}, selection = null, expanded = new Set(), focusNode = null;
  let destroyed = false, generation = 0;
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
    const edges = filtered.slice(0, 500), pos = new Map(basePositions);
    const occupied = [];
    for (const n of visible) {
      const pin = v.pinnedPositions?.[n.id];
      if (pin) {
        if (!Number.isFinite(pin.x) || !Number.isFinite(pin.y)) throw Error(`Invalid pinned position: ${n.id}`);
        pos.set(n.id, {...pin});
      }
      if (pos.has(n.id)) occupied.push(pos.get(n.id));
    }
    for (const n of visible) if (!pos.has(n.id)) {
      const rel = edges.find(r => r.from.nodeId === n.id || r.to.nodeId === n.id);
      const near = rel && pos.get(rel.from.nodeId === n.id ? rel.to.nodeId : rel.from.nodeId);
      let p = near ? {x: near.x + 310, y: near.y} : {x: 30, y: 40};
      if (p.x > 650) p = {x: 30, y: p.y + 165};
      while (occupied.some(o => Math.abs(o.x - p.x) < 290 && Math.abs(o.y - p.y) < 145)) {
        p.x += 310; if (p.x > 650) { p.x = 30; p.y += 165; }
      }
      pos.set(n.id, p); occupied.push(p);
    }
    return {visible, ids, edges, pos, hidden: m.nodes.length - visible.length, filtered: relevant.length - filtered.length, edgeLimit: filtered.length - edges.length};
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
    header.append(nav, search, button('Fit', () => handle.fit()), button('Auto-layout', () => handle.autoLayout()), button('Zoom +', () => zoom(1.2)), button('Zoom −', () => zoom(1 / 1.2)));
    const modes = el('select', undefined, {'aria-label': 'View mode'});
    for (const mode of ['overview', 'dependency', 'data-flow']) modes.append(el('option', mode, {value: mode}));
    modes.value = v.mode || 'overview'; modes.onchange = () => update({view: {...config, mode: modes.value}});
    header.append(modes, results); box.append(header);
    const limits = el('p', `View: ${d.visible.length} nodes. ${d.hidden ? `Limit state: ${d.hidden} nodes not drawn (outside focus, depth, collapse or ${v.maxNodes || 200}-node bound); search or focus to navigate.` : 'All nodes drawn.'} ${d.filtered} relationships excluded by kind filter. ${d.edgeLimit ? `Limit state: ${d.edgeLimit} relationships exceed the 500-edge bound.` : ''}`, {class: 'viz-limit', role: 'status'});
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
    if ((v.mode || 'overview') === 'overview') for (const n of d.visible) {
      const parent = d.ids.has(n.parentId) && d.pos.get(n.parentId), child = d.pos.get(n.id);
      if (parent) {
        const link = el('path', undefined, {d: `M ${parent.x + 270} ${parent.y + 52} L ${child.x} ${child.y + 52}`, class: 'viz-containment'}, true);
        link.append(el('title', `Contains: ${n.parentId} → ${n.id}`, {}, true)); w.append(link);
      }
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
        const path = el('path', undefined, {d: `M ${a.x + 135} ${a.y + 100} Q ${a.x + 135} ${b.y - 25} ${b.x + 135} ${b.y}`, class: `viz-edge ${r.claim || 'declared'}`, tabindex: 0, role: 'button', 'aria-label': `${r.label || r.kind} ${badge(r)}`}, true);
        path.append(el('title', `${r.from.nodeId} → ${r.to.nodeId}: ${r.label || r.kind} · ${badge(r)}`, {}, true));
        path.onclick = () => handle.select(r.id); path.onkeydown = e => { if (e.key === 'Enter') handle.select(r.id); }; w.append(path);
        w.append(el('text', '▼', {x: b.x + 129, y: b.y, class: 'viz-arrow'}, true));
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
      const idx = validate(m), d = plan(m, v, idx, positions), ui = paint(m, v, idx, d);
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
    fit() { if (!drawn || destroyed) return; const ps = drawn.visible.map(n => positions.get(n.id)); if (!ps.length) return; const minX = Math.min(...ps.map(p => p.x)), minY = Math.min(...ps.map(p => p.y)); const width = Math.max(...ps.map(p => p.x + 285)) - minX, height = Math.max(...ps.map(p => p.y + 145)) - minY; const z = Math.min(1, 950 / width, 600 / height); camera = {x: 25 - minX * z, y: 25 - minY * z, zoom: z}; applyCamera(); changed(); },
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
      for (const [line, y] of [[`${current.system.id} · revision ${text(current.revision)}`, 680], [LEGEND.slice(0, 100), 705], [LEGEND.slice(100), 725]]) copy.append(el('text', line, {x: 15, y, fill: computed.color, 'font-size': 12}, true));
      return new XMLSerializer().serializeToString(copy);
    },
    exportText() { if (!drawn) return ''; return [`${current.system.id} · revision ${text(current.revision)}`, ...drawn.visible.map(n => `${crumbs(n.id).map(c => c.name).join(' / ')}: ${n.description || ''} (${badge(n)})`), ...drawn.edges.map(r => `${r.from.systemId}/${r.from.nodeId} → ${r.to.systemId}/${r.to.nodeId}: ${r.label || r.kind} (${badge(r)})\n${(r.evidence || []).map(e => `${e.path || e.file}:${e.line || ''}`).join('\n')}`), `${drawn.hidden} nodes outside drawn view; ${drawn.edgeLimit} edges exceed bound; ${drawn.filtered} filtered edges.`, LEGEND].join('\n'); },
    getState,
    destroy() { destroyed = true; generation++; root.remove(); content.replaceChildren(); root.replaceChildren(); for (const [key, value] of oldTokens) { if (value) element.style.setProperty(key, value); else element.style.removeProperty(key); } svg = world = inspector = drawn = current = index = null; positions.clear(); callbacks = {}; },
  };
  if (update({model, view})) handle.fit();
  return handle;
}
