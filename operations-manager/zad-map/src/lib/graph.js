import ELK from 'elkjs/lib/elk.bundled.js';

import { colorOf } from './colors.js';

const elk = new ELK();

const canvas = document.createElement('canvas').getContext('2d');

/** Breedte van een stuk tekst, om een knoop op te meten voor de layout. */
export function textWidth(text, size = 15, weight = 600, mono = false) {
  canvas.font = `${weight} ${size}px ${mono ? 'Menlo, monospace' : 'RijksSans'}`;
  return canvas.measureText(String(text ?? '')).width;
}

const PAD = '[top=72,left=20,bottom=20,right=20]';
const GRID = { gap: 14, x: 20, top: 72, bottom: 20 };

/** De afmeting van een knoop uit zijn eigen inhoud; ELK krijgt die als vaste maat. */
function measure(node) {
  const rows = node.rows ?? [];
  const round = node.shape === 'pill' || node.shape === 'hub';
  // Ruimte rechts voor de i-knop, zodat die nooit over de tekst valt.
  const extra = node.info ? 40 : 0;
  let w = Math.max(200, textWidth(node.title, round ? 15 : 17, 700) + 70 + extra);
  if (node.eyebrow) w = Math.max(w, textWidth(node.eyebrow, 11.5, 700) + 60);
  if (node.subtitle) w = Math.max(w, textWidth(node.subtitle, 13, 450) + 56 + extra);
  for (const row of rows) {
    w = Math.max(w, textWidth(row.text, 12.5, 500) + (row.bar ? 150 : 64) + extra);
  }
  let h = 60 + (node.eyebrow ? 18 : 0) + (node.subtitle ? 18 : 0) + rows.length * 20;
  if (node.shape === 'stack') h += 20;
  if (round) h = Math.max(h, 74);
  // Een ellips snijdt zijn hoeken af, dus dezelfde tekst heeft er meer ruimte nodig.
  if (node.shape === 'hub') return { w: Math.max(w * 1.35, 300), h: Math.max(h * 1.6, 140) };
  return { w, h };
}

/**
 * Verpakt de inhoud van een blok in een raster. ELK zet alles binnen een blok in één laag,
 * en dan wordt een lijst van 31 kaartjes één eindeloze kolom. Blokken zonder lijnen naar
 * binnen pakken we zelf in; voor ELK zijn ze daarna een gewoon blok met een vaste maat.
 */
function pack(node) {
  for (const child of node.children) if (child.children.length) pack(child);
  if (!node.children.length || node.pack === 'elk') return;
  const kids = node.children;
  const cw = Math.max(...kids.map((k) => k.w));
  const avgH = kids.reduce((sum, k) => sum + k.h, 0) / kids.length;
  const cols =
    node.pack === 'row'
      ? kids.length
      : Math.max(1, Math.min(kids.length, Math.ceil(Math.sqrt((kids.length * 1.5 * avgH) / cw))));
  let y = GRID.top;
  for (let r = 0; r * cols < kids.length; r++) {
    const row = kids.slice(r * cols, r * cols + cols);
    row.forEach((k, i) => {
      k.offset = { x: GRID.x + i * (cw + GRID.gap), y };
      if (!k.children.length) k.w = cw;
    });
    y += Math.max(...row.map((k) => k.h)) + GRID.gap;
  }
  node.w = Math.max(node.w, GRID.x * 2 + cols * cw + (cols - 1) * GRID.gap);
  node.h = y - GRID.gap + GRID.bottom;
  node.packed = true;
}

/** Bouwt een boom van knopen; `meta` houdt per id de gegevens voor rendering bij. */
class Tree {
  constructor() {
    this.meta = new Map();
    this.roots = [];
    this.edges = [];
  }
  add(node, parent = null) {
    node.children = [];
    node.parent = parent;
    this.meta.set(node.id, node);
    if (parent) this.meta.get(parent).children.push(node);
    else this.roots.push(node);
    return node;
  }
  edge(id, source, target, data) {
    if (this.meta.has(source) && this.meta.has(target)) this.edges.push({ id, source, target, data });
  }
}

function toElk(node) {
  const compound = node.children.length > 0 && !node.packed;
  // Kaartjes in één blok even breed: een rommelige rand leest als een fout.
  if (compound) {
    const w = Math.max(...node.children.filter((c) => !c.children.length).map((c) => c.w), 0);
    for (const c of node.children) if (!c.children.length) c.w = w;
  }
  const options = { ...(node.options ?? {}) };
  if (node.partition !== undefined) options['elk.partitioning.partition'] = String(node.partition);
  if (node.ports?.length) options['elk.portConstraints'] = 'FIXED_SIDE';
  if (compound) {
    options['elk.padding'] = PAD;
    options['elk.nodeSize.constraints'] = 'MINIMUM_SIZE';
    options['elk.nodeSize.minimum'] = `(${node.w}, ${node.h})`;
  }
  // ELK breekt op een compound met `width: undefined`; de sleutel moet dan ontbreken.
  const size = compound ? {} : { width: node.w, height: node.h };
  return {
    id: node.id,
    ...size,
    layoutOptions: options,
    ports: (node.ports ?? []).map((p) => ({ id: p.id, width: 1, height: 1, layoutOptions: { 'elk.port.side': p.side } })),
    children: compound ? node.children.map(toElk) : [],
    edges: (node.innerEdges ?? []).map(elkEdge),
  };
}

