<script>
  import { Handle, Position } from '@xyflow/svelte';

  import { ICONS } from './icons.js';
  import { nodeShape, seedOf, toggleShape } from './sketch.js';

  let { data, selected } = $props();

  // Only https becomes a link: the contract promises it, and it keeps javascript: URLs out of an href.
  const isWebAddress = (href) => typeof href === 'string' && href.startsWith('https://');

  // Vorm uit het contract naar een schetsvorm, met de arcering die bij die vorm past.
  const SHAPES = {
    card: { shape: 'rect', style: { hachureGap: 9 } },
    pill: { shape: 'pill', style: { hachureGap: 11, fillWeight: 0.7 } },
    stack: { shape: 'stack', style: {} },
    hub: { shape: 'ellipse', style: { hachureGap: 8, strokeWidth: 2.2 } },
    frame: { shape: 'frame', style: {} },
    external: { shape: 'rect', style: { hachureGap: 14, fillWeight: 0.6, strokeLineDash: [8, 6] } },
  };

  const spec = $derived(SHAPES[data.shape] ?? SHAPES.card);
  // Een compound tekent alleen zijn kader; de inhoud draagt de kleur.
  const shape = $derived(data.compound ? 'frame' : spec.shape);
  const color = $derived(data.compound || data.shape === 'frame' ? null : data.color);
  const style = $derived.by(() => {
    const s = { ...spec.style };
    // Een knoop die ELDERS, EXTERN of UIT staat krijgt een stippelrand.
    if (data.state) s.strokeLineDash = [7, 6];
    if (data.frameStyle === 'external') s.strokeLineDash = [8, 6];
    if (selected) s.strokeWidth = 2.6;
    return s;
  });
  const paths = $derived(nodeShape(data.id, shape, data.w, data.h, color, style));
  // Een fractie scheef, vast per kaart: losser dan een raster, zonder te trillen.
  const tilt = $derived(data.compound || data.shape === 'frame' ? 0 : ((seedOf(data.id) % 140) - 70) / 100);
  const toggle = $derived(data.expandable ? toggleShape(data.id, !!data.open) : null);

  const pct = (value, max) => `${Math.min(100, ((value ?? 0) / (max || 1)) * 100)}%`;
</script>

<Handle type="target" position={Position.Left} isConnectable={false} />
<Handle type="source" position={Position.Right} isConnectable={false} />

<div
  class="node {data.shape}"
  style="transform: rotate({tilt}deg)"
  class:compound={data.compound}
  class:selected
  class:clickable={!!data.click}
