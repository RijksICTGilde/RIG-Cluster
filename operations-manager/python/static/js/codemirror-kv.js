/**
 * CodeMirror integration for Key-Value editor textareas.
 *
 * Finds textareas marked with data-cm-kv inside .kv-editor containers,
 * hides them, and creates a CodeMirror EditorView in their place.
 * The hidden textarea stays in the DOM for form submission; its value
 * is synced on every keystroke.
 *
 * Depends on window.CM (set by codemirror-bundle.js).
 */
(function () {
    "use strict";

    if (typeof CM === "undefined") {
        console.warn("codemirror-kv: CM global not found; skipping init.");
        return;
    }

    var EditorView   = CM.EditorView;
    var basicSetup   = CM.basicSetup;
    var StreamLanguage = CM.StreamLanguage;
    var Compartment  = CM.Compartment;
    var properties   = CM.properties;
    var yaml         = CM.yaml;

    var _instances = {};

    function langExtension(format) {
        if (format === "yaml") return StreamLanguage.define(yaml);
        return StreamLanguage.define(properties);
    }

    /**
     * Initialize CodeMirror editors for all .kv-editor containers within
     * the given root element. Safe to call repeatedly — already-initialized
     * editors are skipped.
     */
    function initKvEditors(container) {
        container = container || document;
        var editors = container.querySelectorAll(".kv-editor");

        for (var i = 0; i < editors.length; i++) {
            var editorDiv = editors[i];
            if (editorDiv.dataset.cmInitialized) continue;

            var textarea = editorDiv.querySelector("textarea[data-cm-kv]");
            if (!textarea) continue;

            var editorId = editorDiv.id;
            var format = editorDiv.dataset.format || "env";
            var langCompartment = new Compartment();

            var view = new EditorView({
                doc: textarea.value,
                extensions: [
                    basicSetup,
                    langCompartment.of(langExtension(format)),
                    EditorView.updateListener.of(function (update) {
                        if (update.docChanged) {
                            textarea.value = update.state.doc.toString();
                            if (typeof kvValidate === "function") {
                                kvValidate(editorId);
                            }
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
            };
            editorDiv.dataset.cmInitialized = "1";
        }
    }

    /**
     * Switch the language mode for an existing CodeMirror KV editor.
     * Called after kvToggleFormat has already converted the textarea value.
     */
    function switchKvLanguage(editorId, newFormat) {
        var inst = _instances[editorId];
        if (!inst) return;

        var editorDiv = document.getElementById(editorId);
        var textarea = editorDiv ? editorDiv.querySelector("textarea[data-cm-kv]") : null;

        inst.view.dispatch({
            effects: inst.langCompartment.reconfigure(langExtension(newFormat)),
        });

        if (textarea) {
            inst.view.dispatch({
                changes: {
                    from: 0,
                    to: inst.view.state.doc.length,
                    insert: textarea.value,
                },
            });
        }
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
