# ROOS Component Adoption

Tracking the migration from custom HTML/CSS to jinja-roos-components across OPI templates.

## Completed Migrations

### Detail Page Section Templates (Phase 1)

All section templates in `templates/project-details/` have been migrated:

| Template | Changes |
|----------|---------|
| `section-header.html.j2` | Inline flex -> `c-layout-flow`, edit button -> `c-button` |
| `section-team.html.j2` | Header flex -> `c-layout-flow`, edit button -> `c-button` |
| `section-services.html.j2` | Header flex -> `c-layout-flow`, edit button -> `c-button`, `.services-grid` -> `c-grid columns="three"`, empty state -> `c-alert` |
| `section-components.html.j2` | Header flex -> `c-layout-flow`, add/edit/delete buttons -> `c-button`, `.components-section` -> `c-layout-flow`, empty state -> `c-alert` |
| `section-deployments.html.j2` | Edit/webadres buttons -> `c-button`, inline flex -> `c-layout-flow`, empty state -> `c-alert` |
| `section-backups.html.j2` | Header flex -> `c-layout-flow`, backup/restore buttons -> `c-button`, type badges (PVC/DB/Bucket) -> `c-tag`, namespace badge -> `c-tag`, table padding -> CSS class |
| `modals.html.j2` | Edit modal close button -> `c-button kind="tertiary"` |

### Standalone Pages (Phase 2)

| Template | Changes |
|----------|---------|
| `tools.html.j2` | Text inputs -> `c-text-input-field`, textarea -> `c-textarea-field`, submit button -> `c-button`, output pre -> CSS class, JS selectors -> `querySelector('[name="..."]')` |
| `metrics-explorer.html.j2` | Show button -> `c-button kind="primary"`, removed `.rvo-button` CSS |

### CSS Cleanup (Phase 3)

Removed from `project-details.html.j2` style block:
- `.edit-section-btn` / `.edit-section-btn:hover` (replaced by `c-button`)
- `.services-grid` + responsive override (replaced by `c-grid`)
- `.components-section` + responsive override (replaced by `c-layout-flow`)

Added:
- `.backup-table th, .backup-table td` padding/alignment CSS

## ROOS Component Limitations Discovered

- `c-button` does not support `title` or `class` attributes; use `className` instead of `class`
- `c-layout-flow` `alignItems` accepts `start`/`center`/`end`, not CSS values like `flex-start`
- `c-select-field` with `:options="{{ var | tojson }}"` causes Jinja syntax errors in `<c-page>` templates; keep raw `<select>` for JS-managed dropdowns
- Log viewer buttons kept as raw HTML due to dark-theme context requiring custom CSS

## Still Using Custom Patterns

These are intentionally not migrated:

- **Log viewer panel** - complex interactive component with custom dark-theme styling
- **Avatar circles** - custom `border-radius: 50%` initials display
- **Deployment selector tabs** - custom tab behavior with JS state management
- **JS-managed selects** (metrics-explorer) - dynamically populated via `innerHTML`
- **Conditional `display: none`** - toggled by JavaScript at runtime

## Remaining Opportunities

| File | Pattern | Effort |
|------|---------|--------|
| `architecture-overview.html.j2` | ~50 inline flex layouts | Medium |
| `dashboard.html.j2` | ~20 inline flex, project cards | Medium |
| `project-progress.html.j2` | Progress bars, task items | Medium |
| `section-env-vars.html.j2` | Empty state div | Low |
| `section-config.html.j2` | Empty state div | Low |
| `deployment_metrics.html.j2` | Empty state div | Low |