// Elke lijn een eigen port aan beide kanten: zo beginnen en eindigen lijnen naast elkaar in
// plaats van op één punt, en blijft te volgen welke knoop waarheen gaat.
const elkEdge = (e) => ({ id: e.id, sources: [`${e.id}>s`], targets: [`${e.id}>t`] });

const ROOT_OPTIONS = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
  'elk.partitioning.activate': 'true',
  'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
  'elk.layered.spacing.nodeNodeBetweenLayers': '170',
  'elk.spacing.nodeNode': '56',
  // Elke lijn een eigen baan en een eigen aansluitpunt, met vaste afstand: zo blijft te zien
  // waar een lijn heen gaat.
  'elk.spacing.edgeNode': '24',
  'elk.spacing.edgeEdge': '14',
  'elk.layered.spacing.edgeEdgeBetweenLayers': '14',
  'elk.layered.spacing.edgeNodeBetweenLayers': '24',
  'elk.spacing.portPort': '14',
  'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
};

async function layout(tree) {
  for (const root of tree.roots) pack(root);
  const defs = [...tree.edges, ...[...tree.meta.values()].flatMap((m) => m.innerEdges ?? [])];
  for (const m of tree.meta.values()) m.ports = [];
  for (const e of defs) {
    tree.meta.get(e.source).ports.push({ id: `${e.id}>s`, side: 'EAST' });
    tree.meta.get(e.target).ports.push({ id: `${e.id}>t`, side: 'WEST' });
  }
  // Hoog genoeg om alle aansluitpunten met hun afstand kwijt te kunnen.
  for (const m of tree.meta.values()) {
    const east = m.ports.filter((p) => p.side === 'EAST').length;
    const west = m.ports.length - east;
    if (!m.children.length || m.packed) m.h = Math.max(m.h, Math.max(east, west) * 14 + 28);
  }
  const graph = {
    id: 'root',
    layoutOptions: {
      'elk.json.shapeCoords': 'ROOT',
      'elk.json.edgeCoords': 'ROOT',
      'elk.edgeRouting': 'ORTHOGONAL',
      ...ROOT_OPTIONS,
    },
    children: tree.roots.map(toElk),
    edges: tree.edges.map(elkEdge),
  };
  const result = await elk.layout(graph);

  const nodes = [];
  const emit = (m, x, y, w, h, depth) => {
    nodes.push({
      id: m.id,
      type: 'sketch',
      position: { x, y },
      width: w,
      height: h,
      zIndex: depth + 1,
      draggable: false,
      data: { ...m.data, id: m.id, w, h, compound: m.children.length > 0, parentId: m.parent },
    });
    if (m.packed) for (const k of m.children) emit(k, x + k.offset.x, y + k.offset.y, k.w, k.h, depth + 1);
  };
  const walk = (n, depth) => {
    for (const c of n.children ?? []) {
      emit(tree.meta.get(c.id), c.x, c.y, c.width, c.height, depth);
      walk(c, depth + 1);
    }
  };
  walk(result, 0);

  const edges = [];
  const collect = (n) => {
    for (const e of n.edges ?? []) {
      const section = e.sections?.[0];
      if (!section) continue;
      const def = defs.find((x) => x.id === e.id);
      edges.push({
        id: e.id,
        source: def.source,
        target: def.target,
        type: 'sketch',
        zIndex: 0,
        data: { ...def.data, points: [section.startPoint, ...(section.bendPoints ?? []), section.endPoint] },
      });
    }
    for (const c of n.children ?? []) collect(c);
  };
  collect(result);

  // Eén label per doel, met alle poorten erin.
  const byTarget = new Map();
  for (const e of edges) {
    if (!e.data.label) continue;
    if (!byTarget.has(e.target)) byTarget.set(e.target, []);
    byTarget.get(e.target).push(e);
  }
  for (const group of byTarget.values()) {
    const labels = [...new Set(group.flatMap((e) => e.data.label.split(' ')))];
    group.forEach((e, i) => (e.data = { ...e.data, label: i === 0 ? labels.join(' ') : '' }));
  }
  return { nodes, edges, meta: tree.meta };
}

