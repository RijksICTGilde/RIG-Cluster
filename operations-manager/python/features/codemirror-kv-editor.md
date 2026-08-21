# CodeMirror 6 Syntax Highlighting for KV Editor

## What it is

The key-value editor widget (`user-env-vars` fields) now uses CodeMirror 6 for syntax highlighting instead of a plain textarea. Users editing ENV (`KEY=value`) or YAML (`KEY: value`) formatted text get visual feedback with proper syntax coloring, line numbers, and a polished editor experience.

Only KV editor textareas are affected - other textareas (e.g. description fields) remain unchanged.

## How it works

### Architecture

CodeMirror 6 is ESM-only, but the project has no JS bundler. We use **esbuild** as a one-time build step to produce a single IIFE file (`codemirror-bundle.js`) that sets `window.CM`. This avoids CDN runtime dependencies and keeps the existing static file pattern.

**Files:**
- `static/js/codemirror-entry.js` - build-time entry point (re-exports needed CM modules)
- `static/js/codemirror-bundle.js` - pre-bundled IIFE output (~422KB minified, committed)
- `static/js/codemirror-kv.js` - integration layer that wires CM into KV editor textareas
- `static/js/package.json` - npm dependencies for the build step

### Textarea sync

The native `<textarea>` stays hidden in the DOM with its `name` attribute. Form submission paths read it as normal:
- **Wizard (HTMX + json-enc.js)**: reads native form `.elements`
- **Detail modals (`collectFormData`)**: uses `querySelectorAll('textarea')`

The CM `updateListener` syncs on every keystroke: `textarea.value = update.state.doc.toString()`.

### Initialization points

CodeMirror editors are initialized via `initKvEditors(container)` in:
- `initWizardWidgets()` - wizard page load and HTMX swaps
- `_sequenceEditModal()` - after sequence add/remove in detail modals
- `openEditModal()` - detail page section edit
- 422 validation error handler - re-rendered form after server validation

### Language switching

When the user toggles between ENV and YAML format, `kvToggleFormat()` converts the text and then calls `switchKvLanguage()` to reconfigure the CM language compartment.

### Kleuren: het thema, niet die van CodeMirror

CodeMirror brengt zijn eigen kleuren mee, en die zijn vast en licht. Het basisthema tekent de kantlijn op `#f5f5f5` met `#6c6c6c` tekst, en `defaultHighlightStyle` zet sleutelnamen op `#219`. In de donkere weergave leverde dat een wit vlak midden in een donkere pagina en donkerblauwe namen op bijna-zwart. Gemeld als onleesbaar.

`codemirror-kv.js` levert daarom twee eigen uitbreidingen:

- `zadTheme` (`EditorView.theme`) voor de editor zelf: vlak, tekst, kantlijn, cursor, selectie en actieve regel.
- `zadHighlight` (`HighlightStyle.define`) voor de tokens, in vier rollen: sleutel/definitie in de accentkleur, commentaar gedempt en cursief, tekst/getal/booleaans in de succeskleur, ongeldig in de kritiekkleur.

Elke waarde is een `var(--semantics-...)` van het NLDD-thema, en die tokens zijn zelf `light-dark()`. Er komt dus geen kleurwaarde bij en de lichte weergave houdt dezelfde bedoeling.

**Waarom dit niet met CSS kan.** `HighlightStyle` genereert zijn klassenamen bij het laden (`ͼ1a` en verder). Die zijn niet stabiel en niet semantisch, dus er valt niets betrouwbaars op te selecteren. De opmaak van een CodeMirror-editor hoort in zijn extensies, niet in een stylesheet.

`defaultHighlightStyle` zit in `basicSetup` met `{fallback: true}`, wat betekent dat hij alleen geldt zolang er geen andere accentuering is. `zadHighlight` vervangt hem daarmee volledig.

### Aanvullen na een dollarteken (alleen het aliassenveld)

Een alias schrijf je als `EIGEN_NAAM=$PLATFORM_VARIABELE`. Welke platform-variabelen bestaan stond alleen in de hulpdialoog achter het vraagteken, dus wie de naam niet uit zijn hoofd wist moest die openen, lezen, sluiten en overtypen.

De editor vult nu aan zodra je een `$` typt:

1. `COMPONENT_ALIASES` (`opi/services/catalog/aliases/visualizers.py`) draagt `attributes={"kv_completions": "aliassen"}`.
2. `FieldWidgetAdapter._kv_completions_json()` haalt de lijst op met `alias_variabelen()` - dezelfde bron als de hulpdialoog en als de validatie - en geeft hem als JSON mee aan het sjabloon.
3. `key_value_editor.html.j2` zet die JSON in `data-completions` op de `.kv-editor`-wikkel.
4. `codemirror-kv.js` leest hem en registreert een `autocompletion({override: [...]})`-bron die op `/\$[A-Za-z0-9_]*/` aanslaat.

Omdat de lijst uit de bron van de validatie komt, kan de editor nooit een naam voorstellen die het formulier vervolgens afkeurt. De extensie staat VOOR `basicSetup` in de lijst: die brengt zijn eigen `autocompletion()` mee, en bij gelijke facetwaarden wint in CodeMirror de extensie die het eerst staat.

Andere KV-velden krijgen geen `kv_completions` en dus geen aanvullingen.

## Waarom niet `<nldd-code-editor>`

Het NLDD-thema heeft sinds kort zelf een `nldd-code-editor`, en die is intern ook CodeMirror, met de themategens al ingevuld. Toch blijft deze eigen bundel staan, om twee dingen die het component niet heeft:

- **Linten.** Wij tonen fouten in de regel zelf: een ENV-regel zonder `=`, en een YAML-parseerfout op de positie waar hij zit. `nldd-code-editor` bouwt zijn extensies in `buildExtensions()` en er zit geen `lint` in.
- **Aanvullen.** Er zit ook geen `autocompletion` in, en het component laat geen eigen extensies toe: `buildExtensions()` is een methode van de klasse, niet een attribuut of een API.

Daar komt bij dat de talenlijst van het component (`json`, `yaml`, `javascript`, `typescript`, `css`, `html`, `xml`, `python`, `rust`, `sql`, `markdown`, `bash`, `toml`, `gherkin`) geen `properties`-modus kent, terwijl dat precies de modus is voor `KEY=value`.

Zodra het component linten en aanvullen doorlaat is de afweging andersom, want dan verdwijnt hier een bundel van 422 KB.

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
