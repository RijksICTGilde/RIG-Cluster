/**
 * Wizard JS - extracted from wizard_page.html.j2 and roos.py
 *
 * Contains: sequence management, KV editor, service card dependencies,
 * rerender handler.
 */

/* ========================================================================
 * Sequence item management
 *
 * Context-aware: works in both the wizard (HTMX form submit) and the
 * detail-page edit modal (fetch to /sequence endpoint).
 * ======================================================================== */

function sequenceAdd(path) {
    _sequenceDispatch('add', path, '');
}

function sequenceRemove(path, index) {
    _sequenceDispatch('remove', path, String(index));
}

/**
 * Route the sequence action to the correct handler based on context.
 */
function _sequenceDispatch(action, path, index) {
    var form = document.getElementById('wizard-step-form')
            || document.getElementById('modal-wizard-form');
    if (form) {
        /* A [data-rerender] change (e.g. toggling a service) fires its own re-render
           submit on this form. While that request is in flight, htmx drops a second
           submit triggered here, so the sequence action would be silently lost and the
           in-flight re-render would then swap the form out from under it. Wait for the
           in-flight request to settle, then re-dispatch against the fresh form. */
        if (form.classList.contains('htmx-request')) {
            document.body.addEventListener(
                'htmx:afterSettle',
                function () { _sequenceDispatch(action, path, index); },
                { once: true }
            );
            return;
        }
        /* Wizard / modal-wizard context: inject hidden fields and trigger HTMX submit */
        _seqHidden(form, '_seq_action', action);
        _seqHidden(form, '_seq_path', path);
        _seqHidden(form, '_seq_index', index);
        htmx.trigger(form, 'submit');
        return;
    }

    /* Detail-edit modal context (legacy non-wizard edit) */
    var modal = document.getElementById('edit-section-modal');
    if (modal && modal.dataset.projectName && modal.dataset.sectionId) {
        _sequenceEditModal(modal, action, path, index);
    }
}

/**
 * Handle sequence action in the detail-edit modal via fetch.
 */
function _sequenceEditModal(modal, action, path, index) {
    var contentEl = document.getElementById('edit-section-content');
    if (!contentEl) return;
    if (typeof collectFormData !== 'function') return;

    var projectName = modal.dataset.projectName;
    var sectionId = modal.dataset.sectionId;

    var formData = collectFormData(contentEl);
    formData['_seq_action'] = action;
    formData['_seq_path'] = path;
    formData['_seq_index'] = index;

    fetch('/projects/' + projectName + '/edit/' + sectionId + '/sequence', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData),
    })
    .then(function(response) {
        if (!response.ok) throw new Error('Fout bij reeks-actie');
        return response.text();
    })
    .then(function(html) {
        // Safe: HTML comes from authenticated OPI endpoint with server-side Jinja2 escaping
        contentEl.innerHTML = html;
        /* Re-init service cards if present */
        contentEl.querySelectorAll('.service-cards-grid').forEach(function(grid) {
            delete grid.dataset.initialized;
            initServiceCards(grid);
        });
        if (typeof initKvEditors === 'function') initKvEditors(contentEl);
    })
    .catch(function(err) {
        var errorEl = document.getElementById('edit-section-error');
        if (errorEl) {
            errorEl.textContent = err.message;
            errorEl.style.display = '';
        }
    });
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
    if (typeof switchKvLanguage === 'function') switchKvLanguage(editorId, newFormat);
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
            var serverLocked = card.dataset.locked === 'true';
            var locked = checked && (requirers.length > 0 || serverLocked);

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
            // Safe: HTML comes from authenticated OPI endpoint with server-side Jinja2 escaping
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
 * Scroll to first validation error after form submission
 * ======================================================================== */

function scrollToFirstError(container) {
    container = container || document;
    var el = container.querySelector('[aria-invalid="true"]')
          || container.querySelector('.rvo-form-field__error-text')
          || container.querySelector('[data-roos-component="alert"]');
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
}

/* ========================================================================
 * Initialization: on page load and after HTMX swaps
 * ======================================================================== */

function initWizardWidgets(container) {
    container = container || document;
    container.querySelectorAll('.service-cards-grid').forEach(initServiceCards);
    if (typeof initKvEditors === 'function') initKvEditors(container);
}

document.addEventListener('DOMContentLoaded', function() {
    initWizardWidgets();
});

document.addEventListener('htmx:afterSettle', function(event) {
    initWizardWidgets(event.detail.target);
    scrollToFirstError(event.detail.target);

    /* Clean up the _rerender hidden field after a re-render swap completes,
       so the next regular form submit is not treated as another re-render. */
    var form = document.getElementById('wizard-step-form')
            || document.getElementById('modal-wizard-form');
    if (form) {
        var rr = form.querySelector('input[name="_rerender"]');
        if (rr) rr.remove();
    }
});


/* ========================================================================
 * Paste cleaner for container image fields
 *
 * Strips "docker pull " and similar prefixes when pasting into fields
 * marked with data-paste-clean="container-image".
 * ======================================================================== */

document.addEventListener('paste', function(e) {
    var el = e.target.closest('[data-paste-clean="container-image"]')
          || (e.target.getRootNode && e.target.getRootNode().host
              && e.target.getRootNode().host.closest
              && e.target.getRootNode().host.closest('[data-paste-clean="container-image"]'));
    if (!el) return;

    var pasted = (e.clipboardData || window.clipboardData).getData('text');
    if (!pasted) return;

    var cleaned = pasted.trim().replace(/^docker\s+pull\s+/i, '');
    if (cleaned !== pasted) {
        e.preventDefault();
        var input = e.target;
        input.value = cleaned;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        flashCleanIndicator(input);
    }
}, true);

function flashCleanIndicator(input) {
    input.style.transition = 'none';
    input.style.outlineStyle = 'solid';
    input.style.outlineWidth = '3px';
    input.style.outlineColor = '#66bb6a';
    // Force reflow so the bright green is applied before the transition starts
    input.offsetHeight;
    input.style.transition = 'outline-color 0.8s ease-out';
    input.style.outlineColor = '#1b5e20';
    input.addEventListener('transitionend', function cleanup() {
        input.removeEventListener('transitionend', cleanup);
        input.style.outline = '';
        input.style.transition = '';
    });
}

/* Re-render the current step when a [data-rerender] field changes */
document.addEventListener('change', function(e) {
    var el = e.target.closest('[data-rerender]');
    if (!el) return;
    var form = document.getElementById('wizard-step-form')
            || document.getElementById('modal-wizard-form');
    if (!form) return;
    _seqHidden(form, '_rerender', '1');
    htmx.trigger(form, 'submit');
});
