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
    var HighlightStyle = CM.HighlightStyle;
    var syntaxHighlighting = CM.syntaxHighlighting;
    var tags          = CM.tags;
    var autocompletion = CM.autocompletion;

    var _instances = {};

    /* ---- weergave ---- */

    /*
     * CodeMirror tekent zijn EIGEN kleuren, en die staan vast in de bundel.
     *
     * Het basisthema zet de kantlijn op #f5f5f5 met #6c6c6c tekst, en
     * defaultHighlightStyle zet de sleutels op #219 (donkerblauw). Beide zijn
     * VASTE lichte waarden: in de donkere weergave werd de kantlijn een wit vlak
     * midden in een donkere pagina, en stonden de namen van de omgevingsvariabelen
     * in donkerblauw op bijna-zwart. Gemeld als onleesbaar.
     *
     * Hieronder staat geen tweede kleurenstelsel: elke waarde is een verwijzing
     * naar een --semantics-token van het thema, en die zijn zelf light-dark().
     * De lichte weergave houdt dus dezelfde bedoeling (accentkleur voor sleutels,
     * gedempt voor commentaar); de donkere krijgt de waarde die erbij hoort.
     *
     * defaultHighlightStyle staat in basicSetup met {fallback: true} - een eigen
     * HighlightStyle vervangt hem daarom volledig zodra hij meegegeven wordt.
     */
    var zadTheme = EditorView.theme({
        "&": {
            minHeight: "7.5rem",
            color: "var(--semantics-content-color)",
            backgroundColor: "var(--semantics-surfaces-base-background-color)",
        },
        ".cm-scroller": { overflow: "auto" },
        ".cm-content, .cm-gutter": { minHeight: "7.5rem" },
        ".cm-gutters": {
            backgroundColor: "var(--semantics-surfaces-tinted-background-color)",
            color: "var(--semantics-content-secondary-color)",
            border: "none",
            borderRight: "1px solid var(--semantics-dividers-color)",
        },
        ".cm-activeLine": { backgroundColor: "var(--semantics-surfaces-tinted-background-color)" },
        ".cm-activeLineGutter": {
            backgroundColor: "var(--semantics-surfaces-tinted-background-color)",
            color: "var(--semantics-content-color)",
        },
        ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--semantics-content-color)" },
        "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
            backgroundColor: "var(--semantics-categories-accent-tinted-background-color)",
        },
        ".cm-panels": {
            backgroundColor: "var(--semantics-surfaces-tinted-background-color)",
            color: "var(--semantics-content-color)",
        },
    });

    /*
     * Vier rollen, niet meer. Een sleutel moet opvallen, een opmerking moet
     * wegvallen, een waarde is gewone tekst. Meer kleuren maken een lijst met
     * omgevingsvariabelen niet leesbaarder.
     *
     * De namen links komen uit de oude modus-tokens: properties geeft "def" voor de
     * sleutel (variableName.definition) en "quote" voor de waarde, yaml geeft
     * atom/meta/number/keyword/string.
     */
    var zadHighlight = HighlightStyle.define([
        { tag: [tags.definition(tags.variableName), tags.propertyName, tags.labelName, tags.atom],
          color: "var(--semantics-content-accent-color)" },
        { tag: tags.heading, color: "var(--semantics-content-accent-color)", fontWeight: "bold" },
        { tag: [tags.comment, tags.meta], color: "var(--semantics-content-secondary-color)", fontStyle: "italic" },
        { tag: [tags.string, tags.number, tags.bool], color: "var(--semantics-content-success-color)" },
        { tag: tags.keyword, color: "var(--semantics-content-warning-color)" },
        { tag: tags.invalid, color: "var(--semantics-content-critical-color)" },
    ]);

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

    /* ---- aanvullen ---- */

    /*
     * Aanvullen na een dollarteken.
     *
     * Alleen voor het aliassenveld: daar schrijf je EIGEN_NAAM=$PLATFORM_VARIABELE, en
     * WELKE platform-variabelen bestaan stond tot nu toe alleen in de hulpdialoog achter
     * het vraagteken. Wie de naam niet uit zijn hoofd wist moest die dialoog openen,
     * lezen, sluiten en overtypen.
     *
     * De lijst komt van de server mee in data-completions en is dezelfde bron als de
     * validatie (alias_variabelen(), zie opi/services/catalog/aliases/overzicht.py). Er
     * kan hier dus nooit een naam voorgesteld worden die het formulier afkeurt.
     */
    function completionSource(items) {
        return function (context) {
            var voor = context.matchBefore(/\$[A-Za-z0-9_]*/);
            if (!voor) return null;
            if (voor.from === voor.to && !context.explicit) return null;
            return {
                from: voor.from,
                options: items,
                validFor: /^\$[A-Za-z0-9_]*$/,
            };
        };
    }

    /* De server levert {naam, dienst, beschrijving}; CodeMirror wil {label, detail,
       info}. Het dollarteken hoort bij het label, want de aanvulling vervangt het
       dollarteken dat de gebruiker net typte. */
    function parseCompletions(editorDiv) {
        var ruw = editorDiv.dataset.completions;
        if (!ruw) return null;
        var items;
        try {
            items = JSON.parse(ruw);
        } catch (e) {
            console.warn("codemirror-kv: data-completions is geen geldige JSON", e);
            return null;
        }
        if (!items.length) return null;
        return items.map(function (item) {
            return {
                label: "$" + item.naam,
                detail: item.dienst,
                info: item.beschrijving,
                type: "variable",
            };
        });
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

        var completions = parseCompletions(editorDiv);

        var extensions = [];
        /* VOOR basicSetup, en dat is geen smaak: basicSetup brengt zijn eigen
           autocompletion() mee, en bij gelijke facetwaarden wint in CodeMirror de
           extensie die het eerst in de lijst staat. Achteraan zou onze override
           genegeerd worden. */
        if (completions) {
            extensions.push(autocompletion({ override: [completionSource(completions)] }));
        }
        extensions.push(
            basicSetup,
            langCompartment.of(langExtension(format)),
            lintCompartment.of(lintExtension(format)),
            EditorView.updateListener.of(function (update) {
                if (update.docChanged) {
                    textarea.value = update.state.doc.toString();
                }
            }),
            zadTheme,
            syntaxHighlighting(zadHighlight)
        );

        var view = new EditorView({
            doc: textarea.value,
            extensions: extensions,
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
     * the given root element. Safe to call repeatedly - already-initialized
     * editors are skipped.
     */
    function initKvEditors(container) {
        container = container || document;

        // Clean up stale instances whose DOM was replaced (e.g. HTMX swap)
        for (var id in _instances) {
            if (!_instances.hasOwnProperty(id)) continue;
            var existing = document.getElementById(id);
            if (!existing || !existing.dataset.cmInitialized) {
                // DOM gone or replaced - destroy the old CM view
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