>
  <svg class="sketch" viewBox="0 0 {data.w} {data.h}" preserveAspectRatio="none" aria-hidden="true">
    {#each paths as path}
      <path
        d={path.d}
        style:stroke={path.stroke}
        style:fill={path.fill ?? 'none'}
        stroke-width={path.strokeWidth}
        stroke-dasharray={style.strokeLineDash?.join(' ')}
        vector-effect="non-scaling-stroke"
      />
    {/each}
  </svg>

  {#if data.info}
    <button class="info" aria-label="Meer informatie" title="Meer informatie">i</button>
  {/if}

  {#if toggle}
    <svg class="toggle" viewBox="0 0 22 22" width="22" height="22" aria-hidden="true">
      {#each toggle as path}
        <path d={path.d} style:stroke={path.stroke} style:fill={path.fill ?? 'none'} stroke-width={path.strokeWidth} />
      {/each}
    </svg>
  {/if}

  <div class="body" class:center={data.shape === 'pill' || data.shape === 'hub'}>
    {#if data.eyebrow}<div class="eyebrow">{data.eyebrow}</div>{/if}
    <div class="title">{data.title}</div>
    {#if data.subtitle}<div class="sub">{data.subtitle}</div>{/if}
    {#if data.rows.length && !data.compound}
      <div class="rows">
        {#each data.rows as row}
          {#if row.bar}
            <div class="row">
              <span class="unit">{row.bar}</span>
              <span class="track" title="request {row.req ?? '?'} · limit {row.lim ?? '?'}">
                <span class="lim" style:width={pct(row.lim ?? row.req, row.max)}></span>
                <span class="req" style:width={pct(row.req, row.max)}></span>
              </span>
              <span>{row.text}</span>
            </div>
          {:else}
            <svelte:element
              this={isWebAddress(row.href) ? 'a' : 'div'}
              class="row"
              class:web={isWebAddress(row.href)}
              class:nodrag={isWebAddress(row.href)}
              href={isWebAddress(row.href) ? row.href : undefined}
              target={isWebAddress(row.href) ? '_blank' : undefined}
              rel={isWebAddress(row.href) ? 'noopener noreferrer' : undefined}
            >
              {#if ICONS[row.icon]}
                <svg viewBox="0 0 16 16"><path d={ICONS[row.icon]} /></svg>
              {/if}
              <span>{row.text}</span>
            </svelte:element>
          {/if}
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  .node { position: relative; width: 100%; height: 100%; }
  /* Papier onder de kaart: lijnen lopen erachter langs. Kaders blijven doorzichtig. */
  .node:not(.compound):not(.frame)::before { content: ''; position: absolute; inset: 6px; background: var(--card); border-radius: 3px; }
  .node.hub:not(.compound):not(.frame)::before { border-radius: 50%; inset: 8px; }
  .node.pill:not(.compound):not(.frame)::before { border-radius: 22px; inset: 7px; }
  .clickable { cursor: pointer; }
  .sketch { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
  .toggle { position: absolute; top: 8px; right: 10px; }
  .info {
    position: absolute; top: 9px; right: 11px; z-index: 2; width: 21px; height: 21px; padding: 0;
    display: grid; place-items: center; cursor: pointer;
    font: italic 700 13px/1 Georgia, serif; color: var(--lintblauw);
    background: var(--card); border: 1.4px solid var(--ink); border-radius: 48% 52% 50% 50% / 52% 48% 52% 48%;
  }
  /* In een pil rechts en verticaal gecentreerd; de marge links is even groot, zodat de
     tekst in het midden blijft. */
  .pill .info, .hub .info { top: 50%; right: 14px; transform: translateY(-50%); }
  .info:hover { background: var(--accent-soft); }
  .body {
    text-shadow: 0 0 2px var(--card), 0 0 4px var(--card), 0 0 7px var(--card);
    position: relative; padding: 14px 18px; height: 100%;
    display: flex; flex-direction: column; gap: 2px; line-height: 1.2;
  }
  .body.center { justify-content: center; text-align: center; padding: 0 40px; }
  .compound .body { height: auto; padding-top: 12px; }
  .stack .body { padding: 26px 30px 20px 18px; }

  .title { font-weight: 650; font-size: 17px; color: var(--ink); letter-spacing: -0.005em; }
  .frame .title { font-size: 20px; font-weight: 720; }
  .hub .title { font-size: 24px; font-weight: 760; color: var(--donkerblauw); }
  .pill .title { font-size: 15px; }
  .eyebrow { font-size: 11.5px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-soft); }
  .sub { font-size: 13px; color: var(--ink-soft); }
  .frame .body { padding: 12px 22px; }

  .rows { margin-top: 6px; display: grid; gap: 3px; }
  .row { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--ink-soft); white-space: nowrap; height: 17px; }
  .row svg { flex: none; width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.3; stroke-linecap: round; stroke-linejoin: round; }
  .row.web { color: var(--lintblauw); font-weight: 600; text-decoration: none; cursor: pointer; }
  .row.web:hover span { text-decoration: underline; }
  .unit { width: 24px; font-size: 10.5px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }
  .track { position: relative; flex: none; width: 76px; height: 7px; border-radius: 4px; background: var(--line); overflow: hidden; }
  .track span { position: absolute; inset: 0 auto 0 0; border-radius: 4px; }
  .lim { background: color-mix(in srgb, var(--lintblauw) 30%, transparent); }
  .req { background: var(--lintblauw); }

  :global(.svelte-flow__handle) { opacity: 0; pointer-events: none; }
</style>
