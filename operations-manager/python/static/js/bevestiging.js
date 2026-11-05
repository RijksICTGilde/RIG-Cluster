/* Bevestigingsdialoog: een formulier draagt data-bevestig, data-bevestig-uitleg,
   data-bevestig-knop; het script vult en opent de dialoog. POST gaat naar het action. */
(function () {
    'use strict';

    /* De luisteraars van de vorige vraag; opruimen bij de volgende houdt het bij een
       enkele afhandeling. Het close-event van de <dialog> in de schaduwboom komt hier niet
       aan, dus Escape heeft zijn eigen afhandeling (zie opEscape). */
    var opruimen = null;

    function vrager(element) {
        return element && element.closest ? element.closest('[data-bevestig]') : null;
    }

    /* Stel de vraag; doorgaan() loopt alleen na een klik op de bevestigknop.

       options.destructive (default true): false for a reversible action, which then gets
       a plain dialog and a primary button instead of the alert form.
       options.explainOnly: no action at all, only the explanation and a close button. */
    window.vraagBevestiging = function (vraag, uitleg, knoplabel, doorgaan, options) {
        var venster = document.getElementById('bevestiging-dialoog');
        var ja = document.getElementById('bevestiging-doorgaan');
        var nee = document.getElementById('bevestiging-annuleren');
        if (!venster || typeof venster.show !== 'function' || !ja || !nee) {
            console.error('bevestiging: #bevestiging-dialoog ontbreekt op deze pagina');
            return;
        }

        if (opruimen) opruimen();

        var destructive = !options || options.destructive !== false;
        var explainOnly = !!(options && options.explainOnly);
        venster.setAttribute('text', vraag || 'Weet je het zeker?');
        venster.setAttribute('supporting-text', uitleg || '');
        venster.setAttribute('variant', destructive || explainOnly ? 'alert' : '');
        ja.setAttribute('text', knoplabel || 'Doorgaan');
        ja.setAttribute('appearance', destructive ? 'destructive' : 'primary');
        ja.style.display = explainOnly ? 'none' : '';
        nee.setAttribute('text', explainOnly ? 'Sluiten' : 'Annuleren');
        nee.setAttribute('appearance', destructive || explainOnly ? 'primary' : 'secondary');

        function los() {
            ja.removeEventListener('click', bevestig);
            nee.removeEventListener('click', annuleer);
            document.removeEventListener('keydown', opEscape, true);
            venster.removeEventListener('close', los);
            opruimen = null;
        }
        function bevestig() {
            los();
            venster.hide();
            doorgaan();
        }
        function annuleer() {
            los();
            venster.hide();
        }

        /* Escape zelf afhandelen in plaats van de browser laten sluiten. Een keydown uit
           de toplaag bubbelt door naar document, en daar sluit edit_modal.js de bewerkmodal
           met het niet-opgeslagen werk waar deze vraag juist over gaat (RC-395). */
        function opEscape(evt) {
            if (evt.key !== 'Escape') return;
            /* Een htmx-swap kan de dialoog hebben vervangen terwijl de vraag open stond.
               Dan hoort deze luisteraar bij een venster dat er niet meer is, en mag Escape
               gewoon door naar wat er wel staat. */
            if (!venster.isConnected) {
                los();
                return;
            }
            evt.preventDefault();
            evt.stopPropagation();
            annuleer();
        }

        ja.addEventListener('click', bevestig);
        nee.addEventListener('click', annuleer);
        document.addEventListener('keydown', opEscape, true);
        /* De component sluit ook zelf, op een klik naast de dialoog. Dat close-event geeft
           hij af op het hostelement, dus hier komt het wel aan. */
        venster.addEventListener('close', los);
        opruimen = los;

        venster.show();
        /* De vraag gaat over iets onomkeerbaars, dus de focus staat op Annuleren en niet
           op de knop die het doet. */
        if (typeof nee.focus === 'function') nee.focus();
    };

    function vraagVoor(element, doorgaan) {
        window.vraagBevestiging(
            element.dataset.bevestig,
            element.dataset.bevestigUitleg,
            element.dataset.bevestigKnop,
            doorgaan
        );
    }

    /* De htmx-weg. htmx geeft het verzoek pas vrij als issueRequest() wordt aangeroepen,
       dus de vraag kan op zijn eigen tempo gesteld worden. */
    document.addEventListener('htmx:confirm', function (evt) {
        var element = vrager(evt.detail.elt);
        if (!element) return;
        evt.preventDefault();
        vraagVoor(element, function () {
            evt.detail.issueRequest(true);
        });
    });

    /* Een gewoon formulier, zonder htmx. In de capture-fase, zodat de submit tegengehouden
       is voordat iets anders hem gezien heeft. Een formulier dat htmx verstuurt slaat deze
       weg over: dat verzoek komt langs htmx:confirm en zou hier dubbel gevraagd worden. */
    document.addEventListener(
        'submit',
        function (evt) {
            var form = vrager(evt.target);
            if (!form || form.hasAttribute('hx-post')) return;
            evt.preventDefault();
            /* form.submit() en niet requestSubmit(): die laatste geeft opnieuw een
               submit-gebeurtenis af, en dan staat de vraag er zo weer. */
            vraagVoor(form, function () {
                form.submit();
            });
        },
        true
    );
})();
