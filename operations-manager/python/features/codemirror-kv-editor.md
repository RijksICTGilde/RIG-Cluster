# CodeMirror 6 Syntax Highlighting for KV Editor

## What it is

The key-value editor widget (`user-env-vars` fields) now uses CodeMirror 6 for syntax highlighting instead of a plain textarea. Users editing ENV (`KEY=value`) or YAML (`KEY: value`) formatted text get visual feedback with proper syntax coloring, line numbers, and a polished editor experience.

Only KV editor textareas are affected — other textareas (e.g. description fields) remain unchanged.

## How it works

### Architecture

CodeMirror 6 is ESM-only, but the project has no JS bundler. We use **esbuild** as a one-time build step to produce a single IIFE file (`codemirror-bundle.js`) that sets `window.CM`. This avoids CDN runtime dependencies and keeps the existing static file pattern.

**Files:**
- `static/js/codemirror-entry.js` — build-time entry point (re-exports needed CM modules)
- `static/js/codemirror-bundle.js` — pre-bundled IIFE output (~382KB minified, committed)
- `static/js/codemirror-kv.js` — integration layer that wires CM into KV editor textareas
- `static/js/package.json` — npm dependencies for the build step

### Textarea sync

The native `<textarea>` stays hidden in the DOM with its `name` attribute. Form submission paths read it as normal:
- **Wizard (HTMX + json-enc.js)**: reads native form `.elements`
- **Detail modals (`collectFormData`)**: uses `querySelectorAll('textarea')`

The CM `updateListener` syncs on every keystroke: `textarea.value = update.state.doc.toString()`.

### Initialization points

CodeMirror editors are initialized via `initKvEditors(container)` in:
- `initWizardWidgets()` — wizard page load and HTMX swaps
- `_sequenceEditModal()` — after sequence add/remove in detail modals
- `openEditModal()` — detail page section edit
- 422 validation error handler — re-rendered form after server validation

### Language switching

When the user toggles between ENV and YAML format, `kvToggleFormat()` converts the text and then calls `switchKvLanguage()` to reconfigure the CM language compartment.

## Rebuilding the bundle

If you need to update CodeMirror or add language modes:

```bash
cd python/static/js
npm install           # install dependencies
npm run build:cm      # rebuild codemirror-bundle.js
```

Or manually:
```bash
npx esbuild --bundle --format=iife --global-name=CM --minify \
  --outfile=codemirror-bundle.js codemirror-entry.js
```

## Marker attribute

The `data-cm-kv="true"` attribute on the `<c-textarea-field>` in `key_value_editor.html.j2` is the sole marker that identifies which textareas get CodeMirror. To add CM to another textarea type, add this attribute.

## Scope

- User-level `user-env-vars` fields only (both wizard pages and detail page edit modals)
- Deployment-component-level env vars are deferred to a follow-up task
