/**
 * Wizard JS - extracted from wizard_page.html.j2 and roos.py
 *
 * Contains: sequence management, KV editor, service card dependencies,
 * rerender handler.
 */

/* ========================================================================
 * Sequence item management
 * ======================================================================== */

function sequenceAdd(path) {
    var form = document.getElementById('wizard-step-form');
    if (!form) return;
    _seqHidden(form, '_seq_action', 'add');
    _seqHidden(form, '_seq_path', path);
    _seqHidden(form, '_seq_index', '');
    htmx.trigger(form, 'submit');
}

function sequenceRemove(path, index) {
    var form = document.getElementById('wizard-step-form');
    if (!form) return;
    _seqHidden(form, '_seq_action', 'remove');
    _seqHidden(form, '_seq_path', path);
    _seqHidden(form, '_seq_index', String(index));
    htmx.trigger(form, 'submit');
}

function _seqHidden(form, name, value) {
    var el = form.querySelector('input[name="' + name + '"]');
    if (!el) {
        el = document.createElement('input');
        el.type = 'hidden';
        el.name = name;
        form.appendChild(el);
    }
    el.value = value;
}

/* ========================================================================
 * Key-value editor: toggle between ENV and YAML format
 * ======================================================================== */

function kvToggleFormat(editorId, newFormat) {
    var editor = document.getElementById(editorId);
    if (!editor) return;
    var oldFormat = editor.dataset.format;
    if (oldFormat === newFormat) return;

    var textarea = editor.querySelector('textarea');
    if (!textarea) return;

    var lines = textarea.value.split('\n');
    var converted = [];
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line || line.charAt(0) === '#') { converted.push(lines[i]); continue; }
        var key, val;
        if (oldFormat === 'env' && line.indexOf('=') !== -1) {
            var eqIdx = line.indexOf('=');
            key = line.substring(0, eqIdx).trim();
            val = line.substring(eqIdx + 1).trim();
        } else if (oldFormat === 'yaml' && line.indexOf(': ') !== -1) {
            var colIdx = line.indexOf(': ');
            key = line.substring(0, colIdx).trim();
            val = line.substring(colIdx + 2).trim();
        } else {
            converted.push(lines[i]); continue;
        }
        converted.push(newFormat === 'env' ? key + '=' + val : key + ': ' + val);
    }
    textarea.value = converted.join('\n');
    editor.dataset.format = newFormat;

    var btns = editor.querySelectorAll('.kv-toggle__btn');
    for (var b = 0; b < btns.length; b++) {
        btns[b].className = btns[b].dataset.format === newFormat
            ? 'kv-toggle__btn kv-toggle__btn--active'
            : 'kv-toggle__btn';
    }
    kvValidate(editorId);
}

function kvValidate(editorId) {
    var editor = document.getElementById(editorId);
    if (!editor) return;
    var textarea = editor.querySelector('textarea');
    var status = document.getElementById(editorId + '-status');
    if (!textarea || !status) return;

    var fmt = editor.dataset.format;
    var lines = textarea.value.split('\n');
    var errors = [];
    var count = 0;
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line || line.charAt(0) === '#') continue;
        count++;
        if (fmt === 'env' && line.indexOf('=') === -1) {
            errors.push('Regel ' + (i+1) + ': verwacht KEY=value');
        } else if (fmt === 'yaml' && line.indexOf(': ') === -1 && line.indexOf(':') === -1) {
            errors.push('Regel ' + (i+1) + ': verwacht KEY: value');
        }
    }
    if (errors.length > 0) {
        status.className = 'kv-editor__status kv-editor__status--error';
        status.textContent = errors[0];
    } else {
        status.className = 'kv-editor__status kv-editor__status--ok';
        status.textContent = count > 0 ? count + ' variabele(n)' : '';
    }
}

/* ========================================================================
 * Service card dependency logic (client-side)
 *
 * Implements the SAME algorithm as the server-side Python:
 *   1. requiresMap  - from data-requires attributes
 *   2. reverseDeps  - dep -> [active services that need it]
 *   3. locked       - checked AND reverseDeps[svc].length > 0
 *
 * IMPORTANT: After process_components, ROOS c-checkbox renders as:
 *   <label class="rvo-checkbox" data-roos-component="checkbox">
 *     <input type="checkbox" class="rvo-checkbox__input" ...>
 *   </label>
 * So we work with native <input type="checkbox"> elements.
 * ======================================================================== */

