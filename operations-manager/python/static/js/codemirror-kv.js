/**
 * CodeMirror integration for Key-Value editor textareas.
 *
 * Finds textareas marked with data-cm-kv inside .kv-editor containers,
 * hides them, and creates a CodeMirror EditorView in their place.
 * The hidden textarea stays in the DOM for form submission; its value
 * is synced on every keystroke.
 *
 * Provides inline linting:
 *   - ENV mode: checks each non-empty/non-comment line for KEY=value
 *   - YAML mode: parses with js-yaml and shows parse errors at position
 *
 * Depends on window.CM (set by codemirror-bundle.js).
 */
(function () {
    "use strict";

    if (typeof CM === "undefined") {
        console.warn("codemirror-kv: CM global not found; skipping init.");
        return;
    }

    var EditorView    = CM.EditorView;
    var basicSetup    = CM.basicSetup;
    var StreamLanguage = CM.StreamLanguage;
    var Compartment   = CM.Compartment;
    var linter        = CM.linter;
    var jsYaml        = CM.jsYaml;
    var properties    = CM.properties;
    var yaml          = CM.yaml;

    var _instances = {};

    /* ---- language extensions ---- */

    function langExtension(format) {
        if (format === "yaml") return StreamLanguage.define(yaml);
        return StreamLanguage.define(properties);
    }

    /* ---- linter extensions ---- */

    function envLinter() {
        return linter(function (view) {
            var diagnostics = [];
            var doc = view.state.doc;
            for (var i = 1; i <= doc.lines; i++) {
                var line = doc.line(i);
                var text = line.text.trim();
                if (!text || text.charAt(0) === "#") continue;
                if (text.indexOf("=") === -1) {
                    diagnostics.push({
                        from: line.from,
                        to: line.to,
                        severity: "error",
                        message: "Verwacht KEY=value",
                    });
                }
            }
            return diagnostics;
        });
    }

    function yamlLinter() {
        return linter(function (view) {
            var text = view.state.doc.toString();
            if (!text.trim()) return [];
            try {
                jsYaml.load(text);
                return [];
            } catch (e) {
                if (e && e.mark) {
                    var pos = e.mark.position || 0;
                    // Clamp to doc length
                    if (pos > view.state.doc.length) pos = view.state.doc.length;
                    return [{
                        from: pos,
                        to: pos,
                        severity: "error",
                        message: e.reason || e.message,
                    }];
                }
                return [];
            }
        });
    }

    function lintExtension(format) {
        if (format === "yaml") return yamlLinter();
        return envLinter();
    }

    /* ---- editor init ---- */

    /**
     * Create a CodeMirror editor for a single .kv-editor container.
     * Extracted to its own function so closure variables are correctly
     * scoped per editor (avoids the var-in-for-loop capture bug).
     */
    function _initSingleEditor(editorDiv) {
        var textarea = editorDiv.querySelector("textarea[data-cm-kv]");
        if (!textarea) return;

        var editorId = editorDiv.id;
        var format = editorDiv.dataset.format || "env";
        var langCompartment = new Compartment();
        var lintCompartment = new Compartment();

        var view = new EditorView({
            doc: textarea.value,
            extensions: [
                basicSetup,
                langCompartment.of(langExtension(format)),
                lintCompartment.of(lintExtension(format)),
                EditorView.updateListener.of(function (update) {
                    if (update.docChanged) {
                        textarea.value = update.state.doc.toString();
                    }
                }),
                EditorView.theme({
                    "&": { minHeight: "7.5rem" },
                    ".cm-scroller": { overflow: "auto" },
                }),
            ],
        });

        var wrapper = document.createElement("div");
        wrapper.className = "cm-kv-wrapper";
        wrapper.appendChild(view.dom);
        textarea.parentNode.insertBefore(wrapper, textarea.nextSibling);
        textarea.style.display = "none";

        _instances[editorId] = {
            view: view,
            langCompartment: langCompartment,
            lintCompartment: lintCompartment,
        };
        editorDiv.dataset.cmInitialized = "1";
    }

    /**
     * Initialize CodeMirror editors for all .kv-editor containers within
     * the given root element. Safe to call repeatedly — already-initialized
     * editors are skipped.
     */
    function initKvEditors(container) {
        container = container || document;

        // Clean up stale instances whose DOM was replaced (e.g. HTMX swap)
        for (var id in _instances) {
            if (!_instances.hasOwnProperty(id)) continue;
            var existing = document.getElementById(id);
            if (!existing || !existing.dataset.cmInitialized) {
                // DOM gone or replaced — destroy the old CM view
                _instances[id].view.destroy();
                delete _instances[id];
            }
        }

        var editors = container.querySelectorAll(".kv-editor");
        for (var i = 0; i < editors.length; i++) {
            if (editors[i].dataset.cmInitialized) continue;
            _initSingleEditor(editors[i]);
        }
    }

    /**
     * Switch the language mode and linter for an existing CodeMirror KV
     * editor, and sync the (already-converted) textarea content into the
     * CM doc.
     */
    function switchKvLanguage(editorId, newFormat) {
        var inst = _instances[editorId];
        if (!inst) return;

        var editorDiv = document.getElementById(editorId);
        var textarea = editorDiv ? editorDiv.querySelector("textarea[data-cm-kv]") : null;
        if (!textarea) return;

        // Single dispatch: reconfigure language + linter AND replace content.
        inst.view.dispatch({
            effects: [
                inst.langCompartment.reconfigure(langExtension(newFormat)),
                inst.lintCompartment.reconfigure(lintExtension(newFormat)),
            ],
            changes: {
                from: 0,
                to: inst.view.state.doc.length,
                insert: textarea.value,
            },
        });
    }

    /**
     * Destroy a CodeMirror KV editor instance and restore the textarea.
     */
    function destroyKvEditor(editorId) {
        var inst = _instances[editorId];
        if (!inst) return;

        inst.view.destroy();
        delete _instances[editorId];

        var editorDiv = document.getElementById(editorId);
        if (editorDiv) {
            delete editorDiv.dataset.cmInitialized;
            var wrapper = editorDiv.querySelector(".cm-kv-wrapper");
            if (wrapper) wrapper.remove();
            var textarea = editorDiv.querySelector("textarea[data-cm-kv]");
            if (textarea) textarea.style.display = "";
        }
    }

    // Expose on window
    window.initKvEditors = initKvEditors;
    window.switchKvLanguage = switchKvLanguage;
    window.destroyKvEditor = destroyKvEditor;
})();
