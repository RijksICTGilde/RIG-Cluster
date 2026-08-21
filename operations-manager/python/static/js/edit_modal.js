/**
 * Shared edit-modal helpers.
 *
 * Loaded globally by base.html.j2 so that any page mounting the modal-wizard
 * (project-details, admin subdomains, ...) gets the close/reload handlers in
 * scope. The wizard templates call these by name from inline event handlers
 * (e.g. modal_wizard_success.html.j2's "Sluiten" button), so they MUST be
 * defined on `window` regardless of which page hosts the modal.
 *
 * Selectors are class-based (`.edit-section-modal` / `.edit-section-backdrop`)
 * rather than id-based so the same helpers work on every modal that follows
 * the convention — project-details (`#edit-section-modal`), admin subdomains
 * (`#approval-modal`), etc.
 */
(function () {
    'use strict';

    // Tracks whether an async edit submission is in flight; backdrop clicks and
    // Escape are ignored while true so the user can't accidentally dismiss a
    // running task. The flag is flipped below, on the shared htmx hook, so it
    // holds on every page that mounts the modal -- not only where someone
    // remembered to wire it up.
    if (typeof window.isEditSubmitting === 'undefined') {
        window.isEditSubmitting = false;
    }

    // Tracks whether the user has edited anything in an open modal, so a
    // backdrop click can warn before discarding unsaved changes. Delegated
    // listeners so content swapped in later (sequence add/remove re-renders)
    // is covered too; a programmatic innerHTML swap does not fire input/change.
    if (typeof window.isEditDirty === 'undefined') {
        window.isEditDirty = false;
    }
    function markDirtyIfInModal(e) {
        if (e.target && e.target.closest && e.target.closest('.edit-section-modal')) {
            window.isEditDirty = true;
        }
    }
    document.addEventListener('input', markDirtyIfInModal);
    document.addEventListener('change', markDirtyIfInModal);

    // Preserve the modal's scroll position across htmx swaps. Adding or removing
    // a sequence row (e.g. an attachment coupling) re-renders the inner content
    // via htmx, which otherwise resets the scroll container to the top and makes
    // the user lose their place. htmx's own swap events are the idiomatic hook.
    // WAAROM DIT ZO OMSLACHTIG IS
    //
    // Terugzetten op htmx:afterSwap alleen werkte niet, en dat is te meten. afterSwap
    // vuurt VOOR de settle-vertraging en dus voordat de NLDD-componenten zijn opgebouwd.
    // Op dat moment is de nieuwe inhoud nog laag, en de browser KLEMT scrollTop op wat er
    // dan past. Daarna groeit de inhoud naar zijn echte hoogte en sta jij bovenaan, precies
    // de klacht: na een herlading kom je niet terug waar je was.
    //
    // Vandaar terugzetten NA het settelen en daarna nog een paar keer opnieuw, zolang de
    // inhoud nog groeit. Zodra de gewenste positie haalbaar is, houdt het op. De grens van
    // 600 ms is een vangnet: componenten die daarna nog groeien zijn een ander probleem, en
    // eindeloos herstellen zou de gebruiker tegenwerken die zelf is gaan scrollen.
    var savedModalScroll = null;

    function herstelScroll(modal, gewenst, pogingenOver) {
        modal.scrollTop = gewenst;
        // Gelukt, of de inhoud is echt korter geworden en dan is dit het maximum.
        var bereikt = Math.abs(modal.scrollTop - gewenst) < 2;
        if (bereikt || pogingenOver <= 0) return;
        // Nog niet haalbaar: de inhoud groeit nog. Opnieuw op het volgende frame.
        requestAnimationFrame(function () {
            herstelScroll(modal, gewenst, pogingenOver - 1);
        });
    }

    document.addEventListener('htmx:beforeSwap', function (evt) {
        var tgt = evt.detail && evt.detail.target;
        var modal = tgt && tgt.closest && tgt.closest('.edit-section-modal');
        savedModalScroll = modal ? modal.scrollTop : null;
    });

    // afterSettle en niet afterSwap: op afterSwap staat de nieuwe inhoud er wel, maar is er
    // nog niet opgemaakt en gemeten.
    document.addEventListener('htmx:afterSettle', function (evt) {
        if (savedModalScroll === null) return;
        var gewenst = savedModalScroll;
        savedModalScroll = null;
        if (gewenst <= 0) return;
        var modal = document.querySelector('.edit-section-modal.is-open');
        if (!modal) return;
        // Ongeveer 600 ms aan frames.
        herstelScroll(modal, gewenst, 36);
    });

    // -- What makes a modal busy --
    //
    // A running task is what makes the modal undismissable, so this is the single
    // place that decides it. This lives here rather than in a page template because
    // every page that opens the shared modal needs it, and a page that forgets it
    // looks exactly like one that has it -- until a task runs.
    //
    // The state is read back from the open modal after every swap rather than from
    // the swapped element. A progress fragment replaces ITSELF on each poll
    // (hx-swap="outerHTML"), and for such a swap htmx hands us the old, already
    // detached element -- so inspecting the event's target sees the finished
    // fragment on exactly the poll that was supposed to release the modal.
    //
    // Busy means: a progress view is in the modal and it has no finish buttons yet.
    // Both halves are needed. The task fragment drops ``edit-progress-view`` when it
    // ends, but the modal wizard keeps its view as a fixed wrapper and only swaps
    // the inside, so there the finish buttons are the only signal that it is done.
    function refreshEditSubmitting() {
        var modal = document.querySelector('.edit-section-modal.is-open');
        if (!modal) {
            window.isEditSubmitting = false;
            return;
        }
        window.isEditSubmitting =
            !!modal.querySelector('.edit-progress-view') && !modal.querySelector('.edit-progress-actions');
    }

    document.addEventListener('htmx:afterSwap', refreshEditSubmitting);

    // Escape closes an open modal, unless an action is running inside it. Class-based
    // for the same reason as the close helpers: project-details, admin approvals and
    // anything else following the convention share one rule.
    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Escape' || window.isEditSubmitting) {
            return;
        }
        if (document.querySelector('.edit-section-modal.is-open')) {
            window.closeEditModal();
        }
    });

    /* De focus die de dialoog opende, zodat hij er bij het sluiten weer heen kan. */
    var focusVoorDialoog = null;

    /*
     * De dialoog tonen EN de focus erin zetten.
     *
     * Dat tweede was er niet, en dat was de bug: de focus bleef op de knop staan waarmee
     * je de dialoog opende, dus op een element ACHTER de dialoog. Page Up en Page Down
     * gaan naar het scrollgebied van het element dat focus heeft, en dat was daarmee de
     * pagina eronder - de dialoog zelf (die max-height 80vh en overflow-y:auto heeft)
     * bewoog niet mee. Gemeld als "page-up en down werkt op de pagina achter de popup".
     *
     * De dialoog krijgt de focus zelf (tabindex="-1" in bg/_modals.html.j2) en niet het
     * eerste veld erin: bij het openen staat er nog "Laden..." en het echte formulier
     * komt pas met het antwoord van de server. Focus op de dialoog werkt in beide
     * gevallen, en een schermlezer leest dan de kop die aria-labelledby aanwijst.
     */
    function toonDialoog(modal, backdrop) {
        focusVoorDialoog = document.activeElement;
        if (backdrop) backdrop.classList.add('is-open');
        modal.classList.add('is-open');
        document.body.style.overflow = 'hidden';
        modal.focus();
    }

    function closeAnyOpenModals() {
        document
            .querySelectorAll('.edit-section-modal.is-open, .edit-section-backdrop.is-open')
            .forEach(function (el) {
                el.classList.remove('is-open');
            });
        document.body.style.overflow = '';
        /* Terug naar waar de gebruiker was. Zonder dit valt de focus naar <body> en
           begint tabben weer bovenaan de pagina. */
        if (focusVoorDialoog && typeof focusVoorDialoog.focus === 'function') {
            focusVoorDialoog.focus();
        }
        focusVoorDialoog = null;
    }

    window.closeEditModal = function () {
        window.isEditSubmitting = false;
        window.isEditDirty = false;
        closeAnyOpenModals();
    };

    window.closeEditModalAndReload = function () {
        window.closeEditModal();
        window.location.reload();
    };

    window.handleEditBackdropClick = function () {
        // Never dismiss while a task is running, and confirm before discarding
        // unsaved edits (large component/attachment modals are easy to close by
        // accident with a stray click outside the dialog).
        if (window.isEditSubmitting) {
            return;
        }
        if (
            window.isEditDirty &&
            !window.confirm('Er zijn niet-opgeslagen wijzigingen. Weet je zeker dat je de bewerking wilt sluiten?')
        ) {
            return;
        }
        window.closeEditModal();
    };

    /* De projectnaam staat op de dialoog zelf (data-project-name). Die stond hiervoor via
       Jinja in de pagina gebakken, en dat is precies waarom deze code niet gedeeld kon
       worden. */
    function projectName() {
        var modal = document.getElementById('edit-section-modal');
        return modal ? modal.dataset.projectName : '';
    }

    /* ---------------------------------------------------------------
     * Edit Section Modal - server-driven wizard via HTMX
     *
     * The submit-in-flight flag lives on ``window`` in
     * /static/js/edit_modal.js so the shared close helpers can read it.
     * --------------------------------------------------------------- */

    window.openEditModal = function (flowId, title, params) {
        window.isEditSubmitting = false;

        // Set title text (icon is already in the DOM via Jinja preprocessing)
        document.getElementById('edit-section-title-text').textContent = title;

        // Hide error
        var errorEl = document.getElementById('edit-section-error');
        errorEl.classList.add('is-hidden');

        // Show loading state
        var innerEl = document.getElementById('edit-section-inner');
        innerEl.innerHTML =
            '<div class="edit-section-content">' +
                '<div class="edit-section-loading"><p>Laden...</p></div>' +
            '</div>';

        // Show modal
        toonDialoog(
            document.getElementById('edit-section-modal'),
            document.getElementById('edit-section-backdrop')
        );

        // Fetch first step from server-driven modal wizard.
        // When the caller needs to scope the flow to a specific deployment (e.g. backup),
        // they pass that via `params` — no URL-hash sniffing, no global lookups.
        let url = '/projects/' + projectName() + '/modal-wizard/' + flowId;
        if (params && Object.keys(params).length > 0) {
            url += '?' + new URLSearchParams(params).toString();
        }
        fetch(url, {
            credentials: 'same-origin',
        })
        .then(function(response) {
            if (!response.ok) throw new Error('Fout bij het laden van het formulier');
            return response.text();
        })
        .then(function(html) {
            innerEl.innerHTML = html;
            // Activate HTMX on the new server-rendered content
            if (typeof htmx !== 'undefined') htmx.process(innerEl);
            // Initialize wizard widgets (service cards, KV editors) in the new content
            if (typeof initWizardWidgets === 'function') initWizardWidgets(innerEl);
        })
        .catch(function(err) {
            innerEl.innerHTML = '<div class="edit-section-error">' + err.message + '</div>';
        });
    };

    /* Service-contributed modal (RC-24): reuse the edit-modal shell and load the body
       from the service's own fragment URL. The body drives itself via HTMX (start, poll
       status, stop). One function for every such modal -- the database console and the
       job runner used to have a hand-copied opener each. */
    window.openServiceModal = function (endpoint, title) {
        document.getElementById('edit-section-title-text').textContent = title;
        var errorEl = document.getElementById('edit-section-error');
        if (errorEl) errorEl.classList.add('is-hidden');
        var innerEl = document.getElementById('edit-section-inner');
        innerEl.innerHTML =
            '<div class="edit-section-content">' +
                '<div class="edit-section-loading"><p>Laden...</p></div>' +
            '</div>';
        toonDialoog(
            document.getElementById('edit-section-modal'),
            document.getElementById('edit-section-backdrop')
        );

        fetch(endpoint, { credentials: 'same-origin' })
        .then(function(response) {
            if (!response.ok) throw new Error('Fout bij het laden van ' + title);
            return response.text();
        })
        .then(function(html) {
            innerEl.innerHTML = html;
            if (typeof htmx !== 'undefined') htmx.process(innerEl);
        })
        .catch(function(err) {
            innerEl.innerHTML = '<div class="edit-section-error">' + err.message + '</div>';
        });
    };

    /* Failure of a confirmed action (project-details/action-confirm.html.j2, which marks
       itself with data-confirm-action). A successful POST answers with the shared task
       progress fragment and htmx swaps it into the dialog, so success needs nothing here;
       only a refusal or an error has no fragment to show, and is reported inside the
       modal itself -- window.confirm/window.alert are gone. */
    document.addEventListener('htmx:afterRequest', function(evt) {
        var trigger = evt.detail.elt;
        if (!trigger || !trigger.closest || !trigger.closest('[data-confirm-action]')) return;
        var status = evt.detail.xhr ? evt.detail.xhr.status : 0;
        if (status >= 200 && status < 300) return;
        var errorEl = document.getElementById('confirm-action-error');
        if (!errorEl) return;
        errorEl.textContent = status ? 'Actie mislukt (' + status + ')' : 'Actie mislukt';
        errorEl.classList.remove('is-hidden');
    });
})();
