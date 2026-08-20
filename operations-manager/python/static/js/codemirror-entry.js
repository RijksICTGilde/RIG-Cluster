/**
 * CodeMirror 6 entry point - bundled into an IIFE that sets window.CM.
 *
 * Build:
 *   npx esbuild --bundle --format=iife --global-name=CM --minify \
 *     --outfile=static/js/codemirror-bundle.js static/js/codemirror-entry.js
 */
export { EditorView, basicSetup } from "codemirror";
export { StreamLanguage, HighlightStyle, syntaxHighlighting } from "@codemirror/language";
export { Compartment } from "@codemirror/state";
export { linter } from "@codemirror/lint";
export { autocompletion } from "@codemirror/autocomplete";
export { properties } from "@codemirror/legacy-modes/mode/properties";
export { yaml } from "@codemirror/legacy-modes/mode/yaml";
export { tags } from "@lezer/highlight";
export { default as jsYaml } from "js-yaml";
