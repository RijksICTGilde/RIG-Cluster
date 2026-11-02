<svelte:options
  customElement={{
    tag: 'zad-map',
    props: { src: { attribute: 'src' }, noPanel: { attribute: 'no-panel', type: 'Boolean' } },
  }}
/>

<script>
  import { Background, BackgroundVariant, Controls, MiniMap, SvelteFlow } from '@xyflow/svelte';
  import { tick } from 'svelte';

  import flowCss from '@xyflow/svelte/dist/style.css?inline';
  import Camera from './lib/Camera.svelte';
  import { buildGraph } from './lib/graph.js';
  import Panel from './lib/Panel.svelte';
  import SketchEdge from './lib/SketchEdge.svelte';
  import SketchNode from './lib/SketchNode.svelte';
  import tokensCss from './lib/tokens.css?inline';

  let { src = '', noPanel = false } = $props();

  const nodeTypes = { sketch: SketchNode };
  const edgeTypes = { sketch: SketchEdge };

  let nodes = $state.raw([]);
  let edges = $state.raw([]);
  let error = $state('');
  // Focus en paneel staan los van elkaar: een klik op een knoop licht hem op, de i opent
  // het paneel.
  let selectedId = $state(null);
  let panelId = $state(null);
  let doc = null;
  let meta = new Map();
  let expanded = new Set();
  let flow;
  let host;

  const panelInfo = $derived.by(() => {
    void nodes;
    return (panelId && meta.get(panelId)?.data.info) || null;
  });

  const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

  $effect(() => {
    if (src) load(src);
  });

  async function load(url) {
    error = '';
    try {
      const response = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      doc = await response.json();
    } catch (cause) {
      error = `De kaart kon niet worden geladen (${cause.message}).`;
      return;
    }
    expanded = new Set();
    selectedId = null;
    panelId = null;
    await render();
    await fit(null, 0);
  }

  async function render() {
    const graph = await buildGraph(doc, { expanded });
    meta = graph.meta;
    nodes = graph.nodes;
    edges = graph.edges;
    applyFocus();
  }

  async function fit(ids = null, duration = 650, maxZoom = 1.25) {
    await tick();
    await frame();
    const options = { duration, padding: '48px', maxZoom };
    if (ids) options.nodes = ids.map((id) => ({ id }));
    await flow?.fitView(options);
  }

  /** Focus: de knoop, zijn voorouders, zijn inhoud, alles aan een lijn en zijn `related`. */
  function applyFocus() {
    if (!selectedId || !meta.has(selectedId)) {
      nodes = nodes.map((node) => ({ ...node, class: '', selected: false }));
      edges = edges.map((edge) => ({ ...edge, class: '' }));
      return;
    }
    const core = new Set();
    const down = (id) => {
      core.add(id);
      for (const child of meta.get(id)?.children ?? []) down(child.id);
    };
    down(selectedId);
    const keep = new Set(core);
    const up = (id) => {
      for (let parent = meta.get(id)?.parent; parent; parent = meta.get(parent)?.parent) keep.add(parent);
    };
    up(selectedId);
    for (const id of meta.get(selectedId)?.data.related ?? []) {
      keep.add(id);
      up(id);
    }
    const live = new Set();
    for (const edge of edges) {
      if (!core.has(edge.source) && !core.has(edge.target)) continue;
      live.add(edge.id);
      for (const id of [edge.source, edge.target]) {
        keep.add(id);
        up(id);
      }
    }
    nodes = nodes.map((node) => ({ ...node, class: keep.has(node.id) ? '' : 'dim', selected: node.id === selectedId }));
    edges = edges.map((edge) => ({ ...edge, class: live.has(edge.id) ? '' : 'dim' }));
  }

  function openPanel(id) {
    panelId = id;
    host?.dispatchEvent(new CustomEvent('zad-map-info', { detail: { id }, bubbles: true, composed: true }));
  }

  async function onnodeclick({ node, event }) {
    // A web address opens its own tab; the map itself does nothing.
    if (event?.target?.closest?.('a[href]')) return;
    const item = meta.get(node.id);
    if (!item) return;
    selectedId = node.id;
    if (event?.target?.closest?.('.info')) {
      applyFocus();
      openPanel(node.id);
      return;
    }
    if (item.data.expandable) {
      const opening = !expanded.has(node.id);
      if (opening) expanded.add(node.id);
      else expanded.delete(node.id);
      await render();
      const ids = opening ? [node.id, ...(meta.get(node.id)?.children ?? []).map((child) => child.id)] : null;
      await fit(ids, 700, opening ? 1.3 : 1.15);
      return;
    }
    applyFocus();
    if (item.data.click === 'info' && item.data.info) openPanel(node.id);
  }

  function onpaneclick() {
    selectedId = null;
    panelId = null;
    applyFocus();
  }

  function fullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else host?.requestFullscreen();
  }
</script>

<!-- De stijl van Svelte Flow en de tokens horen IN de shadow DOM: een stylesheet van de
     pagina bereikt hem niet. -->
{@html `<style>${flowCss}\n${tokensCss}</style>`}

<div class="stage" bind:this={host}>
  {#if error}
    <p class="error">{error}</p>
  {/if}
  <SvelteFlow
    bind:nodes
    bind:edges
    {nodeTypes}
    {edgeTypes}
    {onnodeclick}
    {onpaneclick}
    minZoom={0.04}
    maxZoom={4}
    nodesDraggable={false}
    nodesConnectable={false}
    elevateNodesOnSelect={false}
    elevateEdgesOnSelect={false}
    zoomOnDoubleClick={false}
  >
    <Camera onready={(api) => (flow = api)} />
    <Background variant={BackgroundVariant.Dots} gap={24} size={1.3} patternColor="#c9d0d4" />
    <Controls showLock={false} position="bottom-left" />
    <MiniMap nodeColor={(node) => node.data.color} nodeStrokeWidth={0} pannable zoomable position="bottom-right" />
  </SvelteFlow>

  <button class="screen" onclick={fullscreen} aria-label="Volledig scherm" title="Volledig scherm">⛶</button>

  {#if !noPanel}
    <Panel info={panelInfo} onclose={() => (panelId = null)} />
  {/if}
</div>

<style>
  .stage { position: relative; width: 100%; height: 100%; background: var(--card); }
  .error { position: absolute; inset: 16px auto auto 16px; z-index: 30; margin: 0; color: var(--ink); }
  .screen {
    position: absolute; top: 12px; right: 12px; z-index: 25; padding: 5px 11px; cursor: pointer;
    font-size: 15px; color: var(--ink);
    background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
  }
  .screen:hover { background: var(--paper-2); }
</style>
