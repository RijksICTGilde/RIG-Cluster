/**
 * Formulierwaarden van web-componenten meesturen met htmx.
 *
 * HET PROBLEEM, IN DE BROWSER GEMETEN (lord-of-the-components @ 4307413, NLDD-thema).
 *
 * <c-checkbox-field> wordt onder NLDD een <nldd-checkbox-field>. Dat element is
 * form-associated: het heeft HELEMAAL GEEN <input>, niet in de lichte boom en niet in de
 * schaduwboom, en levert zijn waarde via ElementInternals. Het doet dat correct - het
 * geeft zijn waarde alleen als het aangevinkt staat:
 *
 *     vakje UIT : new FormData(form) bevat de sleutel niet
 *     vakje AAN : new FormData(form) bevat de sleutel met value="true"
 *
 * htmx (1.x) verzamelt zijn parameters NIET met FormData. Het loopt form.elements af en
 * leest van elk element .name en .value, met een uitzondering voor type "checkbox" en
 * "radio" (alleen meesturen als .checked). Een form-associated custom element staat wel
 * in form.elements, maar heeft geen .type, dus die uitzondering slaat niet aan: htmx
 * neemt het altijd mee en leest zijn .value - en die is "true", aangevinkt of niet.
 * Gemeten op de keycloak-stap, met het vakje UIT:
 *
 *     new FormData(form)  -> geen sleutel
 *     htmx.values(form)   -> {".../restrict-access/enabled": "true"}
 *     over de lijn        -> {"restrict-access": {"enabled": "true"}}
 *
 * Daar komt "aanvinken lukt, uitvinken niet" vandaan: uitvinken verstuurde nog steeds
 * true, de server sloeg true op, en het hertekende formulier zette het vakje netjes weer
 * aan. Niets in de keten daarachter is stuk.
 *
 * DE OPLOSSING. htmx biedt htmx:configRequest, waar de parameters van een verzoek nog
 * aangepast mogen worden. Voor elke naam die door een form-associated custom element
 * gedragen wordt, nemen we het oordeel van FormData over: dat is de standaard, het vraagt
 * het element zelf om zijn formulierwaarde, en het klopt voor elk zo'n element - niet
 * alleen voor aanvinkvakjes en niet alleen voor het component van vandaag.
 *
 * Dit is bewust GEEN eigen <input> naast het component: we werken mee met het component
 * en zijn schaduw-DOM. Zie features/aanvinkvakje.md voor het hele verhaal.
 */
(function () {
    'use strict';

    /* Een custom element herken je aan het streepje in zijn tagnaam. Alleen wat in
       form.elements staat telt: daar komt een custom element alleen in als het
       form-associated is, en dat is precies de groep die htmx verkeerd leest. */
    function formAssociatedNamen(form) {
        var namen = [];
        var elements = form.elements;
        for (var i = 0; i < elements.length; i++) {
            var el = elements[i];
            if (!el.tagName || el.tagName.indexOf('-') === -1) continue;
            var naam = el.getAttribute('name');
            if (naam && namen.indexOf(naam) === -1) namen.push(naam);
        }
        return namen;
    }

    function corrigeerParameters(evt) {
        var elt = evt.detail && evt.detail.elt;
        var parameters = evt.detail && evt.detail.parameters;
        if (!elt || !parameters || typeof elt.closest !== 'function') return;

        var form = elt.tagName === 'FORM' ? elt : elt.closest('form');
        if (!form || !form.elements) return;

        var namen = formAssociatedNamen(form);
        if (!namen.length) return;

        var fd = new FormData(form);
        for (var i = 0; i < namen.length; i++) {
            var naam = namen[i];
            var waarden = fd.getAll(naam);
            if (waarden.length === 0) {
                /* Niets aangevinkt / niets ingevuld: het veld hoort niet mee te gaan.
                   Dat is wat een gewoon <input type="checkbox"> ook doet, en de server
                   leest die afwezigheid als "uit". */
                delete parameters[naam];
            } else if (waarden.length === 1) {
                parameters[naam] = waarden[0];
            } else {
                parameters[naam] = waarden;
            }
        }
    }

    document.addEventListener('htmx:configRequest', corrigeerParameters);
})();
