<script>
  import { edgeShape } from './sketch.js';

  let { id, data } = $props();

  const shape = $derived(edgeShape(id, data.points, data.color, data.width));
  // Label vlak voor het doel, waar de lijnen al uiteen liggen.
  const at = $derived.by(() => {
    const p = data.points;
    const a = p[Math.max(0, p.length - 3)];
    const b = p[p.length - 1];
    return { x: a.x + (b.x - a.x) * 0.55, y: a.y + (b.y - a.y) * 0.55 };
  });
</script>

<g>
  {#each shape.line as p}
    <path d={p.d} style:stroke={p.stroke} stroke-width={p.strokeWidth} stroke-dasharray={data.dashed ? '7 6' : undefined} fill="none" stroke-linecap="round" />
  {/each}
  {#each shape.head as p}
    <path d={p.d} style:stroke={p.stroke} stroke-width={p.strokeWidth} fill="none" stroke-linecap="round" />
  {/each}
  {#if data.label}
    <g transform="translate({at.x} {at.y})">
      <rect x={-data.label.length * 4 - 8} y="-11" width={data.label.length * 8 + 16} height="22" rx="9" style:fill="var(--card)" stroke={data.color} stroke-width="1.2" />
      <text text-anchor="middle" dy="4.5" font-size="12" font-weight="700" fill={data.color} style:font-family="var(--mono)">{data.label}</text>
    </g>
  {/if}
</g>