function initServiceCards(grid) {
    if (!grid || grid.dataset.initialized) return;
    grid.dataset.initialized = 'true';

    var processing = false;

    /* step 1: build requiresMap from data-requires attrs */
    var requiresMap = {};
    var allServices = [];
    grid.querySelectorAll('.service-card').forEach(function(card) {
        var svc = card.dataset.service;
        allServices.push(svc);
        var req = card.dataset.requires;
        if (req) {
            try { requiresMap[svc] = JSON.parse(req); } catch(e) {}
        }
    });

    function getCard(svc) {
        return grid.querySelector('[data-service="' + svc + '"]');
    }
    function getCheckbox(svc) {
        var card = getCard(svc);
        return card ? card.querySelector('input[type="checkbox"]') : null;
    }
    function isChecked(svc) {
        var cb = getCheckbox(svc);
        return cb ? cb.checked : false;
    }
    /**
     * Set a checkbox checked state and sync the ROOS label class.
     * ROOS renders: <label class="rvo-checkbox rvo-checkbox--not-checked">
     *                  <input type="checkbox" ...>
     *               </label>
     * Setting .checked directly doesn't update the label class.
     */
    function setChecked(svc, checked) {
        var cb = getCheckbox(svc);
        if (!cb) return;
        cb.checked = checked;
        var label = cb.closest('label[data-roos-component="checkbox"]');
        if (label) {
            label.classList.toggle('rvo-checkbox--checked', checked);
            label.classList.toggle('rvo-checkbox--not-checked', !checked);
        }
    }
    function getLabel(svc) {
        var c = getCard(svc);
        if (!c) return svc;
        var t = c.querySelector('.service-card__title');
        return t ? t.textContent.trim() : svc;
    }

    /* step 2: reverse map (recomputed on every change) */
    function getReverseDeps() {
        var rev = {};
        for (var svc in requiresMap) {
            if (!isChecked(svc)) continue;
            requiresMap[svc].forEach(function(dep) {
                if (!rev[dep]) rev[dep] = [];
                rev[dep].push(svc);
            });
        }
        return rev;
    }

    /* transitive dependencies for auto-select */
    function getTransitiveDeps(svc, visited) {
        visited = visited || {};
        if (visited[svc]) return [];
        visited[svc] = true;
        var deps = requiresMap[svc] || [];
        var all = deps.slice();
        deps.forEach(function(dep) {
            getTransitiveDeps(dep, visited).forEach(function(d) {
                if (all.indexOf(d) === -1) all.push(d);
            });
        });
        return all;
    }

    /* step 3: update locked / selected / hint for all cards */
    function updateAllVisuals() {
        var revDeps = getReverseDeps();
        allServices.forEach(function(svc) {
            var card = getCard(svc);
            if (!card) return;
            var cb = getCheckbox(svc);
            var checked = cb ? cb.checked : false;

            if (checked) {
                card.classList.add('service-card--selected');
            } else {
                card.classList.remove('service-card--selected');
            }

            var requirers = revDeps[svc] || [];
            var locked = checked && requirers.length > 0;

            if (locked) {
                card.classList.add('service-card--locked-checked');
            } else {
                card.classList.remove('service-card--locked-checked');
            }

            if (cb) {
                cb.disabled = locked;
                /* Sync ROOS label class with current checked state */
                var label = cb.closest('label[data-roos-component="checkbox"]');
                if (label) {
                    label.classList.toggle('rvo-checkbox--checked', checked);
                    label.classList.toggle('rvo-checkbox--not-checked', !checked);
                }
            }

            var hint = card.querySelector('.service-card__hint');
            if (locked) {
                var labels = requirers.map(function(r) { return getLabel(r); });
                var text = 'Vereist door: ' + labels.join(', ');
                if (!hint) {
                    hint = document.createElement('p');
                    hint.className = 'service-card__hint';
                    var content = card.querySelector('.service-card__content');
                    if (content) content.appendChild(hint);
                }
                hint.textContent = text;
            } else {
                if (hint) hint.remove();
            }
        });
    }

    /* handle a service being toggled */
    function handleToggle(svc) {
        if (isChecked(svc)) {
            /* auto-select transitive dependencies */
            getTransitiveDeps(svc).forEach(function(dep) {
                if (!isChecked(dep)) {
                    setChecked(dep, true);
                }
            });
        }
        updateAllVisuals();
    }

    /* change event from native checkbox click */
    grid.addEventListener('change', function(e) {
        if (processing) return;
        var card = e.target.closest('.service-card');
        if (!card) return;
        var svc = card.dataset.service;
        processing = true;
        /* Sync the ROOS label class for the clicked checkbox */
        var label = e.target.closest('label[data-roos-component="checkbox"]');
        if (label) {
            var checked = e.target.checked;
            label.classList.toggle('rvo-checkbox--checked', checked);
            label.classList.toggle('rvo-checkbox--not-checked', !checked);
        }
        handleToggle(svc);
        processing = false;
    });

    /* Make entire card clickable */
    grid.querySelectorAll('.service-card').forEach(function(card) {
        card.addEventListener('click', function(e) {
            /* Let the native checkbox/label and help icon handle their own clicks */
            if (e.target.closest('input[type="checkbox"]') ||
                e.target.closest('label[data-roos-component="checkbox"]') ||
                e.target.closest('.service-card__help-btn')) return;
            if (processing) return;

            if (card.classList.contains('service-card--locked-checked')) {
                var hint = card.querySelector('.service-card__hint');
                if (hint) alert(hint.textContent);
                return;
            }

            var svc = card.dataset.service;
            var cb = getCheckbox(svc);
            if (cb && !cb.disabled) {
                processing = true;
                setChecked(svc, !cb.checked);
                handleToggle(svc);
                processing = false;
            }
        });
    });
}

