# ROOS Component Adoption Analysis

Analysis of custom HTML/CSS solutions in OPI templates that could be replaced with jinja-roos-components.

## Summary

| Category | Instances | Effort | Impact |
|----------|-----------|--------|--------|
| Inline flex/grid layouts | ~100 | Medium | High |
| Raw `<button>` elements | ~26 | Low | High |
| Custom badge/tag CSS | ~8 classes | Low | Medium |
| Raw `<select>` elements | ~5 | Medium | Medium |
| Raw `<input>`/`<textarea>` | ~8 | Low-Medium | Medium |
| Custom `<style>` blocks | 12 files | High | High |
| Inline `onclick` handlers | ~26 | Low | Low |

## Phase 1: Quick Wins — Raw Buttons (Low effort, High impact)

Replace raw `<button class="rvo-button ...">` with `<c-button>`.

**Files:**
- `templates/metrics-explorer.html.j2` — "Tonen in Prometheus" button
- `templates/project-details/modals.html.j2` — close button, log control buttons (pause, clear, copy, download)
- `templates/project-details/section-*.html.j2` — edit section buttons

**Before:**
```html
<button id="show-btn" class="rvo-button rvo-button--primary" disabled onclick="showMetric()">
    Tonen in Prometheus
</button>
```

**After:**
```html
<c-button id="show-btn" kind="primary" disabled @click="showMetric()">Tonen in Prometheus</c-button>
```

## Phase 2: Inline Flexbox → `<c-layout-flow>` (Medium effort, High impact)

The most common inline style pattern. Replace `style="display: flex; ..."` with layout components.

**Most affected files:**
- `templates/project-details.html.j2` (~40 instances)
- `templates/architecture-overview.html.j2` (~50 instances)
- `templates/dashboard.html.j2` (~20 instances)
- `templates/project-details/section-team.html.j2` (~8 instances)

**Before:**
```html
<div style="display: flex; justify-content: space-between; align-items: center;">
    <c-heading type="h2" style="margin-bottom: 0;">Title</c-heading>
    <c-button>Action</c-button>
</div>
```

**After:**
```html
<c-layout-flow row="true" justifyContent="space-between" alignItems="center" gap="md">
    <c-heading type="h2">Title</c-heading>
    <c-button>Action</c-button>
</c-layout-flow>
```

## Phase 3: Custom Badges → `<c-tag>` (Low effort, Medium impact)

Replace custom badge CSS classes with ROOS tag components.

**Custom classes to replace:**
- `.project-badge` → `<c-tag>`
- `.cluster-badge` → `<c-tag>`
- `.namespace-badge` → `<c-tag>`
- `.service-tag` → `<c-tag>`
- `.env-vars-count-badge` → `<c-tag>`

**CSS to remove from `project-details.html.j2`:**
```css
.project-badge { background: var(--rvo-color-grijs-100); ... }
.cluster-badge { background: var(--rvo-color-hemelblauw-50); ... }
```

## Phase 4: Raw Form Elements → ROOS Fields (Medium effort, Medium impact)

Replace raw `<select>`, `<input>`, `<textarea>` with ROOS components.

**Files:**
- `templates/metrics-explorer.html.j2` — raw `<select>` for service/metric selection
- `templates/tools.html.j2` — raw `<input>` and `<textarea>` for encryption tools
- `templates/project-details.html.j2` — raw `<select>` for deployment switching

**Before:**
```html
<select id="service-select" class="rvo-select">
    <option value="">-- Selecteer een service --</option>
    {% for service in services %}
        <option value="{{ service.id }}">{{ service.name }}</option>
    {% endfor %}
</select>
```

**After:**
```html
<c-select-field id="service-select" label="Service" options="{{ services_options }}" />
```
Note: Requires passing `options` as a list of dicts from the view context.

## Phase 5: Grid Layouts → `<c-grid>` (Medium effort, Medium impact)

Replace custom CSS grid definitions with `<c-grid>`.

**Before:**
```css
.services-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: var(--rvo-space-lg);
}
```

**After:**
```html
<c-grid columns="two" gap="lg">...</c-grid>
```

Note: `<c-grid>` uses fixed column counts, not `auto-fit`. This may need custom `division` attribute for responsive grids.

## Phase 6: Major CSS Reduction (High effort, High impact)

The largest `<style>` blocks that should be gradually refactored:

| File | CSS Lines | Main custom patterns |
|------|-----------|---------------------|
| `project-details.html.j2` | ~300 | badges, cards, tabs, grids, progress, modal |
| `architecture-overview.html.j2` | ~100 | cards, stats, grids |
| `dashboard.html.j2` | ~80 | project cards, stats, grids |
| `project-progress.html.j2` | ~100 | progress bars, task items, animations |
| `project-details/modals.html.j2` | ~80 | modal, log viewer, backdrop |

## Not Worth Replacing

Some custom patterns are too specialized for generic ROOS components:
- **Log viewer panel** — complex interactive component with search, filtering, pause/resume
- **Progress bar with gradient animation** — custom `@keyframes pulse` animation
- **Avatar circles** — `width: 48px; height: 48px; border-radius: 50%` initials display
- **Deployment selector tabs** — custom tab behavior with JavaScript state management
- **Conditional `display: none`** — toggled by JavaScript, can't use Jinja conditionals

## Key Files by Priority

1. `templates/project-details/section-team.html.j2` — small, mostly layout replacements
2. `templates/project-details/section-services.html.j2` — small, badge + layout
3. `templates/project-details/section-components.html.j2` — small, card + badge
4. `templates/tools.html.j2` — raw form elements, straightforward
5. `templates/metrics-explorer.html.j2` — raw selects + buttons
6. `templates/project-details.html.j2` — largest file, biggest CSS reduction
7. `templates/dashboard.html.j2` — card grid layout
8. `templates/architecture-overview.html.j2` — stats + grids
