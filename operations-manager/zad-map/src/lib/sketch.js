import rough from 'roughjs';

const gen = rough.generator();

// Rough.js tekent bij elke aanroep andere bibberlijnen. Een vaste seed per id houdt de
// kaart stil bij hover, selectie en herberekening.
export function seedOf(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 2147483646) + 1;
}

const cache = new Map();

function memo(key, make) {
  if (!cache.has(key)) cache.set(key, make());
  return cache.get(key);
}

const BASE = { stroke: 'var(--ink)', strokeWidth: 1.4, roughness: 1.15, bowing: 0.9 };

/** Paden voor de vorm van een knoop, in een viewBox van w x h. */
export function nodeShape(id, shape, w, h, color, style = {}) {
  const key = [id, shape, Math.round(w), Math.round(h), color, JSON.stringify(style)].join('|');
  return memo(key, () => {
    const o = {
      ...BASE,
      seed: seedOf(id),
      // Arcering in een lichte tint: de tekst erboven moet leesbaar blijven.
      fill: color ? `${color}33` : undefined,
      fillStyle: 'hachure',
      hachureGap: 10,
      fillWeight: 0.8,
      hachureAngle: -41,
      ...style,
    };
    const m = 4;
    const drawables = [];
    if (shape === 'pill') {
      const r = Math.min(26, (h - m * 2) / 2);
      const x0 = m;
      const y0 = m;
      const x1 = w - m;
      const y1 = h - m;
      const d = `M${x0 + r} ${y0} H${x1 - r} A${r} ${r} 0 0 1 ${x1} ${y0 + r} V${y1 - r} A${r} ${r} 0 0 1 ${x1 - r} ${y1} H${x0 + r} A${r} ${r} 0 0 1 ${x0} ${y1 - r} V${y0 + r} A${r} ${r} 0 0 1 ${x0 + r} ${y0} Z`;
      drawables.push(gen.path(d, o));
    } else if (shape === 'ellipse') {
      drawables.push(gen.ellipse(w / 2, h / 2, w - m * 2, h - m * 2, o));
    } else if (shape === 'stack') {
      const back = { ...o, fill: undefined, seed: o.seed + 7 };
      drawables.push(gen.rectangle(m + 10, m, w - m * 2 - 10, h - m * 2 - 10, back));
      drawables.push(gen.rectangle(m + 5, m + 5, w - m * 2 - 10, h - m * 2 - 10, { ...back, seed: o.seed + 3 }));
      drawables.push(gen.rectangle(m, m + 10, w - m * 2 - 10, h - m * 2 - 10, o));
    } else if (shape === 'frame') {
      drawables.push(gen.rectangle(m, m, w - m * 2, h - m * 2, { ...o, fill: undefined, roughness: 0.9 }));
      drawables.push(gen.line(m + 6, 58, w - m - 6, 58, { ...o, strokeWidth: 0.8, roughness: 0.6, seed: o.seed + 1 }));
    } else {
      drawables.push(gen.rectangle(m, m, w - m * 2, h - m * 2, o));
    }
    return drawables.flatMap((d) => gen.toPaths(d));
  });
}

/** Strakke lijn door de knikpunten van ELK, met pijlpunt. */
export function edgeShape(id, points, color, width, arrow = true) {
  const key = [id, color, width, points.map((p) => `${p.x | 0},${p.y | 0}`).join(' ')].join('|');
  return memo(key, () => {
    // Weinig bibber: parallelle banen liggen 14px uit elkaar en mogen niet in elkaar overlopen.
    const o = { ...BASE, seed: seedOf(id), stroke: color, strokeWidth: width, roughness: 0.55, bowing: 0.4 };
    const line = gen.toPaths(gen.linearPath(points.map((p) => [p.x, p.y]), o));
    const head = [];
    if (arrow && points.length > 1) {
      const a = points[points.length - 2];
      const b = points[points.length - 1];
      const ang = Math.atan2(b.y - a.y, b.x - a.x);
      const len = 11;
      const wing = (s) => [b.x - len * Math.cos(ang + s * 0.42), b.y - len * Math.sin(ang + s * 0.42)];
      const headOpts = { ...o, seed: o.seed + 1, roughness: 0.6 };
      head.push(...gen.toPaths(gen.linearPath([wing(1), [b.x, b.y], wing(-1)], headOpts)));
    }
    return { line, head };
  });
}

/** Klein plus- of minteken in een schetscirkel, voor uitklapbare blokken. */
export function toggleShape(id, open) {
  return memo(`toggle|${id}|${open}`, () => {
    const o = { ...BASE, seed: seedOf(id) + 11, strokeWidth: 1.2, roughness: 0.8 };
    const d = [gen.circle(11, 11, 18, { ...o, fill: 'var(--card)', fillStyle: 'solid' }), gen.line(6, 11, 16, 11, o)];
    if (!open) d.push(gen.line(11, 6, 11, 16, o));
    return d.flatMap((x) => gen.toPaths(x));
  });
}