/* ========================================================================
 * Service help modal
 * ======================================================================== */

function openServiceHelp(templateName) {
    var backdrop = document.getElementById('service-help-backdrop');
    var modal = document.getElementById('service-help-modal');
    var content = document.getElementById('service-help-content');
    if (!backdrop || !modal || !content) return;

    content.innerHTML = '<p>Laden...</p>';
    backdrop.classList.add('is-open');
    modal.classList.add('is-open');
    document.body.style.overflow = 'hidden';

    fetch('/forms/wizard/help/' + encodeURIComponent(templateName))
        .then(function(resp) {
            if (!resp.ok) throw new Error('Not found');
            return resp.text();
        })
        .then(function(html) {
            content.innerHTML = html;
            /* Process ROOS components if the extension is available */
            if (typeof htmx !== 'undefined') {
                htmx.process(content);
            }
        })
        .catch(function() {
            content.innerHTML = '<p>Help-informatie kon niet geladen worden.</p>';
        });
}

function closeServiceHelp() {
    var backdrop = document.getElementById('service-help-backdrop');
    var modal = document.getElementById('service-help-modal');
    if (backdrop) backdrop.classList.remove('is-open');
    if (modal) modal.classList.remove('is-open');
    document.body.style.overflow = '';
}

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var modal = document.getElementById('service-help-modal');
        if (modal && modal.classList.contains('is-open')) {
            closeServiceHelp();
        }
    }
});

/* ========================================================================
 * Initialization: on page load and after HTMX swaps
 * ======================================================================== */

function initWizardWidgets(container) {
    container = container || document;
    container.querySelectorAll('.service-cards-grid').forEach(initServiceCards);
}

document.addEventListener('DOMContentLoaded', function() {
    initWizardWidgets();
});

document.addEventListener('htmx:afterSwap', function(event) {
    initWizardWidgets(event.detail.target);
});

/* Validate key-value editors on input */
document.addEventListener('input', function(e) {
    var editor = e.target.closest('.kv-editor');
    if (editor) kvValidate(editor.id);
});

/* Re-render the current step when a [data-rerender] field changes */
document.addEventListener('change', function(e) {
    var el = e.target.closest('[data-rerender]');
    if (!el) return;
    var form = document.getElementById('wizard-step-form');
    if (!form) return;
    _seqHidden(form, '_rerender', '1');
    htmx.trigger(form, 'submit');
});