/** De gegevens die een knoop aan het sjabloon doorgeeft; alles wat niet de layout raakt. */
const dataOf = (item, kind) => ({
  kind,
  shape: item.shape ?? 'card',
  color: colorOf(item.color),
  colorName: item.color,
  eyebrow: item.eyebrow ?? (item.state ? item.state : ''),
  title: item.title ?? '',
  subtitle: item.subtitle ?? '',
  rows: item.rows ?? [],
  state: item.state ?? '',
  style: item.style ?? '',
  info: item.info ?? null,
  click: item.click ?? '',
  related: item.related ?? [],
  expandable: item.click === 'expand' && (item.children ?? []).length > 0,
});

/**
 * Een graaf volgens contract v1 naar knopen en lijnen voor Svelte Flow.
 *
 * `expanded` is de verzameling open knopen; alleen daarvan komen de kinderen in de boom.
 * Een groep met `band` valt buiten ELK: die hoort zo breed te zijn als de plaat erboven,
 * en dat weet je pas na de layout.
 */
export async function buildGraph(doc, { expanded = new Set() } = {}) {
  const tree = new Tree();
  const columns = new Map((doc.columns ?? []).map((c, index) => [c.id, index]));
  const groups = doc.groups ?? [];
  const band = groups.find((g) => g.band);

  for (const group of groups) {
    if (group.band) continue;
    const node = tree.add({
      id: group.id,
      kind: 'frame',
      pack: group.pack ?? 'elk',
      partition: columns.get(group.column),
      w: 260,
      h: 100,
      data: dataOf({ ...group, shape: 'frame' }, 'frame'),
    });
    node.data.frameStyle = group.style ?? '';
  }

  const place = (item, parent) => {
    const { w, h } = measure(item);
    const node = tree.add(
      { id: item.id, kind: 'node', w, h, partition: parent ? undefined : columns.get(item.column), data: dataOf(item, 'node') },
      parent,
    );
    if ((item.children ?? []).length && expanded.has(item.id)) {
      node.data.open = true;
      for (const child of item.children) place(child, item.id);
    }
    return node;
  };

  const bandNodes = [];
  for (const item of doc.nodes ?? []) {
    if (band && item.group === band.id) {
      bandNodes.push(item);
      continue;
    }
    place(item, item.group && tree.meta.has(item.group) ? item.group : null);
  }

  for (const [index, edge] of (doc.edges ?? []).entries()) {
    tree.edge(`e${index}:${edge.from}:${edge.to}`, edge.from, edge.to, {
      color: colorOf(edge.color),
      width: 1.6,
      label: edge.label ?? '',
    });
  }

  const graph = await layout(tree);
  if (band && bandNodes.length) addBand(graph, tree, band, bandNodes);
  return graph;
}

/** De band onder de plaat: even breed als de rest, met zijn kaartjes in rijen erin. */
function addBand(graph, tree, band, items) {
  const minX = Math.min(...graph.nodes.map((n) => n.position.x));
  const maxX = Math.max(...graph.nodes.map((n) => n.position.x + n.width));
  const maxY = Math.max(...graph.nodes.map((n) => n.position.y + n.height));
  const width = maxX - minX;
  const gap = 16;
  const cards = items.map((item) => ({ item, ...measure(item) }));
  const rows = [];
  let row = [];
  let used = 0;
  for (const card of cards) {
    if (row.length && used + card.w > width - 40) {
      rows.push(row);
      row = [];
      used = 0;
    }
    row.push(card);
    used += card.w + gap;
  }
  if (row.length) rows.push(row);

  const bandY = maxY + 80;
  let y = bandY + GRID.top;
  const children = [];
  for (const line of rows) {
    let x = minX + GRID.x;
    for (const card of line) {
      const data = { ...dataOf(card.item, 'node'), id: card.item.id, w: card.w, h: card.h, compound: false, parentId: band.id };
      tree.meta.set(card.item.id, { id: card.item.id, kind: 'node', data, children: [], parent: band.id });
      children.push(tree.meta.get(card.item.id));
      graph.nodes.push({
        id: card.item.id,
        type: 'sketch',
        position: { x, y },
        width: card.w,
        height: card.h,
        zIndex: 2,
        draggable: false,
        data,
      });
      x += card.w + gap;
    }
    y += Math.max(...line.map((c) => c.h)) + gap;
  }
  const height = y - gap + 20 - bandY;
  const data = { ...dataOf({ ...band, shape: 'frame' }, 'frame'), id: band.id, w: width, h: height, compound: true };
  tree.meta.set(band.id, { id: band.id, kind: 'frame', data, children, parent: null });
  graph.nodes.unshift({
    id: band.id,
    type: 'sketch',
    position: { x: minX, y: bandY },
    width,
    height,
    zIndex: 1,
    draggable: false,
    data,
  });
}
