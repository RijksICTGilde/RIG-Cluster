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
        closeAnyOpenModals();
    };

    window.closeEditModalAndReload = function () {
        window.closeEditModal();
        window.location.reload();
    };

    window.handleEditBackdropClick = function () {
        if (!window.isEditSubmitting) {
            window.closeEditModal();
        }
    };
})();
