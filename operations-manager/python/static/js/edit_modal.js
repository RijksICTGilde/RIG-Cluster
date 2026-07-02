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
 * (`#approval-modal`), danger-confirm (`#danger-confirm-modal`), etc.
 */
(function () {
    'use strict';

    // Tracks whether an async edit submission is in flight; backdrop clicks
    // are ignored while true so the user can't accidentally dismiss a running
    // task. Page-specific htmx integration flips this when a progress view
    // is swapped in.
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
    var savedModalScroll = null;
    document.addEventListener('htmx:beforeSwap', function (evt) {
        var tgt = evt.detail && evt.detail.target;
        var modal = tgt && tgt.closest && tgt.closest('.edit-section-modal');
        savedModalScroll = modal ? modal.scrollTop : null;
    });
    document.addEventListener('htmx:afterSwap', function (evt) {
        if (savedModalScroll === null) return;
        var tgt = evt.detail && evt.detail.target;
        var modal = tgt && tgt.closest && tgt.closest('.edit-section-modal');
        if (modal) modal.scrollTop = savedModalScroll;
        savedModalScroll = null;
    });

    function closeAnyOpenModals() {
        document
            .querySelectorAll('.edit-section-modal.is-open, .edit-section-backdrop.is-open')
            .forEach(function (el) {
                el.classList.remove('is-open');
            });
        document.body.style.overflow = '';
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
})();
