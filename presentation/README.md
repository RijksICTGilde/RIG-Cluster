# RIG-Cluster · ZAD — presentation

A self-contained [reveal.js](https://revealjs.com) deck telling the story of RIG-Cluster /
ZAD: what we built, why, how it grew, what it does, and where it's going. Aimed at a
fellow engineer (deep-technical).

Everything is vendored — **no internet, no build step, no server required.**

## Present

Double-click **`index.html`** (or open it in any browser). Works offline.

| Key | Action |
|---|---|
| `→` / `Space` | next slide |
| `←` | previous |
| `s` | **speaker notes** (notes + timer + next-slide preview) |
| `f` | fullscreen |
| `o` / `Esc` | slide overview |

Slide content lives **inline** in `index.html` (a `<script type="text/template">` Markdown
block). It's inlined rather than kept in a separate `.md` so that opening over `file://`
works without a local web server — Chrome blocks `fetch()` of external files over `file://`.
Edit the Markdown directly in `index.html`; `---` starts a new slide, `Note:` begins speaker
notes.

## Export to PDF (for the later management session)

1. Open **`index.html?print-pdf`** in **Chrome**.
2. Print (`⌘P`) → **Save as PDF** → set margins to *None*, enable *Background graphics*.

## Trimming for management later

This is the deep-technical version. For management, hide the deep-dive slides — the
three-repo model, OPI internals, components/deployments/services, secrets, and the
distributed/async-task slides — by adding `<!-- .slide: data-visibility="hidden" -->` as the
first line of each. No second file needed.

## Layout

```
presentation/
├── index.html        # reveal bootstrap + inline Markdown slides + speaker notes
├── theme/rig.css     # Rijksoverheid-ish palette + green "footprint" callout style
├── reveal/           # vendored reveal.js 5.2.1 (dist + plugins) — offline-safe
└── README.md
```
